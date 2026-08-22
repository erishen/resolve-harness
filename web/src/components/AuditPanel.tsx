import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type { AuditEvent } from '../types'

/** 审计 Tab：查看/回放事件日志（聊天 turn 与任务 run 的完整 trace）。 */

/** 事件类型 → 徽标文案（与任务流/聊天 trace 的视觉语义一致）。 */
const EVENT_META: Record<string, { icon: string; label: string }> = {
  task_start: { icon: '🎯', label: '目标' },
  plan: { icon: '📋', label: '计划' },
  subtask_start: { icon: '🔧', label: '子任务' },
  subtask_done: { icon: '✅', label: '子任务完成' },
  thought: { icon: '💭', label: '思考' },
  tool_call: { icon: '⚙️', label: '工具调用' },
  tool_result: { icon: '📥', label: '工具结果' },
  evaluation: { icon: '📊', label: '验收' },
  re_plan: { icon: '🔄', label: '重规划' },
  task_end: { icon: '📦', label: '交付' },
  error: { icon: '✕', label: '出错' },
  node_enter: { icon: '▶', label: '节点' },
  route: { icon: '🔀', label: '路由' },
  approval_request: { icon: '🚦', label: '审批请求' },
  approval_result: { icon: '✅', label: '审批结果' },
  fastpath: { icon: '⚡', label: '代码直算' },
  codegen: { icon: '🧪', label: '探测器' },
}

const DEFAULT_META = { icon: '•', label: '事件' }

/** 按类型提取关键字段做摘要；未知类型兜底 JSON。 */
function summarize(e: AuditEvent): string {
  const d = e.data as Record<string, unknown>
  switch (e.type) {
    case 'thought':
      return String(d.content ?? '')
    case 'tool_call':
      return `${String(d.name ?? '')} ${d.args ? JSON.stringify(d.args).slice(0, 120) : ''}`
    case 'tool_result':
      return `${String(d.name ?? '')} → ${String(d.content ?? '').slice(0, 160)}`
    case 'task_start':
      return String(d.objective ?? '')
    case 'task_end':
      return String(d.reply ?? '').slice(0, 160)
    case 'evaluation':
      return `通过=${String(d.passed)} 分=${String(d.score ?? '')} ${String(d.feedback ?? '').slice(0, 100)}`
    case 'route':
      return `→ ${String(d.to ?? '')}（${String(d.reason ?? '')}）`
    case 'node_enter':
      return String(d.node ?? '')
    case 'fastpath':
    case 'codegen':
      return String(d.answer ?? String(d.method ?? ''))
    default: {
      const s = JSON.stringify(d)
      return s && s !== '{}' ? s.slice(0, 160) : ''
    }
  }
}

/** ISO 时间戳 → HH:MM:SS */
function fmtTime(ts: string): string {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

export default function AuditPanel() {
  const [scope, setScope] = useState<'chat' | 'task'>('chat')
  const [refs, setRefs] = useState<string[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [events, setEvents] = useState<AuditEvent[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const loadRefs = useCallback(async (s: string, preserveSelection = false) => {
    try {
      const r = await api.events(s)
      setRefs(r.refs)
      if (!preserveSelection) setSelected(r.refs[0] ?? null)
      setError('')
    } catch {
      setRefs([])
      if (!preserveSelection) setSelected(null)
      setError('事件日志读取失败（后端未连接？）')
    }
  }, [])

  const loadEvents = useCallback(async (s: string, ref: string) => {
    try {
      const r = await api.events(s, ref)
      setEvents(r.events)
    } catch {
      /* 轮询失败保留旧数据，不闪烁 */
    }
  }, [])

  const clearAll = useCallback(async () => {
    if (!window.confirm('清空全部事件日志（聊天 + 任务）？此操作不可撤销。')) return
    try {
      await api.clearEvents()
      setSelected(null)
      setEvents([])
      setRefs([])
      setError('')
    } catch {
      setError('清空失败')
    }
  }, [])

  useEffect(() => {
    void loadRefs(scope)
  }, [scope, loadRefs])

  useEffect(() => {
    if (!selected) {
      setEvents([])
      return
    }
    let cancelled = false
    setLoading(true)
    ;(async () => {
      try {
        const r = await api.events(scope, selected)
        if (!cancelled) setEvents(r.events)
      } catch {
        /* keep stale */
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [scope, selected])

  // 实时刷新：审计面板可见时每 2s 拉取最新事件与 refs（不重置当前选中）
  useEffect(() => {
    const id = setInterval(() => {
      void loadRefs(scope, true)
      if (selected) void loadEvents(scope, selected)
    }, 2000)
    return () => clearInterval(id)
  }, [scope, selected, loadRefs, loadEvents])

  return (
    <div className="audit-panel">
      <div className="audit-side">
        <div className="audit-head">
          <div className="audit-title-row">
            <span className="audit-title">事件日志</span>
            <button type="button" className="audit-clear" onClick={() => void clearAll()}>
              🗑 清空
            </button>
          </div>
          <span className="audit-live">● 实时</span>
          <span className="audit-hint">聊天/任务事件持久化 · 可回放</span>
        </div>
        <div className="audit-scope">
          {(['chat', 'task'] as const).map((s) => (
            <button
              key={s}
              type="button"
              className={`audit-scope-btn${scope === s ? ' active' : ''}`}
              onClick={() => setScope(s)}
            >
              {s === 'task' ? '任务' : '聊天'}
            </button>
          ))}
        </div>
        <div className="audit-refs">
          {refs.length === 0 && <div className="empty">{scope === 'task' ? '暂无任务事件' : '暂无聊天事件'}</div>}
          {refs.map((r) => (
            <button
              key={r}
              type="button"
              className={`audit-ref${selected === r ? ' active' : ''}`}
              onClick={() => setSelected(r)}
              title={r}
            >
              {r.slice(0, 12)}
            </button>
          ))}
        </div>
      </div>

      <div className="audit-main">
        {error && <div className="error-banner">{error}</div>}
        {!selected && !error && (
          <div className="empty">
            选择左侧的会话/任务回放其完整事件流（思考、工具调用、验收、交付…）
          </div>
        )}
        {selected && loading && <div className="empty">加载事件…</div>}
        {selected && !loading && events.length === 0 && (
          <div className="empty">该记录暂无事件</div>
        )}
        <div className="audit-timeline">
          {events.map((e, i) => {
            const meta = EVENT_META[e.type] ?? DEFAULT_META
            const summary = summarize(e)
            return (
              <div key={`${e.ts}-${e.type}-${i}`} className="audit-row">
                <span className="audit-ts">{fmtTime(e.ts)}</span>
                <span className={`audit-badge ${e.type}`}>
                  {meta.icon} {meta.label}
                </span>
                <span className="audit-data" title={JSON.stringify(e.data)}>
                  {summary}
                </span>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
