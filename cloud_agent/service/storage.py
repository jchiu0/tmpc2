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


class SourceAgentRunError(RuntimeError):
    pass


class WorkflowNotWaitingError(RuntimeError):
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
                    agent_kind TEXT NOT NULL DEFAULT 'coding',
                    source_code TEXT,
                    source_hash TEXT,
                    source_entrypoint TEXT,
                    workflow_state TEXT,
                    workflow_state_json TEXT NOT NULL DEFAULT '{}',
                    workflow_status TEXT,
                    workflow_result TEXT,
                    workflow_version INTEGER NOT NULL DEFAULT 0,
                    workflow_input_key TEXT,
                    workflow_input_prompt TEXT,
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
                    run_kind TEXT NOT NULL DEFAULT 'llm',
                    workflow_key TEXT,
                    workflow_state TEXT,
                    workflow_state_json TEXT,
                    workflow_version INTEGER,
                    checkpoint_result TEXT,
                    workflow_event_type TEXT,
                    workflow_event_json TEXT,
                    python_activity TEXT,
                    python_input_json TEXT,
                    copy_context INTEGER NOT NULL DEFAULT 1,
                    started_at TEXT,
                    finished_at TEXT,
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

                CREATE TABLE IF NOT EXISTS queue_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    delivered_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE (run_id)
                );

                CREATE INDEX IF NOT EXISTS idx_runs_agent_id
                    ON runs(agent_id, created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_run_per_agent
                    ON runs(agent_id)
                    WHERE status IN ('CREATING', 'RUNNING');
                CREATE INDEX IF NOT EXISTS idx_run_events_run_id
                    ON run_events(run_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_run_key
                    ON runs(agent_id, workflow_key)
                    WHERE workflow_key IS NOT NULL;
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
                "agent_kind": "TEXT NOT NULL DEFAULT 'coding'",
                "source_code": "TEXT",
                "source_hash": "TEXT",
                "source_entrypoint": "TEXT",
                "workflow_state": "TEXT",
                "workflow_state_json": "TEXT NOT NULL DEFAULT '{}'",
                "workflow_status": "TEXT",
                "workflow_result": "TEXT",
                "workflow_version": "INTEGER NOT NULL DEFAULT 0",
                "workflow_input_key": "TEXT",
                "workflow_input_prompt": "TEXT",
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
            run_additions = {
                "assistant_output": "TEXT",
                "run_kind": "TEXT NOT NULL DEFAULT 'llm'",
                "workflow_key": "TEXT",
                "workflow_state": "TEXT",
                "workflow_state_json": "TEXT",
                "workflow_version": "INTEGER",
                "checkpoint_result": "TEXT",
                "workflow_event_type": "TEXT",
                "workflow_event_json": "TEXT",
                "python_activity": "TEXT",
                "python_input_json": "TEXT",
                "copy_context": "INTEGER NOT NULL DEFAULT 1",
                "started_at": "TEXT",
                "finished_at": "TEXT",
            }
            for column, definition in run_additions.items():
                if column not in run_columns:
                    connection.execute(
                        f"ALTER TABLE runs ADD COLUMN {column} {definition}"
                    )
            connection.execute(
                "UPDATE runs SET run_kind = 'llm' WHERE run_kind = 'coding'"
            )
            connection.execute(
                """
                UPDATE runs SET run_kind = CASE
                    WHEN prompt <> '' THEN 'llm' ELSE 'python'
                END
                WHERE run_kind = 'workflow'
                """
            )
            connection.execute(
                """
                UPDATE runs
                SET started_at = (
                    SELECT MIN(run_events.created_at)
                    FROM run_events
                    WHERE run_events.run_id = runs.run_id
                      AND run_events.event_type = 'run.status'
                      AND json_extract(
                          run_events.payload_json, '$.status'
                      ) = 'RUNNING'
                )
                WHERE started_at IS NULL
                """
            )
            connection.execute(
                """
                UPDATE runs
                SET finished_at = COALESCE(
                    (
                        SELECT MAX(run_events.created_at)
                        FROM run_events
                        WHERE run_events.run_id = runs.run_id
                          AND run_events.event_type = 'run.status'
                          AND json_extract(
                              run_events.payload_json, '$.status'
                          ) IN ('FINISHED', 'ERROR')
                    ),
                    updated_at
                )
                WHERE finished_at IS NULL
                  AND status IN ('FINISHED', 'ERROR')
                """
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
                    run_kind, created_at, updated_at
                ) VALUES (?, ?, 'CREATING', ?, ?, ?, ?, ?, 'llm', ?, ?)
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
            self._insert_outbox(connection, run_id, timestamp)
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
                "type": "llm",
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
                if agent["agent_kind"] == "source":
                    raise SourceAgentRunError(agent_id)
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
                        run_kind, created_at, updated_at
                    ) VALUES (?, ?, 'CREATING', ?, ?, ?, ?, ?, 'llm', ?, ?)
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
                self._insert_outbox(connection, run_id, timestamp)
        except sqlite3.IntegrityError as error:
            if "runs.agent_id" in str(error):
                raise AgentBusyError(agent_id) from error
            raise
        return {
            "run": {
                "id": run_id,
                "agentId": agent_id,
                "type": "llm",
                "status": "CREATING",
                "createdAt": timestamp,
                "updatedAt": timestamp,
            }
        }

    def create_source_agent_and_run(
        self,
        *,
        agent_id: str,
        run_id: str,
        name: str,
        source_code: str,
        source_hash: str,
        source_entrypoint: str,
        repo_url: str,
        starting_ref: str | None,
        working_branch: str,
        auto_create_pr: bool,
        mcp_url: str,
    ) -> dict[str, Any]:
        timestamp = now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO agents (
                    agent_id, name, status, repo_url, starting_ref,
                    working_branch, work_on_current_branch, auto_create_pr,
                    agent_kind, source_code, source_hash, source_entrypoint,
                    workflow_status, latest_run_id, created_at, updated_at
                ) VALUES (
                    ?, ?, 'ACTIVE', ?, ?, ?, 0, ?, 'source', ?, ?, ?,
                    'RUNNING', ?, ?, ?
                )
                """,
                (
                    agent_id,
                    name,
                    repo_url,
                    starting_ref,
                    working_branch,
                    int(auto_create_pr),
                    source_code,
                    source_hash,
                    source_entrypoint,
                    run_id,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, agent_id, status, prompt, starting_ref,
                    work_on_current_branch, output_branch, mcp_url, run_kind,
                    created_at, updated_at
                ) VALUES (?, ?, 'CREATING', '', ?, 0, ?, ?, 'python', ?, ?)
                """,
                (
                    run_id,
                    agent_id,
                    starting_ref,
                    working_branch,
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
            self._insert_outbox(connection, run_id, timestamp)
        return {
            "agent": {
                "id": agent_id,
                "name": name,
                "status": "ACTIVE",
                "env": {"type": "cloud"},
                "repos": [{"url": repo_url, "startingRef": starting_ref}],
                "workOnCurrentBranch": False,
                "autoCreatePR": auto_create_pr,
                "url": f"https://cursor.com/agents/{agent_id}",
                "createdAt": timestamp,
                "updatedAt": timestamp,
                "latestRunId": run_id,
            },
            "run": {
                "id": run_id,
                "agentId": agent_id,
                "type": "python",
                "status": "CREATING",
                "createdAt": timestamp,
                "updatedAt": timestamp,
            },
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
                  AND assistant_output IS NOT NULL AND prompt <> ''
                  AND created_at < ?
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
                       agents.auto_create_pr, agents.agent_kind,
                       agents.source_code, agents.source_hash,
                       agents.source_entrypoint,
                       agents.workflow_state AS agent_workflow_state,
                       agents.workflow_state_json AS agent_workflow_state_json,
                       agents.workflow_status, agents.workflow_result,
                       agents.workflow_version AS agent_workflow_version
                FROM runs JOIN agents USING (agent_id)
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
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

    def prepare_workflow_run(
        self,
        run_id: str,
        epoch: int,
        *,
        workflow_key: str,
        state_name: str,
        state_data: dict[str, Any],
        run_type: str,
        prompt: str | None,
        checkpoint_result: str | None,
        python_activity: str | None,
        python_input: Any,
        copy_context: bool = True,
    ) -> dict[str, Any]:
        if run_type not in {"llm", "python"}:
            raise ValueError(f"unsupported run type: {run_type}")
        timestamp = now()
        with self._connection() as connection:
            self._require_epoch(connection, run_id, epoch)
            run = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise StaleExecutionError(run_id)
            if run["workflow_key"] is not None:
                return dict(run)
            agent = connection.execute(
                """
                SELECT workflow_version FROM agents
                WHERE agent_id = ?
                """,
                (run["agent_id"],),
            ).fetchone()
            version = int(agent["workflow_version"]) + 1
            encoded_state = json.dumps(state_data)
            try:
                connection.execute(
                    """
                    UPDATE runs
                    SET workflow_key = ?, run_kind = ?, prompt = ?,
                        workflow_state = ?,
                        workflow_state_json = ?, workflow_version = ?,
                        checkpoint_result = ?,
                        python_activity = ?, python_input_json = ?,
                        copy_context = ?,
                        updated_at = ?
                    WHERE run_id = ? AND attempt_count = ?
                    """,
                    (
                        workflow_key,
                        run_type,
                        prompt or "",
                        state_name,
                        encoded_state,
                        version,
                        checkpoint_result,
                        python_activity,
                        json.dumps(python_input),
                        int(copy_context),
                        timestamp,
                        run_id,
                        epoch,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise AgentBusyError(workflow_key) from error
            connection.execute(
                """
                UPDATE agents
                SET workflow_state = ?, workflow_state_json = ?,
                    workflow_version = ?, updated_at = ?
                WHERE agent_id = ?
                """,
                (
                    state_name,
                    encoded_state,
                    version,
                    timestamp,
                    run["agent_id"],
                ),
            )
            self._insert_event(
                connection,
                run_id,
                "workflow.checkpoint",
                {
                    "key": workflow_key,
                    "state": state_name,
                    "version": version,
                },
                timestamp,
            )
            prepared = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return dict(prepared)

    def finish_workflow_run(
        self,
        run_id: str,
        epoch: int,
        result: dict[str, Any],
        *,
        state_name: str,
        state_data: dict[str, Any],
        command: dict[str, Any],
        next_run_id: str | None,
    ) -> str | None:
        timestamp = now()
        command_type = command["type"]
        arguments = command.get("arguments", {})
        with self._connection() as connection:
            self._require_epoch(connection, run_id, epoch)
            run = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            agent = connection.execute(
                "SELECT * FROM agents WHERE agent_id = ?",
                (run["agent_id"],),
            ).fetchone()
            branch = result.get("branch")
            summary = result.get("summary")
            connection.execute(
                """
                UPDATE runs
                SET status = 'FINISHED', assistant_output = ?,
                    result_json = ?, finished_at = ?, updated_at = ?
                WHERE run_id = ? AND attempt_count = ?
                """,
                (
                    summary,
                    json.dumps(result),
                    timestamp,
                    timestamp,
                    run_id,
                    epoch,
                ),
            )
            self._insert_event(
                connection,
                run_id,
                "run.status",
                {"status": "FINISHED", "result": result},
                timestamp,
            )

            version = int(agent["workflow_version"])
            encoded_state = json.dumps(state_data)
            next_id: str | None = None
            if command_type == "run":
                if next_run_id is None:
                    raise ValueError("next run ID is required")
                key = str(arguments["key"])
                run_type = str(arguments.get("runType", "llm"))
                if run_type not in {"llm", "python"}:
                    raise ValueError(f"unsupported run type: {run_type}")
                prompt = arguments.get("prompt")
                published = branch if (
                    result.get("commit") or run["work_on_current_branch"]
                ) else None
                if published:
                    starting_ref = branch
                    work_on_current_branch = True
                    output_branch = branch
                else:
                    starting_ref = agent["starting_ref"]
                    work_on_current_branch = False
                    output_branch = agent["working_branch"]
                next_version = version + 1
                connection.execute(
                    """
                    INSERT INTO runs (
                        run_id, agent_id, status, prompt, starting_ref,
                        work_on_current_branch, output_branch, mcp_url,
                        run_kind, workflow_key, workflow_state,
                        workflow_state_json, workflow_version,
                        checkpoint_result, python_activity,
                        python_input_json, copy_context,
                        created_at, updated_at
                    ) VALUES (
                        ?, ?, 'CREATING', ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        next_run_id,
                        run["agent_id"],
                        prompt or "",
                        starting_ref,
                        int(work_on_current_branch),
                        output_branch,
                        run["mcp_url"],
                        run_type,
                        key,
                        state_name,
                        encoded_state,
                        next_version,
                        arguments.get("result"),
                        arguments.get("activity"),
                        json.dumps(arguments.get("input")),
                        int(arguments.get("copyContext", True)),
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    UPDATE agents
                    SET status = 'ACTIVE', working_branch = COALESCE(?, working_branch),
                        workflow_state = ?, workflow_state_json = ?,
                        workflow_version = ?, workflow_status = 'RUNNING',
                        latest_run_id = ?, updated_at = ?
                    WHERE agent_id = ?
                    """,
                    (
                        branch,
                        state_name,
                        encoded_state,
                        next_version,
                        next_run_id,
                        timestamp,
                        run["agent_id"],
                    ),
                )
                self._insert_event(
                    connection,
                    next_run_id,
                    "run.status",
                    {"status": "CREATING"},
                    timestamp,
                )
                self._insert_outbox(connection, next_run_id, timestamp)
                next_id = next_run_id
            elif command_type == "wait_for_user":
                connection.execute(
                    """
                    UPDATE agents
                    SET status = 'IDLE', working_branch = COALESCE(?, working_branch),
                        workflow_state = ?, workflow_state_json = ?,
                        workflow_status = 'USER_INPUT',
                        workflow_input_key = ?, workflow_input_prompt = ?,
                        updated_at = ?
                    WHERE agent_id = ?
                    """,
                    (
                        branch,
                        state_name,
                        encoded_state,
                        arguments["key"],
                        arguments["prompt"],
                        timestamp,
                        run["agent_id"],
                    ),
                )
                self._insert_event(
                    connection,
                    run_id,
                    "workflow.user_input",
                    {
                        "key": arguments["key"],
                        "prompt": arguments["prompt"],
                    },
                    timestamp,
                )
            elif command_type == "complete":
                connection.execute(
                    """
                    UPDATE agents
                    SET status = 'IDLE', working_branch = COALESCE(?, working_branch),
                        workflow_state = ?, workflow_state_json = ?,
                        workflow_status = 'COMPLETED', workflow_result = ?,
                        workflow_input_key = NULL,
                        workflow_input_prompt = NULL,
                        updated_at = ?
                    WHERE agent_id = ?
                    """,
                    (
                        branch,
                        state_name,
                        encoded_state,
                        arguments.get("result", ""),
                        timestamp,
                        run["agent_id"],
                    ),
                )
                self._insert_event(
                    connection,
                    run_id,
                    "workflow.completed",
                    {"result": arguments.get("result", "")},
                    timestamp,
                )
            elif command_type == "fail":
                connection.execute(
                    """
                    UPDATE agents
                    SET status = 'IDLE', workflow_state = ?,
                        workflow_state_json = ?, workflow_status = 'FAILED',
                        workflow_result = ?, workflow_input_key = NULL,
                        workflow_input_prompt = NULL, updated_at = ?
                    WHERE agent_id = ?
                    """,
                    (
                        state_name,
                        encoded_state,
                        arguments.get("error", ""),
                        timestamp,
                        run["agent_id"],
                    ),
                )
                self._insert_event(
                    connection,
                    run_id,
                    "workflow.failed",
                    {"error": arguments.get("error", "")},
                    timestamp,
                )
            else:
                raise ValueError(f"unsupported terminal command: {command_type}")
        return next_id

    def resume_workflow_with_input(
        self, agent_id: str, run_id: str, response: str
    ) -> dict[str, Any]:
        timestamp = now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            agent = connection.execute(
                "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
            ).fetchone()
            if agent is None:
                raise AgentNotFoundError(agent_id)
            if (
                agent["agent_kind"] != "source"
                or agent["workflow_status"] != "USER_INPUT"
            ):
                raise WorkflowNotWaitingError(agent_id)
            latest = connection.execute(
                "SELECT result_json FROM runs WHERE run_id = ?",
                (agent["latest_run_id"],),
            ).fetchone()
            latest_result = (
                json.loads(latest["result_json"])
                if latest and latest["result_json"]
                else {}
            )
            published = bool(latest_result.get("commit"))
            starting_ref = (
                agent["working_branch"]
                if published
                else agent["starting_ref"]
            )
            version = int(agent["workflow_version"]) + 1
            input_key = str(agent["workflow_input_key"])
            event = {"key": input_key, "response": response}
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, agent_id, status, prompt, starting_ref,
                    work_on_current_branch, output_branch, mcp_url,
                    run_kind, workflow_key, workflow_state,
                    workflow_state_json, workflow_version,
                    checkpoint_result, workflow_event_type,
                    workflow_event_json, created_at, updated_at
                ) VALUES (
                    ?, ?, 'CREATING', '', ?, ?, ?, ?, 'python', ?, ?,
                    ?, ?, ?, 'user_input', ?, ?, ?
                )
                """,
                (
                    run_id,
                    agent_id,
                    starting_ref,
                    int(published),
                    agent["working_branch"],
                    connection.execute(
                        "SELECT mcp_url FROM runs WHERE run_id = ?",
                        (agent["latest_run_id"],),
                    ).fetchone()["mcp_url"],
                    f"input:{input_key}:{version}",
                    agent["workflow_state"],
                    agent["workflow_state_json"],
                    version,
                    response,
                    json.dumps(event),
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                UPDATE agents
                SET status = 'ACTIVE', workflow_status = 'RUNNING',
                    workflow_version = ?, workflow_input_key = NULL,
                    workflow_input_prompt = NULL, latest_run_id = ?,
                    updated_at = ?
                WHERE agent_id = ?
                """,
                (version, run_id, timestamp, agent_id),
            )
            self._insert_event(
                connection,
                run_id,
                "run.status",
                {"status": "CREATING"},
                timestamp,
            )
            self._insert_event(
                connection,
                run_id,
                "workflow.input_received",
                event,
                timestamp,
            )
            self._insert_outbox(connection, run_id, timestamp)
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return dict(row)

    def pending_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, run_id FROM queue_outbox
                WHERE delivered_at IS NULL
                ORDER BY id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_outbox_delivered(self, outbox_id: int) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE queue_outbox SET delivered_at = ?
                WHERE id = ? AND delivered_at IS NULL
                """,
                (now(), outbox_id),
            )

    def mark_outbox_run_delivered(self, run_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE queue_outbox SET delivered_at = ?
                WHERE run_id = ? AND delivered_at IS NULL
                """,
                (now(), run_id),
            )

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
                       agents.auto_create_pr, agents.agent_kind,
                       agents.source_code, agents.source_hash,
                       agents.source_entrypoint,
                       agents.workflow_state AS agent_workflow_state,
                       agents.workflow_state_json AS agent_workflow_state_json,
                       agents.workflow_status, agents.workflow_result,
                       agents.workflow_version AS agent_workflow_version
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
                    started_at = COALESCE(started_at, ?),
                    updated_at = ?
                WHERE run_id = ?
                """,
                (timestamp, timestamp, run_id),
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
                       agents.auto_create_pr, agents.agent_kind,
                       agents.source_code, agents.source_hash,
                       agents.source_entrypoint,
                       agents.workflow_state AS agent_workflow_state,
                       agents.workflow_state_json AS agent_workflow_state_json,
                       agents.workflow_status, agents.workflow_result,
                       agents.workflow_version AS agent_workflow_version
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
                    result_json = ?, error = ?,
                    finished_at = ?, updated_at = ?
                WHERE run_id = ? AND attempt_count = ?
                """,
                (
                    status,
                    output,
                    json.dumps(result) if result is not None else None,
                    error,
                    timestamp,
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
                    workflow_status = CASE
                        WHEN agent_kind = 'source' AND ? = 'ERROR'
                        THEN 'FAILED'
                        ELSE workflow_status
                    END,
                    workflow_result = CASE
                        WHEN agent_kind = 'source' AND ? = 'ERROR'
                        THEN ?
                        ELSE workflow_result
                    END,
                    updated_at = ?
                WHERE latest_run_id = ?
                """,
                (branch, status, status, error, timestamp, run_id),
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

    @staticmethod
    def _insert_outbox(
        connection: sqlite3.Connection, run_id: str, timestamp: str
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO queue_outbox (run_id, created_at)
            VALUES (?, ?)
            """,
            (run_id, timestamp),
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
