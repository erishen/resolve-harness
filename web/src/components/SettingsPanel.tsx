import { useEffect, useRef, useState } from 'react'
import { api, getApiToken, setApiToken } from '../api'
import type { AppConfig, ModelProfile } from '../api'

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

/**
 * 归一化后的模型库：只保留「别名+模型名都完整」的 profile，且空 base_url /
 * api_key_env 直接剔除（连 key 都不留）——与后端 _load_config 的清洗逻辑保持
 * 一致。用它来比较与提交，可避免「前端含空串 vs 后端已丢弃空串」导致的无限
 * 保存循环。
 */
function buildModels(rows: ProfileRow[]): Record<string, ModelProfile> {
  const raw = rowsToProfiles(rows)
  const out: Record<string, ModelProfile> = {}
  for (const [alias, p] of Object.entries(raw)) {
    const m: Partial<ModelProfile> = { model: p.model }
    if (p.base_url) m.base_url = p.base_url
    if (p.api_key_env) m.api_key_env = p.api_key_env
    out[alias] = m as ModelProfile
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

/** 递归、按 key 排序的 JSON 序列化，保证对象 key 顺序不影响比较结果
 * （前端 buildModels 与后端 _load_config 的 key 顺序不同，必须排序才能
 * 让「未变化则跳过保存」的判断成立）。 */
function stableStringify(v: unknown): string {
  if (Array.isArray(v)) return '[' + v.map(stableStringify).join(',') + ']'
  if (v && typeof v === 'object') {
    const entries = Object.entries(v as Record<string, unknown>)
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
      .map(([k, val]) => JSON.stringify(k) + ':' + stableStringify(val))
    return '{' + entries.join(',') + '}'
  }
  return JSON.stringify(v)
}

/** 把完整配置序列化成稳定字符串，用于判断「相对上次保存是否真的变了」。 */
function serializeConfig(
  parallel: number,
  maxReplan: number,
  maxSteps: number,
  agentModels: Record<string, string>,
  defaultModel: string,
  models: Record<string, ModelProfile>,
): string {
  return stableStringify({ parallel, maxReplan, maxSteps, agentModels, defaultModel, models })
}

/** 字段级基准：上次成功保存后的各字段原始值，用于只提交「真正变化」的字段。 */
interface SavedValues {
  parallel: number
  replan: number
  maxSteps: number
  agentModels: Record<string, string>
  defaultModel: string
  models: Record<string, ModelProfile>
}

function toVals(c: AppConfig): SavedValues {
  return {
    parallel: c.parallel,
    replan: c.max_replan_rounds,
    maxSteps: c.max_steps,
    agentModels: c.agent_models ?? {},
    defaultModel: c.default_model ?? '',
    models: c.models ?? {},
  }
}

const jsonSame = (a: unknown, b: unknown): boolean => JSON.stringify(a) === JSON.stringify(b)

/** 失焦：空串补回下限。 */
function numOnBlur(setter: (v: string) => void, current: string, min: number) {
  return () => {
    if (current === '') setter(String(min))
  }
}

/** 设置 Tab：模型库（baseURL + Key + 模型名）+ 聊天模型 + 任务模型（各 Agent 角色）+ 运行参数。
 * @param onModelChange 任意配置保存成功后触发，通知外层刷新顶栏「当前模型」tag。 */
export default function SettingsPanel({ onModelChange }: { onModelChange?: () => void }) {
  const [cfg, setCfg] = useState<AppConfig | null>(null)
  const [rows, setRows] = useState<ProfileRow[]>([])
  const [defaultModel, setDefaultModel] = useState('')
  const [agentModels, setAgentModels] = useState<Record<string, string>>({})
  // 字符串态：允许清空编辑，blur 补下限，onChange 即时 clamp 上下限
  const [parallel, setParallel] = useState('4')
  const [replan, setReplan] = useState('1')
  const [maxSteps, setMaxSteps] = useState('10')
  // API Token：仅存本机 localStorage，对应 .env 的 API_TOKEN（后端启用校验时必填）
  const [tokenDraft, setTokenDraft] = useState(getApiToken())
  const [tokenMsg, setTokenMsg] = useState('')
  const [autoSaving, setAutoSaving] = useState(false)
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')
  // 上次成功保存到后端的完整配置（序列化），用于判断是否需要触发自动保存。
  const lastSavedRef = useRef<string | null>(null)
  const lastValsRef = useRef<SavedValues | null>(null)

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
        // 以「后端读到的、已清洗的配置」为基准，避免加载后立刻误触发保存。
        // （此处不能用 buildModels(rows)：load effect 闭包里的 rows 仍是初始
        // 空值，setState 不会更新已运行函数内的变量；而 c.models 已是后端归一
        // 化结果，与后续 buildModels(profilesToRows(c.models)) 完全等价。）
        lastSavedRef.current = serializeConfig(
          c.parallel,
          c.max_replan_rounds,
          c.max_steps,
          c.agent_models ?? {},
          c.default_model ?? '',
          c.models ?? {},
        )
        lastValsRef.current = toVals(c)
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  // 即时自动保存：任一设置项变化后防抖 600ms，只提交「相对上次保存真正变化」
  // 的字段（未变字段后端保持磁盘现值，避免 UI 全量提交覆盖外部修改）。
  useEffect(() => {
    if (!cfg) return
    const models = buildModels(rows)
    const candidate = serializeConfig(
      clampNum(Number(parallel) || 4, 1, 16),
      clampNum(Number(replan) || 1, 0, 5),
      clampNum(Number(maxSteps) || 10, 1, 50),
      agentModels,
      defaultModel,
      models,
    )
    if (candidate === lastSavedRef.current) return
    const t = setTimeout(() => {
      ;(async () => {
        setAutoSaving(true)
        try {
          const base = lastValsRef.current
          const curParallel = clampNum(Number(parallel) || 4, 1, 16)
          const curReplan = clampNum(Number(replan) || 1, 0, 5)
          const curMaxSteps = clampNum(Number(maxSteps) || 10, 1, 50)
          const curModels = buildModels(rows)
          const p = base && jsonSame(curParallel, base.parallel) ? null : curParallel
          const r = base && jsonSame(curReplan, base.replan) ? null : curReplan
          const s = base && jsonSame(curMaxSteps, base.maxSteps) ? null : curMaxSteps
          const am = base && jsonSame(agentModels, base.agentModels) ? undefined : agentModels
          const dm = base && jsonSame(defaultModel, base.defaultModel) ? undefined : defaultModel
          const m = base && jsonSame(curModels, base.models) ? undefined : curModels
          if (p === null && r === null && s === null && am === undefined && dm === undefined && m === undefined) {
            return // 理论不可达（candidate 已拦截），防御性兜底
          }
          const c = await api.setConfig(p, r, s, am, dm, m)
          setCfg(c)
          lastSavedRef.current = serializeConfig(
            c.parallel,
            c.max_replan_rounds,
            c.max_steps,
            c.agent_models ?? {},
            c.default_model ?? '',
            c.models ?? {},
          )
          lastValsRef.current = toVals(c)
          setMsg('已自动保存 · 下一任务起生效')
          // 聊天模型 / 模型库可能已变化，通知外层刷新顶栏当前模型 tag。
          onModelChange?.()
        } catch (e) {
          const raw = e instanceof Error ? e.message : String(e)
          let detail = raw
          const m = raw.match(/^\d+\s+(\{.*\})$/)
          if (m) {
            try {
              detail = JSON.parse(m[1]).detail || raw
            } catch {
              /* 解析失败则用原始文案 */
            }
          }
          setMsg(`保存失败：${detail}`)
        } finally {
          setAutoSaving(false)
        }
      })()
    }, 600)
    return () => clearTimeout(t)
  }, [cfg, rows, defaultModel, agentModels, parallel, replan, maxSteps])

  const aliases = Object.keys(buildModels(rows))

  if (error) return <div className="error-banner">{error}</div>
  if (!cfg) return <div className="empty">加载中…</div>

  const setRow = (i: number, key: keyof ProfileRow, value: string) =>
    setRows((prev) => prev.map((r, j) => (j === i ? { ...r, [key]: value } : r)))

  return (
    <div className="settings-panel">
      <div className="agents-intro">
        <div className="tools-title">运行时设置</div>
        <div className="tools-hint">
          修改即时自动保存并持久化到 data/config.json（下一任务起生效）· API Key 可直接粘贴密钥，或填环境变量名（如 OPENAI_API_KEY）从 .env 读取；留空则使用 .env 默认密钥
        </div>
      </div>

      <section className="settings-section">
        <div className="settings-section-title">模型库（baseURL + Key + 模型名）</div>
        <div className="settings-profiles">
          <div className="settings-profile-head">
            <span>别名</span>
            <span>Base URL</span>
            <span>模型名</span>
            <span>API Key（可粘贴密钥 / 环境变量名）</span>
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
                type="password"
                autoComplete="off"
                placeholder="sk-... 或直接粘贴密钥 / OPENAI_API_KEY"
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
        <div className="settings-section-title">聊天模型</div>
        <label className="settings-row">
          <span className="settings-label">
            聊天模型
            <span className="settings-hint">
              聊天当前生效：<code>{cfg.active_model}</code>
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
        <div className="settings-section-title">任务模型（空 = 跟随 .env 基线，与聊天模型独立）</div>
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
              <option value="">默认（跟随 .env 基线）</option>
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

      <section className="settings-section">
        <div className="settings-section-title">API Token（远程访问鉴权）</div>
        <div className="settings-row-group">
          <label className="settings-row">
            <span className="settings-label">
              API Token
              <span className="settings-hint">
                后端 .env 设 API_TOKEN=xxx 时，此处需填相同值；仅存本机浏览器
              </span>
            </span>
            <input
              type="password"
              className="settings-input"
              placeholder="后端未启用则留空"
              value={tokenDraft}
              onChange={(e) => setTokenDraft(e.target.value)}
              onBlur={() => {
                setApiToken(tokenDraft)
                setTokenMsg(tokenDraft.trim() ? '已保存到本机' : '已清除')
              }}
            />
          </label>
          {tokenMsg && (
            <div className="settings-hint" style={{ paddingLeft: 2 }}>
              {tokenMsg}
            </div>
          )}
        </div>
      </section>

      <div className="settings-actions">
        <span className="settings-autosave">
          {autoSaving ? '保存中…' : msg ? msg : '修改即时自动保存'}
        </span>
      </div>
    </div>
  )
}
