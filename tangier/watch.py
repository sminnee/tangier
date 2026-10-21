"""Watchers: a command that follows the gate jobs, woken by each write to one. See `[status-watch]`.

A watcher binds a Unix datagram socket in `~/.tangier/watch/` (see `home`). `jobs` calls `notify`
after each write, which sends every socket there an empty datagram. Sockets are in the stdlib and
work on macOS and Linux alike.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import select
import socket

from tangier import home

# How often a watcher re-reads the jobs without a wakeup, to catch a job process that died and wrote nothing.
POLL = 2


def _dir() -> str:
    return os.path.join(home.root(), "watch")


def notify() -> None:
    """Wake every `Watcher` on the machine: send each socket in `~/.tangier/watch/` an empty datagram.

    It never raises, so a notify cannot fail a gate run. A socket whose watcher died is removed.
    """
    try:
        names = [n for n in os.listdir(_dir()) if n.endswith(".sock")]
    except OSError:
        return
    if not names:
        return
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    except OSError:
        return
    with sock:
        sock.setblocking(False)
        for name in names:
            path = os.path.join(_dir(), name)
            try:
                _ = sock.sendto(b"", path)
            except (ConnectionRefusedError, FileNotFoundError):
                with contextlib.suppress(OSError):
                    os.remove(path)
            except OSError:
                # A full buffer means a wakeup is already pending. A sandbox may forbid the send.
                pass


class Watcher:
    """A socket in `~/.tangier/watch/` that `notify` wakes on each change to a job."""

    sock: socket.socket | None = None

    def __enter__(self) -> Watcher:
        home.makedirs(_dir())
        # Unique, so a `notify` that removes a dead watcher's socket cannot remove a new one with a reused pid.
        self.path = os.path.join(_dir(), f"{os.getpid()}-{secrets.token_hex(4)}.sock")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.bind(self.path)
        except OSError:
            # A sandbox, or a `TANGIER_HOME` path too long for a socket, forbids the bind: the watcher only polls.
            sock.close()
            return self
        sock.setblocking(False)
        self.sock = sock
        return self

    def __exit__(self, *exc: object) -> None:
        if self.sock is None:
            return
        self.sock.close()
        with contextlib.suppress(FileNotFoundError):
            os.remove(self.path)

    def wait(self, timeout: float) -> None:
        """Return on a wakeup or after `timeout` seconds. A burst of wakeups reads as one."""
        _ = select.select([] if self.sock is None else [self.sock], [], [], timeout)
        if self.sock is None:
            return
        with contextlib.suppress(BlockingIOError):
            while True:
                _ = self.sock.recv(1)
