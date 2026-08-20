import type {
  ChatMessage,
  ChatResponse,
  ExampleItem,
  MemoryRow,
  PluginItem,
  StateResponse,
  TaskSnapshot,
  TranscriptItem,
} from './types'

const BASE = '/api'

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
  reset: () => request<{ ok: boolean }>('/reset', { method: 'POST' }),
  memories: () => request<{ memories: MemoryRow[] }>('/memories'),
  forgetMemory: (key: string) =>
    request<{ ok: boolean }>(`/memories?key=${encodeURIComponent(key)}`, {
      method: 'DELETE',
    }),
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
