"""Nonblocking ownership of a dedicated lock file, released when its handle closes."""

import errno
import os

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def acquire_exclusive_nowait(stream):
    if os.name != "nt":
        # Keep cooperating with already running clients that use flock directly.
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return

    # Every client locks byte zero. Windows permits locking past EOF, so even
    # existing empty lock files work without modifying their contents.
    stream.seek(0)
    try:
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        if exc.errno != errno.EACCES:
            raise
        raise BlockingIOError(errno.EWOULDBLOCK, "Lock already held") from exc
