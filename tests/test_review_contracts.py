"""Integration contracts use actual FastAPI/Pydantic/SDK classes, isolated from legacy stubs."""
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('scenario', ['request', 'tool_roundtrip', 'stream', 'handler', 'body_limit', 'rpm', 'vertex', 'error', 'embedding', 'http', 'native_failure', 'vertex_fake', 'vertex_sdk', 'capabilities', 'dispatch', 'client_pool', 'token_identity', 'direct_stream'])
def test_real_dependency_contract(scenario):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / 'tests/integration/protocol_contracts.py'), scenario],
        cwd=root, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
