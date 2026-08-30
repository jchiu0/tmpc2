# Manual Worker Crash-Recovery Test

This runbook reproduces the two-worker crash test captured in
`cloud_agent/logs/manual_crash_recovery.log`.

The test starts Worker A with a deterministic delay after it claims a run,
kills it, and starts Worker B. Worker B should auto-claim the pending Redis
message, increment the SQLite execution epoch, complete the run, update
SQLite, and acknowledge the message.

## Prerequisites

Run commands from the repository root.

1. Install the Python dependencies in `cloud_agent/.venv`.
2. Start Redis:

   ```bash
   brew services start redis
   ```

3. Start the local Grok MCP server in a separate terminal:

   ```bash
   ./local_tool_server/start.sh
   ```

4. Authenticate the GitHub CLI with write access to the test repository:

   ```bash
   gh auth status
   ```

The commands below use a dedicated SQLite database, Redis stream, and consumer
group so the test does not interfere with normal local runs:

```bash
export CLOUD_AGENT_DB="cloud_agent/data/manual_crash_recovery.db"
export AGENT_STREAM="cloud-agents-manual-crash-recovery"
export AGENT_CONSUMER_GROUP="manual-crash-recovery-workers"
export AGENT_STALE_AFTER_MS=3000
export NO_PROXY="127.0.0.1,localhost"
export no_proxy="$NO_PROXY"
```

Set these variables in every terminal used by the test.

## Reset previous test state

Use only these dedicated test names:

```bash
rm -f "$CLOUD_AGENT_DB"
redis-cli DEL "$AGENT_STREAM"
mkdir -p cloud_agent/logs
: > cloud_agent/logs/manual_crash_recovery.log
```

Choose a new branch so a previous test publication cannot be mistaken for the
new run:

```bash
export TEST_BRANCH="cursor/manual-crash-$(date +%Y%m%d-%H%M%S)"
```

## 1. Start the API

In terminal 1, export the shared variables and run:

```bash
cloud_agent/.venv/bin/python -m uvicorn \
  cloud_agent.service.app:app --host 127.0.0.1 --port 8001
```

Wait for Uvicorn to report that startup is complete.

## 2. Start delayed Worker A

In terminal 2, export the shared variables and start Worker A in the
background:

```bash
GITHUB_TOKEN="$(gh auth token)" \
cloud_agent/.venv/bin/python -m cloud_agent.service.worker \
  --once \
  --execution-delay 30 \
  --log-file cloud_agent/logs/manual_crash_recovery.log &
export WORKER_A_PID=$!
echo "Worker A PID: $WORKER_A_PID"
```

The 30-second delay begins only after Worker A has claimed a run. Its heartbeat
continues during the delay, so a healthy Worker A retains Redis ownership.

## 3. Create the agent and first run

In terminal 3, export the shared variables and `TEST_BRANCH`, then submit:

```bash
RESPONSE="$(
  curl -sS -X POST http://127.0.0.1:8001/v1/agents \
    -H 'Content-Type: application/json' \
    --data "{
      \"prompt\": {
        \"text\": \"Create MANUAL_CRASH_RECOVERY.md with a heading Crash Recovery and one sentence saying Worker B recovered the run.\"
      },
      \"repos\": [{
        \"url\": \"https://github.com/jchiu0/scratch1\",
        \"startingRef\": \"main\"
      }],
      \"name\": \"Manual worker crash recovery\",
      \"workOnCurrentBranch\": false,
      \"autoCreatePR\": false,
      \"outputBranch\": \"$TEST_BRANCH\"
    }"
)"
echo "$RESPONSE" | jq .
export RUN_ID="$(echo "$RESPONSE" | jq -r '.run.id')"
echo "Run ID: $RUN_ID"
```

Adjust the repository and `startingRef` if needed.

## 4. Confirm Worker A owns attempt 1

Before killing Worker A, verify all three conditions.

The log shows the claim and delay:

```bash
rg "$RUN_ID|execution_delayed" \
  cloud_agent/logs/manual_crash_recovery.log
```

SQLite reports `RUNNING` with epoch 1:

```bash
sqlite3 -json "$CLOUD_AGENT_DB" \
  "SELECT run_id,status,attempt_count
   FROM runs WHERE run_id='$RUN_ID';"
```

Redis reports one pending message owned by Worker A:

```bash
redis-cli XPENDING \
  "$AGENT_STREAM" "$AGENT_CONSUMER_GROUP" - + 10
```

Do not continue until the log contains `execution_delayed`, the SQLite status
is `RUNNING`, and `attempt_count` is `1`.

## 5. Kill Worker A

While Worker A is still in its delay:

```bash
kill -9 "$WORKER_A_PID"
```

This intentionally prevents graceful cleanup and leaves the Redis message
pending. Wait longer than the configured three-second stale interval:

```bash
sleep 4
```

## 6. Start Worker B

In terminal 4, export the shared variables and run:

```bash
GITHUB_TOKEN="$(gh auth token)" \
cloud_agent/.venv/bin/python -m cloud_agent.service.worker \
  --once \
  --log-file cloud_agent/logs/manual_crash_recovery.log
```

Worker B should auto-claim the stale message and exit after processing one
run.

## 7. Verify recovery

Check the combined worker log:

```bash
rg "$RUN_ID" cloud_agent/logs/manual_crash_recovery.log
```

The expected sequence is:

1. Worker A logs `message_received`.
2. Worker A logs `run_claimed ... attempt=1`.
3. Worker A logs `execution_delayed ... seconds=30.0`.
4. Worker B logs `message_autoclaimed`.
5. Worker B logs `run_claimed ... attempt=2`.
6. Worker B logs `execution_started`.
7. Worker B logs `execution_finished`.
8. Worker B logs `message_acknowledged`.

Verify SQLite:

```bash
sqlite3 -json "$CLOUD_AGENT_DB" \
  "SELECT status,attempt_count,
          json_extract(result_json,'$.branch') AS branch,
          json_extract(result_json,'$.commit') AS commit_sha,
          error
   FROM runs WHERE run_id='$RUN_ID';"
```

Expected values are `status = FINISHED`, `attempt_count = 2`, a nonempty
commit SHA, and no error.

Verify that Redis has no pending message:

```bash
redis-cli XPENDING "$AGENT_STREAM" "$AGENT_CONSUMER_GROUP"
```

The pending count should be zero. Finally, verify that the output branch
exists remotely:

```bash
gh api \
  "repos/jchiu0/scratch1/git/ref/heads/${TEST_BRANCH#refs/heads/}" \
  --jq '.object.sha'
```

## What this test proves

- A live heartbeat prevents premature auto-claim.
- Killing Worker A leaves its message pending rather than acknowledged.
- Worker B can auto-claim after the lease becomes stale.
- Reclaiming increments the SQLite epoch from 1 to 2.
- Worker B completes Git publication before writing terminal SQLite state.
- Worker B writes SQLite before the final ownership-checked `XACK`.

This scenario kills Worker A before Grok starts. Publication-boundary recovery
(a crash after Git branch update but before SQLite completion) is a separate
scenario and is covered by the Git `runId` marker logic.

## Cleanup

Stop the API with `Ctrl-C`. Remove only the dedicated local test state:

```bash
rm -f "$CLOUD_AGENT_DB"
redis-cli DEL "$AGENT_STREAM"
```

The output branch and Git commit are intentionally left in the test repository
for inspection. Delete the branch separately if it is no longer needed.
