#!/usr/bin/env python3
"""Preflight or provision the dedicated pickle administrator account."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from operator_access import main

if __name__ == "__main__":
    main("enroll")
