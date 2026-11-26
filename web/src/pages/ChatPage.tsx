import { useMemo, useState } from 'react';
import { Button, Card, Empty, Input, Space, Tag, Typography, message } from 'antd';
import { useChat } from '@ai-sdk/react';
import { transport } from '@/api/transport';
import type { MyUIMessage } from '@/types/chat';

type FinalAnswer = NonNullable<
  Extract<MyUIMessage['parts'][number], { type: 'data-answer' }>['data']
> | null;

function collectText(uiMessage: MyUIMessage): string {
  let text = '';
  for (const part of uiMessage.parts) {
    if (part.type === 'text') text += part.text;
  }
  return text;
}

function ChatPage() {
  const [input, setInput] = useState('');
  const [finalAnswer, setFinalAnswer] = useState<FinalAnswer>(null);

  const { messages, sendMessage, status, error, stop } = useChat<MyUIMessage>({
    transport,
    onData: (dataPart) => {
      if (dataPart.type === 'data-approval') {
        const { apply_id, summary, amount } = dataPart.data;
        message.info(`待审批 ${apply_id}：${summary}（¥${(amount / 100).toFixed(2)}）`);
      }
      if (dataPart.type === 'data-answer') {
        // 权威答复：覆盖流式输出的观感版本（可能是校验改写后的结果）
        setFinalAnswer(dataPart.data);
      }
    },
    onError: (err) => {
      message.error(err.message || '对话失败，请稍后重试');
    },
  });

  const busy = status === 'submitted' || status === 'streaming';

  /** 最后一轮助手消息用权威答复覆盖 */
  const lastAssistantIndex = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      if (messages[i].role === 'assistant') return i;
    }
    return -1;
  }, [messages]);

  const handleSend = () => {
    const text = input.trim();
    if (!text || busy) return;
    setInput('');
    setFinalAnswer(null);
    void sendMessage({ text });
  };

  return (
    <Card title="对话" style={{ maxWidth: 960, margin: '0 auto' }}>
      <Space orientation="vertical" size="middle" style={{ width: '100%' }}>
        <div style={{ minHeight: 360, maxHeight: '60vh', overflowY: 'auto' }}>
          {messages.length === 0 ? (
            <Empty description="还没有消息，先说点什么吧" />
          ) : (
            messages.map((m, index) => {
              const isLastAssistant = m.role === 'assistant' && index === lastAssistantIndex;
              const authoritative = isLastAssistant && finalAnswer ? finalAnswer.text : '';
              const body = authoritative || collectText(m) || '…';
              return (
                <div key={m.id} style={{ marginBottom: 12 }}>
                  <Space size={4}>
                    <Tag color={m.role === 'user' ? 'blue' : 'green'}>
                      {m.role === 'user' ? '用户' : '助手'}
                    </Tag>
                    {isLastAssistant && finalAnswer?.degraded ? (
                      <Tag color="orange">已转人工</Tag>
                    ) : null}
                  </Space>
                  <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', margin: '4px 0 0' }}>
                    {body}
                  </Typography.Paragraph>
                  {isLastAssistant && finalAnswer?.citations?.length ? (
                    <Space size={4} wrap>
                      {finalAnswer.citations.map((c) => (
                        <Tag key={c.code} color="purple">
                          {c.code}
                          {c.name ? ` ${c.name}` : ''}
                        </Tag>
                      ))}
                    </Space>
                  ) : null}
                </div>
              );
            })
          )}
        </div>

        {error ? <Typography.Text type="danger">{error.message}</Typography.Text> : null}

        <Space.Compact style={{ width: '100%' }}>
          <Input.TextArea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="描述您的问题，Enter 发送，Shift+Enter 换行"
            autoSize={{ minRows: 1, maxRows: 4 }}
            onPressEnter={(e) => {
              if (!e.shiftKey) {
                e.preventDefault();
                handleSend();
              }
            }}
          />
          <Button type="primary" onClick={handleSend} disabled={busy}>
            发送
          </Button>
          {busy ? <Button onClick={stop}>停止</Button> : null}
        </Space.Compact>
      </Space>
    </Card>
  );
}

export default ChatPage;
