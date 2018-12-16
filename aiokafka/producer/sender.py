import asyncio
import collections
import logging

import aiokafka.errors as Errors
from aiokafka.client import ConnectionGroup, CoordinationType
from aiokafka.errors import (
    KafkaError, UnknownTopicOrPartitionError,
    CoordinatorNotAvailableError, NotCoordinatorError,
    CoordinatorLoadInProgressError, InvalidProducerEpoch,
    ProducerFenced, InvalidProducerIdMapping, InvalidTxnState,
    ConcurrentTransactions, DuplicateSequenceNumber, RequestTimedOutError,
    OutOfOrderSequenceNumber)
from aiokafka.protocol.produce import ProduceRequest
from aiokafka.protocol.transaction import (
    InitProducerIdRequest, AddPartitionsToTxnRequest, EndTxnRequest,
    AddOffsetsToTxnRequest, TxnOffsetCommitRequest
)
from aiokafka.structs import TopicPartition
from aiokafka.util import ensure_future

log = logging.getLogger(__name__)

BACKOFF_OVERRIDE = 0.02  # 20ms wait between transactions is better than 100ms.


class Sender:
    """ Background processing abstraction for Producer. By all means just
    separates batch delivery and transaction management from the main Producer
    code
    """

    def __init__(
            self, client, *, acks, txn_manager, message_accumulator,
            retry_backoff_ms, linger_ms, request_timeout_ms, loop):
        self.client = client
        self._txn_manager = txn_manager
        self._acks = acks

        self._message_accumulator = message_accumulator
        self._sender_task = None
        self._in_flight = set()
        self._muted_partitions = set()
        self._coordinators = {}
        self._loop = loop
        self._retry_backoff = retry_backoff_ms / 1000
        self._request_timeout_ms = request_timeout_ms
        self._linger_time = linger_ms / 1000

    @asyncio.coroutine
    def start(self):
        # If producer is indempotent we need to assure we have PID found
        yield from self._maybe_wait_for_pid()
        self._sender_task = ensure_future(
            self._sender_routine(), loop=self._loop)
        self._sender_task.add_done_callback(self._fail_all_batches)

    def _fail_all_batches(self, task):
        """ Called when sender fails. Will fail all pending batches, as they
        will never be delivered.
        """
        if task.exception() is not None:
            self._message_accumulator.fail_all(task.exception())

    @property
    def sender_task(self):
        return self._sender_task

    @asyncio.coroutine
    def close(self):
        if self._sender_task is not None:
            if not self._sender_task.done():
                self._sender_task.cancel()
                yield from self._sender_task

    @asyncio.coroutine
    def _sender_routine(self):
        """ Background task, that sends pending batches to leader nodes for
        batch's partition. This incapsulates same logic as Java's `Sender`
        background thread. Because we use asyncio this is more event based
        loop, rather than counting timeout till next possible even like in
        Java.
        """

        tasks = set()
        txn_task = None  # Track a single task for transaction interactions
        try:
            while True:
                # If indempotence or transactions are turned on we need to
                # have a valid PID to send any request below
                yield from self._maybe_wait_for_pid()

                waiters = set()
                # As transaction coordination is done via a single, separate
                # socket we do not need to pump it to several nodes, as we do
                # with produce requests.
                # We will only have 1 task at a time and will try to spawn
                # another once that is done.
                txn_manager = self._txn_manager
                muted_partitions = self._muted_partitions
                if txn_manager is not None and \
                        txn_manager.transactional_id is not None:
                    if txn_task is None or txn_task.done():
                        txn_task = self._maybe_do_transactional_request()
                        if txn_task is not None:
                            tasks.add(txn_task)
                        else:
                            # Waiters will not be awaited on exit, tasks will
                            waiters.add(txn_manager.make_task_waiter())
                    # We can't have a race condition between
                    # AddPartitionsToTxnRequest and a ProduceRequest, so we
                    # mute the partition until added.
                    muted_partitions = (
                        muted_partitions | txn_manager.partitions_to_add()
                    )
                batches, unknown_leaders_exist = \
                    self._message_accumulator.drain_by_nodes(
                        ignore_nodes=self._in_flight,
                        muted_partitions=muted_partitions)

                # create produce task for every batch
                for node_id, batches in batches.items():
                    task = ensure_future(
                        self._send_produce_req(node_id, batches),
                        loop=self._loop)
                    self._in_flight.add(node_id)
                    for tp in batches:
                        self._muted_partitions.add(tp)
                    tasks.add(task)

                if unknown_leaders_exist:
                    # we have at least one unknown partition's leader,
                    # try to update cluster metadata and wait backoff time
                    fut = self.client.force_metadata_update()
                    waiters |= tasks.union([fut])
                else:
                    fut = self._message_accumulator.data_waiter()
                    waiters |= tasks.union([fut])

                # wait when:
                # * At least one of produce task is finished
                # * Data for new partition arrived
                # * Metadata update if partition leader unknown
                done, _ = yield from asyncio.wait(
                    waiters,
                    return_when=asyncio.FIRST_COMPLETED,
                    loop=self._loop)

                # done tasks should never produce errors, if they are it's a
                # bug
                for task in done:
                    task.result()

                tasks -= done

        except asyncio.CancelledError:
            # done tasks should never produce errors, if they are it's a bug
            for task in tasks:
                yield from task
        except (ProducerFenced, OutOfOrderSequenceNumber):
            raise
        except Exception:  # pragma: no cover
            log.error("Unexpected error in sender routine", exc_info=True)
            raise KafkaError("Unexpected error during batch delivery")

    @asyncio.coroutine
    def _maybe_wait_for_pid(self):
        if self._txn_manager is None or self._txn_manager.has_pid():
            return

        while True:
            # If transactions are used we can't just send to a random node, but
            # need to find a suitable coordination node
            if self._txn_manager.transactional_id is not None:
                node_id = yield from self._find_coordinator(
                    CoordinationType.TRANSACTION,
                    self._txn_manager.transactional_id)
            else:
                node_id = self.client.get_random_node()
            success = yield from self._do_init_pid(node_id)
            if not success:
                yield from self.client.force_metadata_update()
            else:
                break

    def _coordinator_dead(self, coordinator_type):
        self._coordinators.pop(coordinator_type, None)

    @asyncio.coroutine
    def _find_coordinator(self, coordinator_type, coordinator_key):
        assert self._txn_manager is not None
        if coordinator_type in self._coordinators:
            return self._coordinators[coordinator_type]
        while True:
            try:
                coordinator_id = yield from self.client.coordinator_lookup(
                    coordinator_type, coordinator_key)
            except Errors.KafkaError as err:
                log.error("FindCoordinator Request failed: %s", err)
                yield from self.client.force_metadata_update()
                yield from asyncio.sleep(self._retry_backoff, loop=self._loop)
                continue

            # Try to connect to confirm that the connection can be
            # established.
            ready = yield from self.client.ready(
                coordinator_id, group=ConnectionGroup.COORDINATION)
            if not ready:
                yield from asyncio.sleep(self._retry_backoff, loop=self._loop)
                continue

            self._coordinators[coordinator_type] = coordinator_id

            if coordinator_type == CoordinationType.GROUP:
                log.info(
                    "Discovered coordinator %s for group id %s",
                    coordinator_id,
                    coordinator_key
                )
            else:
                log.info(
                    "Discovered coordinator %s for transactional id %s",
                    coordinator_id,
                    coordinator_key
                )
            return coordinator_id

    @asyncio.coroutine
    def _do_init_pid(self, node_id):
        handler = InitPIDHandler(self)
        return (yield from handler.do(node_id))

    ###########################################################################
    # Message delivery handler('s')
    ###########################################################################

    @asyncio.coroutine
    def _send_produce_req(self, node_id, batches):
        """ Create produce request to node
        If producer configured with `retries`>0 and produce response contain
        "failed" partitions produce request for this partition will try
        resend to broker `retries` times with `retry_timeout_ms` timeouts.

        Arguments:
            node_id (int): kafka broker identifier
            batches (dict): dictionary of {TopicPartition: MessageBatch}
        """
        t0 = self._loop.time()

        handler = SendProduceReqHandler(self, batches)
        yield from handler.do(node_id)

        # if batches for node is processed in less than a linger seconds
        # then waiting for the remaining time
        sleep_time = self._linger_time - (self._loop.time() - t0)
        if sleep_time > 0:
            yield from asyncio.sleep(sleep_time, loop=self._loop)

        self._in_flight.remove(node_id)
        for tp in batches:
            self._muted_partitions.remove(tp)

    ###########################################################################
    # Transaction handler('s')
    ###########################################################################

    def _maybe_do_transactional_request(self):
        txn_manager = self._txn_manager

        # If we have any new partitions, still not added to the transaction
        # we need to do that before committing
        tps = txn_manager.partitions_to_add()
        if tps:
            return ensure_future(
                self._do_add_partitions_to_txn(tps),
                loop=self._loop)

        # We need to add group to transaction before we can commit the offset
        group_id = txn_manager.consumer_group_to_add()
        if group_id is not None:
            return ensure_future(
                self._do_add_offsets_to_txn(group_id),
                loop=self._loop)

        # Now commit the added group's offset
        commit_data = txn_manager.offsets_to_commit()
        if commit_data is not None:
            offsets, group_id = commit_data
            return ensure_future(
                self._do_txn_offset_commit(offsets, group_id),
                loop=self._loop)

        commit_result = txn_manager.needs_transaction_commit()
        if commit_result is not None:
            return ensure_future(
                self._do_txn_commit(commit_result),
                loop=self._loop)

    @asyncio.coroutine
    def _do_add_partitions_to_txn(self, tps):
        # First assert we have a valid coordinator to send the request to
        node_id = yield from self._find_coordinator(
            CoordinationType.TRANSACTION, self._txn_manager.transactional_id)
        handler = AddPartitionsToTxnHandler(self, tps)
        return (yield from handler.do(node_id))

    @asyncio.coroutine
    def _do_add_offsets_to_txn(self, group_id):
        # First assert we have a valid coordinator to send the request to
        node_id = yield from self._find_coordinator(
            CoordinationType.TRANSACTION, self._txn_manager.transactional_id)
        handler = AddOffsetsToTxnHandler(self, group_id)
        return (yield from handler.do(node_id))

    @asyncio.coroutine
    def _do_txn_offset_commit(self, offsets, group_id):
        # Fast return if nothing to commit
        if not offsets:
            return
        # NOTE: We send this one to GROUP coordinator, not TRANSACTION
        node_id = yield from self._find_coordinator(
            CoordinationType.GROUP, group_id)
        log.debug(
            "Sending offset-commit request with %s for group %s to %s",
            offsets, group_id, node_id
        )
        handler = TxnOffsetCommitHandler(self, offsets, group_id)
        return (yield from handler.do(node_id))

    @asyncio.coroutine
    def _do_txn_commit(self, commit_result):
        """ Committing transaction should be done with care.
            Transactional requests will be blocked by this coroutine, so no new
        offsets or new partitions will be added.
            Produce requests will be stopped, as accumulator will not be
        yielding any new batches.
        """
        # First we need to ensure that all pending messages were flushed
        # before committing. Note, that this will only flush batches available
        # till this point, no new ones.
        yield from self._message_accumulator.flush_for_commit()

        txn_manager = self._txn_manager

        # If we never sent any data to begin with, no need to commit
        if txn_manager.is_empty_transaction():
            txn_manager.complete_transaction()
            return

        # First assert we have a valid coordinator to send the request to
        node_id = yield from self._find_coordinator(
            CoordinationType.TRANSACTION, txn_manager.transactional_id)

        handler = EndTxnHandler(self, commit_result)
        return (yield from handler.do(node_id))


class BaseHandler:
    group = ConnectionGroup.DEFAULT

    def __init__(self, sender):
        self._sender = sender
        self._default_backoff = sender._retry_backoff
        self._loop = sender._loop

    @asyncio.coroutine
    def do(self, node_id):
        req = self.create_request()
        try:
            resp = yield from self._sender.client.send(
                node_id, req, group=self.group)
        except KafkaError as err:
            log.warning("Could not send %r: %r", req.__class__, err)
            yield from asyncio.sleep(self._retry_backoff, loop=self._loop)
            return False

        retry_backoff = self.handle_reponse(resp)
        if retry_backoff is not None:
            yield from asyncio.sleep(retry_backoff, loop=self._loop)
            return False  # Failure
        else:
            return True  # Success

    def create_request(self):
        raise NotImplementedError  # pragma: no cover

    def handle_reponse(self, response):
        raise NotImplementedError  # pragma: no cover


class InitPIDHandler(BaseHandler):

    def create_request(self):
        txn_manager = self._sender._txn_manager
        return InitProducerIdRequest[0](
            transactional_id=txn_manager.transactional_id,
            transaction_timeout_ms=txn_manager.transaction_timeout_ms)

    def handle_reponse(self, resp):
        error_type = Errors.for_code(resp.error_code)
        if error_type is Errors.NoError:
            log.debug(
                "Successfully found PID=%s EPOCH=%s for Producer %s",
                resp.producer_id, resp.producer_epoch,
                self._sender.client._client_id)
            self._sender._txn_manager.set_pid_and_epoch(
                resp.producer_id, resp.producer_epoch)
            return
        elif (error_type is CoordinatorNotAvailableError or
                error_type is NotCoordinatorError):
            self._sender._coordinator_dead(CoordinationType.TRANSACTION)
        elif (error_type is CoordinatorLoadInProgressError or
                error_type is ConcurrentTransactions):
            pass
        else:
            log.error(
                "Unexpected error during InitProducerIdRequest: %s",
                error_type)
            raise error_type()

        return self._default_backoff


class AddPartitionsToTxnHandler(BaseHandler):
    group = ConnectionGroup.COORDINATION

    def __init__(self, sender, topic_partitions):
        super().__init__(sender)
        self._tps = topic_partitions

    def create_request(self):
        txn_manager = self._sender._txn_manager

        partition_data = collections.defaultdict(list)
        for tp in self._tps:
            partition_data[tp.topic].append(tp.partition)

        req = AddPartitionsToTxnRequest[0](
            transactional_id=txn_manager.transactional_id,
            producer_id=txn_manager.producer_id,
            producer_epoch=txn_manager.producer_epoch,
            topics=list(partition_data.items()))
        return req

    def handle_reponse(self, resp):
        txn_manager = self._sender._txn_manager

        for topic, partitions in resp.errors:
            for partition, error_code in partitions:
                tp = TopicPartition(topic, partition)
                error_type = Errors.for_code(error_code)

                if error_type is Errors.NoError:
                    log.debug("Added partition %s to transaction", tp)
                    txn_manager.partition_added(tp)
                elif (error_type is CoordinatorNotAvailableError or
                        error_type is NotCoordinatorError):
                    self._sender._coordinator_dead(
                        CoordinationType.TRANSACTION)
                    return self._default_backoff
                elif error_type is ConcurrentTransactions:
                    # See KAFKA-5477: There is some time between commit and
                    # actual transaction marker write, that will produce this
                    # ConcurrentTransactions. We don't want the 100ms latency
                    # in that case.
                    if not txn_manager.txn_partitions:
                        return BACKOFF_OVERRIDE
                    else:
                        return self._default_backoff
                elif (error_type is CoordinatorLoadInProgressError or
                        error_type is UnknownTopicOrPartitionError):
                    return self._default_backoff
                elif error_type is InvalidProducerEpoch:
                    raise ProducerFenced()
                elif (error_type is InvalidProducerIdMapping or
                        error_type is InvalidTxnState):
                    raise error_type()
                else:
                    log.error(
                        "Could not add partition %s due to unexpected error:"
                        " %s", partition, error_type)
                    raise error_type()
        return


class AddOffsetsToTxnHandler(BaseHandler):
    group = ConnectionGroup.COORDINATION

    def __init__(self, sender, group_id):
        super().__init__(sender)
        self._group_id = group_id

    def create_request(self):
        txn_manager = self._sender._txn_manager

        req = AddOffsetsToTxnRequest[0](
            transactional_id=txn_manager.transactional_id,
            producer_id=txn_manager.producer_id,
            producer_epoch=txn_manager.producer_epoch,
            group_id=self._group_id
        )
        return req

    def handle_reponse(self, resp):
        txn_manager = self._sender._txn_manager
        group_id = self._group_id

        error_type = Errors.for_code(resp.error_code)
        if error_type is Errors.NoError:
            log.debug(
                "Successfully added consumer group %s to transaction", group_id
            )
            txn_manager.consumer_group_added(group_id)
            return
        elif (error_type is CoordinatorNotAvailableError or
                error_type is NotCoordinatorError):
            self._sender._coordinator_dead(CoordinationType.TRANSACTION)
        elif (error_type is CoordinatorLoadInProgressError or
                error_type is ConcurrentTransactions):
            # We will just retry after backoff
            pass
        elif error_type is InvalidProducerEpoch:
            raise ProducerFenced()
        elif error_type is InvalidTxnState:
            raise error_type()
        else:
            log.error(
                "Could not add consumer group due to unexpected error: %s",
                error_type)
            raise error_type()

        return self._default_backoff


class TxnOffsetCommitHandler(BaseHandler):
    group = ConnectionGroup.COORDINATION

    def __init__(self, sender, offsets, group_id):
        super().__init__(sender)
        self._offsets = offsets
        self._group_id = group_id

    def create_request(self):
        txn_manager = self._sender._txn_manager
        # create the offset commit request structure
        offset_data = collections.defaultdict(list)
        for tp, offset in self._offsets.items():
            offset_data[tp.topic].append(
                (tp.partition,
                 offset.offset,
                 offset.metadata))

        req = TxnOffsetCommitRequest[0](
            transactional_id=txn_manager.transactional_id,
            group_id=self._group_id,
            producer_id=txn_manager.producer_id,
            producer_epoch=txn_manager.producer_epoch,
            topics=list(offset_data.items())
        )
        return req

    def handle_reponse(self, resp):
        txn_manager = self._sender._txn_manager
        group_id = self._group_id

        for topic, partitions in resp.errors:
            for partition, error_code in partitions:
                tp = TopicPartition(topic, partition)
                error_type = Errors.for_code(error_code)

                if error_type is Errors.NoError:
                    offset = self._offsets[tp].offset
                    log.debug(
                        "Offset %s for partition %s committed to group %s",
                        offset, tp, group_id)
                    txn_manager.offset_committed(tp, offset, group_id)
                elif (error_type is CoordinatorNotAvailableError or
                        error_type is NotCoordinatorError or
                        # Copied from Java. Not sure why it's only in this case
                        error_type is RequestTimedOutError):
                    self._sender._coordinator_dead(CoordinationType.GROUP)
                    return self._default_backoff
                elif (error_type is CoordinatorLoadInProgressError or
                        error_type is UnknownTopicOrPartitionError):
                    # We will just retry after backoff
                    return self._default_backoff
                elif error_type is InvalidProducerEpoch:
                    raise ProducerFenced()
                else:
                    log.error(
                        "Could not commit offset for partition %s due to "
                        "unexpected error: %s", partition, error_type)
                    raise error_type()


class EndTxnHandler(BaseHandler):
    group = ConnectionGroup.COORDINATION

    def __init__(self, sender, commit_result):
        super().__init__(sender)
        self._commit_result = commit_result

    def create_request(self):
        txn_manager = self._sender._txn_manager
        req = EndTxnRequest[0](
            transactional_id=txn_manager.transactional_id,
            producer_id=txn_manager.producer_id,
            producer_epoch=txn_manager.producer_epoch,
            transaction_result=self._commit_result)
        return req

    def handle_reponse(self, resp):
        txn_manager = self._sender._txn_manager
        error_type = Errors.for_code(resp.error_code)

        if error_type is Errors.NoError:
            txn_manager.complete_transaction()
            return
        elif (error_type is CoordinatorNotAvailableError or
                error_type is NotCoordinatorError):
            self._sender._coordinator_dead(CoordinationType.TRANSACTION)
        elif (error_type is CoordinatorLoadInProgressError or
                error_type is ConcurrentTransactions):
            # We will just retry after backoff
            pass
        elif error_type is InvalidProducerEpoch:
            raise ProducerFenced()
        elif error_type is InvalidTxnState:
            raise error_type()
        else:
            log.error(
                "Could not end transaction due to unexpected error: %s",
                error_type)
            raise error_type()

        return self._default_backoff


class SendProduceReqHandler(BaseHandler):

    def __init__(self, sender, batches):
        super().__init__(sender)
        self._batches = batches
        self._client = sender.client
        self._to_reenqueue = []

    def create_request(self):
        topics = collections.defaultdict(list)
        for tp, batch in self._batches.items():
            topics[tp.topic].append(
                (tp.partition, batch.get_data_buffer())
            )

        if self._client.api_version >= (0, 11):
            version = 3
        elif self._client.api_version >= (0, 10):
            version = 2
        elif self._client.api_version == (0, 9):
            version = 1
        else:
            version = 0

        kwargs = {}
        if version >= 3:
            if self._sender._txn_manager is not None:
                kwargs['transactional_id'] = \
                    self._sender._txn_manager.transactional_id
            else:
                kwargs['transactional_id'] = None

        request = ProduceRequest[version](
            required_acks=self._sender._acks,
            timeout=self._sender._request_timeout_ms,
            topics=list(topics.items()),
            **kwargs)
        return request

    @asyncio.coroutine
    def do(self, node_id):
        request = self.create_request()
        try:
            response = yield from self._client.send(node_id, request)
        except KafkaError as err:
            log.warning(
                "Got error produce response: %s", err)
            if getattr(err, "invalid_metadata", False):
                self._client.force_metadata_update()

            for batch in self._batches.values():
                if not self._can_retry(err, batch):
                    batch.failure(exception=err)
                else:
                    self._to_reenqueue.append(batch)
        else:
            # noacks, just mark batches as "done"
            if request.required_acks == 0:
                for batch in self._batches.values():
                    batch.done_noack()
            else:
                self.handle_reponse(response)

        if self._to_reenqueue:
            # Wait backoff before reequeue
            yield from asyncio.sleep(self._default_backoff, loop=self._loop)

            for batch in self._to_reenqueue:
                self._sender._message_accumulator.reenqueue(batch)
            # If some error started metadata refresh we have to wait before
            # trying again
            yield from self._client._maybe_wait_metadata()

    def handle_reponse(self, response):
        for topic, partitions in response.topics:
            for partition_info in partitions:
                if response.API_VERSION < 2:
                    partition, error_code, offset = partition_info
                    # Mimic CREATE_TIME to take user provided timestamp
                    timestamp = -1
                else:
                    partition, error_code, offset, timestamp = partition_info
                tp = TopicPartition(topic, partition)
                error = Errors.for_code(error_code)
                batch = self._batches.get(tp)
                if batch is None:
                    continue

                if error is Errors.NoError:
                    batch.done(offset, timestamp)
                elif error is DuplicateSequenceNumber:
                    # If we have received a duplicate sequence error,
                    # it means that the sequence number has advanced
                    # beyond the sequence of the current batch, and we
                    # haven't retained batch metadata on the broker to
                    # return the correct offset and timestamp.
                    #
                    # The only thing we can do is to return success to
                    # the user and not return a valid offset and
                    # timestamp.
                    batch.done(offset, timestamp)
                elif error is InvalidProducerEpoch:
                    error = ProducerFenced

                if not self._can_retry(error(), batch):
                    batch.failure(exception=error())
                else:
                    log.warning(
                        "Got error produce response on topic-partition"
                        " %s, retrying. Error: %s", tp, error)
                    # Ok, we can retry this batch
                    if getattr(error, "invalid_metadata", False):
                        self._client.force_metadata_update()
                    self._to_reenqueue.append(batch)

    def _can_retry(self, error, batch):
        # If indempotence is enabled we never expire batches, but retry until
        # we succeed. We can be sure, that no duplicates will be introduced
        # as long as we set proper sequence, pid and epoch.
        if self._sender._txn_manager is None and batch.expired():
            return False
        # XXX: remove unknown topic check as we fix
        #      https://github.com/dpkp/kafka-python/issues/1155
        if error.retriable or isinstance(error, UnknownTopicOrPartitionError)\
                or error is UnknownTopicOrPartitionError:
            return True
        return False
