"""Compare Responses conversion with a repository revision; no network or model calls.

Run: uv run python scripts/benchmark_protocol_streams.py --baseline 2bc0605
"""
import argparse
import asyncio
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.utils.protocol_streams import openai_stream_to_responses_stream


async def events(count, size):
    wire = 'data: ' + json.dumps({'choices': [{'delta': {'content': 'x' * size}, 'finish_reason': None}]}) + '\n\n'
    for _ in range(count):
        yield wire
    yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'


async def measure(converter, count, size):
    started = time.perf_counter()
    async for _ in converter(events(count, size), 'benchmark'):
        pass
    return time.perf_counter() - started


async def main(args):
    def source_at(name):
        return subprocess.check_output(
            ['git', 'show', f'{args.baseline}:app/utils/{name}.py'], cwd=ROOT, text=True,
        )

    # Measure the baseline's own parser/helpers, even after current helpers
    # have been removed or renamed during refactoring.
    common = types.ModuleType('baseline_protocol_common')
    exec(compile(source_at('protocol_common'), f'{args.baseline}:protocol_common.py', 'exec'), common.__dict__)
    baseline = types.ModuleType('baseline_protocol_streams')
    with patch.dict(sys.modules, {'app.utils.protocol_common': common}):
        exec(compile(source_at('protocol_streams'), f'{args.baseline}:protocol_streams.py', 'exec'), baseline.__dict__)
    results = {'baseline': args.baseline, 'chunks': args.chunks, 'characters_per_chunk': args.size}
    for name, converter in [('before', baseline.openai_stream_to_responses_stream), ('after', openai_stream_to_responses_stream)]:
        samples = [await measure(converter, args.chunks, args.size) for _ in range(args.runs)]
        results[name] = {'seconds': samples, 'median_seconds': statistics.median(samples)}
    results['speedup'] = results['before']['median_seconds'] / results['after']['median_seconds']
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', default='2bc0605')
    parser.add_argument('--chunks', type=int, default=10000)
    parser.add_argument('--size', type=int, default=256)
    parser.add_argument('--runs', type=int, default=3)
    asyncio.run(main(parser.parse_args()))
