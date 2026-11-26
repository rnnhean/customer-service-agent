import { DefaultChatTransport } from 'ai';
import type { MyUIMessage } from '@/types/chat';

/** localStorage 里保存当前会话 thread_id 的 key */
const THREAD_STORAGE_KEY = 'csa.thread_id';

/** 取当前会话 id；没有就生成一个（新会话 id 由前端生成，见接口契约） */
export function getThreadId(): string {
  let threadId = localStorage.getItem(THREAD_STORAGE_KEY);
  if (!threadId) {
    threadId = crypto.randomUUID();
    localStorage.setItem(THREAD_STORAGE_KEY, threadId);
  }
  return threadId;
}

/** 把一条 UIMessage 里的文本 part 拼起来 */
function extractText(uiMessage: MyUIMessage): string {
  let text = '';
  for (const part of uiMessage.parts) {
    if (part.type === 'text') text += part.text;
  }
  return text;
}

export const transport = new DefaultChatTransport<MyUIMessage>({
  api: '/api/chat/stream',
  prepareSendMessagesRequest: ({ messages, body }) => {
    const lastUserMessage = [...messages].reverse().find((m) => m.role === 'user');
    const bodyThreadId = body?.thread_id;
    const threadId =
      typeof bodyThreadId === 'string' && bodyThreadId ? bodyThreadId : getThreadId();

    return {
      body: {
        ...body,
        thread_id: threadId,
        content: lastUserMessage ? extractText(lastUserMessage) : '',
      },
    };
  },
});
