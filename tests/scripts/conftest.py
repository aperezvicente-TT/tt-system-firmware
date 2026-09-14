# Copyright (c) 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0
"""Host-side pytest support for scripts/qsfp_lib.py and scripts/qsfp.py."""

import sys
import types
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

try:
    import pyluwen  # noqa: F401
except ImportError:
    stub = types.ModuleType("pyluwen")
    stub.detect_chips = lambda: []
    sys.modules["pyluwen"] = stub
