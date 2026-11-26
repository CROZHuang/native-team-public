"""Monitor recovery and heartbeat tests that run unchanged on Linux, macOS and Windows.

Decision 20155d0d. Helpers run as separate `sys.executable` processes; nothing here
relies on /proc, POSIX signals, shebang scripts or a single-process lock model.
"""

from pathlib import Path
import sys
import tempfile
import unittest
import uuid

from native_team.peers import PeerStore
from native_team.schema import migrate
from native_team.store import Store


class WorkerRecoveryTests(unittest.TestCase):
    """Decision 20155d0d A1-A3: isolated restarts, honest heartbeat, unit backoff."""

    class FakeWorker:
        def __init__(self, actor):
            import threading
            from types import SimpleNamespace

            self.actor, self.alive, self.failure = actor, True, None
            self.started = self.closed = False
            self.stopping = threading.Event()
            self.thread = SimpleNamespace(is_alive=lambda: self.alive)

        def start(self):
            self.started = True

        def close(self, timeout=30):
            self.closed = True

    def pool(self):
        from native_team.monitor import WorkerPool

        made = []

        def factory(actor):
            worker = self.FakeWorker(actor)
            made.append(worker)
            return worker

        return WorkerPool(factory), made

    def test_one_failing_recipient_backs_off_while_another_keeps_running(self):
        pool, made = self.pool()
        pool.reconcile({"a", "b"}, 0)
        healthy = pool.workers["b"][0]
        due, now, delays = 0, 0, []
        for _ in range(8):
            worker = pool.workers["a"][0]
            worker.alive, worker.failure = False, "OperationalError"
            now = due + 1
            self.assertTrue(pool.reconcile({"a", "b"}, now))
            entry = pool.backoff(now)[0]
            self.assertEqual((entry["member"], entry["error"]), ("a", "OperationalError"))
            delays.append(entry["due_in"])
            due = now + entry["due_in"]
            pool.reconcile({"a", "b"}, due - 0.1)
            self.assertNotIn("a", pool.workers)
            pool.reconcile({"a", "b"}, due)
            self.assertIn("a", pool.workers)
        self.assertEqual(delays, [5, 10, 20, 40, 80, 160, 300, 300])
        self.assertIs(pool.workers["b"][0], healthy)
        self.assertFalse(healthy.closed)
        self.assertEqual(sum(w.actor == "b" for w in made), 1)

    def test_pause_removal_and_shutdown_cancel_pending_restart(self):
        import time

        pool, made = self.pool()
        pool.reconcile({"a"}, 0)
        pool.workers["a"][0].alive = False
        pool.reconcile({"a"}, 1)
        self.assertTrue(pool.pending)
        pool.reconcile(set(), 2)
        self.assertEqual((pool.pending, pool.restarts, pool.workers), ({}, {}, {}))
        pool.reconcile({"a"}, 3)
        self.assertEqual(len(made), 2)
        self.assertEqual(pool.restarts, {})
        pool.workers["a"][0].alive = False
        pool.reconcile({"a"}, 4)
        pool.reconcile({"b"}, 5)
        live = pool.workers["b"][0]
        pool.close(time.monotonic() + 1)
        self.assertEqual(pool.pending, {})
        self.assertTrue(live.stopping.is_set() and live.closed)

    def test_backoff_resets_after_a_healthy_run(self):
        pool, _ = self.pool()
        pool.reconcile({"a"}, 0)
        pool.workers["a"][0].alive = False
        pool.reconcile({"a"}, 1)
        pool.reconcile({"a"}, 6)
        pool.workers["a"][0].alive = False
        pool.reconcile({"a"}, 6 + 600)
        self.assertEqual(pool.backoff(606)[0]["restarts"], 1)
        self.assertEqual(pool.backoff(606)[0]["due_in"], 5)

    def test_restarted_dispatcher_keeps_unpersisted_send_uncertain_until_receipt(self):
        import sqlite3
        from unittest import mock
        from native_team import notifications
        from native_team.outbox import Outbox

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root / "state")
            codex = store.join("codex", "worker", root, str(uuid.uuid4()))["id"]
            agy = store.join("agy", "initiator", root)["id"]
            migrate(store)
            peers = PeerStore(store)
            team = peers.create(agy, "Peers", "Restart", ["initiator", "worker"], "t1")["id"]

            def receive():
                res = peers.sync(codex, team)
                peers.acknowledge(codex, team, res["team"]["epoch"], res["cursor"])

            receive()
            sends = []

            def run(result_fails):
                outbox = Outbox(store)
                original = outbox.notice_result

                def result(notice_id, state, detail):
                    if result_fails:
                        raise sqlite3.OperationalError("database is locked")
                    return original(notice_id, state, detail)

                outbox.notice_result = result
                worker = notifications.Dispatcher(outbox, codex, root / "fake-codex")
                waits = {"n": 0}

                def wait(_timeout):
                    waits["n"] += 1
                    if waits["n"] >= (100 if result_fails else 5):
                        worker.stopping.set()

                def send(_executable, notice):
                    sends.append(notice["id"])
                    return "queued", "Native queue accepted"

                with (
                    mock.patch.object(notifications, "send", side_effect=send),
                    mock.patch.object(worker.stopping, "wait", side_effect=wait),
                    mock.patch.object(notifications.logging, "exception"),
                    mock.patch.object(notifications.logging, "warning"),
                ):
                    worker.run()
                return worker

            def batches():
                with store.readonly() as db:
                    return [r[0] for r in db.execute("SELECT state FROM wake_batches ORDER BY created")]

            peers.message(agy, team, "direct", "hello", "m1", ["worker"])
            failed = run(result_fails=True)
            self.assertEqual(failed.failure, "OperationalError")
            self.assertEqual((len(sends), batches()), (1, ["sending"]))
            run(result_fails=False)  # what WorkerPool does after the backoff
            self.assertEqual((len(sends), batches()), (1, ["uncertain"]))
            peers.message(agy, team, "direct", "while unknown", "m2", ["worker"])
            run(result_fails=False)
            self.assertEqual(len(sends), 1)
            receive()
            peers.message(agy, team, "direct", "after receipt", "m3", ["worker"])
            run(result_fails=False)
            self.assertEqual((len(sends), batches()), (2, ["uncertain", "queued"]))

    HOLDER = r"""
import sys, time
from pathlib import Path
from native_team import monitor
from native_team.file_locks import acquire_exclusive_nowait
from native_team.quota_capture import atomic_json

state, mode, age, backoff, control = Path(sys.argv[1]), sys.argv[2], float(sys.argv[3]), int(sys.argv[4]), Path(sys.argv[5])
main = (state / "monitor.lock").open("a")
acquire_exclusive_nowait(main)
instance = None
if mode != "legacy":
    instance = monitor.Instance(state)
    now = time.time()
    entry = {"member": "m", "restarts": 1, "due_in": 5, "error": "OperationalError"}
    atomic_json(state / monitor.HEARTBEAT, {"version": 1, "instance": instance.id, "started_at": now - age,
        "updated": now - age, "workers": 1, "backoff": [entry] * backoff, "scan_retries": 0})
if mode.startswith("released"):
    if mode == "released":
        instance.close()
    else:
        instance.stream.close()  # released, but the file stays (as when Windows refuses the unlink)
    main.close()
    instance = None
control.with_name(control.name + ".ready").write_text("ready")
while not control.exists():
    time.sleep(0.05)
if instance:
    instance.close()
main.close()
"""

    def test_heartbeat_speaks_only_for_the_live_lock_lifetime_across_processes(self):
        import json as _json
        import os as _os
        import subprocess
        import time as _time
        import native_team
        from native_team import monitor

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = root / "state"
            state.mkdir()
            script = root / "holder.py"
            script.write_text(self.HOLDER)
            env = {**_os.environ, "PYTHONPATH": str(Path(native_team.__file__).resolve().parents[1])}
            procs = []

            def start(name, mode, age=0, backoff=0):
                control = root / name
                proc = subprocess.Popen(
                    [sys.executable, str(script), str(state), mode, str(age), str(backoff), str(control)], env=env
                )
                procs.append((proc, control))
                ready = control.with_name(control.name + ".ready")
                deadline = _time.monotonic() + 30
                while not ready.exists():
                    if proc.poll() is not None:
                        self.fail(f"Holder {name} exited with {proc.returncode}")
                    if _time.monotonic() > deadline:
                        self.fail(f"Holder {name} did not start")
                    _time.sleep(0.05)
                return proc, control

            def stop(proc, control):
                control.write_text("stop")
                self.assertEqual(proc.wait(30), 0)

            def status():
                return monitor.summary(state)["status"]

            try:
                proc, control = start("fresh-backoff", "current", 0, 1)
                self.assertEqual(status(), "degraded")
                stop(proc, control)
                self.assertEqual(status(), "stopped")
                proc, control = start("fresh", "current")
                self.assertEqual(status(), "running")
                stop(proc, control)
                proc, control = start("stale", "current", 121)
                result = monitor.summary(state)
                self.assertEqual(result["status"], "unresponsive")
                self.assertGreaterEqual(result["heartbeat_age"], 121)
                beat = state / monitor.HEARTBEAT
                valid = _json.loads(beat.read_text())
                for broken in (
                    {"version": True},
                    {"version": 2},
                    {"updated": True},
                    {"scan_retries": -1},
                    {"backoff": [{"member": "m"}]},
                    {"backoff": [{"member": "m", "restarts": 0, "due_in": 5, "error": "E"}]},
                    {"backoff": "x"},
                    {"instance": "XYZ"},
                    {"instance": None},
                ):
                    beat.write_text(_json.dumps({**valid, **broken}))
                    self.assertEqual(status(), "running", broken)  # malformed: lock only
                beat.write_text("{not json")
                self.assertEqual(status(), "running")
                beat.unlink()
                beat.mkdir()
                self.assertEqual(status(), "running")
                beat.rmdir()
                stop(proc, control)
                for mode in ("released", "released-leftover"):
                    former, former_control = start(f"former-{mode}", mode, 121, 1)
                    self.assertEqual(status(), "stopped")
                    legacy, legacy_control = start(f"legacy-{mode}", "legacy")
                    # The former publisher is alive and its beat is valid, but it no
                    # longer holds the lock lifetime: the legacy holder gets lock-only.
                    self.assertEqual(status(), "running", mode)
                    stop(legacy, legacy_control)
                    stop(former, former_control)
                self.assertEqual(status(), "stopped")
                leftovers = sorted(p.name for p in state.glob(f"{monitor.INSTANCE_PREFIX}*.lock"))
                self.assertEqual(len(leftovers), 1)  # only the simulated unlink failure
                companion = monitor.Instance(state)
                monitor.sweep_instances(state, companion.path)
                self.assertEqual([p.name for p in state.glob(f"{monitor.INSTANCE_PREFIX}*.lock")], [companion.path.name])
                companion.close()
            finally:
                for proc, control in procs:
                    if proc.poll() is None:
                        control.write_text("stop")
                        try:
                            proc.wait(10)
                        except subprocess.TimeoutExpired:
                            proc.kill()

    READER = r"""
import sys, time
from pathlib import Path
from native_team import monitor
from native_team.file_locks import acquire_exclusive_nowait

state, companion, control = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
# Stop exactly where codex-system suspended its first observer: after the real
# exclusive acquire of the companion probe, before either handle closes.
slot = open(state / monitor.PROBE_LOCK, "r+b" if sys.platform == "win32" else "r")
acquire_exclusive_nowait(slot)
probe = open(companion, "r+b" if sys.platform == "win32" else "r")
acquire_exclusive_nowait(probe)
control.with_name(control.name + ".ready").write_text("ready")
while not control.exists():
    time.sleep(0.05)
probe.close()
slot.close()
"""

    def test_overlapping_status_readers_are_never_mistaken_for_the_writer(self):
        import json as _json
        import os as _os
        import subprocess
        import time as _time
        import native_team
        from native_team import monitor

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = root / "state"
            state.mkdir()
            (root / "holder.py").write_text(self.HOLDER)
            (root / "reader.py").write_text(self.READER)
            env = {**_os.environ, "PYTHONPATH": str(Path(native_team.__file__).resolve().parents[1])}
            procs = []

            def start(name, script, *args):
                control = root / name
                proc = subprocess.Popen([sys.executable, str(root / script), *map(str, args), str(control)], env=env)
                procs.append((proc, control))
                ready = control.with_name(control.name + ".ready")
                deadline = _time.monotonic() + 30
                while not ready.exists():
                    if proc.poll() is not None:
                        self.fail(f"{name} exited with {proc.returncode}")
                    if _time.monotonic() > deadline:
                        self.fail(f"{name} did not start")
                    _time.sleep(0.05)
                return proc, control

            def stop(proc, control):
                control.write_text("stop")
                self.assertEqual(proc.wait(30), 0)

            def status():
                return monitor.summary(state)["status"]

            try:
                # codex-system's R4 setup: unheld leftover companion, valid 121 s beat, legacy holder.
                former = start("former", "holder.py", state, "released-leftover", 121, 0)
                companion = state / f"{monitor.INSTANCE_PREFIX}{_json.loads((state / monitor.HEARTBEAT).read_text())['instance']}.lock"
                self.assertTrue(companion.exists())
                legacy = start("legacy", "holder.py", state, "legacy", 0, 0)
                self.assertEqual(status(), "running")  # solo read
                first = start("first-reader", "reader.py", state, companion)
                self.assertEqual(status(), "running")  # second reader while the first is suspended
                stop(*first)
                self.assertEqual(status(), "running")  # solo again
                stop(*legacy)
                stop(*former)
                # Positive control: a genuine current writer is still distinguishable.
                writer = start("writer", "holder.py", state, "current", 121, 0)
                live = state / f"{monitor.INSTANCE_PREFIX}{_json.loads((state / monitor.HEARTBEAT).read_text())['instance']}.lock"
                self.assertEqual(status(), "unresponsive")
                # A reader that holds the probe slot makes others fall back rather than guess.
                slot = (state / monitor.PROBE_LOCK).open("r+b" if _os.name == "nt" else "r")
                from native_team.file_locks import acquire_exclusive_nowait
                acquire_exclusive_nowait(slot)
                try:
                    self.assertIsNone(monitor.instance_held(live))
                    self.assertEqual(status(), "running")
                finally:
                    slot.close()
                self.assertEqual(status(), "unresponsive")
                stop(*writer)
                self.assertEqual(status(), "stopped")
            finally:
                for proc, control in procs:
                    if proc.poll() is None:
                        control.write_text("stop")
                        try:
                            proc.wait(10)
                        except subprocess.TimeoutExpired:
                            proc.kill()

    def test_removing_a_recovered_worker_clears_its_restart_history(self):
        for remove_at, pending in ((7, False), (3, True)):
            pool, _ = self.pool()
            pool.reconcile({"a"}, 0)
            pool.workers["a"][0].alive = False
            pool.reconcile({"a"}, 1)  # crash: restart 1 due at 6
            if not pending:
                pool.reconcile({"a"}, 6)  # recovered and running again
            pool.reconcile(set(), remove_at)  # paused or removed (live or while pending)
            self.assertEqual((pool.restarts, pool.pending), ({}, {}))
            pool.reconcile({"a"}, remove_at + 1)  # re-added
            pool.workers["a"][0].alive = False
            pool.reconcile({"a"}, remove_at + 2)
            entry = pool.backoff(remove_at + 2)[0]
            self.assertEqual((entry["restarts"], entry["due_in"]), (1, 5), (remove_at, pending))

    def test_heartbeat_write_failure_does_not_stop_delivery(self):
        import json as _json
        from native_team.monitor import Heartbeat, WorkerPool

        with tempfile.TemporaryDirectory() as folder:
            pool = WorkerPool(lambda actor: None)
            broken = Heartbeat(Path(folder) / "missing-directory", "0" * 32)
            self.assertFalse(broken.beat(pool, force=True))
            self.assertTrue(broken.warned)
            pulse = Heartbeat(Path(folder), "0" * 32)
            self.assertTrue(pulse.beat(pool, force=True))
            self.assertFalse(pulse.beat(pool))
            value = _json.loads((Path(folder) / "monitor-heartbeat.json").read_text())
            self.assertEqual((value["version"], value["backoff"], value["scan_retries"]), (1, [], 0))
            self.assertEqual(value["instance"], "0" * 32)
            self.assertNotIn("native_id", _json.dumps(value))

    def test_recipient_scan_retries_locks_without_exiting(self):
        import sqlite3
        from unittest import mock
        from native_team.monitor import scan_recipients

        class Flaky:
            calls = 0

            def recipients(self):
                self.calls += 1
                if self.calls <= 12:
                    raise sqlite3.OperationalError("database is locked")
                return {"x"}

        class Broken:
            def recipients(self):
                raise sqlite3.DatabaseError("database disk image is malformed")

        stopping = mock.Mock()
        outbox, failures = Flaky(), 0
        with mock.patch("native_team.monitor.logging"):
            for _ in range(12):
                active, failures = scan_recipients(outbox, stopping, failures)
                self.assertIsNone(active)
            self.assertEqual(failures, 12)
            self.assertEqual(scan_recipients(outbox, stopping, failures), ({"x"}, 0))
            with self.assertRaises(sqlite3.DatabaseError):
                scan_recipients(Broken(), stopping, 0)
        self.assertLessEqual(max(c.args[0] for c in stopping.wait.call_args_list), 10)


if __name__ == "__main__":
    unittest.main()
