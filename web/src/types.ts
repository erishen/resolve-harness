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
