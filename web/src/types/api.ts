export interface ApprovalItem {
  apply_id: string;
  thread_id: string;
  member_id: string;
  type: 'refund' | 'reissue';
  amount: number;          // 整数分
  reason: string;
  status: 'pending' | 'approved' | 'rejected' | 'expired';
  created_at: string;
  expires_at: string;
  summary: string;
}
export interface ApprovalListResp { items: ApprovalItem[]; total: number; page: number; page_size: number; }
export interface DecisionReq { approved: boolean; reviewer: string; comment: string; }
export interface SessionItem {
  thread_id: string; member_id: string; title: string; last_message: string;
  message_count: number; has_pending_approval: boolean; updated_at: string;
}
export interface SessionListResp { items: SessionItem[]; total: number; }
export interface ChatMessageRow { id: string; role: 'user' | 'assistant'; content: string; created_at: string; }
export interface SessionMessagesResp { thread_id: string; messages: ChatMessageRow[]; }
export interface TraceItem {
  trace_id: string; thread_id: string; turn: number; intent: string | null;
  route: string | null; tools: string[]; latency_ms: number | null;
  tokens_in: number | null; tokens_out: number | null; degraded: number; created_at: string;
}
export interface ApiError { error_no: number; error_msg: string; detail: string | null; }
