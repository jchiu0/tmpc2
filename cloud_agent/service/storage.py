import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


def now() -> str:
    return datetime.now(UTC).isoformat()


class AgentStore:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(agents)")
            }
            if columns and "agent_id" not in columns:
                connection.execute(
                    "ALTER TABLE agent_events RENAME TO legacy_agent_events"
                )
                connection.execute(
                    "ALTER TABLE agents RENAME TO legacy_agents"
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    repo_url TEXT NOT NULL,
                    starting_ref TEXT,
                    work_on_current_branch INTEGER NOT NULL DEFAULT 0,
                    auto_create_pr INTEGER NOT NULL DEFAULT 1,
                    latest_run_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
                    status TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    starting_ref TEXT,
                    work_on_current_branch INTEGER NOT NULL DEFAULT 0,
                    output_branch TEXT,
                    mcp_url TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_runs_agent_id
                    ON runs(agent_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_run_events_run_id
                    ON run_events(run_id, id);
                """
            )
            agent_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(agents)")
            }
            additions = {
                "name": "TEXT NOT NULL DEFAULT ''",
                "starting_ref": "TEXT",
                "work_on_current_branch": "INTEGER NOT NULL DEFAULT 0",
                "auto_create_pr": "INTEGER NOT NULL DEFAULT 1",
            }
            for column, definition in additions.items():
                if column not in agent_columns:
                    connection.execute(
                        f"ALTER TABLE agents ADD COLUMN {column} {definition}"
                    )
            run_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(runs)")
            }
            if "attempt_count" not in run_columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN "
                    "attempt_count INTEGER NOT NULL DEFAULT 0"
                )

    def create_agent_and_run(
        self,
        agent_id: str,
        run_id: str,
        name: str,
        prompt: str,
        repo_url: str,
        starting_ref: str | None,
        work_on_current_branch: bool,
        auto_create_pr: bool,
        output_branch: str | None,
        mcp_url: str,
    ) -> dict[str, Any]:
        timestamp = now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO agents (
                    agent_id, name, status, repo_url, starting_ref,
                    work_on_current_branch, auto_create_pr, latest_run_id,
                    created_at, updated_at
                ) VALUES (?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    name,
                    repo_url,
                    starting_ref,
                    int(work_on_current_branch),
                    int(auto_create_pr),
                    run_id,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, agent_id, status, prompt, starting_ref,
                    work_on_current_branch, output_branch, mcp_url,
                    created_at, updated_at
                ) VALUES (?, ?, 'CREATING', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    agent_id,
                    prompt,
                    starting_ref,
                    int(work_on_current_branch),
                    output_branch,
                    mcp_url,
                    timestamp,
                    timestamp,
                ),
            )
            self._insert_event(
                connection,
                run_id,
                "run.status",
                {"status": "CREATING"},
                timestamp,
            )
        return {
            "agent": {
                "id": agent_id,
                "name": name,
                "status": "ACTIVE",
                "env": {"type": "cloud"},
                "repos": [{"url": repo_url, "startingRef": starting_ref}],
                "workOnCurrentBranch": work_on_current_branch,
                "autoCreatePR": auto_create_pr,
                "url": f"https://cursor.com/agents/{agent_id}",
                "createdAt": timestamp,
                "updatedAt": timestamp,
                "latestRunId": run_id,
            },
            "run": {
                "id": run_id,
                "agentId": agent_id,
                "status": "CREATING",
                "createdAt": timestamp,
                "updatedAt": timestamp,
            },
        }

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT runs.*, agents.repo_url, agents.status AS agent_status
                FROM runs JOIN agents USING (agent_id)
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return dict(row) if row else None

    def claim_execution(self, run_id: str) -> dict[str, Any] | None:
        timestamp = now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT runs.*, agents.repo_url, agents.status AS agent_status
                FROM runs JOIN agents USING (agent_id)
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if (
                row is None
                or row["status"] not in {"CREATING", "RUNNING"}
                or row["agent_status"] != "ACTIVE"
            ):
                return None
            connection.execute(
                """
                UPDATE runs
                SET status = 'RUNNING',
                    attempt_count = attempt_count + 1,
                    updated_at = ?
                WHERE run_id = ?
                """,
                (timestamp, run_id),
            )
            if row["status"] == "CREATING":
                self._insert_event(
                    connection,
                    run_id,
                    "run.status",
                    {"status": "RUNNING"},
                    timestamp,
                )
            else:
                self._insert_event(
                    connection,
                    run_id,
                    "run.retry",
                    {"attempt": row["attempt_count"] + 1},
                    timestamp,
                )
            claimed = connection.execute(
                """
                SELECT runs.*, agents.repo_url, agents.status AS agent_status
                FROM runs JOIN agents USING (agent_id)
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return dict(claimed)

    def append_event(
        self, run_id: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        with self._connection() as connection:
            self._insert_event(connection, run_id, event_type, payload, now())

    def finish(self, run_id: str, result: dict[str, Any]) -> None:
        self._complete(run_id, "FINISHED", result, None)

    def fail(self, run_id: str, error: str) -> None:
        self._complete(run_id, "ERROR", None, error)

    def _complete(
        self,
        run_id: str,
        status: str,
        result: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        timestamp = now()
        payload: dict[str, Any] = {"status": status}
        if result is not None:
            payload["result"] = result
        if error is not None:
            payload["error"] = error
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE runs
                SET status = ?, result_json = ?, error = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    status,
                    json.dumps(result) if result is not None else None,
                    error,
                    timestamp,
                    run_id,
                ),
            )
            connection.execute(
                """
                UPDATE agents SET status = 'IDLE', updated_at = ?
                WHERE agent_id = (
                    SELECT agent_id FROM runs WHERE run_id = ?
                )
                """,
                (timestamp, run_id),
            )
            self._insert_event(
                connection, run_id, "run.status", payload, timestamp
            )

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        timestamp: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO run_events (
                run_id, event_type, payload_json, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (run_id, event_type, json.dumps(payload), timestamp),
        )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            with connection:
                yield connection
        finally:
            connection.close()
