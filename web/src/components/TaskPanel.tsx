import { useCallback, useEffect, useRef, useState } from 'react'
import { marked } from 'marked'
import { api } from '../api'
import type { TaskEvent } from '../types'

type RunState = 'idle' | 'running' | 'done' | 'error'

export default function TaskPanel() {
  const [objective, setObjective] = useState('')
  const [events, setEvents] = useState<TaskEvent[]>([])
  const [state, setState] = useState<RunState>('idle')
  const [error, setError] = useState('')
  const [steps, setSteps] = useState(0)
  const [model, setModel] = useState('')
  const endRef = useRef<HTMLDivElement>(null)
  const esRef = useRef<EventSource | null>(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [events, state])

  useEffect(() => {
    return () => esRef.current?.close() // cleanup on unmount
  }, [])

  const runTask = async () => {
    const goal = objective.trim()
    if (!goal || state === 'running') return
    setError('')
    setEvents([])
    setSteps(0)
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
        if (ev.type === 'tool_call') setSteps((s) => s + 1)
        if (ev.type === 'task_end') {
          setState('done')
          es.close()
          esRef.current = null
        }
        if (ev.type === 'error') {
          setError(String(ev.data.message ?? 'task failed'))
          setState('error')
          es.close()
          esRef.current = null
        }
      }
      es.onerror = () => {
        // server closes the stream after task_end/error; nothing to do here —
        // state is driven by the terminal events above. EventSource auto-retries
        // otherwise, which is fine for a local tool.
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
        <div className="task-label">任务目标（agent 自主规划 → 执行 → 交付）</div>
        <form
          onSubmit={(e) => {
            e.preventDefault()
            void runTask()
          }}
        >
          <input
            value={objective}
            onChange={(e) => setObjective(e.target.value)}
            placeholder="例：写一份 RAG 技术简介并存到沙箱文件，300 字左右"
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
        {events.length === 0 && state === 'idle' && (
          <div className="empty">
            输入一个目标，agent 会自己规划步骤、调用工具、迭代直到完成 ——
            每一步（思考 / 工具调用 / 结果 / 最终交付）都会实时显示在这里
          </div>
        )}
        {events.map((ev, i) => (
          <StepCard key={i} event={ev} />
        ))}
        {state === 'running' && (
          <div className="task-running">
            <span className="spinner" /> agent 正在执行…
          </div>
        )}
        {state === 'done' && (
          <div className="task-done-banner">
            ✅ 任务完成 · 共 {steps} 次工具调用{model ? ` · ${model}` : ''}
          </div>
        )}
        {state === 'error' && <div className="task-done-banner err">✕ 任务失败</div>}
        <div ref={endRef} />
      </div>
    </div>
  )
}

function StepCard({ event }: { event: TaskEvent }) {
  const { type, data } = event
  switch (type) {
    case 'task_start':
      return (
        <div className="step step-start">
          <div className="step-title">🎯 目标</div>
          <div className="step-body">{String(data.objective ?? '')}</div>
        </div>
      )
    case 'thought':
      return (
        <div className="step step-thought">
          <div className="step-title">
            💭 思考{typeof data.step === 'number' ? ` · 第 ${data.step} 轮` : ''}
          </div>
          <div className="step-body">{String(data.content ?? '')}</div>
        </div>
      )
    case 'tool_call':
      return (
        <div className="step step-toolcall">
          <div className="step-title">
            ⚙️ 调用工具 <code>{String(data.name ?? '')}</code>
          </div>
          {data.args && Object.keys(data.args).length > 0 && (
            <pre className="step-args">{JSON.stringify(data.args, null, 2)}</pre>
          )}
        </div>
      )
    case 'tool_result':
      return (
        <div className="step step-toolresult">
          <div className="step-title">📥 工具结果</div>
          <div className="step-body">{String(data.content ?? '')}</div>
        </div>
      )
    case 'task_end':
      return (
        <div className="step step-final">
          <div className="step-title">📦 交付</div>
          <div
            className="step-markdown"
            dangerouslySetInnerHTML={{ __html: marked.parse(String(data.reply ?? '')) }}
          />
        </div>
      )
    case 'error':
      return (
        <div className="step step-error">
          <div className="step-title">✕ 出错</div>
          <div className="step-body">{String(data.message ?? 'unknown error')}</div>
        </div>
      )
    default:
      return null
  }
}
