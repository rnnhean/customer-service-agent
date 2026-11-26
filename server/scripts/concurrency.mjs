// 真并发 CAS 测试：两个 decision 请求同时打同一个审批单，必须恰好一个 200、一个 409。
// 用法: node scripts/concurrency.mjs [baseUrl]
import { execFileSync } from 'node:child_process';

const BASE = process.argv[2] || process.env.CSA_BASE || 'http://127.0.0.1:8020';
// 只读查询用，不涉及任何凭据（本地 dev 库口令见 docker-compose 默认值）
const PSQL = ['exec', 'csa_postgres', 'psql', '-U', 'csa', '-d', 'customer_service', '-t', '-A', '-F', '|', '-c'];

let pass = 0;
let fail = 0;
const ok = (name, extra = '') => {
  pass += 1;
  console.log(`  ✅ ${name}${extra ? ' — ' + extra : ''}`);
};
const bad = (name, extra = '') => {
  fail += 1;
  console.log(`  ❌ ${name}${extra ? ' — ' + extra : ''}`);
};

function sql(query) {
  const out = execFileSync('docker', [...PSQL, query], { encoding: 'utf8' });
  return out
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
    .map((l) => l.split('|'));
}

async function api(path, init) {
  const res = await fetch(BASE + path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
  const text = await res.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  return { status: res.status, body };
}

/** 读 SSE，返回 { approval?, answer? } */
async function streamChat(threadId, content, memberId) {
  const res = await fetch(BASE + '/api/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ thread_id: threadId, content, member_id: memberId }),
  });
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  let approval = null;
  let answer = null;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const parts = buf.split(/\r?\n\r?\n/);
    buf = parts.pop();
    for (const p of parts) {
      const line = p.split(/\r?\n/).find((l) => l.startsWith('data:'));
      if (!line) continue;
      const raw = line.slice(5).trim();
      if (!raw || raw === '[DONE]') continue;
      let frame;
      try {
        frame = JSON.parse(raw);
      } catch {
        continue;
      }
      if (frame.type === 'data-approval') approval = frame.data;
      if (frame.type === 'data-answer') answer = frame.data;
    }
  }
  return { approval, answer };
}

console.log('===== 并发 CAS 测试 =====');

// ---- 1) 挑一个干净的可退款订单（EFFECTIVE 且该会员没有 pending 审批）----
const rows = sql(`
  SELECT o.order_id, o.member_id
    FROM orders o
   WHERE o.status = 'EFFECTIVE'
     AND NOT EXISTS (
           SELECT 1 FROM approvals a
            WHERE a.member_id = o.member_id AND a.status = 'pending'
         )
   ORDER BY o.order_id
   LIMIT 1
`);
if (rows.length === 0) {
  console.log('  ⚠️ 没有可退款订单，先 reset 数据再跑');
  process.exit(0);
}
const [orderId, memberId] = rows[0];
ok('找到可退款订单', `${orderId} / ${memberId}`);

// ---- 2) 走真实对话触发 HITL 中断，落一张 pending 审批单 ----
const threadId = `cas-${Date.now()}`;
const { approval } = await streamChat(threadId, `我要退款，订单 ${orderId}，不想要了`, memberId);
if (!approval?.apply_id) {
  bad('对话触发审批中断', JSON.stringify(approval));
  process.exit(1);
}
ok('对话触发审批中断', approval.apply_id);
const applyId = approval.apply_id;

// ---- 3) 两个请求**同时**打同一个审批单 ----
const payload = JSON.stringify({ approved: true, reviewer: 'cas_bot', comment: '并发测试' });
const [a, b] = await Promise.all([
  api(`/api/approvals/${applyId}/decision`, { method: 'POST', body: payload }),
  api(`/api/approvals/${applyId}/decision`, { method: 'POST', body: payload }),
]);

const codes = [a.status, b.status].sort((x, y) => x - y);
console.log(`  并发结果: ${codes.join(' / ')}`);

if (codes[0] === 200 && codes[1] === 409) {
  ok('恰好一个 200、一个 409');
} else {
  bad('恰好一个 200、一个 409', `实际 ${codes.join(' / ')} → ${JSON.stringify([a.body, b.body])}`);
}

const winner = a.status === 200 ? a : b;
if (winner.body?.ok === true) ok('胜出请求 ok=true', winner.body.status);
else bad('胜出请求 ok=true', JSON.stringify(winner.body));

const loser = a.status === 409 ? a : b;
if (loser.status === 409 && loser.body?.error_msg) {
  ok('失败侧有明确业务错误', `${loser.body.error_no} ${loser.body.error_msg}`);
} else {
  bad('失败侧有明确业务错误', JSON.stringify(loser.body));
}

// ---- 4) 落库结果核对 ----
const [status] = sql(`SELECT status FROM approvals WHERE apply_id = '${applyId}'`)[0] ?? [];
if (status === 'approved') ok('审批单最终状态 approved');
else bad('审批单最终状态 approved', status);

const [orderStatus] = sql(`SELECT status FROM orders WHERE order_id = '${orderId}'`)[0] ?? [];
if (orderStatus === 'REFUNDED') ok('订单已置为 REFUNDED');
else bad('订单已置为 REFUNDED', orderStatus);

// 退款只能执行一次：审批单 version 只 +1，不会有第二次 UPDATE
const [version] = sql(`SELECT version FROM approvals WHERE apply_id = '${applyId}'`)[0] ?? [];
if (Number(version) === 1) ok('version 只递增一次', `version=${version}`);
else bad('version 只递增一次', `version=${version}`);

console.log(`\n===== 结果: ${pass} 通过 / ${fail} 失败 =====`);
process.exit(fail === 0 ? 0 : 1);
