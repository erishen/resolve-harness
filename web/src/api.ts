import type {
  AgentsResponse,
  ApprovalDecision,
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

export interface AppConfig {
  parallel: number
  max_replan_rounds: number
  max_steps: number
  agent_models: Record<string, string>
  default_model: string
  active_model: string
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new Error(`${res.status} ${body.slice(0, 200)}`)
  }
  return res.json() as Promise<T>
}

export const api = {
  health: () => request<{ status: string; model: string }>('/health'),
  state: () => request<StateResponse>('/state'),
  chat: (message: string) =>
    request<ChatResponse>('/chat', { method: 'POST', body: JSON.stringify({ message }) }),
  chatHistory: () => request<{ turns: ChatTurn[] }>('/chat/history'),
  clearChatHistory: () =>
    request<{ deleted: number }>('/chat/history', { method: 'DELETE' }),
  tools: () => request<{ tools: ToolInfo[] }>('/tools'),
  agents: () => request<AgentsResponse>('/agents'),
  getConfig: () => request<AppConfig>('/config'),
  setConfig: (
    parallel: number,
    maxReplanRounds: number,
    maxSteps: number,
    agentModels?: Record<string, string>,
    defaultModel?: string,
  ) =>
    request<AppConfig>('/config', {
      method: 'PUT',
      body: JSON.stringify({
        parallel,
        max_replan_rounds: maxReplanRounds,
        max_steps: maxSteps,
        ...(agentModels !== undefined ? { agent_models: agentModels } : {}),
        ...(defaultModel !== undefined ? { default_model: defaultModel } : {}),
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
  sandbox: () => request<{ files: SandboxFile[]; sandbox_dir: string | null }>('/sandbox'),
  sandboxRawUrl: (path: string) => `/api/sandbox/raw?path=${encodeURIComponent(path)}`,
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
