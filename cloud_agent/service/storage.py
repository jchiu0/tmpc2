import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


class AgentNotFoundError(RuntimeError):
    pass


class AgentBusyError(RuntimeError):
    pass


class StaleExecutionError(RuntimeError):
    pass


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
                    working_branch TEXT,
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
                    assistant_output TEXT,
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

                CREATE TABLE IF NOT EXISTS agent_subagents (
                    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT 'inherit',
                    readonly INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (agent_id, name)
                );

                CREATE INDEX IF NOT EXISTS idx_runs_agent_id
                    ON runs(agent_id, created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_run_per_agent
                    ON runs(agent_id)
                    WHERE status IN ('CREATING', 'RUNNING');
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
                "working_branch": "TEXT",
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
            for column in ("assistant_output",):
                if column not in run_columns:
                    connection.execute(
                        f"ALTER TABLE runs ADD COLUMN {column} TEXT"
                    )

    def create_agent_and_run(
        self,
        agent_id: str,
        run_id: str,
        name: str,
        prompt: str,
        repo_url: str,
        starting_ref: str | None,
        working_branch: str | None,
        work_on_current_branch: bool,
        auto_create_pr: bool,
        output_branch: str | None,
        mcp_url: str,
        custom_subagents: tuple[dict[str, Any], ...] = (),
    ) -> dict[str, Any]:
        timestamp = now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO agents (
                    agent_id, name, status, repo_url, starting_ref,
                    working_branch, work_on_current_branch, auto_create_pr,
                    latest_run_id, created_at, updated_at
                ) VALUES (?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    name,
                    repo_url,
                    starting_ref,
                    working_branch,
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
            for subagent in custom_subagents:
                connection.execute(
                    """
                    INSERT INTO agent_subagents (
                        agent_id, name, description, prompt, model, readonly,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        agent_id,
                        subagent["name"],
                        subagent["description"],
                        subagent["prompt"],
                        subagent.get("model", "inherit"),
                        int(subagent.get("readonly", False)),
                        timestamp,
                        timestamp,
                    ),
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

    def create_run(
        self,
        agent_id: str,
        run_id: str,
        prompt: str,
        mcp_url: str,
        output_branch: str | None = None,
    ) -> dict[str, Any]:
        timestamp = now()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                agent = connection.execute(
                    "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
                ).fetchone()
                if agent is None:
                    raise AgentNotFoundError(agent_id)
                if agent["status"] == "ARCHIVED":
                    raise AgentBusyError(agent_id)

                published = connection.execute(
                    """
                    SELECT 1 FROM runs
                    WHERE agent_id = ? AND status = 'FINISHED'
                      AND result_json IS NOT NULL
                      AND json_extract(result_json, '$.commit') IS NOT NULL
                    LIMIT 1
                    """,
                    (agent_id,),
                ).fetchone()
                if output_branch is not None:
                    starting_ref = (
                        agent["working_branch"]
                        if agent["work_on_current_branch"] or published
                        else agent["starting_ref"]
                    )
                    work_on_current_branch = False
                    selected_output_branch = output_branch
                elif agent["work_on_current_branch"] or published:
                    starting_ref = agent["working_branch"]
                    work_on_current_branch = True
                    selected_output_branch = agent["working_branch"]
                else:
                    starting_ref = agent["starting_ref"]
                    work_on_current_branch = False
                    selected_output_branch = agent["working_branch"]

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
                        selected_output_branch,
                        mcp_url,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    UPDATE agents
                    SET status = 'ACTIVE', latest_run_id = ?, updated_at = ?
                    WHERE agent_id = ?
                    """,
                    (run_id, timestamp, agent_id),
                )
                self._insert_event(
                    connection,
                    run_id,
                    "run.status",
                    {"status": "CREATING"},
                    timestamp,
                )
        except sqlite3.IntegrityError as error:
            if "runs.agent_id" in str(error):
                raise AgentBusyError(agent_id) from error
            raise
        return {
            "run": {
                "id": run_id,
                "agentId": agent_id,
                "status": "CREATING",
                "createdAt": timestamp,
                "updatedAt": timestamp,
            }
        }

    def conversation_before(self, run_id: str) -> list[dict[str, str]]:
        with self._connection() as connection:
            current = connection.execute(
                "SELECT agent_id, created_at FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if current is None:
                return []
            rows = connection.execute(
                """
                SELECT run_id, prompt, assistant_output
                FROM runs
                WHERE agent_id = ? AND status = 'FINISHED'
                  AND assistant_output IS NOT NULL AND created_at < ?
                ORDER BY created_at, run_id
                """,
                (current["agent_id"], current["created_at"]),
            ).fetchall()
            messages: list[dict[str, str]] = []
            for row in rows:
                transcript = connection.execute(
                    """
                    SELECT
                        json_extract(payload_json, '$.role') AS role,
                        json_extract(payload_json, '$.content') AS content
                    FROM run_events
                    WHERE run_id = ?
                      AND event_type = 'conversation.message'
                      AND json_extract(payload_json, '$.attempt') = (
                          SELECT MAX(
                              json_extract(payload_json, '$.attempt')
                          )
                          FROM run_events
                          WHERE run_id = ?
                            AND event_type = 'conversation.message'
                            AND json_extract(
                                payload_json, '$.kind'
                            ) = 'final_response'
                      )
                    ORDER BY id
                    """,
                    (row["run_id"], row["run_id"]),
                ).fetchall()
                if transcript:
                    messages.extend(
                        {
                            "role": message["role"],
                            "content": message["content"],
                        }
                        for message in transcript
                    )
                else:
                    messages.append(
                        {"role": "user", "content": row["prompt"]}
                    )
                    messages.append(
                        {
                            "role": "assistant",
                            "content": row["assistant_output"],
                        }
                    )
        return messages

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT runs.*, agents.repo_url,
                       agents.status AS agent_status,
                       agents.auto_create_pr
                FROM runs JOIN agents USING (agent_id)
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_subagents(self, agent_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT name, description, prompt, model, readonly
                FROM agent_subagents
                WHERE agent_id = ?
                ORDER BY name
                """,
                (agent_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_events(
        self, run_id: str, after_id: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, run_id, event_type, payload_json, created_at
                FROM run_events
                WHERE run_id = ? AND id > ?
                ORDER BY id
                LIMIT ?
                """,
                (run_id, after_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_execution(self, run_id: str) -> dict[str, Any] | None:
        timestamp = now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT runs.*, agents.repo_url,
                       agents.status AS agent_status,
                       agents.auto_create_pr
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
                SELECT runs.*, agents.repo_url,
                       agents.status AS agent_status,
                       agents.auto_create_pr
                FROM runs JOIN agents USING (agent_id)
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return dict(claimed)

    def is_current_epoch(self, run_id: str, epoch: int) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM runs
                WHERE run_id = ? AND status = 'RUNNING'
                  AND attempt_count = ?
                """,
                (run_id, epoch),
            ).fetchone()
        return row is not None

    def append_event(
        self,
        run_id: str,
        epoch: int,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        with self._connection() as connection:
            self._require_epoch(connection, run_id, epoch)
            self._insert_event(connection, run_id, event_type, payload, now())

    def finish(
        self, run_id: str, epoch: int, result: dict[str, Any]
    ) -> None:
        self._complete(run_id, epoch, "FINISHED", result, None)

    def fail(self, run_id: str, epoch: int, error: str) -> None:
        self._complete(run_id, epoch, "ERROR", None, error)

    def _complete(
        self,
        run_id: str,
        epoch: int,
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
        output = result.get("summary") if result is not None else None
        branch = result.get("branch") if result is not None else None
        with self._connection() as connection:
            self._require_epoch(connection, run_id, epoch)
            cursor = connection.execute(
                """
                UPDATE runs
                SET status = ?,
                    assistant_output = COALESCE(?, assistant_output),
                    result_json = ?, error = ?, updated_at = ?
                WHERE run_id = ? AND attempt_count = ?
                """,
                (
                    status,
                    output,
                    json.dumps(result) if result is not None else None,
                    error,
                    timestamp,
                    run_id,
                    epoch,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleExecutionError(run_id)
            connection.execute(
                """
                UPDATE agents
                SET status = 'IDLE',
                    working_branch = COALESCE(?, working_branch),
                    updated_at = ?
                WHERE latest_run_id = ?
                """,
                (branch, timestamp, run_id),
            )
            self._insert_event(
                connection, run_id, "run.status", payload, timestamp
            )

    @staticmethod
    def _require_epoch(
        connection: sqlite3.Connection, run_id: str, epoch: int
    ) -> None:
        row = connection.execute(
            """
            SELECT 1 FROM runs
            WHERE run_id = ? AND status = 'RUNNING' AND attempt_count = ?
            """,
            (run_id, epoch),
        ).fetchone()
        if row is None:
            raise StaleExecutionError(run_id)

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
