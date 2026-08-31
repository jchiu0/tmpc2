# Python State-Machine Workflows

## Core model

- Extend `POST /v1/agents` in `cloud_agent/service/app.py` to accept exactly one of the existing `prompt` or a new `source` object containing `language: "python"`, `code`, and `entrypoint`.
- Treat the Python source as an immutable, hash-addressed workflow definition. It orchestrates ordinary Cloud Agent Runs but is never inserted into LLM message history.
- Runs have an explicit `llm` or `python` type. Initial orchestration, checkpoints, user input, and named trusted activities are Python Runs; model/tool execution is an LLM Run.
- Make typed Runs the primary checkpoint boundary: `ctx.run_llm(key, prompt)`, `ctx.run_python(key, activity, input)`, and compatibility `ctx.run(key, result=...)` atomically persist state and create the next queueable Run.
- When `prompt` is present, the Run invokes the existing coding agent. When omitted, the Run performs no LLM call and immediately persists the state checkpoint plus optional public result text.
- A source agent's initial Run starts with an `entered` event. Each state-machine step may execute at most one `ctx.run(...)`; further autonomous work is scheduled as subsequent Runs. This preserves one checkpoint per Run.

## Runtime and SDK

- Complete `cloud_agent/workflow.py` with `StateMachine`, `@state`, `@activity`, transactional `ctx.state`, typed durable Runs, transitions, completion/failure, and synchronous activity command execution.
- Add `cloud_agent/workflow_host.py`. Each invocation dynamically imports the stored source under a module name derived from its hash, validates the entrypoint, invokes only the current state handler with copied state and one event, validates JSON output, and exits.
- Add a parent wrapper using `sys.executable -m cloud_agent.workflow_host` and a framed JSON-lines protocol for events, terminal command output, and structured errors. Apply timeout, stdout/stderr capture, and source-size limits; terminate the child if fencing or event persistence fails.
- Keep Redis, SQLite, leases, epochs, and acknowledgements exclusively in the parent worker. The subprocess receives only source, state, and the triggering event.
- The subprocess provides fault isolation, not a security sandbox; submitted source remains trusted.
- `ctx.run_command(...)` executes synchronously inside a trusted Python activity with timeout and output capture. The containing Python Run persists its JSON result and Git changes.
- Every `run()` requires a stable key. Computation before that boundary repeats after a crash, so external side effects must be idempotent or explicitly reconciled.

## Durable state and run boundaries

- Extend `cloud_agent/service/storage.py` with immutable workflow definition data and a snapshot containing `current_state`, `state_json`, `workflow_status`, terminal result, and a monotonically increasing version.
- Store explicit `llm`/`python` Run types plus Python activity names and JSON inputs. Enforce uniqueness by agent and workflow key.
- Snapshot workflow state and version onto every activity Run. A retry of Run N always starts from the checkpoint produced after Run N-1.
- On successful activity completion, persist the Run result and enqueue a durable `run_completed` event for the current state handler.
- Commit state changes, operation scheduling, transition events, and next-Run creation in one SQLite transaction with version and epoch fencing. Publish Redis work only after that transaction.
- Add a transactional outbox so agent, Run, state, and user-message writes commit atomically with the intent to publish Redis work. A dispatcher retries outbox delivery; Redis remains a notification mechanism and SQLite remains authoritative.

Recovery rules:

- Crash during Run N: discard provisional changes and retry Run N from Run N-1's checkpoint.
- Crash after Git publication: recover through the existing run-ID commit marker.
- Crash after SQLite completion but before Redis ACK: skip completed work and republish any pending workflow continuation.
- Incomplete attempt messages never enter future LLM context.

## Failure model and transaction boundaries

### Publishing queue work

The outbox dispatcher publishes committed Runs to Redis and then marks the outbox row delivered. If it crashes before publication, the row remains pending. If it crashes after `XADD` but before marking delivery, it publishes a duplicate. Duplicate stream entries are expected and harmless because the Run ID, workflow operation key, Run status, and execution epoch fence execution.

Redis failure never rolls back a created Run. It delays delivery until the outbox dispatcher retries.

### Preparing and executing a Run

Before an LLM-backed Run performs external work, persist its resolved `ctx.run` key, prompt, input state snapshot, state name, workflow version, and input hash. Retrying that Run must use this immutable prepared input rather than invoking the state handler again to produce a potentially different prompt.

If the worker crashes:

- before prepared input commits: rerun the state handler from the previous checkpoint;
- after prepared input commits but before the LLM call: reuse the prepared input and start the LLM call;
- during the LLM call: reclaim the same Run with a new epoch and redo it;
- after Git publication but before SQLite completion: recover using the Run-ID Git marker;
- after SQLite completion but before Redis acknowledgement: observe the terminal status and only acknowledge the duplicate message.

Checkpoint-only Runs follow the same lifecycle but skip the LLM and Git steps.

### Completing a Run and continuing the workflow

After Run N succeeds, invoke the state handler with the durable Run result. One final transaction must fence by Run epoch and workflow version, then:

1. Mark Run N and its keyed operation completed.
2. Persist the new state name and state snapshot.
3. Append the Run result and workflow transition events.
4. Persist a terminal outcome or prepared next Run.
5. Insert an outbox row when more work is runnable.

If this transaction does not commit, Run N is retried and its idempotent Git result is recovered. If it commits, stale workers cannot update the workflow because both the Run epoch and workflow version have advanced.

### Internal computation and `run_command`

Internal Python and `run_command` execute before the next durable `run()` boundary. A subprocess crash, timeout, or worker reclaim discards state mutations and repeats that computation. Therefore:

- pure computation is always safe;
- filesystem work should use atomic replacement or disposable paths;
- external calls must have their own idempotency keys or reconciliation checks;
- long-running processes are unsupported initially;
- stdout, stderr, duration, and exit status are bounded before being copied into state.

### Message and event recovery

Events are append-only and tagged with Run ID, workflow version, and execution attempt. Events from abandoned attempts may remain visible for diagnostics, but only the completed attempt contributes messages to future LLM context. CLI cursors use event IDs, so reconnecting never requires an open connection and duplicate polling does not duplicate stored input.

## Workflow dispatch

- Generalize `cloud_agent/service/queue.py` messages to identify either an activity Run or workflow continuation while retaining ownership-checked lease refresh and acknowledgement.
- Update `cloud_agent/service/worker.py` to dispatch `llm` Runs to the existing runner and `python` Runs to orchestration, checkpoint, input, or named activity execution.
- Process `transition` by atomically changing state and enqueueing an `entered` event. Process `complete` and `fail` as terminal workflow outcomes.
- For app-builder evaluation, a named Python activity checks out the latest branch, runs finite tools such as linters, tests, or Playwright, publishes any changes, and returns structured JSON.

## API visibility

- Add a read endpoint exposing workflow status, current state, state data, and terminal result.
- Continue exposing activity Run events through the existing polling endpoint. Add workflow transition and completion events without exposing state-handler invocations as Runs.
- Source-agent creation returns an initial Python orchestration Run. Its resulting typed activity is a separate immutable Run.
- User input, chat interruption, `wait_for_user`, CLI interaction handling, and webhooks are explicitly deferred.

## Existing-run hardening

- Fix the identified `autoCreatePR` recovery issues in `cloud_agent/lib/runner.py` and `cloud_agent/lib/github.py`: same-head/base follow-ups must not fail after publication, the title limit must interpolate correctly, and merged or closed PRs must be recognized idempotently.
- Preserve current coding-agent behavior and API responses when `prompt` is supplied.

## Verification

- Add SDK and host tests for source loading, entrypoint validation, transactional state copies, malformed or non-JSON state, timeout, and process failure.
- Add storage tests proving `run()` atomically checkpoints state and creates one keyed activity, stale versions cannot commit, and duplicate delivery cannot duplicate Runs.
- Add recovery tests for crashes during Run 3, after Git publication, after state checkpoint, and before Redis ACK; verify resume uses Run 2's state and excludes abandoned Run-3 messages.
- Add API tests for prompt/source exclusivity, workflow state retrieval, autonomous Run chaining, and ordinary coding-agent compatibility.
- Add a numbered manual app-builder scenario covering requirements approval, planning, sequential subtasks, Playwright evaluation, repair, and successful completion.
