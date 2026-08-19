import { useCallback, useEffect, useRef, useState } from 'react'
import { api, formatValue, transcriptToMessages } from './api'
import type { ChatMessage, MemoryRow, ToolEvent } from './types'

export default function App() {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [memories, setMemories] = useState<MemoryRow[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [online, setOnline] = useState(false)
  const [model, setModel] = useState('')
  const endRef = useRef<HTMLDivElement>(null)

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
        setMessages(transcriptToMessages(state.transcript))
        setMemories(state.memories)
      } catch {
        if (!cancelled) setOnline(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, busy])

  const send = async () => {
    const text = input.trim()
    if (!text || busy) return
    setError('')
    setInput('')
    const userMsg: ChatMessage = { id: crypto.randomUUID(), role: 'user', content: text }
    setMessages((m) => [...m, userMsg])
    setBusy(true)
    try {
      const res = await api.chat(text)
      setOnline(true)
      const assistantMsg: ChatMessage = {
        id: crypto.randomUUID(),
        role: 'assistant',
        content: res.reply || '(empty reply)',
        tools: res.trace.length > 0 ? res.trace : undefined,
      }
      setMessages((m) => [...m, assistantMsg])
      await loadMemories()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const resetSession = async () => {
    try {
      await api.reset()
      setMessages([])
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const forgetMemory = async (key: string) => {
    try {
      await api.forgetMemory(key)
      await loadMemories()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="app">
      <section className="chat">
        <header className="chat-header">
          <span className={`status-dot ${online ? 'online' : ''}`} />
          <span className="title">agentpulse</span>
          <span className="subtitle">
            {online ? 'connected' : 'backend offline — run `make api`'}
          </span>
          {model && <span className="model-tag">{model}</span>}
        </header>

        {error && <div className="error-banner">{error}</div>}

        <div className="messages">
          {messages.length === 0 && !busy && (
            <div className="empty">向 agent 提问吧 — 例如「现在几点？」或「记住我喜欢深色主题」</div>
          )}
          {messages.map((m) => (
            <div key={m.id} className={`msg ${m.role}`}>
              <div className="bubble">{m.content}</div>
              {m.tools && m.tools.length > 0 && (
                <div className="tool-trace">
                  {m.tools.map((t, i) => (
                    <TraceItem key={i} event={t} />
                  ))}
                </div>
              )}
            </div>
          ))}
          {busy && (
            <div className="msg assistant">
              <div className="bubble">思考中…</div>
            </div>
          )}
          <div ref={endRef} />
        </div>

        <div className="composer">
          <form
            onSubmit={(e) => {
              e.preventDefault()
              void send()
            }}
          >
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="输入消息，Enter 发送"
              disabled={busy}
              autoFocus
            />
            <button type="submit" disabled={busy || !input.trim()}>
              发送
            </button>
          </form>
          <div className="hint">工具调用会在回复下方以小标签展示 · Loop 上限 10 步 · 长期记忆存于 SQLite</div>
        </div>
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

function TraceItem({ event }: { event: ToolEvent }) {
  if (event.kind === 'tool_call') {
    const args = event.args ? JSON.stringify(event.args) : ''
    return (
      <div className="trace-item">
        <span className="fn">⚙ {event.name}</span>
        {args && <span className="res">{args}</span>}
      </div>
    )
  }
  return (
    <div className="trace-item">
      <span className="fn">→ {event.name}</span>
      {event.content && <span className="res">{event.content}</span>}
    </div>
  )
}
