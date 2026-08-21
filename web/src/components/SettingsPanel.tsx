import { useEffect, useState } from 'react'
import { api } from '../api'
import type { AppConfig } from '../api'

const AGENT_ROLES: { key: string; name: string; hint: string }[] = [
  { key: 'planner', name: 'Planner', hint: '拆解目标' },
  { key: 'specialist', name: 'Specialist', hint: '执行子任务（工具循环）' },
  { key: 'evaluator', name: 'Evaluator', hint: '评估结果' },
  { key: 'reporter', name: 'Reporter', hint: '汇总交付' },
]

/** 设置 Tab：全局默认模型 + 各 Agent 独立 LLM + 运行参数。 */
export default function SettingsPanel() {
  const [cfg, setCfg] = useState<AppConfig | null>(null)
  const [defaultModel, setDefaultModel] = useState('')
  const [agentModels, setAgentModels] = useState<Record<string, string>>({})
  const [parallel, setParallel] = useState(4)
  const [replan, setReplan] = useState(1)
  const [maxSteps, setMaxSteps] = useState(10)
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const c = await api.getConfig()
        if (cancelled) return
        setCfg(c)
        setDefaultModel(c.default_model)
        setAgentModels(c.agent_models ?? {})
        setParallel(c.parallel)
        setReplan(c.max_replan_rounds)
        setMaxSteps(c.max_steps)
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  const save = async () => {
    setSaving(true)
    setMsg('')
    try {
      const c = await api.setConfig(parallel, replan, maxSteps, agentModels, defaultModel)
      setCfg(c)
      setDefaultModel(c.default_model)
      setAgentModels(c.agent_models ?? {})
      const n = Object.keys(c.agent_models ?? {}).length
      setMsg(
        `已保存：默认模型 ${c.active_model}${n ? ` · ${n} 个 Agent 独立模型` : ''}（重启后仍生效）`,
      )
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  if (error) return <div className="error-banner">{error}</div>
  if (!cfg) return <div className="empty">加载中…</div>

  return (
    <div className="settings-panel">
      <div className="agents-intro">
        <div className="tools-title">运行时设置</div>
        <div className="tools-hint">
          修改即时生效（下一任务起）并持久化到 data/config.json · 留空 = 使用 .env 默认
        </div>
      </div>

      <section className="settings-section">
        <div className="settings-section-title">默认模型</div>
        <label className="settings-row">
          <span className="settings-label">
            全局默认模型
            <span className="settings-hint">
              当前生效：<code>{cfg.active_model}</code>
              {defaultModel ? '（来自配置覆盖）' : '（.env LLM_MODEL）'}
            </span>
          </span>
          <input
            className="settings-input"
            placeholder="如 openai/gpt-4o-mini（空 = 用 .env LLM_MODEL）"
            value={defaultModel}
            onChange={(e) => setDefaultModel(e.target.value)}
          />
        </label>
      </section>

      <section className="settings-section">
        <div className="settings-section-title">Agent 独立模型（空 = 跟随默认模型）</div>
        {AGENT_ROLES.map((a) => (
          <label key={a.key} className="settings-row">
            <span className="settings-label">
              {a.name}
              <span className="settings-hint">{a.hint}</span>
            </span>
            <input
              className="settings-input"
              placeholder="默认"
              value={agentModels[a.key] ?? ''}
              onChange={(e) =>
                setAgentModels((prev) => ({ ...prev, [a.key]: e.target.value }))
              }
            />
          </label>
        ))}
      </section>

      <section className="settings-section">
        <div className="settings-section-title">运行参数</div>
        <div className="settings-row-group">
          <label className="settings-row">
            <span className="settings-label">Specialist 循环步数上限</span>
            <input
              type="number"
              className="settings-input settings-num"
              min={1}
              max={50}
              value={maxSteps}
              onChange={(e) =>
                setMaxSteps(Math.max(1, Math.min(50, Number(e.target.value) || 1)))
              }
            />
          </label>
          <label className="settings-row">
            <span className="settings-label">并行子任务数 N</span>
            <input
              type="number"
              className="settings-input settings-num"
              min={1}
              max={16}
              value={parallel}
              onChange={(e) =>
                setParallel(Math.max(1, Math.min(16, Number(e.target.value) || 1)))
              }
            />
          </label>
          <label className="settings-row">
            <span className="settings-label">失败重试（重规划轮数）</span>
            <input
              type="number"
              className="settings-input settings-num"
              min={0}
              max={5}
              value={replan}
              onChange={(e) =>
                setReplan(Math.max(0, Math.min(5, Number(e.target.value) || 0)))
              }
            />
          </label>
        </div>
      </section>

      <div className="settings-actions">
        <button onClick={() => void save()} disabled={saving}>
          {saving ? '保存中…' : '保存设置'}
        </button>
        {msg && <span className="settings-msg">{msg}</span>}
      </div>
    </div>
  )
}
