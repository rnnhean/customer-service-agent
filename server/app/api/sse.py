"""AI SDK UI message stream 帧生成器。

协议实测自 ai@7.0.163：
- 必须有 start（带 messageId）和 finish
- text 三帧必须成套，id 一致
- 自定义数据帧 type 必须以 "data-" 开头
"""

from __future__ import annotations

import json
import uuid
from typing import Any


class ChunkWriter:
    """生成 AI SDK 兼容的 SSE 帧。

    用法：
        w = ChunkWriter()
        yield w.start()
        yield w.text_delta("你", text_id)
        ...
        yield w.finish()
    """

    def __init__(self, message_id: str | None = None) -> None:
        self.message_id = message_id or f"m-{uuid.uuid4().hex[:12]}"
        self._text_id: str | None = None
        self._text_open = False
        self._seq = 0

    def _frame(self, payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False)

    # ---- 生命周期 ----

    def start(self) -> str:
        return self._frame({"type": "start", "messageId": self.message_id})

    def finish(self) -> str:
        # 文本块没关就先关掉，否则前端会丢内容
        parts = []
        if self._text_open:
            parts.append(self.text_end())
        parts.append(self._frame({"type": "finish"}))
        return "\n\n".join(parts)

    def done(self) -> str:
        return "[DONE]"

    # ---- 文本 ----

    def text_start(self, text_id: str | None = None) -> str:
        self._seq += 1
        self._text_id = text_id or f"t-{self._seq}"
        self._text_open = True
        return self._frame({"type": "text-start", "id": self._text_id})

    def text_delta(self, delta: str) -> str:
        if not self._text_open:
            raise RuntimeError("text_delta 之前必须先 text_start")
        return self._frame({"type": "text-delta", "id": self._text_id, "delta": delta})

    def text_end(self) -> str:
        if not self._text_open or self._text_id is None:
            return ""
        self._text_open = False
        return self._frame({"type": "text-end", "id": self._text_id})

    # ---- 自定义数据 ----

    def data(self, name: str, payload: dict[str, Any]) -> str:
        """name 不能带 data- 前缀，这里自动加。"""
        if name.startswith("data-"):
            name = name[5:]
        return self._frame({"type": f"data-{name}", "data": payload})

    def node(self, node_name: str, status: str, **extra: Any) -> str:
        return self.data("node", {"name": node_name, "status": status, **extra})

    def approval(self, payload: dict[str, Any]) -> str:
        return self.data("approval", payload)

    def answer(self, payload: dict[str, Any]) -> str:
        """最终答复（权威版本）。

        流式 delta 只是「过程的观感」，经过引用/数字校验后可能被改写，
        也可能整段是代码生成的降级话术（根本没有 LLM 输出）。前端收到这一帧
        后应以 ``text`` 为准覆盖当前气泡内容。
        """
        return self.data("answer", payload)

    # ---- 错误 ----

    def error(self, text: str) -> str:
        return self._frame({"type": "error", "errorText": text})
