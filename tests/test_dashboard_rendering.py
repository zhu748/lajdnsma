"""Exercise real dashboard templates in a process isolated from legacy stubs."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("dashboard_path", ["", "admin-panel"])
def test_dashboard_rendering(dashboard_path, tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "DASHBOARD_URL": dashboard_path,
        "ENABLE_STORAGE": "false",
        "STORAGE_DIR": str(tmp_path),
        "GOOGLE_CREDENTIALS_JSON": "",
        "GEMINI_API_KEYS": "",
    }
    result = subprocess.run(
        [sys.executable, str(root / "tests/integration/release_contracts.py"), "dashboard_rendering"],
        cwd=root, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
