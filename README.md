# PBM physical-restore hang: reproducible experiments

Lab for the "pbm-agent never finishes cleanup after physical PITR restore" incident
([forum #40156](https://forums.percona.com/t/pbm-agent-internal-cleanup-incomplete-post-restore-of-physical-backup/40156),
[PBM-1712](https://perconadev.atlassian.net/browse/PBM-1712)).
The investigation found two distinct defects in the same family. This repo reproduces both,
with a protocol model that explains why only one of them matches the incident.

All line references are pristine PBM `v2.11.0` unless noted.

## Requirements

| Tool | Needed for |
|------|-----------|
| Python >= 3.10 (or [uv](https://docs.astral.sh/uv/)) | Experiment 1 (model) and the black-hole server in experiment 2 |
| git, Go >= 1.25 | Experiment 2 (clones PBM v2.11.0 and builds the probes) |
| docker | Experiment 2's MinIO no-regression leg (auto-skipped without docker; the black-hole A/B still runs) and all of experiment 3 |
| [just](https://github.com/casey/just) (optional) | The one-word recipes below; every step also works as a plain command |

Network access to github.com (PBM clone) and Docker Hub (minio, mc, percona images).
Linux assumed; on macOS install coreutils for `timeout`. A remote docker engine works:
`run.sh` derives the MinIO host from `DOCKER_HOST` (override with `MINIO_HOST`).

## Quickstart

```
just model         # experiment 1: protocol model + fault injection (~30s)
just stall         # experiment 2: storage-deadline A/B, fail-closed (~10 min)
just stall-quick   # experiment 2 with a 120s hang cutoff (~5 min)
just close-up      # experiment 3: build image + start the 3-node RS + MinIO
just build         # clone v2.11.0, apply both patches, build the patched pbm-agent
```

Three experiments total: 1 and 2 are one-command and self-verifying, 3 is a guided
manual recipe (it needs a live restore lifecycle and a hand-timed kill).

## The incident

3-node replica set, backup taken with PBM 2.7.0, PITR restore with 2.11.0, S3 storage.
Two secondaries logged `recovery successfully finished` and exited ~93s after the third
node's mongod came up. The third node, the PRIMARY, never logged completion and uploaded
`.pbm.restore/<name>/{node.<host>.hb, rs.hb, cluster.hb}` every 120s for 8+ hours until
manually stopped. Data was intact on all three nodes.

Decisive artifacts from the thread:

- The stuck node is `[P]` in `pbm status`. Only the primary runs the PITR oplog replay (`physical.go:1288`).
- Its `mongod.lock` is 8 bytes (held), while the healthy nodes' locks are 0 bytes: the ephemeral replay mongod never shut down, so the agent wedged inside the restore body, before `close()`.
- Its restore log goes silent right after `Transition to primary complete`: a stalled storage read, not a mongod operation.
- The 120s upload cadence is the restore `hb()` goroutine (`physical.go:2231`), which only stops when `close()` runs: direct proof `close()` never ran.

## Two distinct bugs

| # | Bug | Where | Incident role | Status |
|---|-----|-------|---------------|--------|
| A | Self-defeating staleness escape: a node waiting in `waitClusterStatus()` refreshes `cluster.hb` itself every 120s (`physical.go:2267`), so the 240s stuck-check (`physical.go:2348`) can never fire; a missing peer terminal file means an infinite wait | `close()` -> `waitClusterStatus()` (`physical.go:255,369-407`) | Sibling failure mode; NOT what wedged the incident node | Fixed in 2.16.0 via [PR #1339](https://github.com/percona/percona-backup-mongodb/pull/1339) |
| B | No deadline on storage calls: `FileStat` is `HeadObject(context.Background(), ...)` on an `http.Client{}` with zero `Timeout` (`s3.go:538,611`); a silently stalled connection (no RST) blocks the call forever. The replay chunk reader begins every object with `FileStat` (`download.go:64,~100`), while chunk GETs are 60s-bounded and retry-limited (`download.go:184,304-362`) | `pbm/storage/s3` | THE incident trigger, during the primary's oplog replay | Not fixed as of `dev` |

Incident mechanics: a transient storage stall (roughly 4 to 10 minutes) froze the primary's
heartbeat writes past 240s, so the secondaries dropped it under the Done-phase staleness rule
(`physical.go:897-898`), converged to `cluster.partlyDone` (`physical.go:932-934`), and exited
cleanly. The same stall left an in-flight HeadObject blocked forever. Heartbeats resumed on
fresh connections after the stall; the wedged call never returned, so `Snapshot()` never
returned and `close()` never ran.

Note the secondaries reported full success while the cluster ended `partlyDone`, and a failed
final `toState(StatusDone)` writes neither `cluster.done` nor `cluster.error`
(`physical.go:705,712`): no error breadcrumb exists anywhere.

## Experiments

### 1. Protocol model (30 seconds, no dependencies)

```
python3 model/pbm_state_machine.py
```

Discrete-event model of the file-coordination protocol (heartbeats, staleness, Done-phase
convergence, `close()`), with exhaustive single-fault injection: hang, kill, clock skew, lost
status file, bounded and permanent storage outage, each across nodes and phases, each in
pristine and bug-A-patched mode. Expected result: exactly one fault reproduces all six
incident observables, the bounded storage outage on the primary during replay, and it
reproduces identically with the bug-A fix applied. A pure hang fails (secondaries can never
drop a fresh-heartbeat peer), a kill fails (heartbeats were alive 8 hours later), a permanent
outage fails (heartbeats resumed).

Full model spec (protocol semantics, fault space, the six-observable verdict, fidelity
limits): [model/README.md](model/README.md).

### 2. Storage-deadline A/B (about 10 minutes)

```
cd stall-test && ./run.sh
```

Clones PBM `v2.11.0`, builds a `FileStat` probe from the pristine tree, applies
`patches/`, builds the fixed probe, then asserts fail-closed:

| Check | Pristine | Fixed |
|-------|----------|-------|
| `FileStat` vs black hole (accepts TCP, never responds) | Still blocked at the cutoff (default 300s; the incident ran 8+ hours) | Errors at ~60s with `context deadline exceeded` |
| `FileStat` vs real MinIO (needs docker; skipped otherwise) | ~15ms, `err=<nil>` | ~7ms, `err=<nil>` |

Exit code 0 only if every check passes. `CUTOFF=120 ./run.sh` for a faster demo.

### 3. Close-phase kill test (bug A, manual, about 30 minutes)

Validates the PR #1339 mechanism. Recipe, validated 2026-03 on PBM 2.11.0:

1. 3-node RS + MinIO (`close-phase-test/compose.yml` + `Dockerfile.psmdb-pbm` are a starting point; see the lifecycle caveat in the compose header). PITR enabled, physical backup, insert data, PITR restore.
2. Optional but recommended: build agents with `hbFrameSec` lowered from `60*2` to `15` (`physical.go:64`), so staleness fires at ~30s instead of 240s.
3. Watch agent logs. When all three log `wait for cluster status`, kill ONE node's `pbm-agent` process (plain kill; the timing is what matters: after entering the wait, before any `cluster.done` exists).
4. Without the fix, the two survivors upload `.hb` files forever. With the fix, they detect `cluster.hb` staleness in about 2x the frame (measured 61s at the 15s frame) and exit with an error.

Killing the agent validates bug A only. It does not reproduce the incident (see experiment 1):
for that, stall storage I/O on the primary during oplog replay and keep the connection open.

## Patches

- `0001-stop-restore-hb-before-cluster-status-wait.patch`: bug A fix for `v2.11.0`. Moves `close(r.stopHB)` before `waitClusterStatus()` and re-creates the channel for the cleanup heartbeat (the re-create is needed on 2.11 only; since 2.13 `stopCleanupHB` is a separate channel, which is why PR #1339 is a plain move). Also lowers `hbFrameSec` to 15 for fast testing; drop that hunk for production builds.
- `0002-add-deadlines-to-s3-storage-calls.patch`: bug B fix candidate. 60s context deadline on `FileStat`'s HeadObject, and a real `http.Transport` (10s dial, 30s TCP keepalive, 60s `ResponseHeaderTimeout`, stock pool settings). Deliberately no overall `http.Client.Timeout`: backup and restore streams may legitimately run for hours.

## Layout

```
justfile              One-word recipes (model, stall, stall-quick, build, close-up)
model/                pbm_state_machine.py + README with the full model spec (experiment 1)
stall-test/           run.sh, blackhole.py, probe/, patches/  (experiment 2)
close-phase-test/     compose.yml, Dockerfile.psmdb-pbm       (experiment 3)
```

## License

Apache-2.0, matching PBM, whose code the patches modify.
