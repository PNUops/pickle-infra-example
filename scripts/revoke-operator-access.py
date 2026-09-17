#!/usr/bin/env python3
"""Preflight or revoke state-owned access after operator sessions are quiesced."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from operator_access import main

if __name__ == "__main__":
    main("revoke")
