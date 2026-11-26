"""工具集统一出口。

**工具集互斥是多 Agent 成立的核心条件之一。**
如果四个 Agent 共用一套工具，那它就只是同一个 Agent 换了提示词——面试时会被问穿。
每类 Agent 只能看到自己那组工具。
"""

from __future__ import annotations

from app.tools.approval_tools import REFUND_TOOLS
from app.tools.benefit_tools import BENEFIT_TOOLS
from app.tools.complaint_tools import COMPLAINT_TOOLS
from app.tools.order_tools import ORDER_TOOLS

TOOLSETS: dict[str, list] = {
    "benefit_qa": BENEFIT_TOOLS,
    "order_status": ORDER_TOOLS,
    "change_request": REFUND_TOOLS,
    "complaint": COMPLAINT_TOOLS,
}

# 工具名 -> 所属 Agent，用于 trace 归因和权限校验
TOOL_OWNER: dict[str, str] = {
    t.name: agent for agent, tools in TOOLSETS.items() for t in tools
}

ALL_TOOLS = [t for tools in TOOLSETS.values() for t in tools]


def tools_for(intent: str) -> list:
    return TOOLSETS.get(intent, [])


def owner_of(tool_name: str) -> str | None:
    return TOOL_OWNER.get(tool_name)
