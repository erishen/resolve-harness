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
  loopSteps?: number
  usage?: UsageInfo
  approval?: { threadId: string; pending: ApprovalCall[] }
}

// ---- token usage ----------------------------------------------------------

export interface UsageInfo {
  prompt_tokens?: number
  completion_tokens?: number
  total_tokens?: number
}

/** 一条已完成的聊天回合（落库的 token 消耗记录）。 */
export interface ChatTurn {
  created_at: string
  user_msg: string
  reply: string
  steps: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
}

/** 一个工具及其在各模式的可用性。 */
export interface ToolInfo {
  name: string
  description: string
  parameters: Record<string, unknown> | null
  require_approval: boolean
  chat: boolean
  task: boolean
}

/** 任务流水线中的一个 Agent。 */
export interface PipelineAgent {
  name: string
  role: string
  description: string
  tools: string
  phase: string
}

/** 流程图节点。 */
export interface GraphNode {
  id: string
  label: string
  x: number
  y: number
  shape: 'rect' | 'ellipse' | 'diamond'
  kind: string
}

/** 流程图边。 */
export interface GraphEdge {
  from: string
  to: string
  label: string
  loop?: boolean
}

export interface AgentsResponse {
  agents: PipelineAgent[]
  graph: { nodes: GraphNode[]; edges: GraphEdge[] }
}

/** 千分位格式化（1,830）。 */
export function fmtNum(n: number): string {
  return n.toLocaleString('en-US')
}

/** token 构成展示：总数 + 输入/输出拆分。 */
export function fmtUsage(u: UsageInfo | null | undefined): string {
  const total = u?.total_tokens
  const prompt = u?.prompt_tokens
  const completion = u?.completion_tokens
  if (total === undefined) return ''
  const parts = [`${fmtNum(total)} tokens`]
  if (prompt !== undefined) parts.push(`输入 ${fmtNum(prompt)}`)
  if (completion !== undefined) parts.push(`输出 ${fmtNum(completion)}`)
  return parts.join(' · ')
}

// ---- human-in-the-loop approval -----------------------------------------

export type ApprovalAction = 'approve' | 'deny' | 'edit'

export interface ApprovalCall {
  id: string
  name: string
  args: Record<string, unknown>
}

export interface ApprovalDecision {
  id: string
  action: ApprovalAction
  reason?: string
  args?: Record<string, unknown>
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
  status?: 'done' | 'pending_approval'
  pending?: ApprovalCall[] | null
  thread_id?: string | null
  usage?: UsageInfo | null
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
  | 'node_enter'
  | 'route'
  | 'thought'
  | 'tool_call'
  | 'tool_result'
  | 'produced_file'
  | 'subtask_done'
  | 'evaluation'
  | 're_plan'
  | 'task_end'
  | 'task_stopped'
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
  /** true = 内置（核心匹配器 / 晋升检测器），只读，不可删/不可再晋升 */
  builtin?: boolean
  /** 内置分类：内置核心（fastpath 匹配器） / 内置晋升（generated_detectors） */
  kind?: string
}

export interface ExampleItem {
  label: string
  text: string
  source?: 'builtin' | 'generated'
}

export interface SandboxFile {
  path: string
  size: number
  mtime: number
  is_text: boolean
  kind: 'text' | 'markdown' | 'html' | 'image' | 'pdf' | 'csv' | 'json' | 'code' | 'binary'
  /** 沙箱根目录确立前就已存在的「原有文件」，默认隐藏、清空时保留。 */
  preexisting?: boolean
}

/** 事件日志条目（审计 Tab / /api/events 返回）。 */
export interface AuditEvent {
  ts: string
  type: string
  data: Record<string, unknown>
}
