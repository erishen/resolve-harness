import { useCallback, useEffect, useRef, useState } from 'react'
import { api, formatValue, setApiToken } from './api'
import AgentsPanel from './components/AgentsPanel'
import AuditPanel from './components/AuditPanel'
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
  | 'audit'
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

/**
 * Token 门禁条：任何 /api 请求收到 401 时唯一出现的恢复入口（替代原
 * window.prompt——阻塞线程且与并发时序纠缠，会反复弹出）。常驻显示直到
 * 保存成功后整页刷新；也可手动关闭改走「设置 → API Token」。
 */
function TokenGate() {
  const [show, setShow] = useState(false)
  const [value, setValue] = useState('')
  const [checking, setChecking] = useState(false)
  const [bad, setBad] = useState(false)

  useEffect(() => {
    const onUnauthorized = () => setShow(true)
    window.addEventListener('rh:unauthorized', onUnauthorized)
    return () => window.removeEventListener('rh:unauthorized', onUnauthorized)
  }, [])

  // 先拿候选 Token 实际打一次接口验证，通过才落盘 + 刷新；
  // 输错当场提示，避免「保存→刷新→依旧 401」的静默失败循环。
  const submit = async () => {
    const t = value.trim()
    if (!t || checking) return
    setChecking(true)
    setBad(false)
    try {
      const res = await fetch('/api/state', {
        headers: { Authorization: `Bearer ${t}` },
      })
      if (res.ok) {
        setApiToken(t)
        window.location.reload()
        return
      }
      setBad(true)
    } catch {
      setBad(true)
    } finally {
      setChecking(false)
    }
  }

  if (!show) return null
  return (
    <div className="token-gate">
      <span className="token-gate-text">🔒 后端已启用 API Token 校验（.env 的 API_TOKEN）</span>
      <input
        type="password"
        className="token-gate-input"
        placeholder="粘贴 API Token"
        value={value}
        autoFocus
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') void submit()
        }}
      />
      <button type="button" className="token-gate-btn primary" disabled={!value.trim() || checking} onClick={() => void submit()}>
        {checking ? '验证中…' : '验证并保存'}
      </button>
      <button type="button" className="token-gate-btn" onClick={() => setShow(false)}>
        稍后在设置里填
      </button>
      {bad && (
        <span className="token-gate-error">❌ Token 不正确：需与后端 .env 的 API_TOKEN 完全一致</span>
      )}
    </div>
  )
}

export default function App() {
  const [mode, setMode] = useState<Mode>('chat')
  const [toolsFilter, setToolsFilter] = useState<ToolsFilter>('all')
  const [memories, setMemories] = useState<MemoryRow[]>([])
  const [online, setOnline] = useState(false)
  const [model, setModel] = useState('')

  // 长期记忆栏宽度（左拉可扩展），持久化到 localStorage，范围 200–720px
  const [sidebarW, setSidebarW] = useState<number>(() => {
    const saved = Number(localStorage.getItem('resolve_harness.sidebarW'))
    return saved >= 200 && saved <= 720 ? saved : 280
  })
  const sidebarWRef = useRef(sidebarW)
  sidebarWRef.current = sidebarW
  const sidebarDraggingRef = useRef(false)

  const startSidebarDrag = (e: React.MouseEvent) => {
    e.preventDefault()
    sidebarDraggingRef.current = true
    document.body.style.userSelect = 'none'
    document.body.style.cursor = 'col-resize'
    const onMove = (ev: MouseEvent) => {
      if (!sidebarDraggingRef.current) return
      const w = window.innerWidth - ev.clientX
      setSidebarW(Math.min(720, Math.max(200, w)))
    }
    const onUp = () => {
      sidebarDraggingRef.current = false
      document.body.style.userSelect = ''
      document.body.style.cursor = ''
      localStorage.setItem('resolve_harness.sidebarW', String(sidebarWRef.current))
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }

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
    <div className="app" style={{ gridTemplateColumns: `1fr ${sidebarW}px` }}>
      <TokenGate />
      <section className="chat">
        <header className="chat-header">
          <span className={`status-dot ${online ? 'online' : ''}`} />
          <span className="title">Resolve Harness</span>
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
              className={mode === 'audit' ? 'active' : ''}
              onClick={() => setMode('audit')}
            >
              审计
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

        <main className="chat-main">
          {mode === 'chat' ? (
          <ChatPanel onAfterTurn={() => void loadMemories()} />
        ) : mode === 'task' ? (
          <TaskPanel onMemoryChange={() => void loadMemories()} />
        ) : mode === 'history' ? (
          <HistoryPanel onMemoryChange={() => void loadMemories()} />
        ) : mode === 'audit' ? (
          <AuditPanel />
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
        </main>
      </section>

      <div
        className="sidebar-resizer"
        style={{ left: `calc(100% - ${sidebarW}px)` }}
        onMouseDown={startSidebarDrag}
        title="拖动调整长期记忆宽度"
      />

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
