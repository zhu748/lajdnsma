"""PrefillEchoStreamFilter（prefill 回显剥离）单元测试。

覆盖两层：
1. app/utils/prefill_echo.py 的状态机本体 —— 完整回显剥离 / 无回显
   立即放行 / 部分匹配释放 / 跨 chunk 暂存 / 多候选独立 / finalize
   合成 chunk / 空 prefill no-op / ContextVar 桥；
2. Vertex 转换层接入 —— convert_chunk_to_openai（echo_filter）与
   convert_to_openai_format（prefill_echo）的文本过滤行为。

设计对齐社区实现 vertex-master（internal/transform/prefill.go）的
状态机语义（移植自 Go，逐分支等价）。
"""

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def load_prefill_echo():
    """直接从文件加载 prefill_echo（仅依赖标准库，无 app.* 依赖链）。"""
    spec = importlib.util.spec_from_file_location(
        "echo_prefill_echo", ROOT / "app/utils/prefill_echo.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FilterStateMachineTests(unittest.TestCase):
    """状态机 filter_text 的核心语义。"""

    def setUp(self):
        self.pe = load_prefill_echo()

    def test_empty_prefill_is_noop(self):
        filt = self.pe.PrefillEchoStreamFilter("")
        self.assertEqual(filt.filter_text("anything"), "anything")
        self.assertEqual(filt.filter_text("x", final=True), "x")
        self.assertEqual(filt.finalize_text(), "")

    def test_exact_complete_echo_stripped(self):
        filt = self.pe.PrefillEchoStreamFilter("ABCDEF")
        self.assertEqual(filt.filter_text("ABCDEF"), "")

    def test_echo_then_continuation_in_one_chunk(self):
        filt = self.pe.PrefillEchoStreamFilter("ABCDEF")
        self.assertEqual(filt.filter_text("ABCDEF tail"), " tail")

    def test_echo_split_across_chunks(self):
        filt = self.pe.PrefillEchoStreamFilter("ABCDEF")
        self.assertEqual(filt.filter_text("AB"), "")  # 暂存
        self.assertEqual(filt.filter_text("CD"), "")  # 暂存
        self.assertEqual(filt.filter_text("EF"), "")  # 完成，无尾部
        self.assertEqual(filt.filter_text("tail"), "tail")  # decided 直通

    def test_mismatch_releases_everything_immediately(self):
        filt = self.pe.PrefillEchoStreamFilter("ABCDEF")
        self.assertEqual(filt.filter_text("AB"), "")  # 暂存
        # 前缀未完成即失配：暂存 + 本段一并放行。
        self.assertEqual(filt.filter_text("XY"), "ABXY")
        # decided 之后原样透传。
        self.assertEqual(filt.filter_text("Z"), "Z")

    def test_no_prefix_match_releases_first_chunk(self):
        filt = self.pe.PrefillEchoStreamFilter("ABCDEF")
        self.assertEqual(filt.filter_text("Q"), "Q")

    def test_partial_match_released_on_final(self):
        # 只匹配到一半 + final（结束帧）：按真实内容原样释放。
        filt = self.pe.PrefillEchoStreamFilter("ABCDEF")
        self.assertEqual(filt.filter_text("AB"), "")
        self.assertEqual(filt.filter_text("", final=True), "AB")

    def test_finalize_text_releases_undecided(self):
        filt = self.pe.PrefillEchoStreamFilter("ABCDEF")
        self.assertEqual(filt.filter_text("ABC"), "")
        self.assertEqual(filt.finalize_text(), "ABC")
        self.assertEqual(filt.finalize_text(), "")  # 释放后无残留

    def test_exact_echo_completed_then_final_is_empty(self):
        # 完整回显且无后续：decided 在完成分支已置位，final 无可释放。
        filt = self.pe.PrefillEchoStreamFilter("ABCDEF")
        self.assertEqual(filt.filter_text("ABCDEF"), "")
        self.assertEqual(filt.finalize_text(), "")

    def test_echo_shorter_than_prefill_spanning_parts(self):
        # 多 part 拼接后恰好是回显（非流式 filter_complete_response 场景）。
        filt = self.pe.PrefillEchoStreamFilter("ABCDEF")
        self.assertEqual(filt.filter_text("AB", final=False), "")
        self.assertEqual(filt.filter_text("CD", final=False), "")
        self.assertEqual(filt.filter_text("EF", final=False), "")


class ChunkFilterTests(unittest.TestCase):
    """filter_gemini_chunk / filter_complete_response 的 dict 过滤。"""

    def setUp(self):
        self.pe = load_prefill_echo()

    @staticmethod
    def _chunk(text=None, finish=None, thought=False, index=None):
        part = {"text": text}
        if thought:
            part["thought"] = True
        candidate = {"content": {"role": "model", "parts": [part]}}
        if finish:
            candidate["finishReason"] = finish
        if index is not None:
            candidate["index"] = index
        return {"candidates": [candidate]}

    def test_chunk_echo_stripped_and_finish_tail_appended(self):
        filt = self.pe.PrefillEchoStreamFilter("ABCDEF")
        chunk = self._chunk("AB")
        filt.filter_gemini_chunk(chunk)
        self.assertEqual(chunk["candidates"][0]["content"]["parts"][0]["text"], "")

        # 结束帧：未决暂存 "AB" 被释放为追加的普通文本 part。
        finish_chunk = self._chunk(finish="STOP")
        filt.filter_gemini_chunk(finish_chunk)
        parts = finish_chunk["candidates"][0]["content"]["parts"]
        self.assertEqual(parts[-1]["text"], "AB")

    def test_finish_reason_unspecified_does_not_release(self):
        filt = self.pe.PrefillEchoStreamFilter("ABCDEF")
        chunk = self._chunk("AB")
        filt.filter_gemini_chunk(chunk)
        unspecified = self._chunk(finish="FINISH_REASON_UNSPECIFIED")
        filt.filter_gemini_chunk(unspecified)
        # 未决暂存仍在（后续 finalize 才释放）。
        self.assertEqual(filt.finalize_gemini(), [(0, "AB")])

    def test_thought_parts_untouched(self):
        filt = self.pe.PrefillEchoStreamFilter("ABCDEF")
        chunk = self._chunk("thought echo", thought=True)
        filt.filter_gemini_chunk(chunk)
        self.assertEqual(
            chunk["candidates"][0]["content"]["parts"][0]["text"], "thought echo"
        )

    def test_function_call_parts_untouched(self):
        filt = self.pe.PrefillEchoStreamFilter("ABCDEF")
        chunk = {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"functionCall": {"name": "f", "args": {}}}],
                    }
                }
            ]
        }
        filt.filter_gemini_chunk(chunk)
        self.assertIn(
            "functionCall", chunk["candidates"][0]["content"]["parts"][0]
        )

    def test_finalize_gemini_synthesizes_tail(self):
        filt = self.pe.PrefillEchoStreamFilter("ABCDEF")
        chunk = self._chunk("AB")
        filt.filter_gemini_chunk(chunk)
        tails = filt.finalize_gemini()
        self.assertEqual(tails, [(0, "AB")])
        built = self.pe.build_tail_chunk(tails)
        self.assertEqual(
            built["candidates"][0]["content"]["parts"][0]["text"], "AB"
        )
        self.assertNotIn("finishReason", built["candidates"][0])

    def test_finalize_gemini_no_tail_returns_none(self):
        filt = self.pe.PrefillEchoStreamFilter("ABCDEF")
        self.assertEqual(filt.finalize_gemini(), [])
        self.assertIsNone(self.pe.build_tail_chunk([]))

    def test_candidate_index_fallback_position(self):
        # 无 index 字段时按位置回退（候选 0 → index 0）。
        filt = self.pe.PrefillEchoStreamFilter("ABCDEF")
        chunk = self._chunk("AB")
        filt.filter_gemini_chunk(chunk)
        self.assertEqual(filt.finalize_gemini(), [(0, "AB")])

    def test_complete_response_full_echo(self):
        filt = self.pe.PrefillEchoStreamFilter("ABCDEF")
        data = {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": "ABCDEF tail"}]},
                    "index": 0,
                }
            ]
        }
        filt.filter_complete_response(data)
        self.assertEqual(data["candidates"][0]["content"]["parts"][0]["text"], " tail")

    def test_complete_response_partial_echo_released(self):
        filt = self.pe.PrefillEchoStreamFilter("ABCDEF")
        data = {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": "AB"}]}, "index": 0}
            ]
        }
        filt.filter_complete_response(data)
        parts = data["candidates"][0]["content"]["parts"]
        # 原 part 被清空，未决前缀按真实内容追加为新 part。
        self.assertEqual(parts[0]["text"], "")
        self.assertEqual(parts[-1]["text"], "AB")

    def test_complete_response_invalid_shapes_are_noop(self):
        filt = self.pe.PrefillEchoStreamFilter("ABCDEF")
        for bad in (None, "no", {}, {"candidates": "no"}, {"candidates": [None]}):
            filt.filter_complete_response(bad)  # 不抛即通过
            filt.filter_gemini_chunk(bad)


class ContextVarBridgeTests(unittest.TestCase):
    """set/get_current_prefill_echo 的按请求隔离语义。"""

    def setUp(self):
        self.pe = load_prefill_echo()

    def test_default_is_none_and_set_get_roundtrip(self):
        self.assertIsNone(self.pe.get_current_prefill_echo())
        self.pe.set_current_prefill_echo("prefill")
        self.assertEqual(self.pe.get_current_prefill_echo(), "prefill")
        self.pe.set_current_prefill_echo(None)
        self.assertIsNone(self.pe.get_current_prefill_echo())


class VertexConversionEchoTests(unittest.TestCase):
    """Vertex 转换层（convert_chunk_to_openai / convert_to_openai_format）。"""

    # 与 test_vertex_prefill.py 相同的延迟导入约束：不在模块级引入
    # 真实 app.* 模块，setUp 内切换到真实链，conftest 负责恢复。
    _SNAPSHOT = {}

    @classmethod
    def _fresh_real_modules(cls):
        for name in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
            sys.modules.pop(name, None)
        if cls._SNAPSHOT:
            sys.modules.update(cls._SNAPSHOT)
            return sys.modules["app.vertex.message_processing"]
        import app.vertex.message_processing as mp

        cls._SNAPSHOT.update(
            {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}
        )
        return mp

    def setUp(self):
        self.mp = self._fresh_real_modules()

    @staticmethod
    def _fake_chunk(normal_text=None, reasoning_text=None):
        parts = []
        if reasoning_text is not None:
            parts.append(SimpleNamespace(text=reasoning_text, thought=True))
        if normal_text is not None:
            parts.append(SimpleNamespace(text=normal_text, thought=None))
        return SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(parts=parts),
                    text=None,
                )
            ]
        )

    def test_convert_chunk_strips_echo(self):
        from app.utils.prefill_echo import PrefillEchoStreamFilter

        filt = PrefillEchoStreamFilter("ABCDEF")
        sse = self.mp.convert_chunk_to_openai(
            self._fake_chunk(normal_text="AB"), "gemini-3.8-flash", "id", 0,
            created_ts=1, echo_filter=filt,
        )
        self.assertNotIn('"content"', sse)  # 暂存 → 合法空增量形态

        sse = self.mp.convert_chunk_to_openai(
            self._fake_chunk(normal_text="CDEF!"), "gemini-3.8-flash", "id", 0,
            created_ts=1, echo_filter=filt,
        )
        self.assertIn('"content": "!"', sse)

        # 流结束释放（无未决暂存 → 无补发）。
        self.assertEqual(filt.finalize_text(), "")

    def test_convert_chunk_without_filter_unchanged(self):
        sse = self.mp.convert_chunk_to_openai(
            self._fake_chunk(normal_text="ABCDEF"), "gemini-3.8-flash", "id", 0,
            created_ts=1,
        )
        self.assertIn('"content": "ABCDEF"', sse)

    def test_convert_chunk_reasoning_not_filtered(self):
        from app.utils.prefill_echo import PrefillEchoStreamFilter

        filt = PrefillEchoStreamFilter("thought...")
        sse = self.mp.convert_chunk_to_openai(
            self._fake_chunk(
                reasoning_text="thought...", normal_text="answer"
            ),
            "gemini-3.8-flash", "id", 0, created_ts=1, echo_filter=filt,
        )
        self.assertIn('"reasoning_content": "thought..."', sse)
        self.assertIn('"content": "answer"', sse)

    def test_convert_to_openai_format_strips_echo(self):
        response = SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(
                        parts=[SimpleNamespace(text="ABCDEF tail", thought=None)]
                    ),
                    text=None,
                )
            ],
            usage_metadata=None,
        )
        result = self.mp.convert_to_openai_format(
            response, "gemini-3.8-flash", prefill_echo="ABCDEF"
        )
        self.assertEqual(result["choices"][0]["message"]["content"], " tail")

    def test_convert_to_openai_format_without_echo(self):
        response = SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(
                        parts=[SimpleNamespace(text="plain answer", thought=None)]
                    ),
                    text=None,
                )
            ],
            usage_metadata=None,
        )
        result = self.mp.convert_to_openai_format(response, "gemini-3.8-flash")
        self.assertEqual(result["choices"][0]["message"]["content"], "plain answer")


if __name__ == "__main__":
    unittest.main()
