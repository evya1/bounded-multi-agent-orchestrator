#!/usr/bin/env python3
"""Entry point for the bounded multi-agent orchestrator.

    uv run orchestrate_v4.py doctor
    uv run orchestrate_v4.py context --repo police --task T002
"""

from __future__ import annotations

import sys

from orchestrator.cli import main

if __name__ == "__main__":
    sys.exit(main())
