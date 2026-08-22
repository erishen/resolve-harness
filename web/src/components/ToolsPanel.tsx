import { useEffect, useState } from 'react'
import { api } from '../api'
import type { ToolInfo } from '../types'

/** 工具 Tab：展示项目当前提供的所有工具（模式归属 / 参数 / 审批标记）。 */
/** 与 Fast Path 插件对应的工具标注（避免"看着重复"的疑问）。 */
const FASTPATH_NOTES: Record<string, string> = {
  get_current_time:
    '直接问时间走 Fast Path 插件 fastpath.time（零模型）；此工具用于对话/任务执行中途取时间',
}

export type ToolsFilterKey = 'all' | 'chat' | 'task' | 'approval'

interface ToolsPanelProps {
  /** 外部指定的初始过滤（如从 Agent Tab 跳转时锁定"任务"）。 */
  initialFilter?: ToolsFilterKey
}

export default function ToolsPanel({ initialFilter = 'all' }: ToolsPanelProps) {
  const [tools, setTools] = useState<ToolInfo[]>([])
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const { tools } = await api.tools()
        if (!cancelled) setTools(tools)
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  const chatCount = tools.filter((t) => t.chat).length
  const taskCount = tools.filter((t) => t.task).length
  const approvalCount = tools.filter((t) => t.require_approval).length

  const [filter, setFilter] = useState<ToolsFilterKey>(initialFilter)
  const filtered = tools.filter((t) => {
    if (filter === 'chat') return t.chat
    if (filter === 'task') return t.task
    if (filter === 'approval') return t.require_approval
    return true
  })

  const filters: { key: ToolsFilterKey; label: string; count: number }[] = [
    { key: 'all', label: '全部', count: tools.length },
    { key: 'chat', label: '聊天', count: chatCount },
    { key: 'task', label: '任务', count: taskCount },
    { key: 'approval', label: '需审批', count: approvalCount },
  ]

  return (
    <div className="tools-panel">
      <div className="tools-head">
        <div className="tools-title">
          项目工具（共 {tools.length} 个 · 聊天 {chatCount} · 任务 {taskCount}）
        </div>
        <div className="tools-hint">
          chat = 聊天循环可用 · task = 任务流水线可用 · 🔒 = 调用需人工审批
        </div>
        <div className="tools-filters">
          {filters.map((f) => (
            <button
              key={f.key}
              type="button"
              className={`tool-filter ${filter === f.key ? 'active' : ''}`}
              onClick={() => setFilter(f.key)}
            >
              {f.label}（{f.count}）
            </button>
          ))}
        </div>
      </div>
      {error && <div className="error-banner">{error}</div>}
      {tools.length === 0 && !error && <div className="empty">加载中…</div>}
      {tools.length > 0 && filtered.length === 0 && (
        <div className="empty">该筛选下暂无工具</div>
      )}
      <div className="tools-list">
        {filtered.map((t) => {
          const props = (t.parameters?.properties ?? {}) as Record<string, { type?: string }>
          const required = (t.parameters?.required ?? []) as string[]
          return (
            <div key={t.name} className="tool-card">
              <div className="tool-card-head">
                <span className="tool-name">{t.name}</span>
                {t.require_approval && <span className="tool-badge--approve">🔒 需审批</span>}
                {t.chat && <span className="tool-badge--chat">聊天</span>}
                {t.task && <span className="tool-badge--task">任务</span>}
              </div>
              <div className="tool-desc">{t.description}</div>
              {FASTPATH_NOTES[t.name] && (
                <div className="tool-note">⚡ {FASTPATH_NOTES[t.name]}</div>
              )}
              {Object.keys(props).length > 0 && (
                <div className="tool-params">
                  {Object.entries(props).map(([k, v]) => (
                    <span key={k} className={`tool-param ${required.includes(k) ? 'req' : ''}`}>
                      {k}: {v?.type ?? 'any'}
                      {required.includes(k) ? ' *' : ''}
                    </span>
                  ))}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
