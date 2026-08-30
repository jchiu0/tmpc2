from dataclasses import dataclass

import redis
from redis.exceptions import ResponseError


@dataclass(frozen=True)
class QueueMessage:
    message_id: str
    run_id: str


class AgentQueue:
    def __init__(self, redis_url: str, stream: str, group: str):
        self.client = redis.Redis.from_url(
            redis_url, decode_responses=True, socket_timeout=10
        )
        self.stream = stream
        self.group = group

    def initialize(self) -> None:
        try:
            self.client.xgroup_create(
                self.stream, self.group, id="0-0", mkstream=True
            )
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise

    def publish(self, run_id: str) -> str:
        return self.client.xadd(self.stream, {"run_id": run_id})

    def read(self, consumer: str, block_ms: int = 5000) -> QueueMessage | None:
        response = self.client.xreadgroup(
            self.group,
            consumer,
            {self.stream: ">"},
            count=1,
            block=block_ms,
        )
        if not response:
            return None
        _, messages = response[0]
        message_id, fields = messages[0]
        return QueueMessage(message_id, fields["run_id"])

    def claim_stale(
        self, consumer: str, min_idle_ms: int
    ) -> list[QueueMessage]:
        response = self.client.xautoclaim(
            self.stream,
            self.group,
            consumer,
            min_idle_ms,
            "0-0",
            count=1,
        )
        messages = response[1]
        return [
            QueueMessage(message_id, fields["run_id"])
            for message_id, fields in messages
        ]

    def acknowledge(self, message_id: str) -> None:
        self.client.xack(self.stream, self.group, message_id)

    def acknowledge_if_owned(self, consumer: str, message_id: str) -> bool:
        script = """
        local pending = redis.call(
            'XPENDING', KEYS[1], ARGV[1], ARGV[3], ARGV[3], 1
        )
        if #pending == 1 and pending[1][2] == ARGV[2] then
            return redis.call('XACK', KEYS[1], ARGV[1], ARGV[3])
        end
        return 0
        """
        return bool(
            self.client.eval(
                script,
                1,
                self.stream,
                self.group,
                consumer,
                message_id,
            )
        )

    def refresh_lease(self, consumer: str, message_id: str) -> bool:
        script = """
        local pending = redis.call(
            'XPENDING', KEYS[1], ARGV[1], ARGV[3], ARGV[3], 1
        )
        if #pending == 1 and pending[1][2] == ARGV[2] then
            redis.call(
                'XCLAIM', KEYS[1], ARGV[1], ARGV[2], 0, ARGV[3], 'JUSTID'
            )
            return 1
        end
        return 0
        """
        return bool(
            self.client.eval(
                script,
                1,
                self.stream,
                self.group,
                consumer,
                message_id,
            )
        )

    def close(self) -> None:
        self.client.close()
