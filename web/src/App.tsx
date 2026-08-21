import { useCallback, useEffect, useState } from 'react'
import { api, formatValue } from './api'
import AgentsPanel from './components/AgentsPanel'
import ChatPanel from './components/ChatPanel'
import HistoryPanel from './components/HistoryPanel'
import PluginPanel from './components/PluginPanel'
import TaskPanel from './components/TaskPanel'
import SandboxPanel from './components/SandboxPanel'
import SettingsPanel from './components/SettingsPanel'
import ToolsPanel from './components/ToolsPanel'
import type { MemoryRow } from './types'

type Mode =
  | 'task'
  | 'chat'
  | 'history'
  | 'plugins'
  | 'tools'
  | 'agents'
  | 'sandbox'
  | 'settings'

/** 工具 Tab 的外部初始过滤（Agent Tab 点击 Specialist 时锁定"任务"）。 */
type ToolsFilter = 'all' | 'chat' | 'task' | 'approval'

/** ISO 时间 → 本地 YYYY-MM-DD HH:mm（记忆时间标签） */
function fmtTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/** 按更新时间倒序（后端已倒序，这里兜底） */
function sortMemories(rows: MemoryRow[]): MemoryRow[] {
  return [...rows].sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)))
}

/** 侧栏显示值：隐藏快照 value 开头的 [YYYY-MM-DD HH:mm] 前缀——时间已由
 *  🕐 updated_at 标签展示；value 本体保留该前缀供 agent recall 读取。 */
function displayValue(value: unknown): string {
  return formatValue(value).replace(/^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]\s*/, '')
}

export default function App() {
  const [mode, setMode] = useState<Mode>('chat')
  const [toolsFilter, setToolsFilter] = useState<ToolsFilter>('all')
  const [memories, setMemories] = useState<MemoryRow[]>([])
  const [online, setOnline] = useState(false)
  const [model, setModel] = useState('')

  const loadMemories = useCallback(async () => {
    try {
      const { memories } = await api.memories()
      setMemories(sortMemories(memories))
    } catch {
      /* backend down — keep stale list */
    }
  }, [])

  /** 重新拉取「当前生效模型」并写回顶栏 tag。
   *  默认模型若指向模型库别名（如 sensenova-deepseek），则解析为别名对应的
   *  真实模型名展示，与设置页「当前生效」保持一致；否则显示 .env 原始模型。 */
  const refreshModel = useCallback(async () => {
    try {
      const c = await api.getConfig()
      const active = c.active_model || ''
      const prof = (c.models ?? {})[active]
      setModel(prof?.model || active)
    } catch {
      /* backend down — keep stale tag */
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const [_, state] = await Promise.all([api.health(), api.state()])
        if (cancelled) return
        setOnline(true)
        setMemories(sortMemories(state.memories))
        void refreshModel()
      } catch {
        if (!cancelled) setOnline(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [refreshModel])

  const forgetMemory = async (key: string) => {
    try {
      await api.forgetMemory(key)
      await loadMemories()
    } catch {
      /* ignore */
    }
  }

  const clearMemories = async () => {
    if (!window.confirm('清空全部长期记忆？\n（该操作不可恢复）')) return
    try {
      await api.clearMemories()
      await loadMemories()
    } catch {
      /* ignore */
    }
  }

  return (
    <div className="app">
      <section className="chat">
        <header className="chat-header">
          <span className={`status-dot ${online ? 'online' : ''}`} />
          <span className="title">agentpulse</span>
          <span className="subtitle">
            {online ? '已连接' : '后端离线 — 请运行 `make dev`'}
          </span>
          <nav className="mode-tabs">
            <button
              className={mode === 'chat' ? 'active' : ''}
              onClick={() => setMode('chat')}
            >
              聊天
            </button>
            <button
              className={mode === 'task' ? 'active' : ''}
              onClick={() => setMode('task')}
            >
              任务
            </button>
            <button
              className={mode === 'history' ? 'active' : ''}
              onClick={() => setMode('history')}
            >
              历史
            </button>
            <button
              className={mode === 'plugins' ? 'active' : ''}
              onClick={() => setMode('plugins')}
            >
              插件
            </button>
            <button
              className={mode === 'tools' ? 'active' : ''}
              onClick={() => setMode('tools')}
            >
              工具
            </button>
            <button
              className={mode === 'agents' ? 'active' : ''}
              onClick={() => setMode('agents')}
            >
              Agent
            </button>
            <button
              className={mode === 'sandbox' ? 'active' : ''}
              onClick={() => setMode('sandbox')}
            >
              沙箱
            </button>
            <button
              className={mode === 'settings' ? 'active' : ''}
              onClick={() => setMode('settings')}
            >
              设置
            </button>
          </nav>
          {model && <span className="model-tag">{model}</span>}
        </header>

        {mode === 'chat' ? (
          <ChatPanel onAfterTurn={() => void loadMemories()} />
        ) : mode === 'task' ? (
          <TaskPanel onMemoryChange={() => void loadMemories()} />
        ) : mode === 'history' ? (
          <HistoryPanel />
        ) : mode === 'tools' ? (
          <ToolsPanel initialFilter={toolsFilter} />
        ) : mode === 'agents' ? (
          <AgentsPanel
            onGoTools={() => {
              setToolsFilter('task')
              setMode('tools')
            }}
            onGoPlugins={() => setMode('plugins')}
            onGoSettings={() => setMode('settings')}
          />
        ) : mode === 'sandbox' ? (
          <SandboxPanel />
        ) : mode === 'settings' ? (
          <SettingsPanel onModelChange={() => void refreshModel()} />
        ) : (
          <PluginPanel />
        )}
      </section>

      <aside className="sidebar">
        <div className="sidebar-header">
          <h3>长期记忆</h3>
          <button
            className="sidebar-clear"
            title="清空全部长期记忆"
            onClick={() => void clearMemories()}
          >
            清空
          </button>
        </div>
        <div className="memory-list">
          {memories.length === 0 ? (
            <div className="empty">暂无长期记忆 — 让 agent 用 remember 存一条试试</div>
          ) : (
            memories.map((m) => (
              <div key={`${m.scope}:${m.key}`} className="memory-item">
                <div className="k">
                  <span>{m.key}</span>
                  <button title="删除" onClick={() => void forgetMemory(m.key)}>
                    ✕
                  </button>
                </div>
                <div className="v">{displayValue(m.value)}</div>
                <div className="t">🕐 {fmtTime(m.updated_at)}</div>
              </div>
            ))
          )}
        </div>
      </aside>
    </div>
  )
}
