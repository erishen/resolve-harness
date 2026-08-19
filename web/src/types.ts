export interface ToolEvent {
  kind: 'tool_call' | 'tool_result'
  name: string
  args?: Record<string, unknown>
  content?: string
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  tools?: ToolEvent[]
}

export interface MemoryRow {
  scope: string
  key: string
  value: unknown
  updated_at: string
}

export interface TranscriptItem {
  role: string
  content: string
}

export interface ChatResponse {
  reply: string
  steps: number
  trace: ToolEvent[]
  transcript: TranscriptItem[]
}

export interface StateResponse {
  transcript: TranscriptItem[]
  memories: MemoryRow[]
  model: string
}

// ---- task mode -------------------------------------------------------------

export type TaskEventType =
  | 'task_start'
  | 'thought'
  | 'tool_call'
  | 'tool_result'
  | 'task_end'
  | 'error'

export interface TaskEvent {
  task_id: string
  type: TaskEventType
  data: {
    objective?: string
    model?: string
    content?: string
    step?: number
    name?: string
    args?: Record<string, unknown>
    reply?: string
    steps?: number
    message?: string
    [k: string]: unknown
  }
  ts: string
}

export interface TaskSnapshot {
  task_id: string
  objective: string
  model: string
  status: 'running' | 'done' | 'error'
  created_at: string
  error: string | null
  events: TaskEvent[]
}
