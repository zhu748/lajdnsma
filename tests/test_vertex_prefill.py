"""Vertex 路径 prefill 补丁测试（api_helpers.execute_gemini_call）。

验证 execute_gemini_call（Vertex 全部调用分支的唯一汇聚点，auto 三连
attempt 与普通模型都经此）在 prompt_func 产出以 model turn 结尾的
contents 且模型属于受限世代时，自动追加一条非空 user turn；
其余场景（旧模型 / user 结尾 / 开关关闭 / 单 Content）不受影响。

工程约束：与 test_thinking_override.py 相同——本文件不在模块级
import 任何真实 app.* 模块（避免收集阶段的真实父模块属性链污染
其他测试文件注入的假桩，详见该文件头部说明）。所有真实导入都在
setUp 内延迟进行，conftest._isolate_sys_modules 负责恢复基线。
"""

import asyncio
import sys
import unittest
from types import SimpleNamespace


# 首次真实导入后的模块快照（仅 app.* 条目），跨用例复用。
_REAL_APP_SNAPSHOT = {}


def _fresh_real_modules():
    """确保 sys.modules 中是真实 app.* 链（见模块 docstring）。"""
    for name in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
        sys.modules.pop(name, None)

    if _REAL_APP_SNAPSHOT:
        sys.modules.update(_REAL_APP_SNAPSHOT)
        return sys.modules["app.config.settings"], sys.modules["app.vertex.api_helpers"]

    import app.config.settings as settings_module
    import app.vertex.api_helpers as api_helpers_module

    _REAL_APP_SNAPSHOT.update(
        {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}
    )
    return settings_module, api_helpers_module


DEFAULT_PROMPT = (
    "Continue from where you left off, without repeating any content."
)


class VertexPrefillFixTests(unittest.TestCase):
    """execute_gemini_call 的 prefill 追加行为。"""

    def setUp(self):
        self.settings, self.ah = _fresh_real_modules()

        self._saved = {
            key: getattr(self.settings, key, None)
            for key in ("PREFILL_FIX", "PREFILL_FIX_PROMPT", "FAKE_STREAMING")
        }
        self.settings.PREFILL_FIX = True
        self.settings.PREFILL_FIX_PROMPT = DEFAULT_PROMPT
        # 强制 fake streaming 分支：stream=True 时 execute_gemini_call
        # 直接返回未消费的 StreamingResponse（generator 惰性），测试
        # 不会触发任何真实上游调用。
        self.settings.FAKE_STREAMING = True

    def tearDown(self):
        for key, value in self._saved.items():
            setattr(self.settings, key, value)

    # -- 构造辅助 ----------------------------------------------------------

    @staticmethod
    def _content(role, text):
        from google.genai import types

        return types.Content(role=role, parts=[types.Part(text=text)])

    def _run(self, model_to_call, prompt_result, *, stream=True):
        """执行 execute_gemini_call 并返回 prompt_func 的产出容器。

        prompt_func 返回 prompt_result（list 或单个 Content）。由于
        追加是原地 append（list 时），调用后直接检查传入对象即可。
        """
        contents = prompt_result if isinstance(prompt_result, list) else [prompt_result]
        prompt_func = lambda messages: prompt_result  # noqa: E731
        request_obj = SimpleNamespace(
            stream=stream,
            model=model_to_call,
            messages=[{"role": "user", "content": "hi"}],
        )
        # gen_config 需要是可变 dict（thinking 覆盖等逻辑会写键）
        response = asyncio.run(
            self.ah.execute_gemini_call(
                object(),  # client 不被触达（generator 惰性）
                model_to_call,
                prompt_func,
                {},
                request_obj,
                is_auto_attempt=False,
            )
        )
        return contents, response

    # -- 测试 ---------------------------------------------------------------

    def test_restricted_model_with_model_turn_tail_appends_user(self):
        contents, _ = self._run(
            "gemini-3.8-flash",
            [
                self._content("user", "介绍黑洞"),
                self._content("model", "黑洞是一种"),
            ],
        )
        self.assertEqual(len(contents), 3)
        self.assertEqual(contents[-1].role, "user")
        self.assertEqual(contents[-1].parts[0].text, DEFAULT_PROMPT)
        self.assertEqual(contents[-2].role, "model")

    def test_unrestricted_model_keeps_native_prefill(self):
        contents, _ = self._run(
            "gemini-2.5-flash",
            [
                self._content("user", "hi"),
                self._content("model", "prefill"),
            ],
        )
        self.assertEqual(len(contents), 2)
        self.assertEqual(contents[-1].role, "model")

    def test_user_turn_tail_untouched(self):
        contents, _ = self._run(
            "gemini-3.8-flash",
            [
                self._content("model", "resp"),
                self._content("user", "next"),
            ],
        )
        self.assertEqual(len(contents), 2)
        self.assertEqual(contents[-1].role, "user")
        self.assertEqual(contents[-1].parts[0].text, "next")

    def test_single_content_object_is_wrapped(self):
        # create_gemini_prompt 对单条消息可能返回单个 Content 对象；
        # model turn 结尾时同样需要追加（包装成新列表，局部变量重
        # 绑定，外部容器观察不到 —— 以追加日志为断言依据）。
        single = self._content("model", "prefill")
        with self.assertLogs(level="INFO") as cm:
            _, _response = self._run("gemini-4-argon", single)
        self.assertTrue(
            any("已追加 user turn 修复 prefill 兼容" in line for line in cm.output),
            msg=f"追加日志未出现: {cm.output}",
        )

    def test_disabled_switch_disables_fix(self):
        self.settings.PREFILL_FIX = False
        contents, _ = self._run(
            "gemini-3.8-flash",
            [
                self._content("user", "hi"),
                self._content("model", "prefill"),
            ],
        )
        self.assertEqual(len(contents), 2)
        self.assertEqual(contents[-1].role, "model")

    def test_custom_prompt_is_used(self):
        self.settings.PREFILL_FIX_PROMPT = "从停下的地方继续"
        contents, _ = self._run(
            "gemini-3.8-flash",
            [
                self._content("user", "hi"),
                self._content("model", "prefill"),
            ],
        )
        self.assertEqual(contents[-1].parts[0].text, "从停下的地方继续")

    def test_latest_alias_is_covered(self):
        contents, _ = self._run(
            "gemini-pro-latest",
            [
                self._content("user", "hi"),
                self._content("model", "prefill"),
            ],
        )
        self.assertEqual(contents[-1].role, "user")

    def test_non_stream_path_also_appends(self):
        # stream=False 分支的追加同样生效（本用例走非流分支的
        # 前置逻辑；fake client 之前的所有步骤都不触达网络）。
        # 注：非流分支会真正调用 client —— 用会抛错的哨兵 client
        # 断言「追加已发生 + 未追加场景差异」的成本过高，此处仅
        # 验证 stream=False 时追加逻辑不抛异常（genai types 构造兼容）。
        contents = [
            self._content("user", "hi"),
            self._content("model", "prefill"),
        ]

        class _Boom:
            def __getattr__(self, name):
                raise AssertionError("client should not be touched in this test")

        prompt_func = lambda messages: contents  # noqa: E731
        request_obj = SimpleNamespace(
            stream=False, model="gemini-3.8-flash", messages=[]
        )
        try:
            asyncio.run(
                self.ah.execute_gemini_call(
                    _Boom(),
                    "gemini-3.8-flash",
                    prompt_func,
                    {},
                    request_obj,
                    is_auto_attempt=False,
                )
            )
        except AssertionError:
            # _Boom 被触达说明非流分支已进入 client 调用 —— 但追加
            # 发生在 client 调用之前，contents 已应被修改。
            pass
        except Exception:
            # 其他异常（fake client 缺方法等）同理：追加先于调用发生
            pass
        self.assertEqual(len(contents), 3)
        self.assertEqual(contents[-1].role, "user")


if __name__ == "__main__":
    unittest.main()
