"""Normalize fragmented Chat tool metadata before protocol-specific SSE rendering."""
from dataclasses import dataclass, field

from app.utils.stealth import gen_openai_tool_call_id


@dataclass
class _Tool:
    call_id: str = ""
    name: str = ""
    pending: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    started: bool = False


class ToolDeltaNormalizer:
    """Keep identity stable; buffer only until a name and ID are known."""
    def __init__(self):
        self.tools: dict[int, _Tool] = {}

    def feed(self, calls):
        if not isinstance(calls, list):
            raise ValueError("tool_calls must be an array")
        result = []
        for position, call in enumerate(calls):
            if not isinstance(call, dict):
                raise ValueError("Invalid tool delta")
            index = call.get("index", position)
            if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                raise ValueError("Invalid tool index")
            tool = self.tools.setdefault(index, _Tool())
            function = call.get("function") or {}
            if not isinstance(function, dict):
                raise ValueError("Invalid function delta")
            call_id, name = call.get("id"), function.get("name")
            if call_id:
                if not isinstance(call_id, str) or (tool.call_id and call_id != tool.call_id):
                    raise ValueError("Tool identity changed")
                tool.call_id = call_id
            if name:
                if not isinstance(name, str):
                    raise ValueError("Invalid function name")
                if tool.started and name != tool.name:
                    raise ValueError("Function name changed after arguments started")
                if name != tool.name:
                    tool.name += name
            extra = call.get("extra_content")
            if extra is not None:
                if not isinstance(extra, dict) or not isinstance(extra.get("google", {}), dict):
                    raise ValueError("Invalid tool metadata")
                tool.extra.update(extra)
            arguments = function.get("arguments")
            if arguments is not None:
                if not isinstance(arguments, str):
                    raise ValueError("Tool argument deltas must be strings")
                tool.pending.append(arguments)
            if tool.name and tool.call_id and (arguments is not None or tool.started):
                result.append(self._emit(index, tool))
        return result

    @staticmethod
    def _emit(index, tool):
        delta = {"index": index, "function": {"arguments": "".join(tool.pending)}}
        tool.pending.clear()
        if not tool.started:
            delta.update(id=tool.call_id, type="function")
            delta["function"]["name"] = tool.name
            tool.started = True
        if tool.extra:
            delta["extra_content"] = dict(tool.extra)
        return delta

    def finish(self):
        result = []
        for index, tool in self.tools.items():
            if not tool.name:
                raise ValueError("Tool call ended without a function name")
            if not tool.started:
                tool.call_id = tool.call_id or gen_openai_tool_call_id()
                if not tool.pending:
                    tool.pending.append("{}")
                result.append(self._emit(index, tool))
        return result
