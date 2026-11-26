"""澄清节点。意图不明确时问一个能推进的问题。"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from app.graph.context import assemble_messages
from app.graph.prompts import CLARIFY_PROMPT
from app.graph.state import AgentState
from app.llm import get_llm

DEFAULT_CLARIFY = (
    "想确认一下您的需求：是查订单进度、了解会员权益，还是要办理退款？"
)


async def clarify_node(state: AgentState) -> dict:
    history = state.get("messages") or []

    prompt_messages = assemble_messages(
        system_prompt=CLARIFY_PROMPT,
        history=history,
        retrieved=None,
        profile=None,
    )

    try:
        response = await get_llm(temperature=0.3).ainvoke(prompt_messages)
        answer = response.content if isinstance(response.content, str) else str(response.content)
        # 澄清话术要短，模型偶尔会啰嗦
        if len(answer) > 120:
            answer = DEFAULT_CLARIFY
    except Exception:
        answer = DEFAULT_CLARIFY

    return {"messages": [AIMessage(answer)]}
