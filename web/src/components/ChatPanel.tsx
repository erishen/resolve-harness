import { useCallback, useEffect, useRef, useState } from 'react'
import { api, transcriptToMessages } from '../api'
import { fmtNum, fmtUsage } from '../types'
import FastPathModal, { FAST_PATH_METHODS } from './FastPathModal'
import type {
  ApprovalAction,
  ApprovalCall,
  ApprovalDecision,
  ChatMessage,
  ChatResponse,
  ChatTurn,
  ToolEvent,
} from '../types'

/** ISO 时间 → 本地 YYYY-MM-DD HH:mm。 */
function fmtTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/** 该消息是否由 Fast Path 代码直算（trace 首事件是匹配器方法）。 */
function isFastPath(m: ChatMessage): boolean {
  const first = m.tools?.[0]
  return (
    first?.kind === 'tool_call' &&
    first.name !== 'memory_hint' &&
    FAST_PATH_METHODS.has(first.name)
  )
}

interface Props {
  onAfterTurn?: () => void
}

export default function ChatPanel({ onAfterTurn }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [histTurns, setHistTurns] = useState<ChatTurn[]>([])
  const [histOpen, setHistOpen] = useState(false)
  const [fastOpen, setFastOpen] = useState(false)
  // 会话级模型覆盖：'' = 跟随聊天模型；否则本会话用该模型库别名
  const [sessionModel, setSessionModel] = useState('')
  const [modelAliases, setModelAliases] = useState<string[]>([])
  const endRef = useRef<HTMLDivElement>(null)

  const loadHistory = useCallback(async () => {
    try {
      const { turns } = await api.chatHistory()
      setHistTurns(turns)
    } catch {
      /* backend down — keep stale */
    }
  }, [])

  useEffect(() => {
    void loadHistory()
  }, [loadHistory])

  // 模型库别名：供「会话模型」选择器使用（与设置页模型库一致）
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const c = await api.getConfig()
        if (!cancelled) setModelAliases(Object.keys(c.models ?? {}))
      } catch {
        /* backend down — 选择器仅显示默认 */
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

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
      const res = await api.chat(text, sessionModel || undefined)
      if (res.status === 'pending_approval' && res.pending && res.thread_id) {
        // the loop suspended on a human-approval gate — show the approval card
        const tid: string = res.thread_id
        const pending = res.pending
        setMessages((m) => [
          ...m,
          {
            id: crypto.randomUUID(),
            role: 'assistant',
            content: '⏳ 等待人工审批',
            approval: { threadId: tid, pending },
          },
        ])
      } else {
        setMessages((m) => [
          ...m,
          {
            id: crypto.randomUUID(),
            role: 'assistant',
            content: res.reply || '(empty reply)',
            tools: res.trace.length > 0 ? res.trace : undefined,
            loopSteps: res.steps > 0 ? res.steps : undefined,
            usage: res.usage ?? undefined,
          },
        ])
      }
      onAfterTurn?.()
      void loadHistory()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  // swap an approval card for either a chained one or the final reply
  const handleResolved = (res: ChatResponse, messageId: string) => {
    if (res.status === 'pending_approval' && res.pending && res.thread_id) {
      setMessages((m) =>
        m.map((msg) =>
          msg.id === messageId
            ? { ...msg, approval: { threadId: res.thread_id!, pending: res.pending! } }
            : msg,
        ),
      )
    } else {
      setMessages((m) =>
        m.map((msg) =>
          msg.id === messageId
            ? {
                id: msg.id,
                role: 'assistant',
                content: res.reply || '(empty reply)',
                tools: res.trace.length > 0 ? res.trace : undefined,
                loopSteps: res.steps > 0 ? res.steps : undefined,
                usage: res.usage ?? undefined,
              }
            : msg,
        ),
      )
    }
    onAfterTurn?.()
  }

  return (
    <div className="chat">
      {error && <div className="error-banner">{error}</div>}
      <div className="chat-tools-row">
        <label className="chat-session-model" title="本会话使用的模型（空 = 跟随「设置 → 聊天模型」）">
          <span className="chat-session-model-label">会话模型</span>
          <select
            className="chat-session-model-select"
            value={sessionModel}
            onChange={(e) => setSessionModel(e.target.value)}
          >
            <option value="">默认</option>
            {modelAliases.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="chat-clear-session"
          title="清空当前会话（短期记忆，长期记忆保留）"
          onClick={() => {
            if (!window.confirm('清空当前会话？\n（仅清短期对话记忆，长期记忆保留）')) return
            void (async () => {
              try {
                await api.reset()
                setMessages([])
                void loadHistory()
              } catch (e) {
                setError(e instanceof Error ? e.message : String(e))
              }
            })()
          }}
        >
          🗑 清空会话
        </button>
      </div>
      {histTurns.length > 0 && (
        <div className="chat-hist">
          <div className="chat-hist-head">
            <button
              type="button"
              className="chat-hist-toggle"
              onClick={() => setHistOpen((v) => !v)}
            >
              📚 历史消耗（{histTurns.length}）· 每轮输入/输出 tokens {histOpen ? '▾' : '▸'}
            </button>
            <button
              type="button"
              className="chat-hist-clear"
              title="清空全部历史消耗记录"
              onClick={() => {
                if (!window.confirm('清空全部聊天历史消耗记录？')) return
                void (async () => {
                  try {
                    await api.clearChatHistory()
                    setHistTurns([])
                    setHistOpen(false)
                  } catch (e) {
                    setError(e instanceof Error ? e.message : String(e))
                  }
                })()
              }}
            >
              清空
            </button>
          </div>
          {histOpen && (
            <div className="chat-hist-list">
              {histTurns.map((t, i) => (
                <div key={i} className="chat-hist-row" title={t.reply}>
                  <span className="t">{fmtTime(t.created_at)}</span>
                  <span className="m">{t.user_msg.length > 40 ? `${t.user_msg.slice(0, 40)}…` : t.user_msg}</span>
                  <span className="u">
                    ⚡ {fmtNum(t.total_tokens)} · 输入 {fmtNum(t.prompt_tokens)} · 输出 {fmtNum(t.completion_tokens)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      <div className="messages">
        {messages.length === 0 && !busy && (
          <div className="empty">向 agent 提问吧 — 例如「现在几点？」或「记住我喜欢深色主题」</div>
        )}
        {messages.map((m) => (
          <div key={m.id} className={`msg ${m.role}`}>
            <div className="bubble">{m.content}</div>
            {isFastPath(m) && (
              <button
                type="button"
                className="fastpath-tag"
                title="这条回答由 Fast Path 代码直算，未经过大模型"
                onClick={() => setFastOpen(true)}
              >
                ⚡ Fast Path 代码直算
              </button>
            )}
            {m.loopSteps !== undefined && m.loopSteps > 0 && (
              <div className="loop-tag">🔁 LangGraph 循环 · {m.loopSteps} 步</div>
            )}
            {m.usage?.total_tokens !== undefined && (
              <div className="loop-tag">⚡ {fmtUsage(m.usage)}</div>
            )}
            {m.tools && m.tools.length > 0 && (
              <div className="tool-trace">
                {m.tools.map((t, i) => (
                  <TraceItem key={i} event={t} />
                ))}
              </div>
            )}
            {m.approval && (
              <ApprovalCard
                approval={m.approval}
                messageId={m.id}
                onResolved={handleResolved}
              />
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
      {fastOpen && <FastPathModal onClose={() => setFastOpen(false)} />}

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
  if (event.kind === 'tool_call' && event.name === 'memory_hint') {
    // mechanism-level memory pre-fetch (not a real tool call)
    const matched = (event.args as { matched?: string[] } | undefined)?.matched ?? []
    return (
      <div className="trace-item">
        <span className="fn memory-hint">🔎 命中记忆：{matched.join('、')}</span>
      </div>
    )
  }
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

interface ApprovalProps {
  approval: { threadId: string; pending: ApprovalCall[] }
  messageId: string
  onResolved: (res: ChatResponse, messageId: string) => void
}

function ApprovalCard({ approval, messageId, onResolved }: ApprovalProps) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [denyId, setDenyId] = useState<string | null>(null)
  const [editId, setEditId] = useState<string | null>(null)
  const [reason, setReason] = useState('')
  const [editJson, setEditJson] = useState('')

  const submit = async (decisions: ApprovalDecision[] | 'approve' | 'deny') => {
    setBusy(true)
    setError('')
    try {
      const res = await api.approve(approval.threadId, decisions)
      onResolved(res, messageId)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  // 对某个 call 指定动作，其余 call 默认批准（后端对未指定的 call 是保守拒绝）
  const decisionsExcept = (id: string, action: ApprovalAction, extra?: Partial<ApprovalDecision>): ApprovalDecision[] =>
    approval.pending.map((c) =>
      c.id === id
        ? { id: c.id, action, ...extra }
        : { id: c.id, action: 'approve' as const },
    )

  return (
    <div className="approval-card">
      <div className="approval-title">⏳ 需要人工审批（执行前请确认）</div>
      {approval.pending.map((c) => (
        <div key={c.id} className="approval-call">
          <div className="approval-call-head">
            <span className="fn">⚙ {c.name}</span>
            <pre className="approval-args">{JSON.stringify(c.args, null, 2)}</pre>
          </div>
          <div className="approval-call-actions">
            <button disabled={busy} onClick={() => void submit('approve')}>
              批准
            </button>
            <button
              disabled={busy}
              onClick={() => {
                setDenyId(denyId === c.id ? null : c.id)
                setEditId(null)
                setReason('')
              }}
            >
              拒绝
            </button>
            <button
              disabled={busy}
              onClick={() => {
                setEditId(editId === c.id ? null : c.id)
                setDenyId(null)
                setEditJson(JSON.stringify(c.args, null, 2))
              }}
            >
              改参数
            </button>
          </div>
          {denyId === c.id && (
            <div className="approval-inline">
              <input
                className="approval-reason"
                placeholder="拒绝原因（可选）"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
              <button
                disabled={busy}
                onClick={() => {
                  void submit(decisionsExcept(c.id, 'deny', reason ? { reason } : {}))
                }}
              >
                确认拒绝
              </button>
            </div>
          )}
          {editId === c.id && (
            <div className="approval-inline">
              <textarea
                className="approval-edit"
                value={editJson}
                onChange={(e) => setEditJson(e.target.value)}
              />
              <button
                disabled={busy}
                onClick={() => {
                  let args = c.args
                  try {
                    args = JSON.parse(editJson)
                  } catch {
                    /* keep original args on parse failure */
                  }
                  void submit(decisionsExcept(c.id, 'edit', { args }))
                }}
              >
                确认修改
              </button>
            </div>
          )}
        </div>
      ))}
      <div className="approval-footer">
        <button disabled={busy} onClick={() => void submit('approve')}>
          {busy ? '处理中…' : '全部批准'}
        </button>
        <button disabled={busy} onClick={() => void submit('deny')}>
          全部拒绝
        </button>
        {error && <span className="error-inline">{error}</span>}
      </div>
    </div>
  )
}
