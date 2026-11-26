"""流式对话接口。"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from langchain_core.messages import AIMessageChunk, HumanMessage
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.api.sse import ChunkWriter
from app.graph.build import get_graph
from app.obs.langfuse import get_langfuse_handler

logger = logging.getLogger("app.chat")

router = APIRouter(prefix="/api/chat", tags=["chat"])

#: 只有这些节点的 LLM 输出才推进给用户。
#: classify / finalize 这类节点的输出是内部结构（Route JSON、摘要等），
#: 混进正文会让用户看到 `{"intent": "benefit_qa"...}` / `{"confidence":...}` 这种碎片。
#: refund_wait 里没有 LLM 输出，加不加都不影响，列全是为了和 build.py 的节点表对齐。
USER_FACING_NODES = frozenset(
    {
        "benefit_rag",
        "order_flow",
        "refund_approval",
        "refund_wait",
        "complaint",
        "clarify",
    }
)


class ChatRequest(BaseModel):
    thread_id: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=2000)
    member_id: str | None = None


def _accumulate_text(current: str, chunk: AIMessageChunk) -> str:
    text = chunk.text if hasattr(chunk, "text") else ""
    return current + (text or "")


@router.post("/stream")
async def chat_stream(req: ChatRequest, request: Request):
    """SSE 流式对话。

    帧序列：start → (data-node | text-* | data-approval)* → finish → [DONE]
    """

    async def gen():
        writer = ChunkWriter()
        yield writer.start()

        graph = await get_graph()
        config: dict[str, Any] = {
            "configurable": {"thread_id": req.thread_id},
        }
        handler = get_langfuse_handler()
        if handler is not None:
            config["callbacks"] = [handler]

        accumulated = ""
        text_started = False
        pending_approval: dict[str, Any] | None = None

        try:
            async for mode, chunk in graph.astream(
                {
                    "messages": [HumanMessage(req.content)],
                    "thread_id": req.thread_id,
                    "member_id": req.member_id,
                },
                config,
                stream_mode=["messages", "updates"],
            ):
                # 用户关页面就停，别继续烧 token
                if await request.is_disconnected():
                    return

                if mode == "messages":
                    msg, meta = chunk
                    node = (meta or {}).get("langgraph_node")
                    if node not in USER_FACING_NODES:
                        # 内部节点的结构化输出（路由决策等）不推给用户
                        continue
                    if isinstance(msg, AIMessageChunk):
                        text = msg.text if hasattr(msg, "text") else ""
                        if text:
                            if not text_started:
                                yield writer.text_start()
                                text_started = True
                            accumulated += text
                            yield writer.text_delta(text)
                    continue

                # mode == "updates"
                if not isinstance(chunk, dict):
                    continue
                for node_name, update in chunk.items():
                    if node_name == "__interrupt__":
                        pending_approval = _extract_interrupt(update)
                        continue
                    if not isinstance(update, dict):
                        continue
                    yield writer.node(node_name, "done")

                    intr = update.get("__interrupt__")
                    if intr:
                        pending_approval = _extract_interrupt(intr)

        except Exception as e:
            logger.exception("对话流异常 thread=%s", req.thread_id)
            yield writer.error(f"对话服务异常：{type(e).__name__}")
        finally:
            # 文本还没关掉的话收尾
            if text_started:
                yield writer.text_end()

            # 权威答复：流式 delta 只是过程中的观感，节点里可能有后置校验改写，
            # 也可能整段是代码生成的降级话术（根本没有 LLM 输出）。前端以这一帧为准。
            if not pending_approval:
                final = await _final_answer(graph, config)
                if final["text"]:
                    yield writer.answer(final)

            # 中断场景：把审批信息推给前端
            if pending_approval:
                yield writer.approval(pending_approval)

            yield writer.finish()
            yield writer.done()

    return EventSourceResponse(gen(), ping=15)


async def _final_answer(graph: Any, config: dict[str, Any]) -> dict[str, Any]:
    """取本轮最终 AI 消息，附带降级 / 引用信息。拿不到就返回空文本。"""
    try:
        state = await graph.aget_state(config)
        values = (state.values or {}) if state else {}
    except Exception:
        logger.exception("读取最终状态失败")
        return {"text": "", "degraded": False, "citations": []}

    messages = values.get("messages") or []
    text = ""
    for m in reversed(messages):
        if getattr(m, "type", "") in ("ai", "assistant"):
            content = m.content
            text = content if isinstance(content, str) else str(content)
            break

    return {
        "text": text,
        "degraded": bool(values.get("degraded")),
        "citations": values.get("citations") or [],
        "intent": values.get("intent"),
    }


def _extract_interrupt(intr: Any) -> dict[str, Any] | None:
    """从 LangGraph 的 __interrupt__ 里取出我们 push 的 payload。"""
    try:
        items = intr if isinstance(intr, (list, tuple)) else [intr]
        for item in items:
            value = getattr(item, "value", None)
            if isinstance(value, dict) and value.get("type") == "approval_required":
                return value
    except Exception:
        return None
    return None


@router.get("/health")
async def health() -> dict[str, Any]:
    from app.llm import llm_ready

    return {"ok": True, "llm_configured": llm_ready()}
