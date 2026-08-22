import type {
  AgentsResponse,
  ApprovalDecision,
  AuditEvent,
  ChatMessage,
  ChatResponse,
  ChatTurn,
  ExampleItem,
  MemoryRow,
  PluginItem,
  SandboxFile,
  StateResponse,
  TaskSnapshot,
  ToolInfo,
  TranscriptItem,
} from './types'

const BASE = '/api'

export interface ModelProfile {
  base_url: string
  model: string
  /** 环境变量名（实际 key 放 .env，不持久化明文） */
  api_key_env: string
}

export interface AppConfig {
  parallel: number
  max_replan_rounds: number
  max_steps: number
  agent_models: Record<string, string>
  default_model: string
  active_model: string
  models: Record<string, ModelProfile>
  /** .env 默认模型（LLM_MODEL / LLM_API_BASE / LLM_API_KEY），只读展示 */
  env_model: { base_url: string; model: string; api_key_env: string }
  /** 后端 .env 设了 API_TOKEN → true（设置页据此提示必填） */
  api_token_required?: boolean
}

// ---- API Token（对应后端 .env 的 API_TOKEN）----------------------------
// 仅存本机 localStorage；后端启用校验时自动附带到每个 /api 请求。
const TOKEN_KEY = 'resolve_harness.apiToken'

export function getApiToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? ''
}

export function setApiToken(token: string): void {
  const t = token.trim()
  if (t) localStorage.setItem(TOKEN_KEY, t)
  else localStorage.removeItem(TOKEN_KEY)
}

// ---- 401 统一处理：不再用 window.prompt（阻塞线程、与并发时序纠缠，会多次弹出）----
// 遇到 401 只做两件事：① 抛 UnauthorizedError；② 每次页面加载广播一次
// 'rh:unauthorized' 事件，由 App 的「Token 门禁条」负责收集输入（常驻、唯一）。
export class UnauthorizedError extends Error {
  constructor() {
    super('401 API Token 无效，请在下方输入框填写（与 .env 的 API_TOKEN 一致）')
    this.name = 'UnauthorizedError'
  }
}

let unauthorizedNotified = false

function notifyUnauthorizedOnce(): void {
  if (unauthorizedNotified) return
  unauthorizedNotified = true
  window.dispatchEvent(new CustomEvent('rh:unauthorized'))
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getApiToken()
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (token) headers.Authorization = `Bearer ${token}`
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { ...headers, ...(init?.headers as Record<string, string>) },
  })
  if (res.status === 401) {
    notifyUnauthorizedOnce()
    throw new UnauthorizedError()
  }
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new Error(`${res.status} ${body.slice(0, 200)}`)
  }
  return res.json() as Promise<T>
}

export const api = {
  health: () => request<{ status: string; model: string }>('/health'),
  state: () => request<StateResponse>('/state'),
  chat: (message: string, model?: string) =>
    request<ChatResponse>('/chat', {
      method: 'POST',
      body: JSON.stringify({ message, ...(model ? { model } : {}) }),
    }),
  chatHistory: () => request<{ turns: ChatTurn[] }>('/chat/history'),
  clearChatHistory: () =>
    request<{ deleted: number }>('/chat/history', { method: 'DELETE' }),
  tools: () => request<{ tools: ToolInfo[] }>('/tools'),
  agents: () => request<AgentsResponse>('/agents'),
  getConfig: () => request<AppConfig>('/config'),
  setConfig: (
    parallel: number | null,
    maxReplanRounds: number | null,
    maxSteps: number | null,
    agentModels?: Record<string, string>,
    defaultModel?: string,
    models?: Record<string, ModelProfile>,
  ) =>
    request<AppConfig>('/config', {
      method: 'PUT',
      body: JSON.stringify({
        ...(parallel != null ? { parallel } : {}),
        ...(maxReplanRounds != null ? { max_replan_rounds: maxReplanRounds } : {}),
        ...(maxSteps != null ? { max_steps: maxSteps } : {}),
        ...(agentModels !== undefined ? { agent_models: agentModels } : {}),
        ...(defaultModel !== undefined ? { default_model: defaultModel } : {}),
        ...(models !== undefined ? { models } : {}),
      }),
    }),
  approve: (threadId: string, decisions: ApprovalDecision[] | 'approve' | 'deny') =>
    request<ChatResponse>('/chat/approve', {
      method: 'POST',
      body: JSON.stringify({ thread_id: threadId, decisions }),
    }),
  reset: () => request<{ ok: boolean }>('/reset', { method: 'POST' }),
  memories: () => request<{ memories: MemoryRow[] }>('/memories'),
  addMemory: (key: string, value: unknown, scope?: string) =>
    request<{ ok: boolean }>('/memories', {
      method: 'POST',
      body: JSON.stringify({ key, value, scope: scope ?? 'default' }),
    }),
  forgetMemory: (key: string) =>
    request<{ ok: boolean }>(`/memories?key=${encodeURIComponent(key)}`, {
      method: 'DELETE',
    }),
  clearMemories: (scope?: string) =>
    request<{ ok: boolean; deleted: number }>(
      `/memories?scope=${encodeURIComponent(scope ?? 'default')}`,
      { method: 'DELETE' },
    ),
  createTask: (objective: string) =>
    request<{ task_id: string }>('/tasks', {
      method: 'POST',
      body: JSON.stringify({ objective }),
    }),
  getTask: (taskId: string) => request<TaskSnapshot>(`/tasks/${taskId}`),
  listTasks: () => request<{ tasks: TaskSnapshot[] }>('/tasks'),
  clearTasks: () => request<{ deleted: number }>('/tasks', { method: 'DELETE' }),
  stopTask: (taskId: string) =>
    request<{ ok: boolean }>(`/tasks/${taskId}/stop`, { method: 'POST' }),
  /** 事件日志（审计）：scope=chat|task；带 ref 返回该会话/任务的完整回放。 */
  events: (scope?: string, ref?: string, limit?: number) => {
    const q = new URLSearchParams()
    if (scope) q.set('scope', scope)
    if (ref) q.set('ref', ref)
    if (limit) q.set('limit', String(limit))
    const qs = q.toString()
    return request<{ refs: string[]; events: AuditEvent[] }>(`/events${qs ? `?${qs}` : ''}`)
  },
  clearEvents: () => request<{ deleted: number }>('/events', { method: 'DELETE' }),
  plugins: () => request<{ plugins: PluginItem[] }>('/plugins'),
  deletePlugin: (name: string) =>
    request<{ ok: boolean }>(`/plugins/${encodeURIComponent(name)}`, {
      method: 'DELETE',
    }),
  promotePlugins: (names: string[]) =>
    request<{ ok: boolean; promoted: number; file: string }>('/plugins/promote', {
      method: 'POST',
      body: JSON.stringify({ names }),
    }),
  examples: () => request<{ examples: ExampleItem[] }>('/examples'),
  regenerateExamples: () =>
    request<{ examples: ExampleItem[] }>('/examples/regenerate', { method: 'POST' }),
  deleteExample: (label: string, text: string) =>
    request<{ ok: boolean }>('/examples/delete', {
      method: 'POST',
      body: JSON.stringify({ label, text }),
    }),
  sandbox: () =>
    request<{ files: SandboxFile[]; sandbox_dir: string | null; sandbox_history: string[] }>(
      '/sandbox',
    ),
  sandboxSetLocation: (path: string) =>
    request<{ sandbox_dir: string; sandbox_history: string[] }>('/sandbox/location', {
      method: 'PUT',
      body: JSON.stringify({ path }),
    }),
  sandboxDeleteHistory: (path: string) =>
    request<{ sandbox_history: string[] }>(
      `/sandbox/history?path=${encodeURIComponent(path)}`,
      { method: 'DELETE' },
    ),
  sandboxClearHistory: () =>
    request<{ sandbox_history: string[] }>('/sandbox/history/all', { method: 'DELETE' }),
  // <img>/<iframe> 无法携带 Authorization 头，经鉴权取回字节再转 ObjectURL 使用
  sandboxRawObjectUrl: async (path: string): Promise<string> => {
    const headers: Record<string, string> = {}
    const t = getApiToken()
    if (t) headers.Authorization = `Bearer ${t}`
    const res = await fetch(
      `${BASE}/sandbox/raw?path=${encodeURIComponent(path)}`,
      { headers },
    )
    if (!res.ok) throw new Error(String(res.status))
    return URL.createObjectURL(await res.blob())
  },
  sandboxFile: (path: string) =>
    request<{ path: string; content: string; size: number }>(
      `/sandbox/content?path=${encodeURIComponent(path)}`,
    ),
  sandboxWrite: (path: string, content: string) =>
    request<{ path: string; size: number; ok: boolean }>(
      `/sandbox/content?path=${encodeURIComponent(path)}`,
      { method: 'PUT', body: JSON.stringify({ content }) },
    ),
  sandboxDelete: (path: string) =>
    request<{ ok: boolean }>(`/sandbox/file?path=${encodeURIComponent(path)}`, {
      method: 'DELETE',
    }),
  sandboxClear: () =>
    request<{ ok: boolean; deleted: number }>('/sandbox', { method: 'DELETE' }),
}

// transcript items -> ChatMessage (group consecutive tool traces into the reply)
export function transcriptToMessages(items: TranscriptItem[]): ChatMessage[] {
  const out: ChatMessage[] = []
  for (const item of items) {
    if (item.role === 'user') {
      out.push({ id: crypto.randomUUID(), role: 'user', content: item.content })
    } else if (item.role === 'assistant' && item.content) {
      out.push({ id: crypto.randomUUID(), role: 'assistant', content: item.content })
    }
  }
  return out
}

export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return String(value)
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

/** 任务事件流句柄：close() 主动断开（stop / 组件卸载 cleanup）。 */
export interface StreamHandle {
  close: () => void
}

/**
 * 打开任务事件流（SSE）。不用 EventSource：它无法携带 Authorization 头，
 * 后端开启 API Token 校验后会被 401 拦截，故用 fetch + ReadableStream
 * 手工解析 text/event-stream（后端帧格式固定为 `data: {json}\n\n`）。
 * 流正常结束（终态事件后服务端关闭）不触发 onError，状态由事件本身驱动。
 */
export function openTaskStream(
  taskId: string,
  onMessage: (data: string) => void,
  onError?: () => void,
): StreamHandle {
  const ctrl = new AbortController()
  void (async () => {
    try {
      const headers: Record<string, string> = { Accept: 'text/event-stream' }
      const t = getApiToken()
      if (t) headers.Authorization = `Bearer ${t}`
      const res = await fetch(`${BASE}/tasks/${encodeURIComponent(taskId)}/stream`, {
        headers,
        signal: ctrl.signal,
      })
      if (!res.ok || !res.body) {
        if (res.status === 401) notifyUnauthorizedOnce()
        onError?.()
        return
      }
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        let sep: number
        while ((sep = buf.indexOf('\n\n')) !== -1) {
          const frame = buf.slice(0, sep)
          buf = buf.slice(sep + 2)
          for (const line of frame.split('\n')) {
            if (line.startsWith('data:')) onMessage(line.slice(5).replace(/^ /, ''))
          }
        }
      }
    } catch {
      // 主动 close() 走 abort 分支静默；其余（网络断开等）交给调用方兜底
      onError?.()
    }
  })()
  return { close: () => ctrl.abort() }
}
