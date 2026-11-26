"""图构建。

**多 Agent 结构**：

```text
START → classify ─┬─(benefit_qa)────→ benefit_rag ────┐
                  ├─(order_status)──→ order_flow ─────┤
                  ├─(change_request)→ refund_approval ─→ refund_wait ─┤
                  ├─(complaint)─────→ complaint ──────┤
                  └─(unknown)───────→ clarify ────────┘
                                                        ↓
                                                    finalize → END
```

四个业务节点内部各自跑一个 ``create_react_agent`` 子图（见 ``agents.py``），
**每个子图有自己的提示词、互斥的工具集、自己的 think-act-observe 循环** ——
这是「多 Agent」和「带路由的 RAG」的分界线。

路由（classify）只决定**进哪个 Agent**，进去之后不再干预它怎么查、查几次。

**为什么 refund 拆两段**：``interrupt()`` 恢复时节点函数从头重跑，
Agent 和 interrupt 放同一个节点会导致第二遍建审批单（脏数据）。
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.graph.nodes.benefit import benefit_node
from app.graph.nodes.clarify import clarify_node
from app.graph.nodes.classify import classify_node, route_by_intent
from app.graph.nodes.complaint import complaint_node
from app.graph.nodes.finalize import finalize_node
from app.graph.nodes.order import order_node
from app.graph.nodes.refund import refund_approval_node, refund_wait_node
from app.graph.state import AgentState

#: 直接汇入 finalize 的分支节点（refund_approval 要先经过 refund_wait）
DIRECT_NODES = ("benefit_rag", "order_flow", "complaint", "clarify")


def build_graph(checkpointer=None):
    g = StateGraph(AgentState)

    g.add_node("classify", classify_node)
    g.add_node("benefit_rag", benefit_node)
    g.add_node("order_flow", order_node)
    g.add_node("refund_approval", refund_approval_node)
    g.add_node("refund_wait", refund_wait_node)
    g.add_node("complaint", complaint_node)
    g.add_node("clarify", clarify_node)
    g.add_node("finalize", finalize_node)

    g.add_edge(START, "classify")
    g.add_conditional_edges(
        "classify",
        route_by_intent,
        {
            "benefit_qa": "benefit_rag",
            "order_status": "order_flow",
            "change_request": "refund_approval",
            "complaint": "complaint",
            "unknown": "clarify",
        },
    )
    for node in DIRECT_NODES:
        g.add_edge(node, "finalize")

    # 退款这条链路多一跳：Agent 那段 → 挂起/执行那段
    g.add_edge("refund_approval", "refund_wait")
    g.add_edge("refund_wait", "finalize")
    g.add_edge("finalize", END)

    return g.compile(checkpointer=checkpointer)


_graph = None


async def get_graph():
    """全局单例。首次调用时初始化 checkpointer。"""
    global _graph
    if _graph is None:
        from app.graph.checkpointer import get_checkpointer

        checkpointer = await get_checkpointer()
        _graph = build_graph(checkpointer)
    return _graph


def reset_graph() -> None:
    """测试用：清掉单例，下次 get_graph 重新编译。"""
    global _graph
    _graph = None
