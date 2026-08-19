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
  | 'plan'
  | 'subtask_start'
  | 'thought'
  | 'tool_call'
  | 'tool_result'
  | 'subtask_done'
  | 'evaluation'
  | 're_plan'
  | 'task_end'
  | 'error'

export interface Subtask {
  index: number
  title: string
  instruction: string
  artifacts: string[]
}

export interface TaskEvent {
  task_id: string
  type: TaskEventType
  data: {
    objective?: string
    model?: string
    content?: string
    step?: number
    subtask?: number
    subtasks?: Subtask[]
    round?: number
    index?: number
    title?: string
    total?: number
    summary?: string
    name?: string
    args?: Record<string, unknown>
    reply?: string
    steps?: number
    passed?: boolean
    score?: number
    feedback?: string
    missing?: string[]
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

// ---- plugin management ---------------------------------------------------------

export interface PluginItem {
  name: string
  trigger: string
  source: string
  mtime: number
  size: number
}
