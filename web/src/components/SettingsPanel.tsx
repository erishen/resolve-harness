import { useEffect, useState } from 'react'
import { api } from '../api'
import type { AppConfig } from '../api'

const AGENT_ROLES: { key: string; name: string; hint: string }[] = [
  { key: 'planner', name: 'Planner', hint: '拆解目标' },
  { key: 'specialist', name: 'Specialist', hint: '执行子任务（工具循环）' },
  { key: 'evaluator', name: 'Evaluator', hint: '评估结果' },
  { key: 'reporter', name: 'Reporter', hint: '汇总交付' },
]

interface ProfileRow {
  alias: string
  base_url: string
  model: string
  api_key_env: string
}

function profilesToRows(models: Record<string, { base_url: string; model: string; api_key_env: string }>): ProfileRow[] {
  return Object.entries(models ?? {}).map(([alias, p]) => ({
    alias,
    base_url: p.base_url ?? '',
    model: p.model ?? '',
    api_key_env: p.api_key_env ?? '',
  }))
}

function rowsToProfiles(rows: ProfileRow[]): Record<string, { base_url: string; model: string; api_key_env: string }> {
  const out: Record<string, { base_url: string; model: string; api_key_env: string }> = {}
  for (const r of rows) {
    const alias = r.alias.trim()
    if (!alias || !r.model.trim()) continue
    out[alias] = {
      base_url: r.base_url.trim(),
      model: r.model.trim(),
      api_key_env: r.api_key_env.trim(),
    }
  }
  return out
}

/** 数字输入上下限：即时 clamp 到 [min,max]，允许清空编辑，失焦补回下限。 */
function clampNum(n: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, n))
}

/** 受控数字输入 onChange：仅数字串，超上限立即截断，空串允许（编辑中）。 */
function numOnChange(setter: (v: string) => void, min: number, max: number) {
  return (e: { target: { value: string } }) => {
    const v = e.target.value
    if (v === '') {
      setter('')
      return
    }
    if (!/^\d+$/.test(v)) return
    setter(String(clampNum(Number(v), min, max)))
  }
}

/** 失焦：空串补回下限。 */
function numOnBlur(setter: (v: string) => void, current: string, min: number) {
  return () => {
    if (current === '') setter(String(min))
  }
}

/** 设置 Tab：模型库（baseURL + Key + 模型名）+ 默认/各 Agent 模型 + 运行参数。 */
export default function SettingsPanel() {
  const [cfg, setCfg] = useState<AppConfig | null>(null)
  const [rows, setRows] = useState<ProfileRow[]>([])
  const [defaultModel, setDefaultModel] = useState('')
  const [agentModels, setAgentModels] = useState<Record<string, string>>({})
  // 字符串态：允许清空编辑，blur 补下限，onChange 即时 clamp 上下限
  const [parallel, setParallel] = useState('4')
  const [replan, setReplan] = useState('1')
  const [maxSteps, setMaxSteps] = useState('10')
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
        setRows(profilesToRows(c.models ?? {}))
        setDefaultModel(c.default_model)
        setAgentModels(c.agent_models ?? {})
        setParallel(String(c.parallel))
        setReplan(String(c.max_replan_rounds))
        setMaxSteps(String(c.max_steps))
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  const aliases = rows.map((r) => r.alias.trim()).filter(Boolean)

  const save = async () => {
    setSaving(true)
    setMsg('')
    try {
      const c = await api.setConfig(
        clampNum(Number(parallel) || 4, 1, 16),
        clampNum(Number(replan) || 1, 0, 5),
        clampNum(Number(maxSteps) || 10, 1, 50),
        agentModels,
        defaultModel,
        rowsToProfiles(rows),
      )
      setCfg(c)
      setRows(profilesToRows(c.models ?? {}))
      setDefaultModel(c.default_model)
      setAgentModels(c.agent_models ?? {})
      setParallel(String(c.parallel))
      setReplan(String(c.max_replan_rounds))
      setMaxSteps(String(c.max_steps))
      const n = Object.keys(c.models ?? {}).length
      setMsg(
        `已保存：默认模型 ${c.active_model} · 模型库 ${n} 个` +
          `${Object.keys(c.agent_models ?? {}).length ? ` · ${Object.keys(c.agent_models ?? {}).length} 个 Agent 独立模型` : ''}（重启后仍生效）`,
      )
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  if (error) return <div className="error-banner">{error}</div>
  if (!cfg) return <div className="empty">加载中…</div>

  const setRow = (i: number, key: keyof ProfileRow, value: string) =>
    setRows((prev) => prev.map((r, j) => (j === i ? { ...r, [key]: value } : r)))

  return (
    <div className="settings-panel">
      <div className="agents-intro">
        <div className="tools-title">运行时设置</div>
        <div className="tools-hint">
          修改即时生效（下一任务起）并持久化到 data/config.json · API Key 只填「环境变量名」，实际 key 放 .env
        </div>
      </div>

      <section className="settings-section">
        <div className="settings-section-title">模型库（baseURL + Key + 模型名）</div>
        <div className="settings-profiles">
          <div className="settings-profile-head">
            <span>别名</span>
            <span>Base URL</span>
            <span>模型名</span>
            <span>API Key（环境变量名）</span>
            <span />
          </div>
          <div className="settings-profile-row settings-env-row" title="来自 .env 的默认模型（只读）">
            <span className="settings-env-alias">.env 默认</span>
            <span className="settings-env-value">{cfg.env_model?.base_url || '—'}</span>
            <span className="settings-env-value">{cfg.env_model?.model || '—'}</span>
            <span className="settings-env-value">
              {cfg.env_model?.api_key_env || 'LLM_API_KEY'}（.env 内）
            </span>
            <span />
          </div>
          {rows.map((r, i) => (
            <div key={i} className="settings-profile-row">
              <input
                className="settings-input"
                placeholder="如 fast"
                value={r.alias}
                onChange={(e) => setRow(i, 'alias', e.target.value)}
              />
              <input
                className="settings-input"
                placeholder="https://api.example.com/v1"
                value={r.base_url}
                onChange={(e) => setRow(i, 'base_url', e.target.value)}
              />
              <input
                className="settings-input"
                placeholder="openai/gpt-4o-mini"
                value={r.model}
                onChange={(e) => setRow(i, 'model', e.target.value)}
              />
              <input
                className="settings-input"
                placeholder="如 OPENAI_API_KEY"
                value={r.api_key_env}
                onChange={(e) => setRow(i, 'api_key_env', e.target.value)}
              />
              <button
                type="button"
                className="settings-del"
                onClick={() => setRows((prev) => prev.filter((_, j) => j !== i))}
                title="删除该模型"
              >
                ✕
              </button>
            </div>
          ))}
          <button
            type="button"
            className="settings-add"
            onClick={() =>
              setRows((prev) => [...prev, { alias: '', base_url: '', model: '', api_key_env: '' }])
            }
          >
            + 添加模型
          </button>
        </div>
      </section>

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
          <select
            className="settings-input"
            value={defaultModel}
            onChange={(e) => setDefaultModel(e.target.value)}
          >
            <option value="">默认（.env LLM_MODEL）</option>
            {aliases.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
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
            <select
              className="settings-input"
              value={agentModels[a.key] ?? ''}
              onChange={(e) =>
                setAgentModels((prev) => ({ ...prev, [a.key]: e.target.value }))
              }
            >
              <option value="">默认（跟随默认模型）</option>
              {aliases.map((x) => (
                <option key={x} value={x}>
                  {x}
                </option>
              ))}
            </select>
          </label>
        ))}
      </section>

      <section className="settings-section">
        <div className="settings-section-title">运行参数</div>
        <div className="settings-row-group">
          <label className="settings-row">
            <span className="settings-label">
              Specialist 循环步数上限
              <span className="settings-hint">1 - 50</span>
            </span>
            <input
              type="number"
              className="settings-input settings-num"
              min={1}
              max={50}
              value={maxSteps}
              onChange={numOnChange(setMaxSteps, 1, 50)}
              onBlur={numOnBlur(setMaxSteps, maxSteps, 1)}
            />
          </label>
          <label className="settings-row">
            <span className="settings-label">
              并行子任务数 N
              <span className="settings-hint">1 - 16</span>
            </span>
            <input
              type="number"
              className="settings-input settings-num"
              min={1}
              max={16}
              value={parallel}
              onChange={numOnChange(setParallel, 1, 16)}
              onBlur={numOnBlur(setParallel, parallel, 1)}
            />
          </label>
          <label className="settings-row">
            <span className="settings-label">
              失败重试（重规划轮数）
              <span className="settings-hint">0 - 5</span>
            </span>
            <input
              type="number"
              className="settings-input settings-num"
              min={0}
              max={5}
              value={replan}
              onChange={numOnChange(setReplan, 0, 5)}
              onBlur={numOnBlur(setReplan, replan, 0)}
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
