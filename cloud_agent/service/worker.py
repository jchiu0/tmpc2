import argparse
import asyncio
import logging
import os
import socket
import threading
import time
import uuid

from redis.exceptions import RedisError

from cloud_agent.lib import AgentRequest, run_agent

from .config import load_settings
from .queue import AgentQueue, QueueMessage
from .storage import AgentStore, StaleExecutionError


logger = logging.getLogger(__name__)
worker_identity = "unassigned"


class WorkerLogContext(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.service = "cloud-agent-worker"
        record.worker = worker_identity
        return True


def consumer_name() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


def process_message(
    message: QueueMessage,
    store: AgentStore,
    queue: AgentQueue,
    consumer: str,
    stale_after_ms: int,
    execution_delay: float = 0,
) -> None:
    run = store.claim_execution(message.run_id)
    if run is None:
        logger.info("run_skipped run_id=%s reason=not_executable", message.run_id)
        queue.acknowledge(message.message_id)
        return
    logger.info(
        "run_claimed run_id=%s attempt=%s",
        message.run_id,
        run["attempt_count"],
    )
    epoch = int(run["attempt_count"])

    request = AgentRequest(
        prompt=run["prompt"],
        repo=run["repo_url"],
        starting_ref=run["starting_ref"],
        work_on_current_branch=bool(run["work_on_current_branch"]),
        output_branch=run["output_branch"],
        mcp_url=run["mcp_url"],
        idempotency_key=message.run_id,
        history=tuple(store.conversation_before(message.run_id)),
        prepared_commit_sha=run["prepared_commit_sha"],
        prepared_parent_sha=run["prepared_parent_sha"],
        prepared_branch=run["prepared_branch"],
        prepared_output=run["assistant_output"],
    )

    def save_event(event_type: str, payload: dict) -> None:
        if not queue.refresh_lease(consumer, message.message_id):
            raise StaleExecutionError(message.run_id)
        if event_type == "agent.publication_prepared":
            store.checkpoint_publication(
                message.run_id,
                epoch,
                payload["branch"],
                payload["commit"],
                payload.get("parent"),
                payload["output"],
            )
            logger.info(
                "publication_prepared run_id=%s commit=%s",
                message.run_id,
                payload["commit"],
            )
            return
        store.append_event(message.run_id, epoch, event_type, payload)
        if event_type == "agent.status" and payload.get("status") == "running":
            logger.info("grok_started run_id=%s", message.run_id)
        elif event_type == "agent.response":
            logger.info("grok_response_received run_id=%s", message.run_id)

    stop_heartbeat = threading.Event()

    def refresh_lease() -> None:
        interval = max(1.0, stale_after_ms / 3000)
        while not stop_heartbeat.wait(interval):
            try:
                if not store.is_current_epoch(message.run_id, epoch):
                    logger.warning(
                        "heartbeat_fenced run_id=%s epoch=%s",
                        message.run_id,
                        epoch,
                    )
                    return
                if not queue.refresh_lease(consumer, message.message_id):
                    logger.warning(
                        "heartbeat_ownership_lost run_id=%s", message.run_id
                    )
                    return
            except RedisError:
                return

    # TODO: Run the agent in a separate process/container so its workload
    # cannot delay the lease heartbeat through GIL contention.
    heartbeat = threading.Thread(target=refresh_lease, daemon=True)
    heartbeat.start()
    try:
        if execution_delay:
            logger.info(
                "execution_delayed run_id=%s seconds=%s",
                message.run_id,
                execution_delay,
            )
            time.sleep(execution_delay)
        logger.info("execution_started run_id=%s", message.run_id)
        result = asyncio.run(run_agent(request, save_event))
        if not queue.refresh_lease(consumer, message.message_id):
            raise StaleExecutionError(message.run_id)
    except StaleExecutionError:
        logger.warning(
            "execution_fenced run_id=%s epoch=%s", message.run_id, epoch
        )
        return
    except Exception as error:
        try:
            store.fail(message.run_id, epoch, str(error))
        except StaleExecutionError:
            logger.warning(
                "failure_write_fenced run_id=%s epoch=%s",
                message.run_id,
                epoch,
            )
            return
        logger.exception("execution_failed run_id=%s", message.run_id)
    else:
        store.finish(message.run_id, epoch, result)
        logger.info("execution_finished run_id=%s", message.run_id)
    finally:
        stop_heartbeat.set()
        heartbeat.join()
    if queue.acknowledge_if_owned(consumer, message.message_id):
        logger.info("message_acknowledged run_id=%s", message.run_id)
    else:
        logger.warning("acknowledgement_fenced run_id=%s", message.run_id)


def run_worker(once: bool = False, execution_delay: float = 0) -> None:
    global worker_identity

    settings = load_settings()
    store = AgentStore(settings.database_path)
    queue = AgentQueue(
        settings.redis_url, settings.stream_name, settings.consumer_group
    )
    store.initialize()
    queue.initialize()
    consumer = consumer_name()
    worker_identity = consumer
    logger.info("worker_started consumer=%s", consumer)
    claim_stale_next = False
    try:
        while True:
            if claim_stale_next:
                stale = queue.claim_stale(consumer, settings.stale_after_ms)
                message = stale[0] if stale else None
                if message is not None:
                    logger.info(
                        "message_autoclaimed run_id=%s", message.run_id
                    )
            else:
                message = queue.read(consumer)
                if message is not None:
                    logger.info("message_received run_id=%s", message.run_id)
            claim_stale_next = not claim_stale_next
            if message is None:
                continue
            process_message(
                message,
                store,
                queue,
                consumer,
                settings.stale_after_ms,
                execution_delay,
            )
            if once:
                return
    finally:
        queue.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one cloud agent worker")
    parser.add_argument(
        "--once", action="store_true", help="process at most one agent"
    )
    parser.add_argument(
        "--execution-delay",
        type=float,
        default=0,
        help="test-only delay after claiming a run",
    )
    parser.add_argument(
        "--log-file",
        help="append timestamped worker lifecycle logs to this file",
    )
    args = parser.parse_args()
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file))
    for handler in handlers:
        handler.addFilter(WorkerLogContext())
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s %(levelname)s "
            "service=%(service)s worker=%(worker)s %(message)s"
        ),
        handlers=handlers,
    )
    run_worker(once=args.once, execution_delay=args.execution_delay)


if __name__ == "__main__":
    main()
