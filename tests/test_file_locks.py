import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from native_team.monitor import running


class FileLockTests(unittest.TestCase):
    def test_read_only_status_and_release_on_owner_exit(self):
        """Existing empty and nonempty lock files both exclude a second process."""
        for initial in (b"", b"old marker\n"):
            with (
                self.subTest(initial=initial),
                tempfile.TemporaryDirectory() as directory,
            ):
                state = Path(directory)
                lock = state / "monitor.lock"
                ready = state / "ready"
                lock.write_bytes(initial)
                self.assertFalse(running(state))
                owner = subprocess.Popen(
                    [sys.executable, __file__, "--hold", str(lock), str(ready)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                try:
                    deadline = time.monotonic() + 15
                    while not ready.exists():
                        if owner.poll() is not None:
                            self.fail(owner.stderr.read().decode(errors="replace"))
                        if time.monotonic() >= deadline:
                            self.fail("Child did not acquire the monitor lock")
                        time.sleep(0.03)
                    self.assertTrue(running(state))
                finally:
                    if owner.poll() is None:
                        owner.kill()
                    owner.communicate(timeout=10)
                self.assertFalse(running(state))
                self.assertEqual(lock.read_bytes(), initial)


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--hold":
    from native_team.file_locks import acquire_exclusive_nowait

    with Path(sys.argv[2]).open("ab") as handle:
        acquire_exclusive_nowait(handle)
        Path(sys.argv[3]).touch()
        sys.stdin.buffer.read(1)
