#!/usr/bin/env python3
"""Render or inspect whole-SMT CPU isolation candidates."""
from pathlib import Path
import sys
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from cpu_isolation import main, IsolationError

if __name__ == '__main__':
    try:
        main()
    except (IsolationError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print('cpu-isolation: precondition failed; preserve protected records', file=sys.stderr)
        raise SystemExit(1)
