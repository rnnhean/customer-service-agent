-- 客服 Agent 业务表（PostgreSQL 16）
-- 约定：时间一律 timestamptz，JSON 一律 jsonb

-- 业务枚举字典。**枚举是数据，不是代码**：
-- 订单类型、投诉分类这些随业务迭代会变，写死在 Python 里或提示词里，
-- 造数、工具白名单、提示词三处必然漂移（本项目真的漂移过：库里是「物流延迟」，
-- 工具白名单却是「物流问题」，Agent 传对的值反而被拒）。
-- 代码只保留「按 kind 读字典」的机制，新增枚举只改这张表。
CREATE TABLE IF NOT EXISTS lookups (
  kind       TEXT NOT NULL,                 -- order_type / order_status / complaint_category / ...
  code       TEXT NOT NULL,                 -- 落库值
  label      TEXT NOT NULL,                 -- 展示给用户的中文
  sort       INTEGER NOT NULL DEFAULT 0,
  active     boolean NOT NULL DEFAULT true,
  fallback   boolean NOT NULL DEFAULT false, -- 未知值归一到这一条（每个 kind 至多一条）
  PRIMARY KEY (kind, code)
);

CREATE TABLE IF NOT EXISTS members (
  member_id   TEXT PRIMARY KEY,
  level       TEXT NOT NULL,                 -- normal / silver / gold / platinum
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS member_status (
  member_id    TEXT PRIMARY KEY REFERENCES members(member_id) ON DELETE CASCADE,
  status       TEXT NOT NULL,                -- inactive / active / expired / canceling
  effective_at timestamptz,
  expire_at    timestamptz,
  auto_renew   boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS benefits (
  code        TEXT PRIMARY KEY,
  level       TEXT NOT NULL,
  name        TEXT NOT NULL,
  value       TEXT,
  stackable   boolean NOT NULL DEFAULT false,
  expire_days INTEGER
);

CREATE TABLE IF NOT EXISTS orders (
  order_id    TEXT PRIMARY KEY,
  member_id   TEXT NOT NULL REFERENCES members(member_id),
  type        TEXT NOT NULL,
  status      TEXT NOT NULL,                 -- PENDING / EFFECTIVE / REFUNDING / REFUNDED / CLOSED
  amount      INTEGER NOT NULL,              -- 单位：分
  created_at  timestamptz NOT NULL,
  updated_at  timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
  apply_id    TEXT PRIMARY KEY,
  thread_id   TEXT NOT NULL,
  member_id   TEXT NOT NULL REFERENCES members(member_id),
  type        TEXT NOT NULL,
  amount      INTEGER,
  reason      TEXT,
  status      TEXT NOT NULL,                 -- pending / approved / rejected / expired
  version     INTEGER NOT NULL DEFAULT 0,    -- CAS 防重复审批
  reviewer    TEXT,
  comment     TEXT,
  created_at  timestamptz NOT NULL,
  decided_at  timestamptz,
  expires_at  timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS complaints (
  ticket_id    TEXT PRIMARY KEY,
  member_id    TEXT NOT NULL REFERENCES members(member_id),
  category     TEXT NOT NULL,                -- 取值见 lookups(kind='complaint_category')
  priority     INTEGER NOT NULL,
  sla_deadline timestamptz NOT NULL,
  status       TEXT NOT NULL,
  summary      TEXT                          -- 投诉正文，客服回看时全靠它
);

CREATE TABLE IF NOT EXISTS member_profile (
  member_id   TEXT PRIMARY KEY REFERENCES members(member_id) ON DELETE CASCADE,
  profile     jsonb NOT NULL,
  updated_at  timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  thread_id     TEXT PRIMARY KEY,
  member_id     TEXT,
  title         TEXT NOT NULL,
  message_count INTEGER NOT NULL DEFAULT 0,
  last_message  TEXT,
  created_at    timestamptz NOT NULL,
  updated_at    timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS traces (
  trace_id    TEXT PRIMARY KEY,
  thread_id   TEXT NOT NULL,
  turn        INTEGER NOT NULL,
  intent      TEXT,
  route       TEXT,
  tools       jsonb,
  latency_ms  INTEGER,
  tokens_in   INTEGER,
  tokens_out  INTEGER,
  degraded    boolean NOT NULL DEFAULT false,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_orders_member ON orders(member_id);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at);
CREATE INDEX IF NOT EXISTS idx_complaints_member ON complaints(member_id);
CREATE INDEX IF NOT EXISTS idx_traces_thread ON traces(thread_id, turn);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
