import { useEffect, useState } from 'react'
import { api, type AppConfig } from '../api'
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

function NodeShape({
  n,
  label,
  clickable,
  valign = 'center',
}: {
  n: GraphNode
  label?: string
  clickable?: boolean
  valign?: 'center' | 'bottom'
}) {
  const t = NODE_THEME[n.kind] ?? NODE_THEME.agent
  const common = { fill: t.fill, stroke: t.stroke, strokeWidth: 1.4 }
  // 「居中下」：横向居中，纵向落在节点真正中心（比默认略低一点点，不过度下移）。
  const textY = valign === 'bottom' ? cy(n) : cy(n)
  const dy0 = valign === 'bottom' ? 0 : '-0.3em'
  const lines = (label ?? n.label).split('\n')
  const tspans = lines.map((l, i) => (
    <tspan key={i} x={cx(n)} dy={i === 0 ? dy0 : '1.05em'}>
      {l}
    </tspan>
  ))
  const text = (
    <text
      y={textY}
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
  // 可点击节点：外层加 accent 虚线「可点击环」(halo)，hover 高亮。
  const halo = clickable ? (
    <g className="click-halo">
      {n.shape === 'diamond' ? (
        <polygon points={`${cx(n)},${n.y - 5} ${n.x + 125},${cy(n)} ${cx(n)},${n.y + 65} ${n.x - 5},${cy(n)}`} />
      ) : n.shape === 'ellipse' ? (
        <ellipse cx={cx(n)} cy={cy(n)} rx={NODE_W / 2 + 4} ry={NODE_H / 2 + 4} />
      ) : (
        <rect x={n.x - 4} y={n.y - 4} width={NODE_W + 8} height={NODE_H + 8} rx={12} />
      )}
    </g>
  ) : null
  const shapeEl =
    n.shape === 'diamond' ? (
      <polygon
        points={`${cx(n)},${n.y} ${n.x + 120},${cy(n)} ${cx(n)},${n.y + 60} ${n.x},${cy(n)}`}
        {...common}
      />
    ) : n.shape === 'ellipse' ? (
      <ellipse cx={cx(n)} cy={cy(n)} rx={NODE_W / 2} ry={NODE_H / 2} {...common} />
    ) : (
      <rect x={n.x} y={n.y} width={NODE_W} height={NODE_H} rx={8} {...common} />
    )
  return (
    <g className={clickable ? 'graph-node clickable' : 'graph-node'}>
      {halo}
      {shapeEl}
      {text}
    </g>
  )
}

interface AgentsPanelProps {
  /** 点击 Specialist 节点 → 跳到工具 Tab（查看其工具）。 */
  onGoTools?: () => void
  /** 点击 codegen 节点 → 跳到插件 Tab（查看已注册插件）。 */
  onGoPlugins?: () => void
  /** 点击图上方运行参数统计 → 跳到设置 Tab（统一编辑步数/并行/重试/模型）。 */
  onGoSettings?: () => void
}

export default function AgentsPanel({ onGoTools, onGoPlugins, onGoSettings }: AgentsPanelProps) {
  const [data, setData] = useState<AgentsResponse | null>(null)
  const [error, setError] = useState('')
  const [fastOpen, setFastOpen] = useState(false)
  // 模型映射（agent_models[phase] → default_model → .env），用于展示每个角色实际采用的模型
  const [config, setConfig] = useState<AppConfig | null>(null)
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
      try {
        const cfg = await api.getConfig()
        if (!cancelled) setConfig(cfg)
      } catch {
        /* 模型映射缺失不影响主流程展示 */
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

  // 解析某角色实际采用的模型：优先该角色的独立配置，否则 .env 基线（与聊天模型独立）。
  // 若标识命中模型库别名则取具体 model 名，否则原样展示（已是具体模型名）。
  const resolveModel = (phase: string): { name: string; override: boolean } | null => {
    if (!config) return null
    const id =
      config.agent_models?.[phase] ||
      config.env_model?.model ||
      ''
    if (!id) return { name: '未配置', override: false }
    const profile = config.models?.[id]
    return { name: profile?.model || id, override: Boolean(config.agent_models?.[phase]) }
  }

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
            {(() => {
              const m = resolveModel(a.phase)
              if (!m) {
                return (
                  <div className="agent-model-note">🧠 模型配置见「设置」Tab</div>
                )
              }
              return (
                <div
                  className="agent-model-note agent-model-link"
                  role="button"
                  tabIndex={0}
                  title="点击修改模型配置（跳转设置 Tab）"
                  onClick={() => onGoSettings?.()}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault()
                      onGoSettings?.()
                    }
                  }}
                >
                  🧠 模型：<b>{m.name}</b>
                  {m.name && m.name !== '未配置' ? `（${m.override ? '独立配置' : '默认'}）` : ''}
                </div>
              )
            })()}
          </div>
        ))}
      </div>

      <div className="graph-wrap">
        <div className="graph-title">
          Orchestrator 流程
          <span
            className="graph-stats graph-stats-link"
            role="button"
            tabIndex={0}
            title="点击修改这些运行参数（跳转设置 Tab）"
            onClick={() => onGoSettings?.()}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                onGoSettings?.()
              }
            }}
          >
            🔁 Specialist 循环 ≤{maxSteps} 步 · ⚙ 并行 ×{parallel} · ↻ 失败重试 {replan} 轮
          </span>
        </div>
        <svg viewBox="0 0 700 720" width="100%" style={{ maxWidth: 700 }}>
          <defs>
            <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7.5" markerHeight="7.5" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--text-dim)" />
            </marker>
          </defs>

          {/* edges — 第一遍只画连线（所有 line/path），第二遍再画标签。
              否则 fastpath→end 与 codegen→end 都落在 x=595 的竖线上，
              后画的 codegen→end 连线会压在先画的 fastpath→end 胶囊上，
              导致「代码直算 / 零模型」像是被线压住。 */}
          {graph.edges.map((e, i) => {
            const a = byId.get(e.from)
            const b = byId.get(e.to)
            if (!a || !b) return null
            const x1 = cx(a)
            const y1 = cy(a)
            const x2 = cx(b)
            const y2 = cy(b)
            if (e.loop) {
              const outerX = x1 + 110 // decision 右尖(400) -> 450：垂直线段在「结束」左缘(520)内、Fast Path 垂直边(595)外
              // 末段从右侧走廊左拐、箭头停在 Planner 右缘(x2+NODE_W/2)中点(y2)，
              // 箭头头落在节点边框外、清晰指向「Planner 拆解目标」(不再埋进节点内被 rect 遮住)。
              const path = `M ${x1 + 60} ${y1} L ${outerX} ${y1} L ${outerX} ${y2} L ${x2 + NODE_W / 2} ${y2}`
              return <path key={`g${i}`} d={path} fill="none" stroke="var(--text-dim)" strokeWidth="1.4" markerEnd="url(#arrow)" strokeDasharray="5 4" />
            }
            const horiz = Math.abs(y2 - y1) < 4
            return horiz ? (
              <line key={`g${i}`} x1={x1} y1={y1} x2={x2} y2={y2} stroke="var(--text-dim)" strokeWidth="1.4" markerEnd="url(#arrow)" />
            ) : (
              <path key={`g${i}`} d={`M ${x1} ${y1} C ${x1} ${(y1 + y2) / 2}, ${x2} ${(y1 + y2) / 2}, ${x2} ${y2}`} fill="none" stroke="var(--text-dim)" strokeWidth="1.4" markerEnd="url(#arrow)" />
            )
          })}
          {/* edge labels — 全部画在连线之上，保证胶囊永远遮住线 */}
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
              // 文案放在循环回环可见区域的「中间」：垂直线段 x=outerX 上、纵向中点处，
              // 居中对齐并带面板底色描边，清晰可读且不压任何节点 / 路径。
              const outerX = x1 + 110
              const lx = outerX
              const ly = (y1 + y2) / 2 - 5
              const loopLines = renderEdgeLabel(e).split('\n')
              const loopPillW = Math.max(...loopLines.map((l) => l.length)) * 10 * 0.95 + 10
              const loopPillH = loopLines.length * 12 + 6
              const loopBlockCenter = ly + ((loopLines.length - 1) * 11) / 2
              return (
                <g key={`l${i}`}>
                  <rect
                    x={lx - loopPillW / 2}
                    y={loopBlockCenter - loopPillH / 2}
                    width={loopPillW}
                    height={loopPillH}
                    rx={5}
                    fill="var(--panel)"
                    stroke="var(--border)"
                    strokeWidth={0.8}
                  />
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
                    {loopLines.map((l, j) => (
                      <tspan key={j} x={lx} dy={j === 0 ? 0 : 11}>
                        {l}
                      </tspan>
                    ))}
                  </text>
                </g>
              )
            }
            const isFastEdge = e.from === 'start' && e.to === 'fastpath'
            const labelLines = renderEdgeLabel(e).split('\n')
            // 背景胶囊：完全遮住底下的连线，文案不再「压在线上」看不清
            //（尤其是右侧竖直/陡边上的「代码直算 / 零模型 / 成功即持久化复用」）。
            const FS = 10
            const LHEIGHT = 12
            const extra = isFastEdge ? 1 : 0
            const totalLines = labelLines.length + extra
            const maxChars = Math.max(...labelLines.map((l) => l.length), isFastEdge ? 1 : 0)
            const pillW = maxChars * FS * 0.95 + (isFastEdge ? 16 : 10)
            const pillH = totalLines * LHEIGHT + (isFastEdge ? 8 : 6)
            const pillX = labelX - pillW / 2
            const pillY = labelY - pillH / 2
            const firstDy = -((totalLines - 1) * LHEIGHT) / 2 + (totalLines === 1 ? 3.5 : 1.5)
            return (
              <g key={`l${i}`}>
                <rect x={pillX} y={pillY} width={pillW} height={pillH} rx={5} fill="var(--panel)" stroke="var(--border)" strokeWidth={0.8} />
                <text
                  x={labelX}
                  y={labelY}
                  fontSize={FS}
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
                    <tspan x={labelX} dy={firstDy} fontSize="11" fontWeight="700">
                      ⓘ
                    </tspan>
                  )}
                  {labelLines.map((l, j) => (
                    <tspan key={j} x={labelX} dy={j === 0 ? (isFastEdge ? LHEIGHT : firstDy) : LHEIGHT}>
                      {l}
                    </tspan>
                  ))}
                </text>
              </g>
            )
          })}

          {/* nodes */}
          {graph.nodes.map((n) => {
            const onClick =
              n.id === 'fastpath'
                ? () => setFastOpen(true)
                : n.id === 'execute'
                  ? () => onGoTools?.()
                  : n.id === 'codegen'
                    ? () => onGoPlugins?.()
                    : undefined
            const isClickable = Boolean(onClick)
            const tip =
              n.id === 'execute'
                ? '点击查看 Specialist 的工具（跳转工具 Tab）'
                : n.id === 'fastpath'
                  ? '点击查看 Fast Path 说明'
                  : n.id === 'codegen'
                    ? '点击查看已注册插件（跳转插件 Tab）'
                    : undefined
            return (
              <g
                key={n.id}
                className={isClickable ? 'graph-node clickable' : 'graph-node'}
                onClick={onClick}
                style={{
                  cursor: isClickable ? 'pointer' : 'default',
                }}
              >
                {tip && <title>{tip}</title>}
                <NodeShape
                  n={n}
                  label={renderLabel(n)}
                  clickable={isClickable}
                  valign={n.id === 'start' || n.id === 'end' || n.id === 'error' ? 'bottom' : 'center'}
                />
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
          ⓘ <b>objective 命中确定性查询</b> → 点「Fast Path」或其上方标签查看说明 · 点「Specialist ×N」查看其工具 · 点「codegen」查看已注册插件 · 运行参数与模型配置见「设置」Tab（带虚线环的节点可点击）
        </div>
      </div>
      {fastOpen && <FastPathModal onClose={() => setFastOpen(false)} />}
    </div>
  )
}
