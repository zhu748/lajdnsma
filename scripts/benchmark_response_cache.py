"""Hot-key FIFO consumption benchmark; no network or model calls.

Run: uv run python scripts/benchmark_response_cache.py --baseline 2bc0605
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.utils.cache import ResponseCacheManager


async def measure(cache_type, count):
    cache = cache_type(expiry_time=600, max_entries=count + 1)
    for index in range(count):
        await cache.store('hot-key', index)
    started = time.perf_counter()
    for index in range(count):
        assert await cache.get_and_remove('hot-key') == (index, True)
    return time.perf_counter() - started


async def main(args):
    source = subprocess.check_output(['git', 'show', f'{args.baseline}:app/utils/cache.py'], cwd=ROOT, text=True)
    baseline = types.ModuleType('baseline_cache')
    exec(compile(source, f'{args.baseline}:cache.py', 'exec'), baseline.__dict__)
    result = {'baseline': args.baseline, 'responses_per_key': args.count}
    for name, cache_type in [('before', baseline.ResponseCacheManager), ('after', ResponseCacheManager)]:
        samples = [await measure(cache_type, args.count) for _ in range(args.runs)]
        result[name] = {'seconds': samples, 'median_seconds': statistics.median(samples)}
    result['speedup'] = result['before']['median_seconds'] / result['after']['median_seconds']
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', default='2bc0605')
    parser.add_argument('--count', type=int, default=5000)
    parser.add_argument('--runs', type=int, default=3)
    asyncio.run(main(parser.parse_args()))
