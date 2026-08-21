import { useEffect, useState } from 'react'
import { api } from '../api'
import FastPathModal from './FastPathModal'
import type { AgentsResponse, GraphNode } from '../types'

const NODE_W = 150
const NODE_H = 54

/** 节点按类型配色（CSS 变量，自动适配明暗主题）。 */
const NODE_THEME: Record<string, { fill: string; stroke: string; color: string }> = {
  agent: { fill: 'var(--chip)', stroke: 'var(--accent)', color: 'var(--text)' },
  shortcut: { fill: 'var(--panel-2)', stroke: 'var(--ok, #3d9a50)', color: 'var(--text)' },
  gate: { fill: 'var(--panel-2)', stroke: 'var(--warn, #b98a2f)', color: 'var(--text)' },
  error: { fill: 'var(--danger, #c0392b)', stroke: '#a93226', color: '#fff' },
  entry: { fill: 'var(--accent)', stroke: 'var(--accent)', color: '#fff' },
  exit: { fill: 'var(--ok, #3d9a50)', stroke: 'var(--ok, #3d9a50)', color: '#fff' },
}

/** 节点中心坐标（后端给的是左上角）。 */
function cx(n: GraphNode): number {
  return n.x + (n.shape === 'diamond' ? 60 : NODE_W / 2)
}
function cy(n: GraphNode): number {
  return n.y + (n.shape === 'diamond' ? 30 : NODE_H / 2)
}

function NodeShape({ n, label }: { n: GraphNode; label?: string }) {
  const t = NODE_THEME[n.kind] ?? NODE_THEME.agent
  const common = { fill: t.fill, stroke: t.stroke, strokeWidth: 1.4 }
  const lines = (label ?? n.label).split('\n')
  const tspans = lines.map((l, i) => (
    <tspan key={i} x={cx(n)} dy={i === 0 ? '-0.3em' : '1.05em'}>
      {l}
    </tspan>
  ))
  const text = (
    <text
      y={cy(n)}
      textAnchor="middle"
      fontSize="12"
      fill={t.color}
      paintOrder="stroke"
      stroke="var(--panel)"
      strokeWidth={3}
      strokeLinejoin="round"
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
  // 只读展示：配置（模型/步数/并行/重试）统一在「设置」Tab 编辑
  const [parallel, setParallel] = useState(4)
  const [replan, setReplan] = useState(1)
  const [maxSteps, setMaxSteps] = useState(10)

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
        }
      } catch {
        /* backend down */
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

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
            <div className="agent-model-note">
              🧠 模型配置见「设置」Tab（此角色当前用默认或已配置的独立模型）
            </div>
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
        <svg viewBox="0 0 700 720" width="100%" style={{ maxWidth: 700 }}>
          <defs>
            <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7.5" markerHeight="7.5" orient="auto-start-reverse">
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
              // decision -> plan: 走 evaluate/decision 之间的走廊向上绕回，
              // 避免穿过 Specialist（控制点保持在 x>x1-130 通道内）
              const path = `M ${x1 - 60} ${y1} C ${x1 - 130} ${y1 - 20}, ${x1 - 130} ${y1 - 220}, ${x2 - 5} ${y2 + 28}`
              const lx = x1 - 130
              const ly = (y1 + y2) / 2 - 30
              return (
                <g key={i}>
                  <path d={path} fill="none" stroke="var(--text-dim)" strokeWidth="1.4" markerEnd="url(#arrow)" strokeDasharray="5 4" />
                  <text
                    x={lx}
                    y={ly}
                    fontSize="10"
                    fill="var(--text-dim)"
                    textAnchor="middle"
                    paintOrder="stroke"
                    stroke="var(--panel)"
                    strokeWidth={3}
                    strokeLinejoin="round"
                  >
                    {renderEdgeLabel(e).split('\n').map((l, j) => (
                      <tspan key={j} x={lx} dy={j === 0 ? 0 : 11}>
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
                  paintOrder="stroke"
                  stroke="var(--panel)"
                  strokeWidth={3}
                  strokeLinejoin="round"
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
        <div className="graph-legend">
          <span className="legend-item legend-agent">Agent</span>
          <span className="legend-item legend-shortcut">快捷短路</span>
          <span className="legend-item legend-gate">判定</span>
          <span className="legend-item legend-entry">开始 / 结束</span>
          <span className="legend-item legend-error">错误</span>
          <span className="legend-item legend-loop">虚线 = 失败重规划回环</span>
        </div>
        <div className="graph-hint">
          ⓘ <b>objective 命中确定性查询</b> → 点「Fast Path」或其上方标签查看说明 · 点「Specialist ×N」查看其工具 · 运行参数与模型配置见「设置」Tab
        </div>
      </div>
      {fastOpen && <FastPathModal onClose={() => setFastOpen(false)} />}
    </div>
  )
}
