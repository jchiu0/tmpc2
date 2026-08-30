# Worker Failure Recovery

This document describes the worker's current processing order, what happens
when it crashes between steps, and the fencing mechanisms that prevent an old
worker from overwriting a newer attempt.

## Processing sequence

1. **Acquire a Redis message.**
   The worker alternates between `XREADGROUP` for new messages and
   `XAUTOCLAIM` for messages whose lease has become stale. Redis records the
   message as pending and owned by the consumer.

2. **Claim the run in SQLite.**
   In a `BEGIN IMMEDIATE` transaction, the worker verifies that the run is
   `CREATING` or `RUNNING` and that its agent is `ACTIVE`. It changes the run
   to `RUNNING`, increments `attempt_count`, and records either a
   `run.status` or `run.retry` event. The new `attempt_count` is this
   execution's epoch.

   If the run is missing or already terminal, the worker does not execute it.
   It acknowledges the Redis message only if it still owns the message.

3. **Load execution input.**
   The worker loads the prompt, repository settings, MCP URL, output branch,
   and prior conversation history from SQLite. The `runId` becomes the Git
   idempotency key.

4. **Start the lease heartbeat.**
   A background thread periodically verifies the SQLite epoch and refreshes
   the Redis pending-message idle time. It stops refreshing if either check
   fails.

5. **Check for an already-published result.**
   The runner reads the output branch. If its head commit message begins with
   `[cloud-agent:<runId>]`, this run was already published. It returns the
   existing branch and commit without invoking Grok again.

6. **Prepare a temporary workspace.**
   The runner downloads `startingRef` into a new temporary directory and
   computes its initial content digest.

7. **Run the coding loop.**
   Before persisting each emitted event, the worker refreshes Redis ownership
   and verifies the SQLite epoch. Grok reads and modifies only the temporary
   workspace.

8. **Handle a no-change result.**
   If the workspace is unchanged, the runner returns a `no_changes` result.
   No Git commit is created.

9. **Create the Git commit object.**
   For changed work, the runner creates a remote commit whose message begins
   with `[cloud-agent:<runId>]`.

10. **Update the remote branch.**
    The runner updates the branch with an expected previous SHA. This is a
    compare-and-set operation: it fails if the branch changed unexpectedly.
    If the API response is ambiguous, the runner rereads the branch and treats
    a matching run marker as success.

11. **Perform the final Redis ownership check.**
    After the runner returns, the worker refreshes the lease once more. A
    worker that no longer owns the message stops without writing terminal
    state.

12. **Commit terminal state to SQLite.**
    In one SQLite transaction, the worker verifies its epoch, writes
    `FINISHED` and the result (or `ERROR` and the caught error), updates the
    agent, and appends the terminal event.

13. **Stop the heartbeat.**
    The worker signals and joins the heartbeat thread.

14. **Acknowledge the Redis message.**
    As the final durable workflow action, the worker runs an ownership-checked
    `XACK`. Acknowledgment removes the message from the consumer group's
    pending list; it does not delete the stream entry.

## Crash boundaries

The following cases describe a process crash, kill, host loss, or other abrupt
termination. An ordinary caught exception follows the separate error path
described below.

### Before step 1

No message has been acquired. It remains available for a worker.

### Between steps 1 and 2

The message remains pending in Redis, but SQLite is unchanged. After the lease
becomes stale, another worker auto-claims it and starts the first SQLite
execution epoch.

### Between steps 2 and 4

SQLite says `RUNNING`, and the message is pending without a heartbeat. Another
worker eventually auto-claims it and increments the epoch. The abandoned
worker cannot write with its old epoch if it resumes.

### Between steps 4 and 5

The heartbeat normally keeps the message from becoming stale. If the process
dies, the heartbeat dies too; another worker eventually auto-claims the
message and starts a newer epoch.

### Between steps 5 and 6

No new durable Git state exists. A replacement worker repeats the branch
marker check and workspace preparation.

### Between steps 6 and 7

Only the dead worker's temporary files are lost. A replacement worker creates
a fresh workspace and reruns.

### During step 7

Already-saved events remain in SQLite. Unsaved temporary edits are lost. A
replacement worker starts the coding loop again with a fresh workspace.
Repeated attempt events are acceptable because queue delivery is at least
once.

### Between steps 7 and 8

The completed model output and temporary edits may exist only in the dead
process. With no Git publication marker, a replacement worker reruns Grok.

### After step 8 but before step 12

For a no-change result, no Git marker exists. A replacement worker reruns the
coding loop and recomputes the no-change result.

### Between steps 9 and 10

The remote commit object may exist, but no branch points to it. The replacement
worker cannot discover it from the branch and creates another commit. The
orphaned object is harmless and may later be garbage-collected.

### Between steps 10 and 11

Git publication succeeded, but SQLite may still say `RUNNING`. A replacement
worker reads the branch marker, recognizes the same `runId`, skips Grok, and
continues with the existing commit.

### Between steps 11 and 12

Ownership could change after the Redis check. A replacement worker increments
the SQLite epoch. The old worker's terminal SQLite transaction then fails its
epoch check instead of overwriting the newer attempt.

### Between steps 12 and 13

SQLite already contains terminal state. If the heartbeat briefly continues,
its epoch check sees that the run is no longer `RUNNING` and stops.

### Between steps 13 and 14

The result is durable in SQLite, but the Redis message is still pending. A
replacement worker auto-claims it, sees that the run is terminal, skips
execution, and acknowledges the message if it owns it.

### After step 14

The message is no longer pending and will not be auto-claimed. Processing is
complete.

## Caught errors

If execution raises an ordinary exception while the worker still owns the
message and epoch, the worker writes `ERROR` to SQLite and then acknowledges
the message. The run is terminal and is not retried automatically.

If ownership or the SQLite epoch has been lost, the worker does not record
`ERROR` and does not acknowledge the message. The current owner is responsible
for continuing the run.

## Fencing

Fencing prevents a paused or partitioned worker from resuming later and
committing stale results.

### SQLite epoch fencing

`attempt_count` is a monotonically increasing execution epoch. Every successful
claim increments it. Event insertion and terminal updates require both:

- the run is still `RUNNING`; and
- `attempt_count` equals the worker's captured epoch.

A worker holding an older epoch receives `StaleExecutionError` and stops.

### Redis ownership fencing

Lease refresh and acknowledgment use Lua scripts so the ownership check and
Redis mutation are atomic. Each script reads `XPENDING` and proceeds only when
the message's current consumer matches this worker:

- lease refresh uses `XCLAIM ... JUSTID` to reset idle time; and
- completion uses `XACK` to remove the message from the pending list.

This prevents an old consumer from extending or acknowledging a message after
another consumer has auto-claimed it.

### Git publication fencing and idempotency

GitHub cannot participate in the SQLite/Redis transaction, so Git uses two
complementary safeguards:

- branch updates include the expected previous SHA, preventing an unexpected
  overwrite; and
- commit messages include `[cloud-agent:<runId>]`, allowing retries to
  recognize publication that succeeded before SQLite was updated.

For a new output branch, concurrent attempts may both create commit objects,
but only one branch creation wins. The loser rereads the branch and accepts it
only if its commit carries the same run marker.

### Remaining transaction boundaries

There is no distributed transaction spanning Redis, SQLite, and GitHub.
Correctness therefore comes from ordering, at-least-once delivery, fencing,
conditional Git updates, and idempotent recovery:

1. Git publication precedes terminal SQLite state.
2. Terminal SQLite state precedes `XACK`.
3. A Git run marker repairs the first gap.
4. Redis redelivery plus terminal-state detection repairs the second gap.

The heartbeat currently runs in a thread and does not forcibly cancel
`run_agent` when ownership is lost. The stale worker may continue computing
temporarily, but Redis and SQLite fencing prevent it from refreshing,
acknowledging, or committing database state. Conditional Git updates and the
run marker protect the publication boundary.
