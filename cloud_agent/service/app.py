import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, HttpUrl, model_validator
from redis.exceptions import RedisError

from cloud_agent.lib.runner import generated_branch

from .config import load_settings
from .queue import AgentQueue
from .storage import (
    AgentBusyError,
    AgentNotFoundError,
    AgentStore,
    SourceAgentRunError,
    WorkflowNotWaitingError,
)


class Prompt(BaseModel):
    text: str = Field(min_length=1)


class Source(BaseModel):
    language: Literal["python"]
    code: str = Field(min_length=1, max_length=200_000)
    entrypoint: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")


class Repository(BaseModel):
    url: HttpUrl
    startingRef: str | None = None


class CustomSubagent(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9-]+$", min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=500)
    prompt: str = Field(min_length=1, max_length=20_000)
    model: str = Field(default="inherit", min_length=1, max_length=100)
    readonly: bool = False


class CreateAgentRequest(BaseModel):
    prompt: Prompt | None = None
    source: Source | None = None
    repos: Annotated[list[Repository], Field(min_length=1, max_length=1)]
    name: str | None = Field(default=None, max_length=100)
    workOnCurrentBranch: bool = False
    autoCreatePR: bool | None = None
    outputBranch: str | None = None
    customSubagents: list[CustomSubagent] = Field(
        default_factory=list, max_length=20
    )

    @model_validator(mode="after")
    def validate_input(self) -> "CreateAgentRequest":
        if (self.prompt is None) == (self.source is None):
            raise ValueError("exactly one of prompt or source is required")
        return self


class AgentEnvironment(BaseModel):
    type: str


class AgentResponse(BaseModel):
    id: str
    name: str
    status: str
    env: AgentEnvironment
    repos: list[Repository]
    workOnCurrentBranch: bool
    autoCreatePR: bool
    url: str
    createdAt: str
    updatedAt: str
    latestRunId: str


class RunResponse(BaseModel):
    id: str
    agentId: str
    type: Literal["llm", "python"]
    status: str
    createdAt: str
    updatedAt: str


class RunDetailResponse(RunResponse):
    workflowKey: str | None
    startedAt: str | None
    finishedAt: str | None
    queueDurationMs: int | None
    durationMs: int | None


class CreateAgentResponse(BaseModel):
    agent: AgentResponse
    run: RunResponse


class CreateRunRequest(BaseModel):
    prompt: Prompt
    outputBranch: str | None = None


class WorkflowInputRequest(BaseModel):
    response: str = Field(min_length=1)


class CreateRunResponse(BaseModel):
    run: RunResponse


class EventResponse(BaseModel):
    id: int
    runId: str
    type: str
    payload: dict[str, Any]
    createdAt: str


class GetEventsResponse(BaseModel):
    runId: str
    status: str
    events: list[EventResponse]
    nextCursor: str


settings = load_settings()
store = AgentStore(settings.database_path)
queue = AgentQueue(
    settings.redis_url, settings.stream_name, settings.consumer_group
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.initialize()
    queue.initialize()
    yield
    queue.close()


app = FastAPI(title="Local Cloud Agents", lifespan=lifespan)


@app.post(
    "/v1/agents",
    response_model=CreateAgentResponse,
    status_code=202,
)
def create_agent(request: CreateAgentRequest) -> dict:
    subagent_names = [
        subagent.name for subagent in request.customSubagents
    ]
    if len(subagent_names) != len(set(subagent_names)):
        raise HTTPException(
            status_code=422,
            detail="custom subagent names must be unique",
        )
    agent_id = f"bc-{uuid.uuid4()}"
    run_id = f"run-{uuid.uuid4()}"
    repository = request.repos[0]
    source = request.source
    prompt_text = request.prompt.text if request.prompt is not None else None
    auto_create_pr = (
        request.autoCreatePR
        if request.autoCreatePR is not None
        else source is None
    )
    if request.workOnCurrentBranch:
        working_branch = repository.startingRef
        output_branch = repository.startingRef
    else:
        suffix = hashlib.sha256(agent_id.encode("utf-8")).hexdigest()[:6]
        working_branch = request.outputBranch or generated_branch(
            prompt_text or f"workflow-{source.entrypoint}", suffix
        )
        output_branch = working_branch
    if source is not None:
        created = store.create_source_agent_and_run(
            agent_id=agent_id,
            run_id=run_id,
            name=request.name or source.entrypoint,
            source_code=source.code,
            source_hash=hashlib.sha256(source.code.encode("utf-8")).hexdigest(),
            source_entrypoint=source.entrypoint,
            repo_url=str(repository.url),
            starting_ref=repository.startingRef,
            working_branch=working_branch,
            auto_create_pr=auto_create_pr,
            mcp_url=settings.mcp_url,
        )
    else:
        created = store.create_agent_and_run(
            agent_id=agent_id,
            run_id=run_id,
            name=request.name or prompt_text[:100],
            prompt=prompt_text,
            repo_url=str(repository.url),
            starting_ref=repository.startingRef,
            working_branch=working_branch,
            work_on_current_branch=request.workOnCurrentBranch,
            auto_create_pr=auto_create_pr,
            output_branch=output_branch,
            mcp_url=settings.mcp_url,
            custom_subagents=tuple(
                subagent.model_dump() for subagent in request.customSubagents
            ),
        )

    # TODO: Replace this SQLite/Redis dual write with a transactional outbox.
    try:
        queue.publish(run_id)
        store.mark_outbox_run_delivered(run_id)
    except RedisError as error:
        raise HTTPException(
            status_code=503,
            detail=(
                f"agent {agent_id} and run {run_id} were saved "
                "but the run could not be queued"
            ),
        ) from error
    return created


@app.post(
    "/v1/agents/{agent_id}/runs",
    response_model=CreateRunResponse,
    status_code=202,
)
def create_run(
    agent_id: str, request: CreateRunRequest
) -> dict[str, dict[str, str]]:
    run_id = f"run-{uuid.uuid4()}"
    try:
        created = store.create_run(
            agent_id,
            run_id,
            request.prompt.text,
            settings.mcp_url,
            request.outputBranch,
        )
    except AgentNotFoundError as error:
        raise HTTPException(status_code=404, detail="agent not found") from error
    except AgentBusyError as error:
        raise HTTPException(
            status_code=409,
            detail={"code": "agent_busy", "message": "agent has an active run"},
        ) from error
    except SourceAgentRunError as error:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "workflow_autonomous",
                "message": "source agents do not accept follow-up runs",
            },
        ) from error

    # TODO: Replace this SQLite/Redis dual write with a transactional outbox.
    try:
        queue.publish(run_id)
        store.mark_outbox_run_delivered(run_id)
    except RedisError as error:
        raise HTTPException(
            status_code=503,
            detail=f"run {run_id} was saved but could not be queued",
        ) from error
    return created


@app.get(
    "/v1/agents/{agent_id}/state",
)
def get_agent_state(agent_id: str) -> dict[str, Any]:
    agent = store.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    if agent["agent_kind"] != "source":
        raise HTTPException(
            status_code=409, detail="agent does not have workflow state"
        )
    return {
        "agentId": agent_id,
        "status": agent["workflow_status"],
        "state": agent["workflow_state"],
        "stateData": json.loads(agent["workflow_state_json"]),
        "result": agent["workflow_result"],
        "version": agent["workflow_version"],
        "latestRunId": agent["latest_run_id"],
        "userInput": (
            {
                "key": agent["workflow_input_key"],
                "prompt": agent["workflow_input_prompt"],
            }
            if agent["workflow_status"] == "USER_INPUT"
            else None
        ),
    }


@app.post("/v1/agents/{agent_id}/input", status_code=202)
def provide_workflow_input(
    agent_id: str, request: WorkflowInputRequest
) -> dict[str, Any]:
    run_id = f"run-{uuid.uuid4()}"
    try:
        created = store.resume_workflow_with_input(
            agent_id, run_id, request.response
        )
    except AgentNotFoundError as error:
        raise HTTPException(status_code=404, detail="agent not found") from error
    except WorkflowNotWaitingError as error:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "workflow_not_waiting",
                "message": "workflow is not waiting for user input",
            },
        ) from error
    try:
        queue.publish(run_id)
        store.mark_outbox_run_delivered(run_id)
    except RedisError as error:
        raise HTTPException(
            status_code=503,
            detail="workflow input persisted but queue is unavailable",
        ) from error
    return {
        "agent": {
            "id": agent_id,
            "status": "ACTIVE",
            "latestRunId": run_id,
        },
        "run": {
            "id": created["run_id"],
            "agentId": agent_id,
            "type": created["run_kind"],
            "status": created["status"],
            "createdAt": created["created_at"],
            "updatedAt": created["updated_at"],
        },
    }


def elapsed_ms(start: str | None, end: str | None) -> int | None:
    if start is None or end is None:
        return None
    return round(
        (
            datetime.fromisoformat(end) - datetime.fromisoformat(start)
        ).total_seconds()
        * 1000
    )


@app.get(
    "/v1/agents/{agent_id}/runs/{run_id}",
    response_model=RunDetailResponse,
)
def get_run(agent_id: str, run_id: str) -> dict[str, Any]:
    run = store.get_run(run_id)
    if run is None or run["agent_id"] != agent_id:
        raise HTTPException(status_code=404, detail="run not found")
    return {
        "id": run_id,
        "agentId": agent_id,
        "type": run["run_kind"],
        "status": run["status"],
        "workflowKey": run["workflow_key"],
        "createdAt": run["created_at"],
        "updatedAt": run["updated_at"],
        "startedAt": run["started_at"],
        "finishedAt": run["finished_at"],
        "queueDurationMs": elapsed_ms(
            run["created_at"], run["started_at"]
        ),
        "durationMs": elapsed_ms(run["started_at"], run["finished_at"]),
    }


@app.get(
    "/v1/agents/{agent_id}/runs/{run_id}/events",
    response_model=GetEventsResponse,
)
def get_run_events(
    agent_id: str,
    run_id: str,
    after: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    try:
        after_id = int(after) if after is not None else 0
    except ValueError as error:
        raise HTTPException(status_code=400, detail="invalid cursor") from error
    if after_id < 0:
        raise HTTPException(status_code=400, detail="invalid cursor")
    run = store.get_run(run_id)
    if run is None or run["agent_id"] != agent_id:
        raise HTTPException(status_code=404, detail="run not found")
    rows = store.list_events(run_id, after_id, limit)
    events = [
        {
            "id": row["id"],
            "runId": row["run_id"],
            "type": row["event_type"],
            "payload": json.loads(row["payload_json"]),
            "createdAt": row["created_at"],
        }
        for row in rows
    ]
    return {
        "runId": run_id,
        "status": run["status"],
        "events": events,
        "nextCursor": str(rows[-1]["id"] if rows else after_id),
    }
