# Model-vs-code fidelity audit

Line-by-line comparison of `pbm_state_machine.py` against a fresh clone of
`percona-backup-mongodb` at `v2.11.0` (commit `2f499a6`). Reproduce with:

```
git clone --depth 1 --branch v2.11.0 https://github.com/percona/percona-backup-mongodb
```

All references are `pbm/restore/physical.go` unless noted.

## Load-bearing semantics: exact matches

| Semantic | Model | Code |
|----------|-------|------|
| Heartbeat frame | `HB_FRAME = 120` | `hbFrameSec = 60 * 2` (`:64`) |
| Staleness threshold | `STALE = 240` | `t + hbFrameSec*2 < ts` in `checkHB` (`:2348`) |
| Wait-loop tick | `POLL = 5` | `time.NewTicker(time.Second * 5)` in both `waitClusterStatus` (`:375`) and `waitFiles` (`:856`) |
| Heartbeat writes | One tick writes `node.<n>.hb`, `rs.hb`, `cluster.hb` with the same timestamp | `hb()` writes all three from one `now` (`:2254-2273`) |
| Heartbeat lifetime (pristine) | Runs until CLOSE completes | `close(r.stopHB)` is deferred (`:271-273`) and runs only when `close()` returns; `waitClusterStatus()` is called before it (`:255`) |
| Heartbeat lifetime (patched) | Stops at CLOSE entry | PR #1339 moves `close(r.stopHB)` before `waitClusterStatus()` |
| CLOSE escape order | `cluster.error`, `cluster.done`, `cluster.partlyDone`, then `checkHB(cluster.hb)` | Same order (`:378-403`) |
| Done write gates | Every node writes `rs.*` and `cluster.*` on Done | `IsPrimary \|\| status == StatusDone` (`:729`), `IsClusterLeader() \|\| status == StatusDone` (`:742`) |
| Done drop-on-stale | Peer with stale `node.hb` is dropped; any drop makes the converged status `partlyDone` | `checkHB` error records `curErr` and `delete(objs, f)` (`:891-898`); `haveDone && !cluster` returns `StatusPartlyDone` (`:932-934`) |
| Replay is primary-only | Only n2 has a REPLAY phase | `if !pitr.IsZero() && r.nodeInfo.IsPrimary` (`:1288`) |
| Phase order | FLUSH, COPY, PREPDATA, REPLAY, RESETRS, DONE, CLOSE | `flush` (`:1177`), `copyFiles` (`:1254`), `prepareData` (`:1277`), `recoverStandalone` (`:1283`), `replayOplog` (`:1290`), `resetRS` (`:1297`), `toState(StatusDone)` (`:1308`), `close` via the `Snapshot` defer (`:1085-1095`) |
| Wedge-implies-no-close | A wedged phase means heartbeats never stop and no success log | `close()` runs only via the defer when `Snapshot` unwinds; the completion log requires `Snapshot` returning nil (`cmd/pbm-agent/restore.go:207-226`) |

## Documented abstractions (none affect the verdicts)

| # | Abstraction | Code reality | Why immaterial here |
|---|-------------|--------------|---------------------|
| 1 | Missing `.hb` file counts as stale | `checkHB` grants a 240s grace from restore start when the file does not exist yet (`:2323-2327`) | The simulation writes every heartbeat at t=0, so the branch never fires |
| 2 | The two intermediate Done waits (rs file, cluster file) are treated as satisfied by the node's own writes | `toState(Done)` also runs `waitFiles` on the rs file (`:744`) and the cluster file (`:756`) | On Done every node writes both files itself, so they resolve on the next tick; the cluster-file wait shares the self-defeating `cluster.hb` property already modeled in CLOSE |
| 3 | No cleanup heartbeat | `startCleanupHb()` writes `cluster.cleanup.hb` every 15s after the wait (`:260`, `hbCleanupFrame :67`) | Runs only after `waitClusterStatus` returns, which is past every modeled wedge |
| 4 | No error-file paths | `MarkFailed` writes `.error` files; `toState` suppresses them when `status == StatusDone` (`:705,:712`) | The fault space injects silent losses and stalls, not explicit errors; no scenario produces an `.error` file |
| 5 | `recoverStandalone` folded into PREPDATA; init, `prepareBackup`, Starting and Running compressed to t=0 | Separate steps (`:1097-1159`) | Faults are injected from COPY onward; pre-fault convergence is assumed healthy, matching the incident |
| 6 | mongod state is a per-phase constant | `prepareData`/`recoverStandalone` briefly start and stop an ephemeral mongod; `resetRS` ends by shutting it down | Only REPLAY's "mongod up" matters for the lock-file observable, and replay does run against a live ephemeral mongod |
| 7 | Storage reads never error | `waitClusterStatus`/`waitFiles` log and continue on read errors | Read-error loops are a second, equivalent way to keep waiting; modeling them adds no new outcome class |

## Conclusion

Every constant, ordering, and convergence rule the verdicts depend on is byte-accurate
against `v2.11.0`. The abstractions are simplifications of code paths that either cannot
fire in the modeled scenarios or reduce to behavior already covered.
