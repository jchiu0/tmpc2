import os
from dataclasses import dataclass
from pathlib import Path

from cloud_agent.lib.runner import DEFAULT_MCP_URL


@dataclass(frozen=True)
class Settings:
    database_path: Path
    redis_url: str
    mcp_url: str
    stream_name: str
    consumer_group: str
    stale_after_ms: int


def load_settings() -> Settings:
    default_db = Path(__file__).resolve().parents[1] / "data" / "cloud_agents.db"
    return Settings(
        database_path=Path(os.getenv("CLOUD_AGENT_DB", default_db)),
        redis_url=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
        mcp_url=os.getenv("MCP_URL", DEFAULT_MCP_URL),
        stream_name=os.getenv("AGENT_STREAM", "cloud-agents"),
        consumer_group=os.getenv("AGENT_CONSUMER_GROUP", "cloud-agent-workers"),
        stale_after_ms=int(os.getenv("AGENT_STALE_AFTER_MS", "60000")),
    )
