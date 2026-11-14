"""Point every test at a throwaway state directory before any skill module loads.

``common`` resolves the config path and state home at import time, so the
environment has to be set here: pytest loads this file before it collects any
test module, whatever their order.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

TMP = Path(tempfile.mkdtemp(prefix="solo2611-test-"))
os.environ["CODEX_HOME"] = str(TMP / "codex")
os.environ["SOLO2611_CONFIG"] = str(TMP / "codex" / "solo-2611" / "config.json")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


@pytest.fixture(scope="session", autouse=True)
def _isolated_state_home() -> None:
    import common

    for path in (common.STATE_HOME, common.CONFIG_PATH):
        assert Path(path).is_relative_to(TMP), f"测试指向了真实状态目录：{path}"
