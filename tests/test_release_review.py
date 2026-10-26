"""Release contracts with actual dependencies, isolated from legacy stubs."""
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('scenario', ['numeric', 'credentials', 'credential_update', 'auth', 'vertex_input', 'vertex_lifespan'])
def test_release_contract(scenario):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / 'tests/integration/release_contracts.py'), scenario],
                            cwd=root, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
