// 单条对话探测：node scripts/probe.mjs <thread> <content> [memberId]
const [, , thread = "tprobe", content = "你好", memberId = "M0001"] = process.argv;

const BASE = process.env.CSA_BASE || "http://127.0.0.1:8020";
const res = await fetch(`${BASE}/api/chat/stream`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ thread_id: thread, content, member_id: memberId }),
});

const reader = res.body.getReader();
const dec = new TextDecoder();
let buf = "";
const frames = [];

while (true) {
  const { done, value } = await reader.read();
  if (done) break;
  buf += dec.decode(value, { stream: true });
  const parts = buf.split(/\r?\n\r?\n/);
  buf = parts.pop();
  for (const p of parts) {
    const line = p.split(/\r?\n/).find((l) => l.startsWith("data:"));
    if (!line) continue;
    const raw = line.slice(5).trim();
    if (!raw || raw === "[DONE]") continue;
    try {
      frames.push(JSON.parse(raw));
    } catch {
      /* ignore */
    }
  }
}

const answer = frames.find((f) => f.type === "data-answer");
console.log("=== ANSWER ===");
console.log(answer?.data?.text ?? "(无 data-answer 帧)");
console.log("=== META ===");
console.log(JSON.stringify(answer?.data ?? {}, null, 2));
