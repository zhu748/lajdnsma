import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_response_loop_helpers():
    fake_error_response = types.ModuleType("app.utils.error_response")
    fake_logging = types.ModuleType("app.utils.logging")
    log_calls = []

    def build_error_response(**kwargs):
        return kwargs

    def log(level, message, extra=None):
        log_calls.append((level, message, extra))

    fake_error_response.build_error_response = build_error_response
    fake_logging.log = log
    sys.modules.update(
        {
            "app.utils.error_response": fake_error_response,
            "app.utils.logging": fake_logging,
        }
    )
    spec = importlib.util.spec_from_file_location(
        "response_loop_helpers",
        ROOT / "app/utils/response_loop_helpers.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module._log_calls = log_calls
    return module


class ResponseLoopHelpersTestCase(unittest.TestCase):
    def test_logging_helpers_use_consistent_extra(self):
        module = load_response_loop_helpers()
        module.log_request_start("apikey123", request_type="stream", model="m", label="start")
        module.log_request_success("apikey123", request_type="stream", model="m", label="ok")
        module.log_empty_response_count(
            "apikey123",
            request_type="stream",
            model="m",
            empty_response_count=1,
            max_empty_responses=3,
        )
        self.assertEqual(len(module._log_calls), 3)
        self.assertEqual(module._log_calls[0][2]["key"], "apikey12")
        self.assertEqual(module._log_calls[2][0], "warning")

    def test_all_keys_failed_response(self):
        module = load_response_loop_helpers()
        module.log_all_keys_failed(request_type="stream", model="m", key="ALL")
        response = module.build_all_keys_failed_response(
            is_gemini=False,
            model="m",
            stream=True,
        )
        self.assertEqual(module._log_calls[-1][0], "error")
        self.assertEqual(response["content"], module.ALL_KEYS_FAILED_CONTENT)
        self.assertTrue(response["stream"])

    def test_build_keyed_tasks_and_json_dump(self):
        module = load_response_loop_helpers()

        def task_factory(api_key):
            return f"task:{api_key}"

        tasks, tasks_map = module.build_keyed_tasks(
            ["key-one", "key-two"],
            request_type="non-stream",
            model="m",
            label="start",
            task_factory=task_factory,
        )

        self.assertEqual(tasks, [("key-one", "task:key-one"), ("key-two", "task:key-two")])
        self.assertEqual(tasks_map["task:key-one"], "key-one")
        self.assertEqual(module.dump_json_response({"text": "中文"}), '{"text": "中文"}')
        self.assertEqual(len(module._log_calls), 2)


if __name__ == "__main__":
    unittest.main()
