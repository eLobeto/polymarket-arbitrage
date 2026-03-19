"""
Double-fork daemonization for kalshi-pm-arb.

After daemonize() returns, the process:
  - Is a grandchild of PID 1 (init) — immune to session cleanup & cgroup purges
  - Has its own session (setsid)
  - Stdout/stderr redirected to logfile
  - PID written to pidfile

Pattern mirrors polymarket-lag/src/daemon.py.
"""

import os
import sys
import logging
from pathlib import Path

PIDFILE = Path(__file__).parent.parent / "logs" / "scanner.pid"
LOGFILE = Path(__file__).parent.parent / "logs" / "scanner.log"

logger = logging.getLogger(__name__)


def daemonize():
    """
    Double-fork to fully detach from the calling process tree.
    Call this before starting the asyncio event loop.
    """
    PIDFILE.parent.mkdir(exist_ok=True)

    # ── Fork #1 ───────────────────────────────────────────────────────────────
    pid = os.fork()
    if pid > 0:
        # First parent exits cleanly — shell prompt returns immediately
        sys.exit(0)

    # Child: become session leader, detach from terminal
    os.setsid()

    # ── Fork #2 ───────────────────────────────────────────────────────────────
    pid = os.fork()
    if pid > 0:
        # Second parent exits — grandchild is now orphaned → adopted by PID 1
        sys.exit(0)

    # ── Grandchild: the actual daemon ─────────────────────────────────────────
    os.umask(0)
    os.chdir("/")

    # Redirect stdin to /dev/null
    with open(os.devnull, "r") as devnull:
        os.dup2(devnull.fileno(), sys.stdin.fileno())

    # Redirect stdout + stderr to logfile
    logfile = open(LOGFILE, "a", buffering=1)
    os.dup2(logfile.fileno(), sys.stdout.fileno())
    os.dup2(logfile.fileno(), sys.stderr.fileno())

    # Write PID file
    PIDFILE.write_text(str(os.getpid()))


def is_running() -> tuple[bool, int]:
    """
    Returns (running, pid).
    Checks PID file and verifies process is alive.
    """
    if not PIDFILE.exists():
        return False, 0
    try:
        pid = int(PIDFILE.read_text().strip())
        os.kill(pid, 0)  # signal 0 = existence check only
        return True, pid
    except (ValueError, ProcessLookupError, PermissionError):
        return False, 0


def clear_pidfile():
    try:
        PIDFILE.unlink(missing_ok=True)
    except Exception:
        pass
