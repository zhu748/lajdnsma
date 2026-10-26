"""Capture final HTTP requests with actual SDKs, isolated from legacy fake modules."""
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('scenario', [
    'google_rest', 'native_express', 'native_sa', 'direct_json', 'direct_stream',
    'direct_fake', 'direct_pool', 'cors', 'vertex_cors',
])
def test_upstream_header_contract(scenario):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / 'tests/integration/upstream_header_contracts.py'), scenario],
        cwd=root, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
