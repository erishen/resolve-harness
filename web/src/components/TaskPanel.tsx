import { useCallback, useEffect, useRef, useState } from 'react'
import { marked } from 'marked'
import { api } from '../api'
import type { Subtask, TaskEvent } from '../types'

type RunState = 'idle' | 'running' | 'done' | 'error'

/** A display-level item: plan / subtask (with nested events) / verdict / etc. */
type GroupedItem =
  | { kind: 'goal'; objective: string }
  | { kind: 'plan'; round: number; subtasks: Subtask[] }
  | { kind: 'subtask'; index: number; title: string; total: number; events: TaskEvent[] }
  | { kind: 'subtask-done'; index: number; title: string; summary: string }
  | { kind: 'event'; event: TaskEvent }
  | {
      kind: 'evaluation'
      passed: boolean
      score: number
      feedback: string
      missing: string[]
      round: number
    }
  | { kind: 'replan'; feedback: string; round: number }
  | { kind: 'final'; reply: string; passed: boolean; score: number; rounds: number }
  | { kind: 'error'; message: string }

function groupEvents(events: TaskEvent[]): GroupedItem[] {
  const out: GroupedItem[] = []
  let current: { index: number; title: string; total: number; events: TaskEvent[] } | null = null

  for (const ev of events) {
    const d = ev.data
    switch (ev.type) {
      case 'task_start':
        out.push({ kind: 'goal', objective: String(d.objective ?? '') })
        break
      case 'plan':
        out.push({ kind: 'plan', round: Number(d.round ?? 0), subtasks: (d.subtasks ?? []) as Subtask[] })
        break
      case 'subtask_start':
        current = {
          index: Number(d.index ?? 0),
          title: String(d.title ?? ''),
          total: Number(d.total ?? 0),
          events: [],
        }
        out.push({ kind: 'subtask', ...current })
        break
      case 'subtask_done':
        out.push({
          kind: 'subtask-done',
          index: Number(d.index ?? 0),
          title: String(d.title ?? ''),
          summary: String(d.summary ?? ''),
        })
        current = null
        break
      case 'thought':
      case 'tool_call':
      case 'tool_result':
        if (d.subtask != null && current) {
          current.events.push(ev)
        } else {
          out.push({ kind: 'event', event: ev })
        }
        break
      case 'evaluation':
        out.push({
          kind: 'evaluation',
          passed: Boolean(d.passed),
          score: Number(d.score ?? 0),
          feedback: String(d.feedback ?? ''),
          missing: (d.missing ?? []) as string[],
          round: Number(d.round ?? 0),
        })
        break
      case 're_plan':
        out.push({ kind: 'replan', feedback: String(d.feedback ?? ''), round: Number(d.round ?? 0) })
        break
      case 'task_end':
        out.push({
          kind: 'final',
          reply: String(d.reply ?? ''),
          passed: Boolean(d.passed),
          score: Number(d.score ?? 0),
          rounds: Number(d.rounds ?? 1),
        })
        break
      case 'error':
        out.push({ kind: 'error', message: String(d.message ?? 'unknown error') })
        break
    }
  }
  return out
}

interface Props {
  /** fired when a task finishes/errors — lets the app refresh long-term memory */
  onMemoryChange?: () => void
}

export default function TaskPanel({ onMemoryChange }: Props) {
  const [objective, setObjective] = useState('')
  const [events, setEvents] = useState<TaskEvent[]>([])
  const [state, setState] = useState<RunState>('idle')
  const [error, setError] = useState('')
  const [model, setModel] = useState('')
  const [examples, setExamples] = useState<{ label: string; text: string }[]>([])
  const [examplesLoading, setExamplesLoading] = useState(false)
  const endRef = useRef<HTMLDivElement>(null)
  const esRef = useRef<EventSource | null>(null)

  const loadExamples = useCallback(async (force = false) => {
    setExamplesLoading(true)
    try {
      const { examples } = await api.examples(force)
      setExamples(examples)
    } catch {
      /* backend down — keep whatever we have */
    } finally {
      setExamplesLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadExamples()
  }, [loadExamples])

  const items = groupEvents(events)
  const toolCalls = events.filter((e) => e.type === 'tool_call').length

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [events, state])

  useEffect(() => {
    return () => esRef.current?.close()
  }, [])

  const runTask = async () => {
    const goal = objective.trim()
    if (!goal || state === 'running') return
    setError('')
    setEvents([])
    setState('running')
    try {
      const { task_id } = await api.createTask(goal)
      const es = new EventSource(`/api/tasks/${task_id}/stream`)
      esRef.current = es
      es.onmessage = (msg) => {
        let ev: TaskEvent
        try {
          ev = JSON.parse(msg.data) as TaskEvent
        } catch {
          return
        }
        setEvents((prev) => [...prev, ev])
        if (ev.type === 'task_start') setModel(String(ev.data.model ?? ''))
        if (ev.type === 'task_end') {
          setState('done')
          onMemoryChange?.()
          es.close()
          esRef.current = null
        }
        if (ev.type === 'error') {
          setError(String(ev.data.message ?? 'task failed'))
          setState('error')
          onMemoryChange?.()
          es.close()
          esRef.current = null
        }
      }
      es.onerror = () => {
        /* server closes stream after terminal events; state driven by events */
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setState('error')
    }
  }

  const stop = useCallback(() => {
    esRef.current?.close()
    esRef.current = null
    setState((s) => (s === 'running' ? 'idle' : s))
  }, [])

  const busy = state === 'running'

  return (
    <div className="task-panel">
      <div className="task-composer">
        <div className="task-label">
          任务目标 — Planner 拆解 → Specialist 执行 → Evaluator 验收
        </div>
        <div className="task-examples">
          <span className="ex-label">示例</span>
          {examples.map((ex) => (
            <button
              key={`${ex.label}:${ex.text.slice(0, 12)}`}
              type="button"
              className="ex-chip"
              disabled={busy}
              title={ex.text}
              onClick={() => setObjective(ex.text)}
            >
              {ex.label}
            </button>
          ))}
          <button
            type="button"
            className="ex-chip ex-regen"
            disabled={busy || examplesLoading}
            title="根据长期记忆偏好重新生成示例"
            onClick={() => void loadExamples(true)}
          >
            {examplesLoading ? '生成中…' : '🔄 重新生成'}
          </button>
        </div>
        <form
          onSubmit={(e) => {
            e.preventDefault()
            void runTask()
          }}
        >
          <input
            value={objective}
            onChange={(e) => setObjective(e.target.value)}
            placeholder="例：调研 RAG 的常见方案并写一份对比报告存入沙箱"
            disabled={busy}
          />
          {busy ? (
            <button type="button" className="btn-stop" onClick={stop}>
              停止
            </button>
          ) : (
            <button type="submit" disabled={!objective.trim()}>
              运行
            </button>
          )}
        </form>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="task-stream">
        {items.length === 0 && state === 'idle' && (
          <div className="empty">
            输入一个目标。任务会由多个 Agent 协作完成：Planner 拆解子任务、
            Specialist 逐个执行（可调工具）、Evaluator 验收不达标自动打回重规划 ——
            全过程实时显示
          </div>
        )}
        {items.map((item, i) => (
          <GroupedCard key={i} item={item} />
        ))}
        {state === 'running' && (
          <div className="task-running">
            <span className="spinner" /> agent 协作执行中…
          </div>
        )}
        {state === 'done' && (
          <div className="task-done-banner">
            ✅ 任务完成 · {toolCalls} 次工具调用{model ? ` · ${model}` : ''}
          </div>
        )}
        {state === 'error' && <div className="task-done-banner err">✕ 任务失败</div>}
        <div ref={endRef} />
      </div>
    </div>
  )
}

// ---- grouped card rendering ----------------------------------------------------

function GroupedCard({ item }: { item: GroupedItem }) {
  switch (item.kind) {
    case 'goal':
      return (
        <div className="step step-start">
          <div className="step-title">🎯 目标</div>
          <div className="step-body">{item.objective}</div>
        </div>
      )
    case 'plan':
      return (
        <div className="step step-plan">
          <div className="step-title">
            📋 计划{item.round > 0 ? `（第 ${item.round + 1} 轮修订）` : ''}
          </div>
          <div className="plan-list">
            {item.subtasks.map((st, i) => (
              <div key={i} className="plan-item">
                <span className="plan-idx">{st.index + 1}</span>
                <div>
                  <div className="plan-title">{st.title}</div>
                  {st.artifacts.length > 0 && (
                    <div className="plan-artifacts">{st.artifacts.join(' · ')}</div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )
    case 'subtask':
      return (
        <div className="step step-subtask">
          <div className="step-title">
            🔧 子任务 {item.index + 1}/{item.total}：{item.title}（执行中）
          </div>
          <div className="subtask-events">
            {item.events.map((ev, i) => (
              <InlineEvent key={i} event={ev} />
            ))}
          </div>
        </div>
      )
    case 'subtask-done':
      return (
        <div className="step step-subtask-done">
          <div className="step-title">✅ 子任务完成：{item.title}</div>
          {item.summary && <div className="step-body">{item.summary}</div>}
        </div>
      )
    case 'event':
      return <InlineEvent event={item.event} standalone />
    case 'evaluation':
      return (
        <div className={`step step-evaluation ${item.passed ? 'ok' : 'fail'}`}>
          <div className="step-title">
            📊 验收{item.round > 0 ? `（第 ${item.round + 1} 轮）` : ''} ·{' '}
            {item.passed ? `通过（${item.score} 分）` : `未通过（${item.score} 分）`}
          </div>
          {item.feedback && <div className="step-body">{item.feedback}</div>}
          {item.missing.length > 0 && (
            <div className="missing-list">
              {item.missing.map((m, i) => (
                <div key={i}>• {m}</div>
              ))}
            </div>
          )}
        </div>
      )
    case 'replan':
      return (
        <div className="step step-replan">
          <div className="step-title">🔄 打回重规划</div>
          <div className="step-body">{item.feedback}</div>
        </div>
      )
    case 'final':
      return (
        <div className="step step-final">
          <div className="step-title">
            📦 交付{item.passed ? ` · ${item.score} 分 · ${item.rounds} 轮完成` : '（未完全达标）'}
          </div>
          <div
            className="step-markdown"
            dangerouslySetInnerHTML={{ __html: marked.parse(item.reply) }}
          />
        </div>
      )
    case 'error':
      return (
        <div className="step step-error">
          <div className="step-title">✕ 出错</div>
          <div className="step-body">{item.message}</div>
        </div>
      )
  }
}

function InlineEvent({ event, standalone }: { event: TaskEvent; standalone?: boolean }) {
  const d = event.data
  if (event.type === 'thought') {
    return (
      <div className={`inline-event inline-thought${standalone ? ' standalone' : ''}`}>
        <span className="ie-badge">💭 思考</span> {String(d.content ?? '')}
      </div>
    )
  }
  if (event.type === 'tool_call') {
    return (
      <div className="inline-event inline-call">
        <span className="ie-badge">⚙️ {String(d.name ?? '')}</span>
        {d.args && Object.keys(d.args).length > 0 && (
          <span className="ie-args">{JSON.stringify(d.args)}</span>
        )}
      </div>
    )
  }
  if (event.type === 'tool_result') {
    return (
      <div className="inline-event inline-result">
        <span className="ie-badge">📥 {String(d.name ?? '')}</span>
        <span className="ie-res">{String(d.content ?? '')}</span>
      </div>
    )
  }
  return null
}
