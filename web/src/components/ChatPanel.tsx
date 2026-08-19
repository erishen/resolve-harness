import { useEffect, useRef, useState } from 'react'
import { api, transcriptToMessages } from '../api'
import type { ChatMessage, ToolEvent } from '../types'

interface Props {
  onAfterTurn?: () => void
}

export default function ChatPanel({ onAfterTurn }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const state = await api.state()
        if (cancelled) return
        setMessages(transcriptToMessages(state.transcript))
      } catch {
        /* backend down — empty chat */
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
    setMessages((m) => [...m, { id: crypto.randomUUID(), role: 'user', content: text }])
    setBusy(true)
    try {
      const res = await api.chat(text)
      setMessages((m) => [
        ...m,
        {
          id: crypto.randomUUID(),
          role: 'assistant',
          content: res.reply || '(empty reply)',
          tools: res.trace.length > 0 ? res.trace : undefined,
        },
      ])
      onAfterTurn?.()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="chat">
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
