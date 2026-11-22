"""Do not let the legacy conftest's eager imports mask a cold HA boot."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("order", ["active", "shadow", "active-shadow", "shadow-active"])
def test_clean_process_platform_startup(order):
    root = Path(__file__).resolve().parents[1]
    isolated = root / "tests" / "isolated"
    env = {key: os.environ[key] for key in
           ("PATH", "HOME", "LANG", "TMPDIR", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE")
           if key in os.environ}
    result = subprocess.run([
        sys.executable, "-m", "pytest", "-p", "no:respx", "-q", "--noconftest",
        "--disable-socket", "--allow-unix-socket", "--timeout=20",
        "-o", "asyncio_mode=auto", "-o", "asyncio_default_fixture_loop_scope=function",
        "-o", "cache_dir=.pytest_cache/cold-start",
        str(isolated / "cold_start_cases.py") + "::test_cold_start[" + order + "]",
    ], cwd=root, env=env, text=True, capture_output=True, timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr
