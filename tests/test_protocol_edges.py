from pathlib import Path
import subprocess
import sys
import pytest


@pytest.mark.parametrize('case', ['interleaved', 'fragmented_tools', 'malformed', 'optional_fields', 'parallel_history', 'chat_input'])
def test_protocol_edge_contract(case):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / 'tests/integration/protocol_edge_contracts.py'), case],
                            cwd=root, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
