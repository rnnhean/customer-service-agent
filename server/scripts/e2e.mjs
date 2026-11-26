// 端到端自测：对话 SSE + 四类意图 + HITL 完整闭环 + CAS 防重复
// 用法: node scripts/e2e.mjs [baseUrl]
const BASE = process.argv[2] || 'http://127.0.0.1:8000';

let pass = 0;
let fail = 0;
const failures = [];

function check(name, ok, extra = '') {
  if (ok) {
    pass++;
    console.log(`  ✅ ${name}${extra ? ' — ' + extra : ''}`);
  } else {
    fail++;
    failures.push(name);
    console.log(`  ❌ ${name}${extra ? ' — ' + extra : ''}`);
  }
}

async function api(path, init) {
  const res = await fetch(BASE + path, init);
  const text = await res.text();
  let body = null;
  try {
    body = JSON.parse(text);
  } catch {
    body = text;
  }
  return { status: res.status, body };
}

/** 跑一轮对话，收集所有 SSE 帧 */
async function chat(threadId, content, memberId) {
  const res = await fetch(BASE + '/api/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ thread_id: threadId, content, member_id: memberId }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);

  const ctype = res.headers.get('content-type') || '';
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  const frames = [];
  let finished = false;

  const consume = (text) => {
    // sse_starlette 用 \r\n 分隔，先归一化
    const blocks = text.split(/\r?\n\r?\n/);
    const rest = blocks.pop();
    for (const block of blocks) {
      for (const line of block.split(/\r?\n/)) {
        if (!line.startsWith('data:')) continue;
        const raw = line.slice(5).trim();
        if (!raw) continue;
        if (raw === '[DONE]') {
          finished = true;
          continue;
        }
        try {
          frames.push(JSON.parse(raw));
        } catch {
          frames.push({ type: '_unparsed', raw });
        }
      }
    }
    return rest;
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    buf = consume(buf);
  }
  buf += decoder.decode();
  consume(buf + '\n\n');

  for (const f of frames) if (f.type === 'finish') finished = true;
  return { frames, finished, ctype };
}

function textOf(frames) {
  // data-answer 是权威答复（后置校验可能改写，也可能是代码生成的降级话术）
  const authoritative = frames.find((f) => f.type === 'data-answer');
  if (authoritative?.data?.text) return authoritative.data.text;
  return frames
    .filter((f) => f.type === 'text-delta' && f.delta)
    .map((f) => f.delta)
    .join('');
}

function frameTypes(frames) {
  return [...new Set(frames.map((f) => f.type))].join(',');
}

const ts = Date.now();
const T = (name) => `e2e-${ts}-${name}`;

/** 找一个还有 EFFECTIVE 订单的会员，避免反复跑之后固定订单已被退款 */
async function findRefundable() {
  for (let i = 1; i <= 60; i++) {
    const id = `M${String(i).padStart(4, '0')}`;
    const res = await api(`/api/members/${id}`);
    if (res.status !== 200) continue;
    const order = (res.body.recent_orders || []).find((o) => o.status === 'EFFECTIVE');
    if (order) return { member_id: id, order_id: order.order_id };
  }
  return null;
}

async function main() {
  console.log('\n=== 0. 健康检查 ===');
  const health = await api('/health');
  check('GET /health 200', health.status === 200, JSON.stringify(health.body));
  check('LLM 已配置', health.body?.llm_configured === true);

  console.log('\n=== 1. 权益咨询（RAG + 强制引用）===');
  const t1 = T('benefit');
  const r1 = await chat(t1, '我是白银会员，有哪些权益？', 'M0001');
  check('SSE 帧序列含 start/finish', r1.frames.some((f) => f.type === 'start') && r1.frames.some((f) => f.type === 'finish'), frameTypes(r1.frames));
  check('有正文输出', textOf(r1.frames).length > 0, `${textOf(r1.frames).length} 字`);
  const a1 = textOf(r1.frames);
  console.log('  回答节选:', a1.slice(0, 120).replace(/\n/g, ' '));
  const hasCite = /\[权益-[A-Za-z0-9_-]+\]/.test(a1);
  check('回答带权益引用标记', hasCite);

  console.log('\n=== 2. 订单查询 ===');
  const t2 = T('order');
  const r2 = await chat(t2, '帮我查一下订单 ORD20260002 的状态', 'M0182');
  const a2 = textOf(r2.frames);
  check('有正文输出', a2.length > 0, `${a2.length} 字`);
  console.log('  回答节选:', a2.slice(0, 140).replace(/\n/g, ' '));
  check('回答提到订单号', a2.includes('ORD20260002'));

  console.log('\n=== 3. 投诉（代码拼装工单号 + SLA）===');
  const t3 = T('complaint');
  const r3 = await chat(t3, '我要投诉，你们的会员权益根本用不了，我要发到网上曝光', 'M0182');
  const a3 = textOf(r3.frames);
  check('有正文输出', a3.length > 0, `${a3.length} 字`);
  console.log('  回答节选:', a3.slice(0, 140).replace(/\n/g, ' '));
  check('回答含工单号', /T[KC]\d{8,}/.test(a3), (a3.match(/T[KC]\d{8,}/) || [''])[0]);
  check('未出现模型编造的单号', !/[A-Z]{2,4}\d{6,}-\d+/.test(a3));

  console.log('\n=== 4. 低置信度澄清 ===');
  const t4 = T('clarify');
  const r4 = await chat(t4, '嗯', 'M0182');
  const a4 = textOf(r4.frames);
  check('有回复', a4.length > 0, `${a4.length} 字`);
  console.log('  回答节选:', a4.slice(0, 100).replace(/\n/g, ' '));

  console.log('\n=== 5. HITL 退款完整闭环 ===');
  const target = await findRefundable();
  check('找到可退款订单', !!target, target ? `${target.member_id} / ${target.order_id}` : '无');
  if (!target) {
    console.log('  无法继续 HITL 测试，跳过');
  } else {
    const t5 = T('refund');
    const r5 = await chat(t5, `订单 ${target.order_id} 我要退款`, target.member_id);
    const frames5 = r5.frames;
    const approvalFrame = frames5.find((f) => f.type === 'data-approval');
    check('收到 data-approval 帧', !!approvalFrame, frameTypes(frames5));
    if (!approvalFrame) {
      console.log('  回答:', textOf(frames5).slice(0, 120));
      console.log('  无法继续 HITL 测试，跳过');
    } else {
    const ap = approvalFrame.data;
    console.log('  审批单:', JSON.stringify({ apply_id: ap.apply_id, amount: ap.amount, status: ap.status }));
    check('审批单有 apply_id', !!ap.apply_id);
    check('审批单有过期时间', !!ap.expires_at);

    const list = await api('/api/approvals?status=pending');
    check('审批台能查到该单', list.body?.items?.some((i) => i.apply_id === ap.apply_id));

    const d1 = await api(`/api/approvals/${ap.apply_id}/decision`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ approved: true, reviewer: 'e2e', comment: '同意退款' }),
    });
    check('同意审批 200', d1.status === 200 && d1.body?.ok === true, JSON.stringify(d1.body));

    // 订单应变成 REFUNDED
    const memberRes = await api(`/api/members/${target.member_id}`);
    if (memberRes.status === 200) {
      const o = (memberRes.body.recent_orders || []).find((x) => x.order_id === target.order_id);
      check('订单状态变为 REFUNDED', o?.status === 'REFUNDED', o?.status);
    } else {
      console.log('  （会员接口异常，跳过订单状态校验）');
    }

    // 同一审批单再提交 → 409
    const d2 = await api(`/api/approvals/${ap.apply_id}/decision`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ approved: false, reviewer: 'e2e', comment: '重复提交' }),
    });
    check('重复提交返回 409', d2.status === 409, `status=${d2.status}`);
    }
  }

  console.log('\n=== 6. 驳回必须填理由 ===');
  const pend = await api('/api/approvals?status=pending');
  if (pend.body?.items?.length) {
    const id = pend.body.items[0].apply_id;
    const rej = await api(`/api/approvals/${id}/decision`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ approved: false, reviewer: 'e2e', comment: '' }),
    });
    check('空理由驳回返回 422', rej.status === 422, `status=${rej.status}`);
  } else {
    console.log('  （当前无 pending 单，跳过）');
  }

  console.log('\n=== 7. 会话与轨迹 ===');
  const sess = await api('/api/sessions');
  check('会话列表 200', sess.status === 200 && Array.isArray(sess.body?.items), `共 ${sess.body?.items?.length} 条`);
  const tr = await api(`/api/traces/${t1}`);
  check('轨迹接口 200', tr.status === 200, `共 ${(tr.body?.items || []).length} 条`);

  console.log('\n=== 8. 参数校验 ===');
  const bad = await api('/api/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ thread_id: '', content: '' }),
  });
  check('空参数返回 422', bad.status === 422, `status=${bad.status}`);

  console.log(`\n===== 结果: ${pass} 通过 / ${fail} 失败 =====`);
  if (fail) {
    console.log('失败项:');
    for (const f of failures) console.log('  -', f);
    process.exitCode = 1;
  }
}

main().catch((e) => {
  console.error('测试脚本异常:', e);
  process.exitCode = 2;
});
