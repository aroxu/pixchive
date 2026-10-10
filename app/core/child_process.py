"""Linux worker children must not outlive a forcibly terminated worker."""

import ctypes
import os
import signal
import sys


def main():
    parent = int(sys.argv[1])
    if sys.platform == "linux":
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
            raise SystemExit(1)
        # Close the race where the parent died before prctl was installed.
        if os.getppid() != parent:
            raise SystemExit(1)
    os.execvp(sys.argv[2], sys.argv[2:])


if __name__ == "__main__":
    main()
