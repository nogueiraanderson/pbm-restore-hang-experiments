#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""Executable state-machine model of PBM v2.11.0 physical-restore coordination.

Encodes the file-coordination protocol extracted from pbm/restore/physical.go
(pristine v2.11.0) and exhaustively injects single faults to find which one
reproduces the forum #40156 incident trace:

  - nodes 01+03 (secondaries): log success, exit ~93s after node 02's mongod
    came up, terminal cluster file = partlyDone
  - node 02 (PRIMARY): never logs success, never exits, hb uploads (120s
    cadence) still active 8+ hours later, ephemeral replay mongod still UP
    (mongod.lock held), data phase complete

Protocol semantics modeled (file:line refs are pristine v2.11.0):
  - hb() writes node/rs/cluster .hb every 120s until stopHB closed (2231-2270)
  - toState(Done): every node writes node.done (724), waits peers' node.done
    with drop-on-stale-240s -> partlyDone (848-941), writes rs.<cstat> (736)
    and cluster.<cstat> (749)
  - close(): waitClusterStatus polls 5s for cluster.{error,done,partlyDone}
    else checkHB(cluster.hb) stale>240s (369-407); PRISTINE: own hb still
    running; PATCHED (PBM-1712 / PR #1339): own hb stopped at close() entry
  - REPLAY (PITR oplog apply) runs on the PRIMARY only (1288); chunk GET has
    no read deadline -> an in-flight read stalled by a storage outage hangs
    forever even after the outage clears (new PUT connections recover)
"""

import itertools

HB_FRAME = 120           # hbFrameSec (physical.go:64)
STALE = 240              # checkHB threshold = 2*HB_FRAME (physical.go:2348)
POLL = 5                 # wait-loop tick (physical.go:375, 856)
HORIZON = 10 * 3600      # 10h sim, incident ran 8+ hours

# phase -> ephemeral/target mongod state when wedged there (for lock evidence)
PHASES = ["FLUSH", "COPY", "PREPDATA", "REPLAY", "RESETRS", "DONE", "CLOSE"]
MONGOD_UP_AT = {"FLUSH": False, "COPY": False, "PREPDATA": False,
                "REPLAY": True, "RESETRS": True, "DONE": False, "CLOSE": False}
# nominal end times (s) per phase per node; primary replays PITR, secs skip it
def timeline(is_primary):
    t = {"FLUSH": 120, "COPY": 400, "PREPDATA": 600}
    t["REPLAY"] = 660 if is_primary else 600   # secs: zero-length
    t["RESETRS"] = 720 if is_primary else 660
    return t

class Node:
    def __init__(self, name, primary):
        self.name, self.primary = name, primary
        self.tl = timeline(primary)
        self.phase_i = 0
        self.done_written = False
        self.cstat = None
        self.exited = False
        self.success = False
        self.error_exit = False
        self.wedged = False
        self.hb_alive = True          # goroutine running
        self.hb_stopped = False       # stopHB closed
        self.dropped_peers = set()
        self.resolved = {}            # peer -> 'done'|'dropped'

    def phase(self):
        return PHASES[self.phase_i] if self.phase_i < len(PHASES) else "EXITED"

class Sim:
    def __init__(self, fault, patched):
        self.fault = fault            # dict: kind/target/phase/t0/dur/path/delta
        self.patched = patched
        self.nodes = {"n1": Node("n1", False),
                      "n2": Node("n2", True),
                      "n3": Node("n3", False)}
        self.files = {}               # path -> write time (content ts for .hb)
        self.wedged_get = set()       # nodes with a forever-stalled in-flight GET

    def outage_active(self, node, t):
        f = self.fault
        return (f["kind"] == "OUTAGE" and f["target"] == node.name
                and f["t0"] <= t < f["t0"] + f["dur"])

    def hb_write(self, node, t):
        if not node.hb_alive or node.hb_stopped or node.exited:
            return
        if self.fault["kind"] == "KILL" and f_matches(self.fault, node, t):
            return
        if self.outage_active(node, t):
            return                    # PUTs fail during outage, resume after
        ts = t + self.fault.get("delta", 0) if (
            self.fault["kind"] == "SKEW" and self.fault["target"] == node.name) else t
        for p in (f"node.{node.name}.hb", "rs.hb", "cluster.hb"):
            self.files[p] = ts

    def stale(self, path, t):
        return path not in self.files or self.files[path] + STALE < t

    def step_node(self, node, t):
        if node.exited or node.wedged:
            return
        ph = node.phase()
        f = self.fault

        # fault entry checks
        if f["kind"] == "HANG" and f["target"] == node.name and ph == f["phase"]:
            node.wedged = True
            return
        if f["kind"] == "KILL" and f["target"] == node.name and ph == f["phase"]:
            node.hb_alive = False
            node.exited = True
            return
        if (f["kind"] == "OUTAGE" and f["target"] == node.name
                and self.outage_active(node, t)
                and ph in ("COPY", "REPLAY")):
            # an in-flight storage GET during the outage never returns
            self.wedged_get.add(node.name)
            node.wedged = True
            return

        if ph in ("FLUSH", "COPY", "PREPDATA", "REPLAY", "RESETRS"):
            if t >= node.tl.get(ph, 0):
                node.phase_i += 1
            return

        if ph == "DONE":
            if not node.done_written:
                # toState(Done): write node.done (physical.go:724);
                # a LOSE fault silently swallows this node's write
                if not (f["kind"] == "LOSE"
                        and f["path"] == f"node.{node.name}.done"):
                    self.files[f"node.{node.name}.done"] = t
                node.done_written = True
            # wait peers (physical.go:848-941, Done mode: drop on stale)
            for peer in self.nodes.values():
                if peer.name == node.name or peer.name in node.resolved:
                    continue
                if f"node.{peer.name}.done" in self.files:
                    node.resolved[peer.name] = "done"
                elif self.stale(f"node.{peer.name}.hb", t):
                    node.resolved[peer.name] = "dropped"   # physical.go:897
            if len(node.resolved) == len(self.nodes) - 1:
                dropped = [p for p, v in node.resolved.items() if v == "dropped"]
                node.cstat = "partlyDone" if dropped else "done"
                if not (f["kind"] == "LOSE" and f["path"] == f"cluster.{node.cstat}"
                        and f.get("target") == node.name):
                    self.files[f"rs.{node.cstat}"] = t      # physical.go:736
                    self.files[f"cluster.{node.cstat}"] = t # physical.go:749
                node.phase_i += 1
                if self.patched:                            # PBM-1712 fix
                    node.hb_stopped = True                  # close(stopHB) first
            return

        if ph == "CLOSE":
            # waitClusterStatus (physical.go:369-407)
            for st in ("error", "done", "partlyDone"):
                if f"cluster.{st}" in self.files:
                    node.exited = True
                    node.success = st != "error"
                    node.hb_stopped = True                  # close() defer
                    return
            if self.stale("cluster.hb", t):                 # physical.go:400-403
                node.exited = True
                node.error_exit = True
                node.hb_stopped = True
            return

    def run(self):
        for t in range(0, HORIZON, POLL):
            for n in self.nodes.values():
                if t % HB_FRAME == 0:
                    self.hb_write(n, t)
                self.step_node(n, t)
        return self.verdict()

    def verdict(self):
        n1, n2, n3 = self.nodes["n1"], self.nodes["n2"], self.nodes["n3"]
        secs_ok = all(n.exited and n.success for n in (n1, n3))
        partly = "cluster.partlyDone" in self.files
        no_cluster_done = "cluster.done" not in self.files
        n2_stuck = (not n2.exited) and (not n2.success)
        # hb still active at horizon: goroutine alive, not stopped, and the
        # fault is not permanently blocking writes
        n2_hb_live = (n2.hb_alive and not n2.hb_stopped
                      and not (self.fault["kind"] == "OUTAGE"
                               and self.fault["target"] == "n2"
                               and self.fault["dur"] >= HORIZON))
        n2_mongod_up = n2.wedged and MONGOD_UP_AT.get(n2.phase(), False)
        checks = {
            "secs_exit_success": secs_ok,
            "cluster_partlyDone": partly,
            "no_cluster_done": no_cluster_done,
            "n2_never_exits": n2_stuck,
            "n2_hb_live_8h": n2_hb_live,
            "n2_mongod_up(lock held)": n2_mongod_up,
        }
        return all(checks.values()), checks, n2.phase()

def f_matches(f, node, t):
    return f.get("target") == node.name

def label(f):
    k = f["kind"]
    if k == "OUTAGE":
        return f"OUTAGE({f['target']},t0={f['t0']},dur={f['dur']})"
    if k in ("HANG", "KILL"):
        return f"{k}({f['target']},{f['phase']})"
    if k == "LOSE":
        return f"LOSE({f['path']})"
    if k == "SKEW":
        return f"SKEW({f['target']},{f['delta']})"
    return "NO_FAULT"

faults = [{"kind": "NONE"}]
for n in ("n1", "n2", "n3"):
    for ph in ("COPY", "REPLAY", "RESETRS", "DONE", "CLOSE"):
        faults.append({"kind": "HANG", "target": n, "phase": ph})
        faults.append({"kind": "KILL", "target": n, "phase": ph})
    faults.append({"kind": "SKEW", "target": n, "delta": -400})
    # outage overlapping REPLAY start (t0 chosen so hb goes stale by ~720)
    faults.append({"kind": "OUTAGE", "target": n, "t0": 610, "dur": 300})
    faults.append({"kind": "OUTAGE", "target": n, "t0": 610, "dur": HORIZON})
faults.append({"kind": "LOSE", "path": "node.n2.done"})
faults.append({"kind": "LOSE", "path": "node.n1.done"})

print(f"{'FAULT':44} {'MODE':9} {'REPRO':6} {'N2-PHASE':9} FAILED-CHECKS")
for f, patched in itertools.product(faults, (False, True)):
    ok, checks, n2ph = Sim(f, patched).run()
    failed = ",".join(k for k, v in checks.items() if not v) or "-"
    mode = "patched" if patched else "pristine"
    mark = "YES" if ok else "no"
    if ok or f["kind"] in ("NONE",) or "n2" in str(f.get("target", "")) or "n2" in str(f.get("path", "")):
        print(f"{label(f):44} {mode:9} {mark:6} {n2ph:9} {failed}")
