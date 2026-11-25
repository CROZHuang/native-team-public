"""Keep a live native MCP connection's alias exclusive until disconnect."""

import hashlib

from .file_locks import acquire_exclusive_nowait
from .store import TeamError


class ConnectionIdentity:
    def __init__(self, state):
        self.state, self.lock = state, None

    def acquire(self, provider, project, alias):
        if self.lock is not None:
            return
        digest = hashlib.sha256(f"{project}\0{alias}".encode()).hexdigest()
        lock = (self.state / f"{provider}-{digest}.lock").open("a")
        try:
            acquire_exclusive_nowait(lock)
        except BlockingIOError:
            lock.close()
            raise TeamError(
                f"This {provider} alias is held by another live native MCP connection; use your own alias or close the old connection"
            ) from None
        except BaseException:
            lock.close()
            raise
        self.lock = lock

    def close(self):
        if self.lock is not None:
            self.lock.close()
            self.lock = None
