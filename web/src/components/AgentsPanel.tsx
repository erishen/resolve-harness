import { useEffect, useState } from 'react'
import { api } from '../api'
import FastPathModal from './FastPathModal'
import type { AgentsResponse, GraphNode } from '../types'

const NODE_W = 150
const NODE_H = 54

/** 节点中心坐标（后端给的是左上角）。 */
function cx(n: GraphNode): number {
  return n.x + (n.shape === 'diamond' ? 60 : NODE_W / 2)
}
function cy(n: GraphNode): number {
  return n.y + (n.shape === 'diamond' ? 30 : NODE_H / 2)
}

function NodeShape({ n, label }: { n: GraphNode; label?: string }) {
  const fill =
    n.kind === 'agent'
      ? 'var(--chip)'
      : n.kind === 'shortcut'
        ? 'var(--panel-2)'
        : n.kind === 'error'
          ? 'var(--danger, #c0392b)'
          : 'var(--accent)'
  const color = n.kind === 'error' ? '#fff' : 'var(--text)'
  const common = { fill, stroke: 'var(--border)', strokeWidth: 1.2 }
  const lines = (label ?? n.label).split('\n')
  const tspans = lines.map((l, i) => (
    <tspan key={i} x={cx(n)} dy={i === 0 ? '-0.35em' : '1.15em'}>
      {l}
    </tspan>
  ))
  const text = (
    <text
      y={cy(n)}
      textAnchor="middle"
      fontSize="12"
      fill={color}
      style={{ pointerEvents: 'none' }}
    >
      {tspans}
    </text>
  )
  if (n.shape === 'diamond') {
    return (
      <g>
        <polygon
          points={`${cx(n)},${n.y} ${n.x + 120},${cy(n)} ${cx(n)},${n.y + 60} ${n.x},${cy(n)}`}
          {...common}
        />
        {text}
      </g>
    )
  }
  if (n.shape === 'ellipse') {
    return (
      <g>
        <ellipse cx={cx(n)} cy={cy(n)} rx={NODE_W / 2} ry={NODE_H / 2} {...common} />
        {text}
      </g>
    )
  }
  return (
    <g>
      <rect x={n.x} y={n.y} width={NODE_W} height={NODE_H} rx={8} {...common} />
      {text}
    </g>
  )
}

interface AgentsPanelProps {
  /** 点击 Specialist 节点 → 跳到工具 Tab（查看其工具）。 */
  onGoTools?: () => void
}

export default function AgentsPanel({ onGoTools }: AgentsPanelProps) {
  const [data, setData] = useState<AgentsResponse | null>(null)
  const [error, setError] = useState('')
  const [fastOpen, setFastOpen] = useState(false)
  const [parallel, setParallel] = useState(4)
  const [replan, setReplan] = useState(1)
  const [maxSteps, setMaxSteps] = useState(10)
  const [agentModels, setAgentModels] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)
  const [configMsg, setConfigMsg] = useState('')

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const res = await api.agents()
        if (!cancelled) setData(res)
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
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
        const cfg = await api.getConfig()
        if (!cancelled) {
          setParallel(cfg.parallel)
          setReplan(cfg.max_replan_rounds)
          setMaxSteps(cfg.max_steps)
          setAgentModels(cfg.agent_models ?? {})
        }
      } catch {
        /* backend down */
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  const saveParallel = async () => {
    setSaving(true)
    setConfigMsg('')
    try {
      const cfg = await api.setConfig(parallel, replan, maxSteps, agentModels)
      setParallel(cfg.parallel)
      setReplan(cfg.max_replan_rounds)
      setMaxSteps(cfg.max_steps)
      setAgentModels(cfg.agent_models ?? {})
      const modelCount = Object.keys(cfg.agent_models ?? {}).length
      setConfigMsg(
        `已保存：Specialist 循环 ≤${cfg.max_steps} 步 · 并行 ×${cfg.parallel} · 失败重试 ${cfg.max_replan_rounds} 轮` +
          (modelCount ? ` · ${modelCount} 个 Agent 使用独立模型（重启后仍生效）` : '（重启后仍生效）'),
      )
    } catch (e) {
      setConfigMsg(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  if (error) return <div className="error-banner">{error}</div>
  if (!data) return <div className="empty">加载中…</div>

  const { agents, graph } = data
  const byId = new Map(graph.nodes.map((n) => [n.id, n]))
  const renderLabel = (n: GraphNode): string => {
    if (n.id === 'execute') {
      return `${n.label.replace('×N', `×${parallel}`)}\n🔁 ≤${maxSteps} 步`
    }
    return n.label
  }
  const renderEdgeLabel = (e: { from: string; to: string; label: string }): string =>
    e.from === 'decision' && e.to === 'plan'
      ? e.label.replace('限轮次', `限 ${replan} 轮`)
      : e.label

  return (
    <div className="agents-panel">
      <div className="agents-intro">
        <div className="tools-title">任务流水线 Agent（{agents.length} 个）</div>
        <div className="tools-hint">
          任务 Orchestrator 的四个角色 · 全程 LangGraph 循环驱动 · 每个任务独立沙箱与工具注册表
        </div>
      </div>

      <div className="agents-cards">
        {agents.map((a) => (
          <div key={a.name} className="agent-card">
            <div className="agent-card-head">
              <span className="agent-name">{a.name}</span>
              <span className="agent-role">{a.role}</span>
            </div>
            <div className="agent-desc">{a.description}</div>
            <div className="agent-tools">🛠 {a.tools}</div>
            <label className="agent-model-row">
              <span className="agent-model-label">LLM 模型（空 = 默认）</span>
              <input
                className="agent-model-input"
                placeholder="默认"
                value={agentModels[a.phase] ?? ''}
                onChange={(e) =>
                  setAgentModels((prev) => ({ ...prev, [a.phase]: e.target.value }))
                }
              />
            </label>
          </div>
        ))}
      </div>

      <div className="graph-wrap">
        <div className="graph-title">
          Orchestrator 流程
          <span className="graph-stats">
            🔁 Specialist 循环 ≤{maxSteps} 步 · ⚙ 并行 ×{parallel} · ↻ 失败重试 {replan} 轮
          </span>
        </div>
        <svg viewBox="0 0 680 700" width="100%" style={{ maxWidth: 680 }}>
          <defs>
            <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--text-dim)" />
            </marker>
          </defs>

          {/* edges */}
          {graph.edges.map((e, i) => {
            const a = byId.get(e.from)
            const b = byId.get(e.to)
            if (!a || !b) return null
            const x1 = cx(a)
            const y1 = cy(a)
            const x2 = cx(b)
            const y2 = cy(b)
            const labelX = (x1 + x2) / 2
            const labelY = (y1 + y2) / 2
            if (e.loop) {
              // decision -> plan: a curved loop on the left
              const path = `M ${x1 - 10} ${y1} C ${x1 - 120} ${y1 - 20}, ${x1 - 120} ${y1 - 90}, ${x2 - 10} ${y2 + 28}`
              return (
                <g key={i}>
                  <path d={path} fill="none" stroke="var(--text-dim)" strokeWidth="1.4" markerEnd="url(#arrow)" strokeDasharray="5 4" />
                  <text x={x1 - 125} y={(y1 + y2) / 2 - 40} fontSize="10" fill="var(--text-dim)" textAnchor="middle">
                    {renderEdgeLabel(e).split('\n').map((l, j) => (
                      <tspan key={j} x={x1 - 125} dy={j === 0 ? 0 : 11}>
                        {l}
                      </tspan>
                    ))}
                  </text>
                </g>
              )
            }
            const horiz = Math.abs(y2 - y1) < 4
            const isFastEdge = e.from === 'start' && e.to === 'fastpath'
            return (
              <g key={i}>
                {horiz ? (
                  <line x1={x1} y1={y1} x2={x2} y2={y2} stroke="var(--text-dim)" strokeWidth="1.4" markerEnd="url(#arrow)" />
                ) : (
                  <path d={`M ${x1} ${y1} C ${x1} ${(y1 + y2) / 2}, ${x2} ${(y1 + y2) / 2}, ${x2} ${y2}`} fill="none" stroke="var(--text-dim)" strokeWidth="1.4" markerEnd="url(#arrow)" />
                )}
                <text
                  x={labelX}
                  y={labelY}
                  fontSize="10"
                  fill={isFastEdge ? 'var(--ok, #3d9a50)' : 'var(--text-dim)'}
                  textAnchor="middle"
                  style={{ pointerEvents: isFastEdge ? 'auto' : 'none', cursor: isFastEdge ? 'pointer' : 'default' }}
                  onClick={isFastEdge ? () => setFastOpen(true) : undefined}
                >
                  {isFastEdge && (
                    <tspan x={labelX} dy={horiz ? -4 : 0} fontSize="11" fontWeight="700">
                      ⓘ
                    </tspan>
                  )}
                  {renderEdgeLabel(e).split('\n').map((l, j) => (
                    <tspan key={j} x={labelX} dy={j === 0 ? (horiz ? -4 : 0) : 11}>
                      {l}
                    </tspan>
                  ))}
                </text>
              </g>
            )
          })}

          {/* nodes */}
          {graph.nodes.map((n) => {
            const clickable =
              n.id === 'fastpath'
                ? () => setFastOpen(true)
                : n.id === 'execute'
                  ? () => onGoTools?.()
                  : undefined
            const tip =
              n.id === 'execute'
                ? '点击查看 Specialist 的工具（跳转工具 Tab）'
                : n.id === 'fastpath'
                  ? '点击查看 Fast Path 说明'
                  : undefined
            return (
              <g
                key={n.id}
                onClick={clickable}
                style={{
                  cursor: clickable ? 'pointer' : 'default',
                }}
              >
                {tip && <title>{tip}</title>}
                <NodeShape n={n} label={renderLabel(n)} />
              </g>
            )
          })}
        </svg>
        <div className="parallel-row">
          <span className="parallel-label">🔁 Specialist 循环步数上限</span>
          <input
            type="number"
            min={1}
            max={50}
            value={maxSteps}
            onChange={(e) => setMaxSteps(Math.max(1, Math.min(50, Number(e.target.value) || 1)))}
          />
          <span className="parallel-label">⚙ 并行子任务数 N（Specialist 并发数）</span>
          <input
            type="number"
            min={1}
            max={16}
            value={parallel}
            onChange={(e) => setParallel(Math.max(1, Math.min(16, Number(e.target.value) || 1)))}
          />
          <span className="parallel-label">失败重试（重规划轮数）</span>
          <input
            type="number"
            min={0}
            max={5}
            value={replan}
            onChange={(e) => setReplan(Math.max(0, Math.min(5, Number(e.target.value) || 0)))}
          />
          <button onClick={() => void saveParallel()} disabled={saving}>
            {saving ? '保存中…' : '保存'}
          </button>
          {configMsg && <span className="parallel-msg">{configMsg}</span>}
        </div>
        <div className="graph-hint">
          ⓘ <b>objective 命中确定性查询</b> → 点「Fast Path」或其上方标签查看说明 · 点「Specialist ×N」查看其工具
        </div>
      </div>
      {fastOpen && <FastPathModal onClose={() => setFastOpen(false)} />}
    </div>
  )
}
