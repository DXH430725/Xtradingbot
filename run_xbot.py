#!/usr/bin/env python3
"""XBot runner script for easy execution."""

import sys
import asyncio
from pathlib import Path

# Add xbot to path
sys.path.insert(0, str(Path(__file__).parent))

from xbot.app.main import main

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))