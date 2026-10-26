"""兼容导出：协议各自维护事件状态，共用 protocol_common 的上游生命周期。"""
from app.utils.responses_stream import openai_stream_to_responses_stream
from app.utils.anthropic_stream import openai_stream_to_claude_stream

__all__ = ["openai_stream_to_responses_stream", "openai_stream_to_claude_stream"]
