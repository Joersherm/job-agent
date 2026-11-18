#!/usr/bin/env python3
"""guard.py SECONDS -- command ...   Run a command; if it is still running after SECONDS, stop it.

Used by run-cycle.sh so one stuck cycle can never block every later cycle. The command and
everything it started (scans, the apply agent) are stopped together. Windows left open on purpose
for you to fix an application are started in their own sessions and are not touched.
"""
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone


def main() -> int:
    limit = int(sys.argv[1])
    cmd = sys.argv[sys.argv.index("--") + 1:]
    proc = subprocess.Popen(cmd, start_new_session=True)
    try:
        return proc.wait(timeout=limit)
    except subprocess.TimeoutExpired:
        print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} Cycle stuck for {limit}s; stopping it",
              flush=True)
        for sig, wait in ((signal.SIGTERM, 15), (signal.SIGKILL, 5)):
            try:
                os.killpg(proc.pid, sig)
                proc.wait(timeout=wait)
                break
            except (ProcessLookupError, subprocess.TimeoutExpired):
                continue
        # the hidden Chrome used for Workday scans, if the scan died with it open
        pids = subprocess.run(["lsof", "-ti", "tcp:9225", "-sTCP:LISTEN"], capture_output=True, text=True).stdout.split()
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError:
                pass
        return 124


if __name__ == "__main__":
    sys.exit(main())
