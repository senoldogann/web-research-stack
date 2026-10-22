"""Test configuration for local package imports."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# The API lifespan warms a real Google browser; the tests must not.
os.environ.setdefault("GOOGLE_WARMUP_ON_STARTUP", "false")

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
