import type {
  ApiError,
  ApprovalItem,
  ApprovalListResp,
  DecisionReq,
  SessionListResp,
  SessionMessagesResp,
  TraceItem,
} from '@/types/api';

/** 审批状态筛选，`all` 表示不筛选 */
export type ApprovalStatusFilter = ApprovalItem['status'] | 'all';

export interface ApprovalQuery {
  status?: ApprovalStatusFilter;
  page?: number;
  page_size?: number;
}

export interface DecisionResp {
  ok: boolean;
  apply_id: string;
  status: ApprovalItem['status'];
  decided_at?: string;
  /** ok=false 时的原因：审批已落库，但图恢复失败 */
  warning?: string;
}

export interface DeleteSessionResp {
  ok: boolean;
}

/** 带业务错误码的请求异常，`error_no` 取自后端 ApiError */
export class ApiRequestError extends Error {
  readonly error_no: number;
  readonly detail: string | null;
  readonly status: number;

  constructor(status: number, error_no: number, error_msg: string, detail: string | null) {
    super(error_msg);
    this.name = 'ApiRequestError';
    this.status = status;
    this.error_no = error_no;
    this.detail = detail;
  }
}

function isApiError(value: unknown): value is Partial<ApiError> {
  return typeof value === 'object' && value !== null;
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  });

  const text = await res.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (!res.ok) {
    const err = isApiError(payload) ? payload : {};
    throw new ApiRequestError(
      res.status,
      typeof err.error_no === 'number' ? err.error_no : res.status * 100,
      typeof err.error_msg === 'string' ? err.error_msg : `请求失败（HTTP ${res.status}）`,
      typeof err.detail === 'string' ? err.detail : null,
    );
  }

  return payload as T;
}

function toQuery(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : '';
}

export function fetchApprovals(params: ApprovalQuery = {}): Promise<ApprovalListResp> {
  return request<ApprovalListResp>(
    `/api/approvals${toQuery({
      status: params.status,
      page: params.page,
      page_size: params.page_size,
    })}`,
  );
}

export function decideApproval(applyId: string, body: DecisionReq): Promise<DecisionResp> {
  return request<DecisionResp>(`/api/approvals/${encodeURIComponent(applyId)}/decision`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export function fetchSessions(page = 1, pageSize = 20): Promise<SessionListResp> {
  return request<SessionListResp>(`/api/sessions${toQuery({ page, page_size: pageSize })}`);
}

export function fetchSessionMessages(threadId: string): Promise<SessionMessagesResp> {
  return request<SessionMessagesResp>(
    `/api/sessions/${encodeURIComponent(threadId)}/messages`,
  );
}

export function deleteSession(threadId: string): Promise<DeleteSessionResp> {
  return request<DeleteSessionResp>(`/api/sessions/${encodeURIComponent(threadId)}`, {
    method: 'DELETE',
  });
}

/**
 * 后端实际返回 `{ thread_id, items }`（见 server/app/api/sessions.py），
 * 也兼容直接返回数组的形态，统一归一化成 `TraceItem[]`。
 */
export async function fetchTraces(threadId: string): Promise<TraceItem[]> {
  const resp = await request<TraceItem[] | { thread_id: string; items: TraceItem[] }>(
    `/api/traces/${encodeURIComponent(threadId)}`,
  );
  if (Array.isArray(resp)) return resp;
  return Array.isArray(resp?.items) ? resp.items : [];
}
