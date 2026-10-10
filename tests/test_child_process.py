import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "linux", reason="Linux worker process supervision")
def test_child_terminates_when_worker_is_force_killed(tmp_path):
    script = """
import os, subprocess, sys
child = subprocess.Popen([
    sys.executable, '-m', 'app.core.child_process', str(os.getpid()),
    sys.executable, '-c', 'import time; print("ready", flush=True); time.sleep(60)',
], stdout=subprocess.PIPE)
child.stdout.readline()
print(child.pid, flush=True)
child.wait()
"""
    parent = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    child_pid = int(parent.stdout.readline())
    try:
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=5)
        for _ in range(100):
            status = Path(f"/proc/{child_pid}/stat")
            try:
                state = status.read_text().rsplit(")", 1)[1].split()[0]
            except (FileNotFoundError, ProcessLookupError):
                # The child can disappear between opening and reading /proc.
                break
            if state == "Z":
                break
            time.sleep(0.02)
        else:
            pytest.fail("worker child survived SIGKILL")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
