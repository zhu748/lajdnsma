"""Process-local, bounded request history; active HTTP requests are never evicted."""
from collections import Counter, deque
from datetime import datetime, timezone
import os
import time
import uuid


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class RequestRegistry:
    def __init__(self, capacity=300):
        self.capacity = capacity
        self.instance_id = uuid.uuid4().hex[:12]
        self.active = {}
        self.upstream_reports = {}
        self.recent = deque(maxlen=capacity)
        self.sequence = 0
        self.reset()

    def reset(self):
        self.period_started_at = utc_now()
        self.generation = getattr(self, 'generation', 0) + 1
        self.counts = Counter()
        self.tokens = Counter()
        self.series = {}

    def start(self, report):
        report.generation = self.generation
        self.active[report.request_id] = report
        self.counts['received'] += 1

    def finish(self, report, record):
        self.active.pop(report.request_id, None)
        self.sequence += 1
        record["sequence"] = self.sequence
        self.recent.appendleft(record)
        if getattr(report, 'generation', self.generation) != self.generation:
            return
        self.counts[record['outcome']] += 1
        self.counts['completed'] += 1
        self.counts['cache_hits'] += record['source'] == '缓存'
        self.counts['usage_known'] += record['usage']['total_tokens'] is not None
        minute = int(time.time()) // 60
        bucket = self.series.setdefault(minute, {'calls': 0, 'tokens': 0, 'failures': 0})
        bucket['calls'] += 1
        bucket['failures'] += record['outcome'] != '成功'
        if record['source'] == '上游':
            for name, value in record['usage'].items():
                if value is not None:
                    self.tokens[name] += value
            bucket['tokens'] += record['usage']['total_tokens'] or 0
        for key in list(self.series):
            if key < minute - 1440:
                del self.series[key]

    def snapshot(self, after=0):
        if after > self.sequence:
            after = 0
        active = list(self.active.values())
        stages = Counter(r.stage for r in active)
        minute = int(time.time()) // 60
        points = []
        for key in range(minute - 30, minute + 1):
            bucket = self.series.get(key, {})
            points.append({'timestamp': key * 60000, 'calls': bucket.get('calls', 0), 'tokens': bucket.get('tokens', 0), 'failures': bucket.get('failures', 0)})
        return {
            'instance_id': self.instance_id, 'scope': '当前进程', 'pid': os.getpid(),
            'period_started_at': self.period_started_at,
            'active_count': len(active), 'waiting_count': stages['等待上游'],
            'streaming_count': stages['正在输出'], 'retrying_count': stages['重试等待'],
            'coalesced_count': stages['合并等待'],
            'upstream_active': sum(r.upstream_active for r in self.upstream_reports.values()),
            'counts': dict(self.counts), 'tokens': dict(self.tokens),
            'requests': [r.snapshot() for r in reversed(active)] + [r for r in self.recent if r['sequence'] > after],
            'cursor': self.sequence, 'gap': bool(after and self.recent and after < self.recent[-1]['sequence'] - 1),
            'history_capacity': self.capacity, 'time_series': points,
        }


request_registry = RequestRegistry()
