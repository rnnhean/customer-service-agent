import type { UIMessage } from 'ai';

export type MyDataParts = {
  node: { name: string; status: 'running' | 'done'; intent?: string };
  approval: { apply_id: string; amount: number; summary: string; expires_at: string };
  /**
   * 权威答复。流式 text 只是「过程观感」——节点里可能因引用/数字校验被改写，
   * 也可能整段是代码生成的降级话术（没有 LLM 输出）。收到这一帧后以它为准。
   */
  answer: {
    text: string;
    degraded: boolean;
    citations: { code: string; name: string }[];
    intent?: string | null;
  };
};

export type MyUIMessage = UIMessage<unknown, MyDataParts>;
