# -*- coding: utf-8 -*-
"""Close a running ScriptDB.exe before a rebuild, then confirm it is gone.

Windows locks a running executable, so PyInstaller (and the ``rd`` that removes
the old dist folder) fails with "Access denied" while the previous build is
still open. This helper closes it and waits for the file lock to be released.

It is written in Python on purpose: ``taskkill``, ``tasklist``, ``find`` and
``timeout`` are absent on some minimal PATH setups, and Python is already a
build requirement, so it is the one tool guaranteed to be available.

Usage:
    python stop_running.py ScriptDB 20

Exit codes:
    0 -- no such process was running, or it closed and the lock was released
    1 -- a process is still running after the timeout (the build should stop)
"""
import os
import subprocess
import sys
import time


def find_pids(image_name):
    """The PIDs of a process, via tasklist; empty when the tool is missing."""
    try:
        output = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq %s" % image_name, "/NH"],
            stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return []
    pids = []
    for line in output.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower() == image_name.lower():
            try:
                pids.append(int(parts[1]))
            except ValueError:
                continue
    return pids


def kill(pid, force, image_name):
    """Terminate one PID, preferring the direct os.kill (no taskkill needed)."""
    try:
        if os.name == "nt":
            subprocess.call(["taskkill", "/PID", str(pid)] +
                            (["/F", "/T"] if force else []),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
        else:
            os.kill(pid, 15)
    except (OSError, subprocess.SubprocessError):
        pass


def still_running(image_name):
    """True when a process with this image name is still present."""
    return bool(find_pids(image_name))


def main(argv):
    image_name = (argv[1] if len(argv) > 1 else "ScriptDB") + ".exe"
    timeout = float(argv[2]) if len(argv) > 2 else 20.0
    deadline = time.time() + timeout

    if not still_running(image_name):
        print("       %s is not running" % image_name)
        return 0

    print("       closing %s ..." % image_name)
    force = False
    while time.time() < deadline:
        for pid in find_pids(image_name):
            kill(pid, force, image_name)
        time.sleep(0.4)
        if not still_running(image_name):
            # The process is gone; give Windows a moment to drop the file lock.
            time.sleep(0.6)
            print("       %s closed" % image_name)
            return 0
        # After a few polite attempts, switch to the forceful form.
        force = True

    if still_running(image_name):
        print("       %s is STILL running after %.0fs" % (image_name, timeout))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
