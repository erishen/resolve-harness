import { useCallback, useEffect, useState } from 'react'
import { api, formatValue } from './api'
import ChatPanel from './components/ChatPanel'
import PluginPanel from './components/PluginPanel'
import TaskPanel from './components/TaskPanel'
import type { MemoryRow } from './types'

type Mode = 'chat' | 'task' | 'plugins'

export default function App() {
  const [mode, setMode] = useState<Mode>('chat')
  const [memories, setMemories] = useState<MemoryRow[]>([])
  const [online, setOnline] = useState(false)
  const [model, setModel] = useState('')

  const loadMemories = useCallback(async () => {
    try {
      const { memories } = await api.memories()
      setMemories(memories)
    } catch {
      /* backend down — keep stale list */
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const [health, state] = await Promise.all([api.health(), api.state()])
        if (cancelled) return
        setOnline(true)
        setModel(health.model)
        setMemories(state.memories)
      } catch {
        if (!cancelled) setOnline(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  const resetSession = async () => {
    try {
      await api.reset()
    } catch {
      /* ignore */
    }
  }

  const forgetMemory = async (key: string) => {
    try {
      await api.forgetMemory(key)
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
            {online ? 'connected' : 'backend offline — run `make dev`'}
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
              className={mode === 'plugins' ? 'active' : ''}
              onClick={() => setMode('plugins')}
            >
              插件
            </button>
          </nav>
          {model && <span className="model-tag">{model}</span>}
        </header>

        {mode === 'chat' ? (
          <ChatPanel onAfterTurn={() => void loadMemories()} />
        ) : mode === 'task' ? (
          <TaskPanel />
        ) : (
          <PluginPanel />
        )}
      </section>

      <aside className="sidebar">
        <div className="sidebar-header">
          <h3>Long-term Memory</h3>
          <div className="sidebar-actions">
            <button onClick={() => void resetSession()}>清空会话</button>
          </div>
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
                <div className="v">{formatValue(m.value)}</div>
              </div>
            ))
          )}
        </div>
      </aside>
    </div>
  )
}
