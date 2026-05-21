import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_retry_state():
    spec = importlib.util.spec_from_file_location(
        "retry_state",
        ROOT / "app/utils/retry_state.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RetryStateTestCase(unittest.TestCase):
    def test_retry_bounds(self):
        module = load_retry_state()
        self.assertTrue(module.should_continue_retry(0, 3, 0, 2))
        self.assertFalse(module.should_continue_retry(3, 3, 0, 2))
        self.assertFalse(module.should_continue_retry(0, 3, 2, 2))

    def test_batch_and_concurrency_bounds(self):
        module = load_retry_state()
        self.assertEqual(module.next_batch_size(2, 5, 10), 3)
        self.assertEqual(module.next_batch_size(2, 5, 2), 2)
        self.assertEqual(module.increase_concurrency(2, 3, 4), 4)
        self.assertEqual(module.increase_concurrency(2, 1, 4), 3)

    def test_empty_limit_and_task_prune(self):
        module = load_retry_state()
        self.assertTrue(module.reached_empty_response_limit(2, 2))
        self.assertFalse(module.reached_empty_response_limit(1, 2))

        class Task:
            def __init__(self, done):
                self._done = done

            def done(self):
                return self._done

        pending = module.remove_completed_tasks([("a", Task(True)), ("b", Task(False))])
        self.assertEqual([key for key, _ in pending], ["b"])


if __name__ == "__main__":
    unittest.main()
