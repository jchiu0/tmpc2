# Simple Local Cloud Agent

This script implements a minimal Cursor-style coding agent locally:

1. Download a GitHub repository ref into a temporary directory.
2. Ask Grok for bounded file actions through the local MCP server.
3. Apply text-file changes without model-generated shell commands.
4. Create Git blobs, a tree, a commit, and the output ref through GitHub's API.
5. Print the branch and commit as JSON.
6. Delete the temporary checkout.

## Prerequisites

Start the Grok tool server in a separate terminal:

```bash
./local_tool_server/start.sh
```

Set `GITHUB_TOKEN` or `GH_TOKEN` to a token with repository contents write
access. The script uses GitHub APIs for both checkout and publishing. Restart
the MCP server after changing its implementation.

Install the app-builder's Python linter with `brew install ruff`.

## Run the asynchronous service

Start Redis and the API from the project root:

```bash
brew services start redis
cloud_agent/.venv/bin/python -m uvicorn \
  cloud_agent.service.app:app --host 127.0.0.1 --port 8001
```

Run a worker in another terminal. The worker continuously blocks on the Redis
Stream, processes runs, commits their SQLite results, acknowledges each
message, and waits for the next run:

```bash
GITHUB_TOKEN="$(gh auth token)" \
  cloud_agent/.venv/bin/python -m cloud_agent.service.worker
```

Create an agent and its first run:

```bash
curl -X POST http://127.0.0.1:8001/v1/agents \
  -H 'Content-Type: application/json' \
  --data '{
    "prompt": {
      "text": "Create QUEUE_E2E.md describing the Redis worker flow."
    },
    "repos": [{
      "url": "https://github.com/jchiu0/scratch1",
      "startingRef": "cursor/create-a-concise-readme-md-expla-386dcb"
    }],
    "name": "Verify Redis worker end to end",
    "workOnCurrentBranch": false,
    "autoCreatePR": false,
    "outputBranch": "cursor/redis-e2e"
  }'
```

The endpoint returns Cursor-shaped `agent` and `run` objects. The first run is
returned as `CREATING`; the worker transitions it to `RUNNING`, then
`FINISHED` or `ERROR`. Agent, run, result, and event state is stored in
`cloud_agent/data/cloud_agents.db`.

After that run finishes, create a follow-up on the same agent:

```bash
curl -X POST http://127.0.0.1:8001/v1/agents/AGENT_ID/runs \
  -H 'Content-Type: application/json' \
  --data '{"prompt":{"text":"Also add tests"}}'
```

Only one run may be active per agent. Follow-ups reuse the agent's Git branch
and send each prior finished run's ordered prompt, assistant tool calls, tool
results, and final response to Grok as conversation history. System
instructions are regenerated for the current run.

Poll a run's durable events and current status:

```bash
curl "http://127.0.0.1:8001/v1/agents/AGENT_ID/runs/RUN_ID/events?after=0&limit=100"
```

The response contains ordered status, conversation, retry, response, and
terminal events plus `status` and `nextCursor`. Pass `nextCursor` as the next
request's `after` value. An empty event list means there are no new events;
continue polling until `status` is terminal.

Run the real two-run polling and conversation-continuation check:

```bash
cloud_agent/.venv/bin/python cloud_agent/manual_tests/02_multiple_runs.py
```

This starts an isolated API and one-shot workers, uses a unique Redis stream
and database, and verifies that the second run reuses both the first run's
conversation result and Git branch.

Agent creation may include up to 20 custom foreground subagents:

```json
"customSubagents": [{
  "name": "reviewer",
  "description": "Reviews repository changes",
  "prompt": "Inspect the delegated task carefully and report issues.",
  "model": "inherit",
  "readonly": true
}]
```

The parent invokes one with a `delegate` action. Each child gets a clean model
context and the same temporary workspace, while only its final result returns
to the parent context. Children never publish Git. If the worker dies during
delegation, Redis retries the complete parent run from its Git starting point;
there is no separate subagent recovery.

Run the real delegation and parent-owned Git publication check:

```bash
cloud_agent/.venv/bin/python \
  cloud_agent/manual_tests/04_subagent_delegation.py
```

To verify bounded parallelism with two readonly analyses followed by one
writable implementation:

```bash
cloud_agent/.venv/bin/python \
  cloud_agent/manual_tests/05_parallel_subagent_delegation.py
```

To verify `autoCreatePR` with a generated LeetCode solution:

```bash
cloud_agent/.venv/bin/python \
  cloud_agent/manual_tests/06_auto_create_pr.py
```

To verify one agent chaining follow-up work onto a new output branch:

```bash
cloud_agent/.venv/bin/python \
  cloud_agent/manual_tests/03_multiple_runs_multiple_branches.py
```

## Python state-machine agents

Create a trusted workflow by sending `source` instead of `prompt`. The
entrypoint must be a `StateMachine` subclass:

```python
from cloud_agent.workflow import StateMachine, activity, state

@activity
def verify_application(ctx, _input):
    return ctx.run_command(["python", "-m", "pytest", "-q"])

class Builder(StateMachine):
    initial_state = "build"

    @state
    def build(self, ctx, event):
        if event.type == "entered":
            return ctx.run_llm("build-0", "Implement the application")
        ctx.state["build"] = event.result["summary"]
        return ctx.transition("evaluate")

    @state
    def evaluate(self, ctx, event):
        if event.type == "entered":
            return ctx.run_python("evaluate-0", "verify_application")
        return ctx.complete("Application built")
```

Runs have an explicit `llm` or `python` type. `ctx.run_llm(...)` uses the
existing model/tool loop; `ctx.run_python(...)` invokes a trusted source
function decorated with `@activity` in a fresh repository checkout. Each keyed
Run is a durable boundary. SQLite atomically stores the
new state, state data, Run result, and next queue outbox entry. A replacement
worker replays the current Run after a crash; its execution epoch fences stale
workers. `ctx.run(key, result=...)` creates a Python checkpoint without
calling the LLM. `ctx.run_command(...)` is a synchronous trusted-code helper.
Pass `copy_context=False` to `ctx.run_llm(...)` when workflow Python supplies
the required state and durable files explicitly instead of replaying all prior
LLM transcripts.

SQLite records `started_at` on the first worker claim and `finished_at` on
completion. `GET /v1/agents/AGENT_ID/runs/RUN_ID` reports queue wait separately
and defines `durationMs` as worker execution time, excluding queue wait.

Poll `GET /v1/agents/AGENT_ID/state` for workflow state. When its status is
`USER_INPUT`, send a response to `POST /v1/agents/AGENT_ID/input`.
The bundled app-builder client runs `compileall` and Homebrew `ruff` immediately
after each implementation Run. Lint failures return to the LLM with captured
diagnostics before the separate pytest evaluation Run may complete.

Run the real four-state app-builder checks:

```bash
cloud_agent/.venv/bin/python \
  cloud_agent/manual_tests/07_state_machine_happy_path.py
cloud_agent/.venv/bin/python \
  cloud_agent/manual_tests/08_state_machine_crash_recovery.py
```

Worker execution is idempotent by `runId`. Generated branch names are stable,
published commits include the run ID, and stale-run recovery recognizes a
commit that was pushed before a worker crash. A lease heartbeat prevents
healthy long-running work from being auto-claimed by another worker.

## Create a generated branch

From the project root:

```bash
./cloud_agent/run.sh \
  --repo https://github.com/jchiu0/scratch1 \
  --starting-ref main \
  --prompt "Create a README describing this scratch repository"
```

The agent creates and pushes a branch such as
`cursor/create-a-readme-describing-th-12ab34`.

To choose the branch name:

```bash
./cloud_agent/run.sh \
  --repo https://github.com/jchiu0/scratch1 \
  --starting-ref main \
  --output-branch cursor/my-test \
  --prompt "Create a README"
```

## Write to the working branch

This mode updates `startingRef` through the GitHub API without force:

```bash
./cloud_agent/run.sh \
  --repo https://github.com/jchiu0/scratch1 \
  --starting-ref main \
  --work-on-current-branch \
  --prompt "Improve the README"
```

## Result

The final line is queryable JSON:

```json
{
  "status": "finished",
  "repo": "https://github.com/jchiu0/scratch1",
  "startingRef": "main",
  "workOnCurrentBranch": false,
  "branch": "cursor/create-a-readme-12ab34",
  "commit": "0123456789abcdef",
  "summary": "Create repository README"
}
```

## Safety limits

- Grok can only list, read, and write UTF-8 text files under the temporary
  checkout.
- `.git`, absolute paths, traversal, and symlink escapes are rejected.
- Grok cannot execute shell commands.
- GitHub API uploads are limited to 10 MB per workspace in this prototype.
- The workspace is on the host, not in a security boundary. Docker isolation
  is intentionally deferred until the local flow is proven.
