"""Prefill 回显剥离（响应侧过滤）。

背景：请求侧 prefill 补丁（见 app/utils/model_mapping.py）在末尾
model turn 之后追加了一条 user 续写指令。新世代模型收到这种形态
时有相当概率先把 prefill 原文复述（回显）一遍、再接续写内容——
客户端拿到的文本就成了「prefill + prefill + 续写」，拼接后内容
重复。本模块在响应侧用跨 chunk 状态机精确剥离回显的前缀：

  * 仅在输出仍与 prefill 前缀逐字匹配期间暂存文本（此时无法区分
    「回显」与「恰好相同开头的新内容」），一旦失配立即放行全部
    暂存——正常输出首 chunk 即释放，零额外延迟；
  * 前缀完整匹配完成时剥离之，后续输出原样透传（decided）；
  * 流在未决状态中途结束（无 finishReason 帧）时，暂存的未决前缀
    按「真实内容」原样释放——能完整走完的前缀才会被剥离，绝不
    吞掉合法输出；
  * thought 思考摘要、工具调用、媒体 part 一概不动，只过滤普通
    文本 part；多候选（candidateCount>1）按 candidate index 各自
    独立维护状态。

状态机语义与社区实现 vertex-master（internal/transform/prefill.go）
逐分支对齐，行为等价。

ContextVar 桥：请求侧（convert_messages / 原生 payload 路径）在同一
async 任务内记录 prefill 原文，响应侧（stream_chat / complete_chat）
读取——FastAPI 的请求处理与响应流式发送共享任务上下文（或其拷贝），
ContextVar 天然按请求隔离且对重试循环安全（每次调用新建过滤器）。
"""

from __future__ import annotations

import contextvars
from typing import Dict, List, Optional, Tuple

# Gemini finishReason 的"未指定"哨兵：出现真实结束原因（STOP/MAX_TOKENS/...）
# 才触发该候选的最终释放。
_FINISH_REASON_UNSPECIFIED = "FINISH_REASON_UNSPECIFIED"

# candidate index 合法上限（防脏数据炸内存）。
_MAX_CANDIDATE_INDEX = 1 << 30


# ---------------------------------------------------------------------------
# ContextVar 桥（Gemini API 路径：请求转换层 → 响应层）
# ---------------------------------------------------------------------------

_current_prefill_echo: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "lajdnsma_prefill_echo", default=None
)


def set_current_prefill_echo(prefill: Optional[str]) -> None:
    """记录当前请求的 prefill 原文（None = 本请求未命中 prefill 补丁）。

    请求侧每个请求必须显式调用（含 None），避免同任务内前一个请求
    的值泄漏到下一个请求（测试场景尤甚）。
    """
    _current_prefill_echo.set(prefill)


def get_current_prefill_echo() -> Optional[str]:
    return _current_prefill_echo.get()


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------

class _CandidateState:
    """单个候选的匹配状态：matched = 已确认匹配的前缀字符数。"""

    __slots__ = ("matched", "decided")

    def __init__(self) -> None:
        self.matched = 0
        self.decided = False


class PrefillEchoStreamFilter:
    """跨 chunk 剥离被模型回显的 prefill 前缀。

    用法（Gemini API 原生 dict 形态的响应）::

        filt = PrefillEchoStreamFilter(prefill_text)
        for chunk in sse_chunks:
            filt.filter_gemini_chunk(chunk)      # 原地改写普通文本 part
        tail = filt.finalize_gemini()             # 流结束仍未决 → 释放
        if tail:
            forward(build_tail_chunk(tail))

    单候选文本流（Vertex 等已抽出纯文本的路径）直接用
    filter_text(text, final) / finalize_text()。
    """

    def __init__(self, prefill: str) -> None:
        self.prefill = prefill or ""
        self._primary = _CandidateState()
        self._primary_seen = False
        self._others: Dict[int, _CandidateState] = {}
        self._other_order: List[int] = []

    # -- 核心状态机 ---------------------------------------------------------

    def _state(self, index: int) -> _CandidateState:
        if index == 0:
            self._primary_seen = True
            return self._primary
        state = self._others.get(index)
        if state is None:
            state = _CandidateState()
            self._others[index] = state
            self._other_order.append(index)
        return state

    def filter_text(self, text: str, final: bool = False, index: int = 0) -> str:
        """过滤一段文本：返回应向外转发的内容。

        * 完成前缀：返回去除前缀后的尾部；
        * 前缀未完成即失配：返回此前暂存 + 本段（回滚放行）；
        * 本段整体仍是前缀的一部分：暂存，返回 ""（等后续 chunk 决断）；
          final=True 时（结束帧/一次性场景）按真实内容释放暂存。
        """
        if not self.prefill:
            return text

        state = self._state(index)
        if state.decided:
            return text

        previously_matched = state.matched
        remaining = self.prefill[previously_matched:]
        matched_now = _common_prefix_length(text, remaining)

        if matched_now == len(remaining):
            # 本段把剩余前缀补完：完整回显确认，剥离之。
            state.matched += matched_now
            state.decided = True
            return text[matched_now:]

        if matched_now < len(text):
            # 前缀尚未补完就出现差异：此前暂存的一定等于前缀开头，
            # 属于正常输出，连同本段一并放行（无需逐 chunk 缓存）。
            out = self.prefill[:previously_matched] + text if previously_matched else text
            state.matched = 0
            state.decided = True
            return out

        # 本段全部匹配、但完整前缀还没结束：继续暂存。
        state.matched += matched_now
        if not final:
            return ""
        out = self.prefill[: state.matched]
        state.matched = 0
        state.decided = True
        return out

    def finalize_text(self, index: int = 0) -> str:
        """单候选便利方法：流结束时释放仍处于未决状态的暂存前缀。"""
        return self.filter_text("", final=True, index=index)

    # -- Gemini dict 形态的整 chunk 过滤 ------------------------------------

    def filter_gemini_chunk(self, chunk: dict) -> None:
        """原地过滤一个流式 GenerateContentResponse dict。

        只动 candidates[].content.parts[] 中 thought 为假且含非空 text
        的普通文本 part；遇到真实 finishReason 时做该候选的最终释放，
        释放文本以追加的普通文本 part 挂回同一 chunk（不产生额外帧）。
        """
        if not self.prefill or not isinstance(chunk, dict):
            return
        candidates = chunk.get("candidates")
        if not isinstance(candidates, list):
            return
        for position, raw_candidate in enumerate(candidates):
            candidate = raw_candidate if isinstance(raw_candidate, dict) else None
            if candidate is None:
                continue
            index = _candidate_filter_index(candidate, position)
            content = candidate.get("content")
            if not isinstance(content, dict):
                content = {}
            parts = content.get("parts")
            if not isinstance(parts, list):
                parts = []
            for raw_part in parts:
                part = raw_part if isinstance(raw_part, dict) else None
                if part is None or part.get("thought"):
                    continue
                text = part.get("text")
                if isinstance(text, str) and text:
                    part["text"] = self.filter_text(text, final=False, index=index)

            finish = candidate.get("finishReason")
            if finish and finish != _FINISH_REASON_UNSPECIFIED:
                tail = self.filter_text("", final=True, index=index)
                if tail:
                    _append_ordinary_text(candidate, tail)

    def filter_complete_response(self, data: dict) -> None:
        """一次性过滤完整（非流式）响应 dict：逐候选剥离 + 最终释放。"""
        if not self.prefill or not isinstance(data, dict):
            return
        candidates = data.get("candidates")
        if not isinstance(candidates, list):
            return
        for position, raw_candidate in enumerate(candidates):
            candidate = raw_candidate if isinstance(raw_candidate, dict) else None
            if candidate is None:
                continue
            index = _candidate_filter_index(candidate, position)
            content = candidate.get("content")
            if not isinstance(content, dict):
                continue
            parts = content.get("parts")
            if not isinstance(parts, list):
                continue
            for raw_part in parts:
                part = raw_part if isinstance(raw_part, dict) else None
                if part is None or part.get("thought"):
                    continue
                text = part.get("text")
                if isinstance(text, str) and text:
                    part["text"] = self.filter_text(text, final=False, index=index)
            # 完整响应即最终态：释放该候选可能暂存的未决前缀。
            tail = self.filter_text("", final=True, index=index)
            if tail:
                _append_ordinary_text(candidate, tail)

    def finalize_gemini(self) -> List[Tuple[int, str]]:
        """流整体结束时（无 finishReason 帧）逐候选释放未决暂存。

        返回 [(candidate_index, text)]，候选 0 在前、其余按首次出现
        顺序，保证合成帧的确定性。
        """
        if not self.prefill:
            return []
        tails: List[Tuple[int, str]] = []
        if self._primary_seen:
            text = self.filter_text("", final=True, index=0)
            if text:
                tails.append((0, text))
        for index in self._other_order:
            text = self.filter_text("", final=True, index=index)
            if text:
                tails.append((index, text))
        return tails


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _common_prefix_length(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    for i in range(limit):
        if left[i] != right[i]:
            return i
    return limit


def _candidate_filter_index(candidate: dict, fallback: int) -> int:
    index = candidate.get("index")
    if isinstance(index, bool):
        return fallback
    if isinstance(index, int) and 0 <= index <= _MAX_CANDIDATE_INDEX:
        return index
    if (
        isinstance(index, float)
        and index.is_integer()
        and 0 <= index <= _MAX_CANDIDATE_INDEX
    ):
        return int(index)
    return fallback


def _append_ordinary_text(candidate: dict, text: str) -> None:
    """把释放的文本以普通文本 part 追加到候选的 content.parts 末尾。"""
    if not text:
        return
    content = candidate.get("content")
    if not isinstance(content, dict):
        content = {"role": "model"}
        candidate["content"] = content
    parts = content.get("parts")
    if not isinstance(parts, list):
        parts = []
        content["parts"] = parts
    parts.append({"text": text})


def build_tail_chunk(tails: List[Tuple[int, str]]) -> Optional[dict]:
    """把 finalize 释放的 [(index, text)] 合成一个新的最小流式 chunk。

    仅用于上游流关闭却仍存在未决暂存的罕见场景（连接中断等）；
    形态与普通流式 chunk 一致（无 finishReason），下游按普通增量处理。
    """
    if not tails:
        return None
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": text}]},
                "index": index,
            }
            for index, text in tails
        ]
    }
