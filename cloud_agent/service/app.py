import uuid
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, HttpUrl
from redis.exceptions import RedisError

from .config import load_settings
from .queue import AgentQueue
from .storage import AgentStore


class Prompt(BaseModel):
    text: str = Field(min_length=1)


class Repository(BaseModel):
    url: HttpUrl
    startingRef: str | None = None


class CreateAgentRequest(BaseModel):
    prompt: Prompt
    repos: Annotated[list[Repository], Field(min_length=1, max_length=1)]
    name: str | None = Field(default=None, max_length=100)
    workOnCurrentBranch: bool = False
    autoCreatePR: bool = True
    outputBranch: str | None = None


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
    status: str
    createdAt: str
    updatedAt: str


class CreateAgentResponse(BaseModel):
    agent: AgentResponse
    run: RunResponse


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
    agent_id = f"bc-{uuid.uuid4()}"
    run_id = f"run-{uuid.uuid4()}"
    repository = request.repos[0]
    created = store.create_agent_and_run(
        agent_id=agent_id,
        run_id=run_id,
        name=request.name or request.prompt.text[:100],
        prompt=request.prompt.text,
        repo_url=str(repository.url),
        starting_ref=repository.startingRef,
        work_on_current_branch=request.workOnCurrentBranch,
        auto_create_pr=request.autoCreatePR,
        output_branch=request.outputBranch,
        mcp_url=settings.mcp_url,
    )

    # TODO: Replace this SQLite/Redis dual write with a transactional outbox.
    try:
        queue.publish(run_id)
    except RedisError as error:
        raise HTTPException(
            status_code=503,
            detail=(
                f"agent {agent_id} and run {run_id} were saved "
                "but the run could not be queued"
            ),
        ) from error
    return created
