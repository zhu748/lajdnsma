"""日志保留策略契约：定时淘汰、每日清空与手动清空。

覆盖三层清理机制：
1. LogManager.purge_older_than —— 按 created_ns 时间序淘汰过期条目；
2. purge_expired_logs —— 按 settings.LOG_RETENTION_HOURS（默认 24 小时）
   同步清理增量 feed 与两个面板日志管理器；
3. clear_all_logs —— 整体清空并轮换 feed 实例 ID（前端据此丢弃本地
   缓存）；以及 POST /api/clear-logs 端点的鉴权与效果。
"""
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 本仓库未把 app 打包安装进环境（其余测试通过假模块注入或 subprocess
# 规避）；此处直接把仓库根加入 sys.path 以导入真实实现。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _feed_snapshot():
    from app.utils import logging as logging_module
    return logging_module.get_log_feed(0)


class LogRetentionTestCase(unittest.TestCase):
    def setUp(self):
        from app.utils import logging as logging_module

        self.logging = logging_module
        self._ts_counter = 0
        # 每个测试从干净状态开始：清空三个缓冲并固定实例 ID。
        with logging_module._feed_lock:
            logging_module._feed.clear()
            logging_module._feed_instance = "test-instance"
        logging_module.log_manager.clear()
        logging_module.vertex_log_manager.clear()

    def tearDown(self):
        # 恢复默认保留时长，避免污染其它测试。
        import app.config.settings as settings

        settings.LOG_RETENTION_HOURS = 24.0

    def _add_entries(self, count, age_seconds=0.0):
        """直接向三个缓冲写入指定时龄的日志条目（绕过 log() 热路径）。

        时间戳全局递增，保证跨调用的 created_ns/seq/oldest 断言可预测。
        add_log 会同时写增量 feed，故仅经主管理器入 feed；Vertex 管理
        器直接追加，避免同一条日志在 feed 中出现两次。
        """
        created_ns = time.time_ns() - int(age_seconds * 1_000_000_000)
        for _ in range(count):
            self._ts_counter += 1
            timestamp = f"2026-11-03 12:{self._ts_counter // 60:02d}:{self._ts_counter % 60:02d}"
            entry = {
                "created_ns": created_ns,
                "timestamp": timestamp,
                "level": "INFO",
                "message": f"entry-{self._ts_counter}",
            }
            self.logging.log_manager.add_log(entry)
            self.logging.vertex_log_manager.logs.append(dict(entry))
        return created_ns

    def test_purge_older_than_removes_only_expired_entries(self):
        manager = self.logging.LogManager(max_logs=100)
        fresh_ns = time.time_ns()
        old_ns = fresh_ns - 2 * 3600 * 1_000_000_000
        manager.logs.append({"created_ns": old_ns, "message": "old"})
        manager.logs.append({"created_ns": fresh_ns, "message": "fresh"})
        removed = manager.purge_older_than(fresh_ns - 3600 * 1_000_000_000)
        self.assertEqual(removed, 1)
        self.assertEqual([e["message"] for e in manager.logs], ["fresh"])

    def test_purge_older_than_ignores_nonpositive_cutoff(self):
        manager = self.logging.LogManager(max_logs=10)
        manager.logs.append({"created_ns": 1, "message": "old"})
        self.assertEqual(manager.purge_older_than(0), 0)
        self.assertEqual(len(manager.logs), 1)

    def test_purge_expired_logs_honors_retention_setting(self):
        import app.config.settings as settings

        self._add_entries(3, age_seconds=25 * 3600)  # 超过 24 小时
        self._add_entries(2, age_seconds=3600)       # 1 小时前，保留

        settings.LOG_RETENTION_HOURS = 24.0
        removed = self.logging.purge_expired_logs()
        self.assertEqual(removed, 9)  # feed + 两个 manager 各淘汰 3 条
        self.assertEqual(len(self.logging.log_manager.logs), 2)
        self.assertEqual(len(self.logging.vertex_log_manager.logs), 2)
        feed = _feed_snapshot()
        self.assertEqual(len(feed["logs"]), 2)
        self.assertEqual(feed["retention_hours"], 24.0)

    def test_purge_expired_logs_disabled_when_retention_zero(self):
        import app.config.settings as settings

        self._add_entries(2, age_seconds=48 * 3600)
        settings.LOG_RETENTION_HOURS = 0.0
        self.assertEqual(self.logging.purge_expired_logs(), 0)
        self.assertEqual(len(self.logging.log_manager.logs), 2)

    def test_purge_expired_logs_keeps_feed_order_and_cursor(self):
        import app.config.settings as settings

        self._add_entries(1, age_seconds=30 * 3600)   # -> 12:00:01，将被淘汰
        self._add_entries(2, age_seconds=60)          # -> 12:00:02 / 12:00:03
        settings.LOG_RETENTION_HOURS = 24.0
        self.logging.purge_expired_logs()
        feed = _feed_snapshot()
        sequences = [entry["seq"] for entry in feed["logs"]]
        self.assertEqual(sequences, sorted(sequences)[-2:])
        # oldest_timestamp 随淘汰前移，前端据此丢弃本地过期条目。
        self.assertEqual(feed["oldest_timestamp"], "2026-11-03 12:00:02")

    def test_clear_all_logs_rotates_instance_and_audits(self):
        self._add_entries(4)
        old_instance = _feed_snapshot()["instance_id"]
        self.logging.clear_all_logs(reason="unittest-clear")
        feed = _feed_snapshot()
        self.assertNotEqual(feed["instance_id"], old_instance)
        # 清空后写入一条审计日志（进入新实例的 feed 与主日志管理器）。
        self.assertEqual(len(feed["logs"]), 1)
        self.assertEqual(len(self.logging.log_manager.logs), 1)
        self.assertEqual(len(self.logging.vertex_log_manager.logs), 0)
        self.assertIn("unittest-clear", feed["logs"][0]["message"])

    def test_ring_buffer_evicts_oldest_when_full(self):
        # 「日志满了有新请求先删旧记录」：deque(maxlen) 自动淘汰最旧条目。
        manager = self.logging.LogManager(max_logs=3)
        for i in range(5):
            manager.add_log({"created_ns": time.time_ns(), "message": f"m{i}"})
        self.assertEqual([e["message"] for e in manager.logs], ["m2", "m3", "m4"])

    def test_clear_logs_endpoint_contract(self):
        code = r'''
import json
from fastapi.testclient import TestClient
from app.main import app
from app.utils import logging as logging_module
from app.utils.logging import log, get_log_feed
import app.config.settings as settings

settings.WEB_PASSWORD = "retention-test"
settings.PASSWORD = "retention-test"
log("info", "before-clear-1")
log("info", "before-clear-2")
old_instance = get_log_feed(0)["instance_id"]
client = TestClient(app)

missing = client.post("/api/clear-logs", json={})
assert missing.status_code == 400, missing.text
wrong = client.post("/api/clear-logs", json={"password": "nope"})
assert wrong.status_code == 401, wrong.text

result = client.post("/api/clear-logs", json={"password": "retention-test"})
assert result.status_code == 200, result.text
assert result.json()["status"] == "success"

feed = get_log_feed(0)
assert feed["instance_id"] != old_instance
assert len(feed["logs"]) == 1  # 清空后的审计日志
assert "手动清空" in feed["logs"][0]["message"]
assert len(logging_module.log_manager.logs) == 1
assert len(logging_module.vertex_log_manager.logs) == 0

# 鉴权缺失时 GET /api/logs 仍然 401（沿用既有契约）。
assert client.get("/api/logs").status_code == 401
ok = client.get("/api/logs", headers={"Authorization": "Bearer retention-test"})
assert ok.status_code == 200
assert ok.json()["retention_hours"] == 24.0
'''
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"endpoint contract failed:\n{completed.stdout}\n{completed.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
