"""生成演示用假数据（固定随机种子，结果可复现）。

规模：200 会员 / 200 会员状态 / 40 权益规则 / 400 订单 / 50 审批单 / 30 投诉单。

命令行用法::

    python -m app.db.seed            # 保留已有数据，仅补建表并插入
    python -m app.db.seed --reset    # 先清空业务表再重新灌数据
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import random
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from app.db.connection import get_conn, init_db

#: 固定种子：保证每次跑出来数据完全一致，方便做评测与截图
SEED = 20261106

#: 基准时间，所有时间字段都基于它加减，避免"今天跑和明天跑不一样"
BASE_TIME = datetime(2026, 11, 6, 9, 0, 0, tzinfo=UTC)

LEVELS: tuple[str, ...] = ("normal", "silver", "gold", "platinum")

#: 各等级权益规则（每等级 10 条，共 40 条）。name / value 使用真实客服文案。
BENEFIT_RULES: dict[str, list[tuple[str, str, int, int]]] = {
    "normal": [
        ("免运费券", "单笔满 99 元可用，每月 1 张", 1, 30),
        ("会员积分", "消费 1 元累计 1 积分，积分可抵现", 1, None),
        ("生日提醒", "生日当月推送专属祝福与优惠信息", 0, 365),
        ("新人礼包", "首单立减 10 元", 0, 30),
        ("评价返积分", "晒单评价返 20 积分", 1, 90),
        ("满减券", "满 199 减 15 元，每月可领 1 张", 1, 30),
        ("售后咨询", "每日 9:00-21:00 在线客服", 1, None),
        ("物流查询", "实时查看订单物流节点", 1, None),
        ("电子发票", "支持在线申请电子发票", 1, None),
        ("活动早知道", "促销活动提前 1 天推送", 1, None),
    ],
    "silver": [
        ("免运费券", "每月 3 张，单笔满 99 元可用", 1, 30),
        ("专属客服", "工作日 9:00-21:00 人工优先接入", 1, None),
        ("生日礼包", "生日当月赠送 50 元无门槛券", 0, 30),
        ("积分加速", "消费累计积分上浮 20%", 0, None),
        ("免费退换", "享受 7 天无理由退换", 1, 90),
        ("满减券", "满 199 减 25 元，每月可领 2 张", 1, 30),
        ("新品优先购", "新品上架提前 24 小时购买", 0, None),
        ("换货免运费", "换货往返运费由平台承担", 1, 180),
        ("工单优先处理", "售后工单 24 小时内响应", 1, None),
        ("会员日双倍积分", "每月 8 日消费积分翻倍", 0, None),
    ],
    "gold": [
        ("免运费券", "每月 3 张，单笔满 99 元可用", 1, 30),
        ("专属客服", "7x24 小时专属客服通道", 1, None),
        ("生日礼包", "生日当月赠送 200 元无门槛券", 0, 30),
        ("优先发货", "下单后 2 小时内出库", 1, None),
        ("免费退换", "享受 15 天无理由退换", 1, 90),
        ("积分加速", "消费累计积分上浮 50%", 0, None),
        ("极速退款", "审核通过后 2 小时内到账", 1, None),
        ("免费包装", "每单可免费选择礼盒包装", 1, None),
        ("满减券", "满 299 减 50 元，每月可领 2 张", 1, 30),
        ("专属活动", "不定期参与会员专属内购会", 0, None),
    ],
    "platinum": [
        ("免运费券", "不限张数，单笔满 59 元即可使用", 1, 30),
        ("专属客服", "7x24 小时专属客服通道，10 秒内接入", 1, None),
        ("生日礼包", "生日当月赠送 500 元无门槛券", 0, 30),
        ("优先发货", "下单后 1 小时内出库，顺丰优先", 1, None),
        ("免费退换", "享受 30 天无理由退换，退货运费全免", 1, 90),
        ("积分加速", "消费累计积分翻倍", 0, None),
        ("极速退款", "审核通过后 30 分钟内到账", 1, None),
        ("专属管家", "一对一会员管家，提供选品与售后建议", 0, None),
        ("满减券", "满 399 减 100 元，每月可领 3 张", 1, 30),
        ("新品体验官", "每月可申请 1 次新品免费试用", 0, 30),
    ],
}

#: 会员状态分布：active 60% / expired 25% / canceling 10% / inactive 5%
STATUS_DISTRIBUTION: tuple[tuple[str, int], ...] = (
    ("active", 120),
    ("expired", 50),
    ("canceling", 20),
    ("inactive", 10),
)

#: 业务枚举字典 —— **全项目唯一的枚举定义处**。
#:
#: 以前这些值在三个地方各写了一份：造数脚本、工具的白名单、提示词。
#: 结果三方漂移：库里投诉分类是「物流延迟」，工具白名单写的是「物流问题」，
#: Agent 照着报告传了库里真实存在的值，反被工具以「分类不合法」拒掉。
#: 现在只有这里定义，工具和提示词一律运行时从 lookups 表读。
#:
#: 字段：kind / code / label / sort / fallback
LOOKUPS: tuple[tuple[str, str, str, int, bool], ...] = (
    # ---- 订单状态（code 是落库的英文枚举，label 是给用户看的中文）----
    ("order_status", "PENDING", "待支付", 10, False),
    ("order_status", "EFFECTIVE", "已生效", 20, False),
    ("order_status", "REFUNDING", "退款处理中", 30, False),
    ("order_status", "REFUNDED", "已退款", 40, False),
    ("order_status", "CLOSED", "已关闭", 50, False),
    # ---- 订单类型（库里存的就是中文，不需要再翻译）----
    ("order_type", "会员开通", "会员开通", 10, False),
    ("order_type", "会员续费", "会员续费", 20, False),
    ("order_type", "等级升级", "等级升级", 30, False),
    ("order_type", "权益包购买", "权益包购买", 40, False),
    ("order_type", "单次服务购买", "单次服务购买", 50, False),
    # ---- 会员等级 ----
    ("member_level", "normal", "普通会员", 10, False),
    ("member_level", "silver", "白银会员", 20, False),
    ("member_level", "gold", "黄金会员", 30, False),
    ("member_level", "platinum", "铂金会员", 40, False),
    # ---- 投诉分类。「其他」是 fallback：Agent 传了字典外的分类时归到这里，
    #      既不丢真实诉求，也不会让分类无限发散 ----
    ("complaint_category", "物流延迟", "物流延迟", 10, False),
    ("complaint_category", "商品质量问题", "商品质量问题", 20, False),
    ("complaint_category", "退款未到账", "退款未到账", 30, False),
    ("complaint_category", "客服态度", "客服态度", 40, False),
    ("complaint_category", "服务态度", "服务态度", 50, False),
    ("complaint_category", "会员权益未生效", "会员权益未生效", 60, False),
    ("complaint_category", "发票问题", "发票问题", 70, False),
    ("complaint_category", "其他", "其他", 999, True),
    # ---- 审批状态 / 类型 ----
    ("approval_status", "pending", "待审批", 10, False),
    ("approval_status", "approved", "已通过", 20, False),
    ("approval_status", "rejected", "已驳回", 30, False),
    ("approval_status", "expired", "已过期", 40, False),
    ("approval_type", "refund", "退款", 10, False),
    ("approval_type", "compensation", "补偿", 20, False),
    ("approval_type", "coupon_grant", "补发优惠券", 30, False),
    ("approval_type", "address_change", "修改收货地址", 40, False),
)


def lookup_codes(kind: str) -> tuple[str, ...]:
    """取某个 kind 下所有落库值（造数和测试用）。"""
    return tuple(code for k, code, _label, _sort, _fb in LOOKUPS if k == kind)


def lookup_label(kind: str, code: str) -> str:
    """取展示名，查不到就原样返回。"""
    for k, c, label, _sort, _fb in LOOKUPS:
        if k == kind and c == code:
            return label
    return code


#: 订单状态（必须全部覆盖到）
ORDER_STATUSES: tuple[str, ...] = lookup_codes("order_status")

#: 订单类型
ORDER_TYPES: tuple[str, ...] = lookup_codes("order_type")

#: 审批类型
APPROVAL_TYPES: tuple[str, ...] = lookup_codes("approval_type")

APPROVAL_REASONS: dict[str, str] = {
    "refund": "商品签收后 3 天内申请退款，商品未拆封",
    "compensation": "物流延误超过 48 小时，按平台规则补偿 50 元",
    "coupon_grant": "专属客服承诺补发一张满 199 减 50 优惠券",
    "address_change": "订单已支付未发货，申请修改收货地址",
}

APPROVERS: tuple[str, ...] = ("客服主管-李静", "客服主管-王强", "运营-赵敏", "运营-陈磊")

#: 投诉分类（「其他」是 fallback，造数时不主动用，留给 Agent 兜底归类）
COMPLAINT_CATEGORIES: tuple[str, ...] = tuple(
    code
    for kind, code, _label, _sort, fallback in LOOKUPS
    if kind == "complaint_category" and not fallback
)

COMPLAINT_STATUSES: tuple[str, ...] = ("open", "processing", "resolved")

#: 业务表清单（DROP 顺序按外键依赖从子到父排列）
BUSINESS_TABLES_DROP_ORDER: tuple[str, ...] = (
    "traces",
    "sessions",
    "member_profile",
    "complaints",
    "approvals",
    "orders",
    "benefits",
    "member_status",
    "members",
    "lookups",
)


def _iso(dt: datetime) -> datetime:
    """时间列直接传 datetime，让 psycopg 按 timestamptz 绑定。

    名字保留 ``_iso``，是为了不动下游所有调用点。
    """
    return dt


def _member_id(index: int) -> str:
    """生成会员 ID，形如 M0001。"""
    return f"M{index:04d}"


def _build_members(rng: random.Random) -> list[tuple[Any, ...]]:
    """构造 200 条会员：等级按 normal 50% / silver 30% / gold 15% / platinum 5% 分布。"""
    weights = (50, 30, 15, 5)
    rows: list[tuple[Any, ...]] = []
    for i in range(1, 201):
        level = rng.choices(LEVELS, weights=weights, k=1)[0]
        # 注册时间分布在过去两年内
        created = BASE_TIME - timedelta(days=rng.randint(30, 730), hours=rng.randint(0, 23))
        rows.append((_member_id(i), level, _iso(created)))
    return rows


def _build_member_statuses(
    rng: random.Random, members: list[tuple[Any, ...]]
) -> list[tuple[Any, ...]]:
    """构造 200 条会员状态，严格按 120/50/20/10 的数量分布并打乱顺序。"""
    statuses: list[str] = []
    for status, count in STATUS_DISTRIBUTION:
        statuses.extend([status] * count)
    rng.shuffle(statuses)

    rows: list[tuple[Any, ...]] = []
    for (member_id, _level, created_at), status in zip(members, statuses, strict=True):
        if isinstance(created_at, datetime):
            created = created_at
        else:
            created = datetime.fromisoformat(str(created_at))
        effective = created + timedelta(days=rng.randint(0, 3))
        if status in ("active", "canceling"):
            expire = BASE_TIME + timedelta(days=rng.randint(15, 400))
        elif status == "expired":
            expire = BASE_TIME - timedelta(days=rng.randint(1, 200))
        else:  # inactive：从未开通过，没有到期时间
            expire = None
        auto_renew = status == "active" and rng.random() < 0.45
        rows.append(
            (
                member_id,
                status,
                _iso(effective),
                _iso(expire) if expire else None,
                auto_renew,
            )
        )
    return rows


def _build_benefits() -> list[tuple[Any, ...]]:
    """构造 40 条权益规则：四个等级各 10 条。"""
    rows: list[tuple[Any, ...]] = []
    for level in LEVELS:
        for idx, (name, value, stackable, expire_days) in enumerate(BENEFIT_RULES[level], start=1):
            code = f"{level}_{idx:02d}"
            rows.append((code, level, name, value, bool(stackable), expire_days))
    return rows


def _build_orders(rng: random.Random, members: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    """构造 400 条订单，状态均分到 5 种，保证每种都出现。"""
    status_pool: list[str] = [ORDER_STATUSES[i % len(ORDER_STATUSES)] for i in range(400)]
    rng.shuffle(status_pool)

    amounts = (1900, 2900, 4900, 9900, 19900, 29900, 39900, 59800, 99800)
    rows: list[tuple[Any, ...]] = []
    for i in range(400):
        member_id = rng.choice(members)[0]
        order_type = rng.choice(ORDER_TYPES)
        status = status_pool[i]
        amount = rng.choice(amounts)
        created = BASE_TIME - timedelta(
            days=rng.randint(0, 400), hours=rng.randint(0, 23), minutes=rng.randint(0, 59)
        )
        updated = created + timedelta(days=rng.randint(0, 5), hours=rng.randint(1, 23))
        if updated > BASE_TIME:
            updated = BASE_TIME
        rows.append(
            (
                f"ORD{20260000 + i + 1:08d}",
                member_id,
                order_type,
                status,
                amount,
                _iso(created),
                _iso(updated),
            )
        )
    return rows


def _build_approvals(rng: random.Random, members: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    """构造 50 条审批单：已通过 25 / 已驳回 15 / 已过期 5 / 待审批 5。"""
    statuses: list[str] = ["approved"] * 25 + ["rejected"] * 15 + ["expired"] * 5 + ["pending"] * 5
    rng.shuffle(statuses)

    rows: list[tuple[Any, ...]] = []
    for i, status in enumerate(statuses, start=1):
        member_id = rng.choice(members)[0]
        apply_type = rng.choice(APPROVAL_TYPES)
        amount: int | None = rng.choice([2000, 5000, 9900, 19900, 29900])
        if apply_type == "address_change":
            amount = None
        created = BASE_TIME - timedelta(days=rng.randint(1, 120), hours=rng.randint(0, 23))
        expire = created + timedelta(hours=72)

        if status == "pending":
            # 待审批单必须尚未过期，方便演示列表与 CAS 审批
            created = BASE_TIME - timedelta(hours=rng.randint(1, 24))
            expire = BASE_TIME + timedelta(hours=rng.randint(24, 72))
            decided_at: str | None = None
            reviewer: str | None = None
            version = 0
        elif status == "expired":
            expire = BASE_TIME - timedelta(hours=rng.randint(1, 48))
            decided_at = None
            reviewer = None
            version = 0
        else:
            decided_at = _iso(created + timedelta(hours=rng.randint(1, 24)))
            reviewer = rng.choice(APPROVERS)
            version = 1

        rows.append(
            (
                f"AP{20260000 + i:08d}",
                f"thread-{i:04d}",
                member_id,
                apply_type,
                amount,
                APPROVAL_REASONS[apply_type],
                status,
                version,
                reviewer,
                _iso(created),
                decided_at,
                _iso(expire),
            )
        )
    return rows


def _build_complaints(rng: random.Random, members: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    """构造 30 条投诉工单，优先级 1~3，状态覆盖 open / processing / resolved。"""
    statuses: list[str] = (["open"] * 12) + (["processing"] * 10) + (["resolved"] * 8)
    rng.shuffle(statuses)

    rows: list[tuple[Any, ...]] = []
    for i, status in enumerate(statuses, start=1):
        member_id = rng.choice(members)[0]
        category = rng.choice(COMPLAINT_CATEGORIES)
        priority = rng.choices((1, 2, 3), weights=(20, 50, 30), k=1)[0]
        # SLA：优先级越高，时限越短
        sla_hours = {1: 4, 2: 24, 3: 48}[priority]
        created = BASE_TIME - timedelta(hours=rng.randint(1, 200))
        sla_deadline = created + timedelta(hours=sla_hours)
        rows.append(
            (
                f"TK{20260000 + i:08d}",
                member_id,
                category,
                priority,
                _iso(sla_deadline),
                status,
            )
        )
    return rows


async def _drop_business_tables() -> None:
    """删除全部业务表（不动 LangGraph 的 checkpoint / writes 表）。"""
    async with get_conn() as conn:
        for table in BUSINESS_TABLES_DROP_ORDER:
            await conn.execute(f"DROP TABLE IF EXISTS {table}")
        await conn.commit()


async def seed_all(reset: bool = False) -> dict[str, int]:
    """灌入全部演示数据，返回每张表实际插入的行数。

    ``reset=True`` 时先删除业务表再按 ``schema.sql`` 重建，
    LangGraph checkpoint 相关的表不会被触碰。
    """
    if reset:
        await _drop_business_tables()
    await init_db()

    rng = random.Random(SEED)
    members = _build_members(rng)
    statuses = _build_member_statuses(rng, members)
    benefits = _build_benefits()
    orders = _build_orders(rng, members)
    approvals = _build_approvals(rng, members)
    complaints = _build_complaints(rng, members)

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            # 字典先灌：工具和提示词都要在运行时读它，缺了业务就跑不起来
            await cur.executemany(
                """
                INSERT INTO lookups (kind, code, label, sort, active, fallback)
                VALUES (%s, %s, %s, %s, true, %s)
                ON CONFLICT (kind, code) DO UPDATE
                   SET label = EXCLUDED.label,
                       sort = EXCLUDED.sort,
                       active = true,
                       fallback = EXCLUDED.fallback
                """,
                LOOKUPS,
            )
            await cur.executemany(
                """
                INSERT INTO members (member_id, level, created_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (member_id) DO UPDATE
                   SET level = EXCLUDED.level, created_at = EXCLUDED.created_at
                """,
                members,
            )
            await cur.executemany(
                """
                INSERT INTO member_status
                    (member_id, status, effective_at, expire_at, auto_renew)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (member_id) DO UPDATE
                   SET status = EXCLUDED.status,
                       effective_at = EXCLUDED.effective_at,
                       expire_at = EXCLUDED.expire_at,
                       auto_renew = EXCLUDED.auto_renew
                """,
                statuses,
            )
            await cur.executemany(
                """
                INSERT INTO benefits
                    (code, level, name, value, stackable, expire_days)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (code) DO UPDATE
                   SET level = EXCLUDED.level, name = EXCLUDED.name,
                       value = EXCLUDED.value, stackable = EXCLUDED.stackable,
                       expire_days = EXCLUDED.expire_days
                """,
                benefits,
            )
            await cur.executemany(
                """
                INSERT INTO orders
                    (order_id, member_id, type, status, amount, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (order_id) DO UPDATE
                   SET member_id = EXCLUDED.member_id, type = EXCLUDED.type,
                       status = EXCLUDED.status, amount = EXCLUDED.amount,
                       created_at = EXCLUDED.created_at, updated_at = EXCLUDED.updated_at
                """,
                orders,
            )
            await cur.executemany(
                """
                INSERT INTO approvals
                    (apply_id, thread_id, member_id, type, amount, reason,
                     status, version, reviewer, created_at, decided_at, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (apply_id) DO UPDATE
                   SET thread_id = EXCLUDED.thread_id, member_id = EXCLUDED.member_id,
                       type = EXCLUDED.type, amount = EXCLUDED.amount,
                       reason = EXCLUDED.reason, status = EXCLUDED.status,
                       version = EXCLUDED.version, reviewer = EXCLUDED.reviewer,
                       created_at = EXCLUDED.created_at, decided_at = EXCLUDED.decided_at,
                       expires_at = EXCLUDED.expires_at
                """,
                approvals,
            )
            await cur.executemany(
                """
                INSERT INTO complaints
                    (ticket_id, member_id, category, priority, sla_deadline, status)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (ticket_id) DO UPDATE
                   SET member_id = EXCLUDED.member_id, category = EXCLUDED.category,
                       priority = EXCLUDED.priority, sla_deadline = EXCLUDED.sla_deadline,
                       status = EXCLUDED.status
                """,
                complaints,
            )

            # 附带两条示例会话与画像，方便前端联调时有东西可看（不计入返回统计）
            now = _iso(BASE_TIME)
            await cur.executemany(
                """
                INSERT INTO sessions
                    (thread_id, member_id, title, message_count, last_message,
                     created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (thread_id) DO UPDATE
                   SET member_id = EXCLUDED.member_id, title = EXCLUDED.title,
                       message_count = EXCLUDED.message_count,
                       last_message = EXCLUDED.last_message,
                       updated_at = EXCLUDED.updated_at
                """,
                [
                    (
                        "thread-0001",
                        members[0][0],
                        "会员权益咨询",
                        3,
                        "我的金卡有哪些权益？",
                        now,
                        now,
                    ),
                    (
                        "thread-0002",
                        members[1][0],
                        "退款进度查询",
                        5,
                        "退款什么时候到账？",
                        now,
                        now,
                    ),
                ],
            )
            await cur.executemany(
                """
                INSERT INTO member_profile (member_id, profile, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (member_id) DO UPDATE
                   SET profile = EXCLUDED.profile, updated_at = EXCLUDED.updated_at
                """,
                [
                    (
                        members[0][0],
                        Jsonb({"preferred_channel": "在线客服", "tags": ["高价值", "复购意愿强"]}),
                        now,
                    ),
                    (
                        members[1][0],
                        Jsonb({"preferred_channel": "电话", "tags": ["物流敏感", "曾有投诉"]}),
                        now,
                    ),
                ],
            )
        await conn.commit()

    return {
        "lookups": len(LOOKUPS),
        "members": len(members),
        "member_status": len(statuses),
        "benefits": len(benefits),
        "orders": len(orders),
        "approvals": len(approvals),
        "complaints": len(complaints),
    }


async def main() -> None:
    """命令行入口：解析参数并执行灌数。"""
    # Windows 控制台默认 GBK，直接打中文会乱码/报错，这里统一切到 UTF-8 输出
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    parser = argparse.ArgumentParser(description="生成客服 Agent 演示数据")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="先删除全部业务表再重建（不会动 LangGraph checkpoint 表）",
    )
    args = parser.parse_args()

    counts = await seed_all(reset=args.reset)
    print("数据灌入完成：")
    for table, n in counts.items():
        print(f"  {table:<14} {n:>5} 行")


if __name__ == "__main__":
    asyncio.run(main())
