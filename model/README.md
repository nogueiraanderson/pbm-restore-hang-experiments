# Protocol model

`pbm_state_machine.py` is a discrete-event model of PBM v2.11.0 physical-restore
coordination, extracted from `pbm/restore/physical.go`. It exists to answer one question
mechanically: which single fault, injected where, reproduces the incident trace? Run it
with `uv run pbm_state_machine.py` or `python3 pbm_state_machine.py` (stdlib only).

## What it encodes

| Piece | Semantics (v2.11.0 reference) |
|-------|-------------------------------|
| Topology | 3 nodes; n2 is the primary, the only node that runs the PITR oplog replay (`physical.go:1288`) |
| Phases per node | FLUSH, COPY, PREPDATA, REPLAY (primary only), RESETRS, DONE, CLOSE, EXITED |
| Heartbeat | Every node writes `node.<n>.hb`, `rs.hb`, `cluster.hb` every 120s (`hbFrameSec`, `physical.go:64,2231-2270`) from init until `close(stopHB)`; file content is the write timestamp |
| Staleness | `checkHB`: content timestamp + 240s < now (`physical.go:2348`) |
| DONE convergence | Each node writes `node.done` (`:724`), then polls peers every 5s: peer resolves as done if its `node.done` exists, is dropped if its `node.hb` is stale (`:897-898`), else wait. Converged status is `done` only if nobody was dropped, else `partlyDone` (`:932-934`); every node then writes `rs.<status>` and `cluster.<status>` (`:729-749`, Done bypasses the leader-only gate) |
| CLOSE | Poll every 5s for `cluster.{error,done,partlyDone}`, else `checkHB(cluster.hb)` (`:369-407`). Pristine mode: the node's own heartbeat keeps refreshing `cluster.hb`, so the escape is self-defeating. Patched mode (PR #1339): own heartbeat stops at CLOSE entry |
| Clock | 5s ticks, 10h horizon (the incident ran 8+ hours) |

## Fault space

One fault per run, swept across nodes and phases, each in pristine and patched mode:

| Fault | Semantics |
|-------|-----------|
| `HANG(node, phase)` | Wedge forever; heartbeat keeps running |
| `KILL(node, phase)` | Process death; heartbeat stops |
| `OUTAGE(node, t0, dur)` | Storage outage: heartbeat writes fail during the window; a blocking read in flight during the window wedges FOREVER (deadline-less HeadObject on a half-open connection); writes resume after the window. Permanent variant included |
| `LOSE(path)` | One status-file write silently swallowed |
| `SKEW(node, delta)` | Heartbeat content timestamps offset (clock skew) |

## Verdict: the six incident observables

A run reproduces the incident only if ALL hold:

1. Both secondaries exit with success (they logged `recovery successfully finished`).
2. `cluster.partlyDone` exists.
3. `cluster.done` does not exist.
4. n2 never exits.
5. n2's heartbeat is still live at the 10h horizon (the reporter's 09:25 uploads).
6. n2 is wedged in a phase where its ephemeral mongod is up (the 8-byte held `mongod.lock`).

## Results and what each negative proves

| Fault | Verdict | Lesson |
|-------|---------|--------|
| `OUTAGE(n2, bounded, over REPLAY)` | REPRODUCES, in pristine AND patched mode | The incident trigger; the PR #1339 fix is orthogonal to it |
| `HANG(n2, REPLAY)` | Fails: secondaries never exit | They cannot drop a fresh-heartbeat peer, so the heartbeat freeze is REQUIRED |
| `KILL(n2, *)` | Fails observable 5 | Heartbeats were alive 8 hours later |
| `OUTAGE` permanent | Fails observable 5 | Writes must recover while the wedged read persists: exactly the no-read-deadline semantics |
| `LOSE(node.n2.done)` | Fails 3 and 4 | n2 converges via its peers' done files and exits successfully |
| `SKEW(n2)` | Fails 3 and 4 | n2 completes normally |

## Fidelity limits

Protocol-level model, not the Go code. It abstracts replay internals to a single
wedged-read flag, models one non-sharded replica set, uses nominal phase durations tuned
to the incident timeline, and does not model SDK retry ladders or partial-read errors in
`waitFiles`. It proves consistency and uniqueness within this fault space, not the
absence of exotic multi-fault explanations.
