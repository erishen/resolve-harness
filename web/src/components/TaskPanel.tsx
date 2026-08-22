import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, openTaskStream, type AppConfig, type StreamHandle } from '../api'
import { safeMarkdown, safeHighlight } from '../safeHtml'
import { fmtNum, fmtUsage } from '../types'
import type { Subtask, TaskEvent, UsageInfo } from '../types'

type RunState = 'idle' | 'running' | 'done' | 'stopped' | 'error'

/** Fallback built-ins shown when the backend is unreachable (e.g. static preview). */
const FALLBACK_EXAMPLES: { label: string; text: string; source: string }[] = [
  { label: '计算', text: '计算 12 × 34 是多少', source: 'builtin' },
  { label: '多步计算', text: '计算 (23+45) 和 (67+89)，并告诉我哪个结果更大', source: 'builtin' },
  { label: '写文档', text: '写一份 150 字左右的 RAG 技术简介，保存为沙箱文件 rag-intro.md', source: 'builtin' },
  { label: '小项目', text: '创建一个小型 Python 项目：README.md 写项目说明，hello.py 写一个打印问候的脚本', source: 'builtin' },
  { label: '代码+文档', text: '写一个 Python 快速排序函数保存为 quicksort.py，并写 100 字使用说明保存为 quicksort-notes.md', source: 'builtin' },
]

/** 参数化示例模板：点击命中的示例卡片时，显示对应的参数下拉，选择后自动生成任务文本。 */
interface ParamOption {
  value: string
  label: string
  group?: string
}

interface ParamTemplate {
  key: string
  label: string
  default: string
  options: ParamOption[]
  build: (value: string) => string
}

const COMPANY_OPTIONS: ParamOption[] = [
  { value: 'usAAPL', label: '苹果 · usAAPL', group: '美股' },
  { value: 'usNVDA', label: '英伟达 · usNVDA', group: '美股' },
  { value: 'usTSLA', label: '特斯拉 · usTSLA', group: '美股' },
  { value: 'usMSFT', label: '微软 · usMSFT', group: '美股' },
  { value: 'usGOOGL', label: '谷歌 · usGOOGL', group: '美股' },
  { value: 'usAMZN', label: '亚马逊 · usAMZN', group: '美股' },
  { value: 'hk00700', label: '腾讯控股 · hk00700', group: '港股' },
  { value: 'hk09988', label: '阿里巴巴 · hk09988', group: '港股' },
  { value: 'hk01810', label: '小米集团 · hk01810', group: '港股' },
  { value: 'hk03690', label: '美团 · hk03690', group: '港股' },
  { value: 'sh600519', label: '贵州茅台 · sh600519', group: 'A股' },
  { value: 'sz002594', label: '比亚迪 · sz002594', group: 'A股' },
  { value: 'sz300750', label: '宁德时代 · sz300750', group: 'A股' },
  { value: 'sh601318', label: '中国平安 · sh601318', group: 'A股' },
]

const FX_OPTIONS: ParamOption[] = [
  { value: 'whUSDCNY', label: '美元 / 人民币' },
  { value: 'whEURCNY', label: '欧元 / 人民币' },
  { value: 'whJPYCNY', label: '日元 / 人民币' },
  { value: 'whGBPCNY', label: '英镑 / 人民币' },
  { value: 'whHKDCNY', label: '港币 / 人民币' },
]

const INDEX_OPTIONS: ParamOption[] = [
  { value: 'usDJI', label: '道琼斯' },
  { value: 'usIXIC', label: '纳斯达克' },
  { value: 'usINX', label: '标普 500' },
  { value: 'hkHSI', label: '恒生指数' },
  { value: 'sh000001', label: '上证指数' },
]

function optionLabel(options: ParamOption[], value: string): string {
  return options.find((o) => o.value === value)?.label.split(' · ')[0] ?? value
}

function quoteObjective(value: string): string {
  const name = optionLabel(COMPANY_OPTIONS, value)
  const symbol = value.slice(2).toLowerCase()
  if (value.startsWith('us')) {
    return `用 fetch 工具获取 https://qt.gtimg.cn/q=${value} 的美股行情，解析${name}的当前价格与涨跌幅，保存为沙箱文件 ${symbol}-quote.md`
  }
  if (value.startsWith('hk')) {
    return `用 fetch 工具获取 https://qt.gtimg.cn/q=${value} 的港股行情，解析${name}的当前价格与涨跌幅，保存为沙箱文件 ${symbol}-quote.md`
  }
  return `用 fetch 工具获取 https://qt.gtimg.cn/q=${value} 的股票行情，解析${name}的当前价格、涨跌额与涨跌幅，保存为沙箱文件 ${symbol}-quote.md`
}

const PARAM_TEMPLATES: ParamTemplate[] = [
  {
    key: 'company',
    label: '目标公司',
    default: 'usAAPL',
    options: COMPANY_OPTIONS,
    build: quoteObjective,
  },
  {
    key: 'fx',
    label: '汇率对',
    default: 'whUSDCNY',
    options: FX_OPTIONS,
    build: (value) => {
      const name = optionLabel(FX_OPTIONS, value)
      const symbol = value.slice(2).toLowerCase()
      return `用 fetch 工具获取 https://qt.gtimg.cn/q=${value} 的汇率数据，解析${name}的当前汇率、涨跌额与涨跌幅，保存为沙箱文件 fx-${symbol}.md`
    },
  },
  {
    key: 'index',
    label: '指数',
    default: 'usDJI',
    options: INDEX_OPTIONS,
    build: (value) => {
      const name = optionLabel(INDEX_OPTIONS, value)
      const symbol = value.slice(2).toLowerCase()
      return `用 fetch 工具获取 https://qt.gtimg.cn/q=${value} 的指数行情，解析${name}的当前点位与涨跌幅，保存为沙箱文件 idx-${symbol}.md`
    },
  },
]

function templateByKey(key: string | null): ParamTemplate | null {
  return PARAM_TEMPLATES.find((t) => t.key === key) ?? null
}

/** 示例卡片 → 参数模板：命中则点击卡片时显示对应下拉。关键词匹配，互斥。 */
function matchTemplate(ex: { label: string; text: string }): ParamTemplate | null {
  const hay = `${ex.label} ${ex.text}`
  if (/查行情|实时行情/.test(hay)) return templateByKey('company')
  if (/汇率/.test(hay)) return templateByKey('fx')
  if (/指数/.test(hay)) return templateByKey('index')
  return null
}

/** Example cards are grouped into a few coarse categories for quick scanning. */
const CATEGORY_META = {
  net: { icon: '🌐', name: '联网' },
  math: { icon: '🔢', name: '计算' },
  doc: { icon: '📝', name: '文档' },
  code: { icon: '💻', name: '代码' },
  project: { icon: '🗂️', name: '项目' },
  data: { icon: '📊', name: '数据' },
  generic: { icon: '✨', name: '示例' },
} as const

type ExCategory = keyof typeof CATEGORY_META

/** Keyword-based classifier — best effort, order matters (net > project > code > data > doc > math). */
function exCategory(ex: { label: string; text: string }): ExCategory {
  const hay = `${ex.label} ${ex.text}`
  if (/fetch|抓取|获取.*(列表|数据|信息)|爬|api\b|https?:\/\//i.test(hay)) return 'net'
  if (/项目|工程|README/i.test(hay)) return 'project'
  if (/代码|函数|脚本|python|quicksort|语法|程序/i.test(hay)) return 'code'
  if (/数据|统计|csv|清洗|样本|温度|记录|表格/i.test(hay)) return 'data'
  if (/文档|报告|简介|说明|笔记|文章|markdown|\bmd\b/i.test(hay)) return 'doc'
  if (/计算|求和|相加|相减|乘以|除以|算术|运算|数学|[+\-*/×÷]|\d/.test(hay)) return 'math'
  return 'generic'
}

/** A display-level item: plan / subtask (with nested events) / verdict / etc. */
type GroupedItem =
  | { kind: 'goal'; objective: string }
  | { kind: 'plan'; round: number; subtasks: Subtask[] }
  | { kind: 'subtask'; index: number; title: string; total: number; events: TaskEvent[] }
  | { kind: 'subtask-done'; index: number; title: string; summary: string; usage: UsageInfo | null }
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
  | { kind: 'final'; reply: string; passed: boolean; score: number; rounds: number; usage: UsageInfo | null }
  | { kind: 'error'; message: string }

export function groupEvents(events: TaskEvent[]): GroupedItem[] {
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
          usage: (d.usage as UsageInfo | null | undefined) ?? null,
        })
        current = null
        break
      case 'thought':
      case 'tool_call':
      case 'tool_result':
      case 'node_enter':
      case 'route':
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
          usage: (d.usage as UsageInfo | null | undefined) ?? null,
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
  const [paramKey, setParamKey] = useState<string | null>('company')
  const [paramValue, setParamValue] = useState('usAAPL')

  // 任务模板选择记忆：切 Tab 回来不丢（用户上次选的模板/参数）
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem('resolve_harness_task_param')
      if (raw) {
        const p = JSON.parse(raw) as { key: string | null; value: string }
        if (typeof p.key === 'string' || p.key === null) setParamKey(p.key)
        if (typeof p.value === 'string') setParamValue(p.value)
      }
    } catch {
      /* 损坏则忽略，用默认 */
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    sessionStorage.setItem(
      'resolve_harness_task_param',
      JSON.stringify({ key: paramKey, value: paramValue }),
    )
  }, [paramKey, paramValue])
  const [events, setEvents] = useState<TaskEvent[]>([])
  const [state, setState] = useState<RunState>('idle')
  const [error, setError] = useState('')
  const [model, setModel] = useState('')
  const [config, setConfig] = useState<AppConfig | null>(null)
  const [examples, setExamples] = useState<{ label: string; text: string; source?: string }[]>([])
  const [examplesCollapsed, setExamplesCollapsed] = useState(false)
  const [regenerating, setRegenerating] = useState(false)
  const [confirmingDel, setConfirmingDel] = useState<string | null>(null)
  const [preview, setPreview] = useState<{ name: string; path: string; fallback: string } | null>(null)
  const [saveMemOpen, setSaveMemOpen] = useState(false)
  const [saveMemKey, setSaveMemKey] = useState('')
  const [saveMemMsg, setSaveMemMsg] = useState('')
  const [saveMemSrc, setSaveMemSrc] = useState<{ title: string; body: string } | null>(null)
  const endRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const esRef = useRef<StreamHandle | null>(null)
  const delTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // SSE 高频事件缓冲：并行 Specialist 事件密集时合并为每 ~50ms 一次批量渲染，
  // 避免每个事件触发一次全量重渲染（会卡住 Tab 切换等交互）。
  const pendingRef = useRef<TaskEvent[]>([])
  const flushTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const flushPending = useCallback(() => {
    if (flushTimerRef.current) {
      clearTimeout(flushTimerRef.current)
      flushTimerRef.current = null
    }
    if (pendingRef.current.length) {
      const batch = pendingRef.current
      pendingRef.current = []
      setEvents((prev) => [...prev, ...batch])
    }
  }, [])

  const pushEvent = useCallback((ev: TaskEvent) => {
    pendingRef.current.push(ev)
    if (flushTimerRef.current) return
    flushTimerRef.current = setTimeout(() => {
      flushTimerRef.current = null
      const batch = pendingRef.current
      pendingRef.current = []
      if (batch.length) setEvents((prev) => [...prev, ...batch])
    }, 50)
  }, [])

  const loadExamples = useCallback(async () => {
    try {
      const { examples } = await api.examples()
      // trust the backend: an empty list means every example was deleted
      setExamples(examples)
    } catch {
      /* backend down — keep the built-in set so the UI never looks empty */
      setExamples(FALLBACK_EXAMPLES)
    }
  }, [])

  const regenerateExamples = useCallback(async () => {
    if (regenerating) return
    setRegenerating(true)
    try {
      const { examples } = await api.regenerateExamples()
      if (examples.length > 0) setExamples(examples)
    } catch {
      /* keep the current examples on failure — regeneration is best-effort */
    } finally {
      setRegenerating(false)
    }
  }, [regenerating])

  useEffect(() => {
    void loadExamples()
  }, [loadExamples])

  // 顶部「任务目标」行常驻显示当前生效模型（与 AgentsPanel 同一解析：
  // agent_models[phase] → default_model → .env 基线；命中模型库别名时取具体模型名）。
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const cfg = await api.getConfig()
        if (!cancelled) setConfig(cfg)
      } catch {
        /* backend down — 模型标签缺失不影响任务运行 */
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    // 默认激活「查行情」模板并预填苹果任务：打开任务 Tab 即可直接运行
    if (!objective) {
      const t = templateByKey('company')
      if (t) setObjective(t.build(t.default))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const items = groupEvents(events)
  const toolCalls = events.filter((e) => e.type === 'tool_call').length
  const param = templateByKey(paramKey)

  // 顶部模型标签：按 Agent 角色解析任务模型（agent_models[phase] 独立配置 →
  // 空 = 跟随 .env 基线 env_model，与「聊天模型」default_model 独立）。
  // task_start 事件里的实时值（后端已解析）覆盖默认模型显示。
  const agentModels = useMemo(() => {
    if (!config) return null
    const resolveOne = (phase: string): { phase: string; name: string; override: boolean } => {
      const override = config.agent_models?.[phase]
      const id = override || config.env_model?.model || ''
      const name = id ? config.models?.[id]?.model || id : '未配置'
      return { phase, name, override: Boolean(override) }
    }
    return ['planner', 'specialist', 'evaluator', 'reporter'].map(resolveOne)
  }, [config])
  const activeDefaultName = agentModels?.[0]?.name ?? null
  // 顶部标签正文：独立配置的角色逐个列出，其余合并为「跟随默认」。
  const modelLabel = useMemo(() => {
    if (!agentModels) return null
    const overrides = agentModels.filter((m) => m.override)
    const base = model || activeDefaultName
    if (overrides.length === 0) return base ? `${base}（默认）` : null
    const parts = overrides.map((m) => `${m.phase}: ${m.name}`)
    if (base) parts.push(`其余: ${base}（默认）`)
    return parts.join(' · ')
  }, [agentModels, model, activeDefaultName])

  // last task_end (deliverable) + the task's objective — for "save to memory"
  const finalEvent = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      if (events[i].type === 'task_end') return events[i]
    }
    return null
  }, [events])
  const taskObjective = useMemo(() => {
    const st = events.find((e) => e.type === 'task_start')
    return st ? String(st.data.objective ?? '') : ''
  }, [events])

  // 累计 token：累加各子任务完成的 usage（运行中实时增长）。
  // task_end 的 usage 是整任务全量（含 planner/evaluator/reporter），
  // 不参与累加，避免与子任务重复计数。
  const totalTokens = useMemo(() => {
    let t = 0
    for (const ev of events) {
      if (ev.type === 'task_end') continue
      const u = (ev.data as { usage?: UsageInfo | null }).usage
      if (u && typeof u.total_tokens === 'number') t += u.total_tokens
    }
    return t
  }, [events])
  const finalTotal = finalEvent
    ? Number((finalEvent.data as { usage?: UsageInfo | null }).usage?.total_tokens ?? 0)
    : 0

  // 找任务产出的「报告文档」：遍历产出文件（含 fallback），取第一个能提取出
  // markdown 标题的；返回其标题 + 去标题后的正文。
  const findReportFile = async (): Promise<{ title: string; body: string } | null> => {
    for (const f of producedFiles) {
      let content = ''
      try {
        const r = await api.sandboxFile(f.path)
        content = r.content
      } catch {
        try {
          if (f.fallback && f.fallback !== f.path) {
            const r = await api.sandboxFile(f.fallback)
            content = r.content
          }
        } catch {
          continue
        }
      }
      const title = extractTitle(content)
      if (title) return { title, body: stripTitle(content) }
    }
    return null
  }

  const openSaveMem = async () => {
    setSaveMemMsg('')
    setSaveMemOpen(true)
    // key 与正文都来自报告文档（fx-usdcny.md 这类），不是 Reporter 的交付文本
    const src = await findReportFile()
    setSaveMemSrc(src)
    let hint = src?.title ?? ''
    if (!hint && finalEvent) hint = extractTitle(String(finalEvent.data.reply ?? ''))
    setSaveMemKey(hint || taskObjective.slice(0, 30) || '任务结果')
  }

  const saveToMemory = async () => {
    const key = saveMemKey.trim()
    if (!key) return
    try {
      // 快照型记忆：时间戳 + 报告文档正文（去标题、≤500 字）；
      // 找不到报告文档时回退交付文本
      const ts = fmtNow()
      let body = saveMemSrc?.body ?? ''
      if (!body && finalEvent) body = String(finalEvent.data.reply ?? '')
      await api.addMemory(key, `[${ts}] ${mdToText(body).slice(0, 500)}`)
      setSaveMemOpen(false)
      setSaveMemMsg('✅ 已存入长期记忆（带保存时间）')
      onMemoryChange?.()
    } catch (e) {
      setSaveMemMsg(`存入失败：${e instanceof Error ? e.message : String(e)}`)
    }
  }

  // files the task produced via write_file (deduped, in call order). Since
  // each task writes into <sandbox>/tasks/<task_id>/, the read path is prefixed;
  // `fallback` is the bare relative path — pre-isolation history tasks wrote to
  // the sandbox root, so the modal retries there on 404.
  const producedFiles = useMemo(() => {
    const taskId = events[0] ? String(events[0].task_id) : null
    if (!taskId) return []
    const seen = new Set<string>()
    const out: { name: string; path: string; fallback: string }[] = []
    for (const ev of events) {
      if (ev.type === 'tool_call' && String(ev.data.name ?? '') === 'write_file') {
        const p = String((ev.data.args as Record<string, unknown> | undefined)?.path ?? '')
        if (p && !seen.has(p)) {
          seen.add(p)
          out.push({ name: p, path: `tasks/${taskId}/${p}`, fallback: p })
        }
      } else if (ev.type === 'produced_file') {
        // run_script 等子进程工具真实落盘的文件（如 futures.md）
        const p = String((ev.data as Record<string, unknown>).path ?? '')
        if (p && !seen.has(p)) {
          seen.add(p)
          out.push({ name: p, path: `tasks/${taskId}/${p}`, fallback: p })
        }
      }
    }
    return out
  }, [events])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [events, state])

  useEffect(() => {
    return () => {
      esRef.current?.close()
      if (delTimerRef.current) clearTimeout(delTimerRef.current)
      // 卸载（切 Tab）前把缓冲事件落进 state，避免最后一批丢失
      flushPending()
      if (flushTimerRef.current) clearTimeout(flushTimerRef.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const runTask = async (goalOverride?: string) => {
    const goal = (goalOverride ?? objective).trim()
    if (!goal || state === 'running') return
    setError('')
    setEvents([])
    setState('running')
    try {
      const { task_id } = await api.createTask(goal)
      // 记录当前运行任务：切 Tab 再切回时据此恢复进度
      sessionStorage.setItem('resolve_harness_live_task', task_id)
      esRef.current = openTaskStream(task_id, (data) => {
        let ev: TaskEvent
        try {
          ev = JSON.parse(data) as TaskEvent
        } catch {
          return
        }
        if (ev.type === 'task_start') setModel(String(ev.data.model ?? ''))
        if (ev.type === 'task_end' || ev.type === 'task_stopped') {
          // 终态事件也必须进 events：finalEvent（保存记忆按钮）依赖它
          pushEvent(ev)
          flushPending()
          setState(ev.type === 'task_end' ? 'done' : 'stopped')
          onMemoryChange?.()
          sessionStorage.removeItem('resolve_harness_live_task')
          esRef.current?.close()
          esRef.current = null
        } else if (ev.type === 'error') {
          pushEvent(ev)
          flushPending()
          setError(String(ev.data.message ?? 'task failed'))
          setState('error')
          onMemoryChange?.()
          sessionStorage.removeItem('resolve_harness_live_task')
          esRef.current?.close()
          esRef.current = null
        } else {
          pushEvent(ev)
        }
      })
      /* 流异常结束无需处理：终态由事件驱动（与原 EventSource.onerror 逻辑一致） */
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setState('error')
    }
  }

  const stop = useCallback(() => {
    const tid = sessionStorage.getItem('resolve_harness_live_task')
    if (tid) {
      // 通知后端真正中止任务（best-effort：无法取消已在进行的同步 LLM 调用）
      void api.stopTask(tid).catch(() => {})
    }
    esRef.current?.close()
    esRef.current = null
    sessionStorage.removeItem('resolve_harness_live_task')
    setState((s) => (s === 'running' ? 'stopped' : s))
  }, [])

  // 切回恢复：上次运行中的任务（sessionStorage 记录）重连 SSE 恢复进度。
  // 后端 subscribe 会补发全部历史事件再流式推送实时事件，因此无论任务是
  // 仍在运行还是已结束，切回都能拿到完整视图。
  useEffect(() => {
    const tid = sessionStorage.getItem('resolve_harness_live_task')
    if (!tid) return
    let received = false
    esRef.current = openTaskStream(
      tid,
      (data) => {
        received = true
        let ev: TaskEvent
        try {
          ev = JSON.parse(data) as TaskEvent
        } catch {
          return
        }
        if (ev.type === 'task_start') {
          setModel(String(ev.data.model ?? ''))
          setObjective(String(ev.data.objective ?? '')) // 输入框显示真实任务目标
          setState('running')
        }
        if (ev.type === 'task_end') {
          pushEvent(ev)
          flushPending()
          setState('done')
          sessionStorage.removeItem('resolve_harness_live_task')
          esRef.current?.close()
          esRef.current = null
        } else if (ev.type === 'error') {
          pushEvent(ev)
          flushPending()
          setError(String(ev.data.message ?? 'task failed'))
          setState('error')
          sessionStorage.removeItem('resolve_harness_live_task')
          esRef.current?.close()
          esRef.current = null
        } else {
          pushEvent(ev)
        }
      },
      () => {
        if (!received) {
          // 初始连接即失败（任务已被清理 / 后端重启 / Token 未配置）：放弃恢复
          esRef.current?.close()
          esRef.current = null
          sessionStorage.removeItem('resolve_harness_live_task')
        }
      },
    )
    // SSE 连接与缓冲由既有的卸载 cleanup 统一关闭/flush
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const busy = state === 'running'

  const pickExample = (ex: { label: string; text: string }) => {
    // 参数化示例：激活对应下拉并生成默认任务；普通示例：隐藏下拉、填原文
    const t = matchTemplate(ex)
    if (t) {
      setParamKey(t.key)
      setParamValue(t.default)
      setObjective(t.build(t.default))
    } else {
      setParamKey(null)
      setObjective(ex.text)
    }
    inputRef.current?.focus()
  }

  const deleteExample = useCallback(
    async (ex: { label: string; text: string }) => {
      try {
        await api.deleteExample(ex.label, ex.text)
        if (objective === ex.text) setObjective('')
        await loadExamples()
      } catch {
        /* best-effort — keep the card on failure */
      } finally {
        setConfirmingDel(null)
      }
    },
    [loadExamples, objective],
  )

  const onDeleteClick = (ex: { label: string; text: string }) => {
    if (busy) return
    if (confirmingDel === ex.text) {
      if (delTimerRef.current) clearTimeout(delTimerRef.current)
      void deleteExample(ex)
    } else {
      setConfirmingDel(ex.text)
      if (delTimerRef.current) clearTimeout(delTimerRef.current)
      delTimerRef.current = setTimeout(() => setConfirmingDel(null), 3000)
    }
  }

  return (
    <div className="task-panel">
      <div className="task-composer">
        <div className="task-label">
          <span>任务目标 — Planner 拆解 → Specialist 执行 → Evaluator 验收</span>
          {modelLabel && (
            <span
              className="task-model"
              title="任务模型：各 Agent 独立配置，空 = 跟随 .env 基线（可在「设置」Tab 修改）"
            >
              🧠 {modelLabel}
            </span>
          )}
        </div>
        <div className="task-examples">
          <div className="ex-head">
            <div className="ex-title">
              <button
                type="button"
                className="ex-collapse"
                onClick={() => setExamplesCollapsed((v) => !v)}
                title={examplesCollapsed ? '展开示例任务' : '折叠示例任务'}
                aria-expanded={!examplesCollapsed}
              >
                {examplesCollapsed ? '▸' : '▾'}
              </button>
              <span className="ex-title-main">示例任务</span>
              <span className="ex-title-sub">
                {examplesCollapsed
                  ? `已折叠 · 共 ${examples.length} 个`
                  : '单击填入 · 双击直接运行 · 共 ' + examples.length + ' 个'}
              </span>
            </div>
            <button
              type="button"
              className="ex-regen-link"
              onClick={regenerateExamples}
              disabled={busy || regenerating}
              title="让模型生成一批新的示例任务（不依赖记忆偏好）"
            >
              {regenerating ? (
                <>
                  <span className="spinner spinner-sm" /> 生成中…
                </>
              ) : (
                <>🪄 重新生成</>
              )}
            </button>
          </div>

          {examplesCollapsed ? null : examples.length === 0 ? (
              <div className="ex-empty">
                示例已全部删除 — 点右上角「重新生成」补充一批新示例
              </div>
            ) : (
              <div className="ex-grid">
              {examples.map((ex, i) => {
                const cat = exCategory(ex)
                const meta = CATEGORY_META[cat]
                const isGenerated = ex.source === 'generated'
                const isSelected = objective === ex.text
                const isConfirming = confirmingDel === ex.text
                return (
                  <div
                    key={`${ex.source ?? 'builtin'}-${i}`}
                    role="button"
                    tabIndex={0}
                    className={`ex-card${isSelected ? ' selected' : ''}${busy ? ' busy' : ''}`}
                    onClick={() => {
                      if (!busy) pickExample(ex)
                    }}
                    onDoubleClick={() => {
                      if (busy) return
                      setObjective(ex.text)
                      void runTask(ex.text)
                    }}
                    onKeyDown={(e) => {
                      if (busy) return
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault()
                        pickExample(ex)
                      }
                    }}
                  >
                    <span className="ex-card-top">
                      <span className="ex-card-label">
                        <span className="ex-card-icon" aria-hidden>
                          {meta.icon}
                        </span>
                        {ex.label}
                      </span>
                      <span className="ex-card-actions">
                        <span
                          className={`ex-src${isGenerated ? ' gen' : ' builtin'}`}
                          title={isGenerated ? '模型生成' : '内置示例'}
                        >
                          {isGenerated ? '生成' : '内置'}
                        </span>
                        <button
                          type="button"
                          className={`ex-del${isConfirming ? ' confirming' : ''}`}
                          disabled={busy}
                          title={isConfirming ? '确认删除该示例（删除后不可恢复）' : '删除该示例'}
                          onClick={(e) => {
                            e.stopPropagation()
                            onDeleteClick(ex)
                          }}
                        >
                          {isConfirming ? '确认删除？' : '✕'}
                        </button>
                      </span>
                    </span>
                    <span className="ex-card-text">{ex.text}</span>
                    <span className="ex-card-hint">＋ 填入 · 双击运行</span>
                  </div>
                )
              })}
            </div>
          )}
        </div>
        <form
          onSubmit={(e) => {
            e.preventDefault()
            void runTask()
          }}
        >
          {param && (
            <select
              className="quote-select"
              value={paramValue}
              disabled={busy}
              title={`${param.label}：选择后自动生成任务文本`}
              onChange={(e) => {
                const v = e.target.value
                setParamValue(v)
                setObjective(param.build(v))
              }}
            >
              {param.options.some((o) => o.group) ? (
                [...new Set(param.options.map((o) => o.group))].map((group) => (
                  <optgroup key={group} label={group}>
                    {param.options
                      .filter((o) => o.group === group)
                      .map((o) => (
                        <option key={o.value} value={o.value}>
                          {o.label}
                        </option>
                      ))}
                  </optgroup>
                ))
              ) : (
                param.options.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))
              )}
            </select>
          )}
          <input
            ref={inputRef}
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
      {state === 'stopped' && <div className="stopped-banner">⏹ 任务已停止</div>}

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
        {producedFiles.length > 0 && (
          <div className="produced-files">
            <div className="produced-title">
              📄 产出文件（{producedFiles.length}）· 点击查看
            </div>
            <div className="produced-list">
              {producedFiles.map((f) => (
                <button
                  key={f.name}
                  type="button"
                  className="file-chip"
                  onClick={() => setPreview({ name: f.name, path: f.path, fallback: f.fallback })}
                  title={`查看 ${f.name}`}
                >
                  {f.name}
                </button>
              ))}
            </div>
          </div>
        )}
        {finalEvent && (
          <div className="mem-save">
            {!saveMemOpen ? (
              <button
                type="button"
                className="mem-save-btn"
                onClick={() => void openSaveMem()}
                title="把本次任务的核心交付内容（带保存时间，≤500 字）存入长期记忆（scope=default）"
              >
                💾 把本次结果存入长期记忆
              </button>
            ) : (
              <div className="mem-save-inline">
                <span className="mem-save-label">记忆 key</span>
                <input
                  className="mem-save-input"
                  value={saveMemKey}
                  onChange={(e) => setSaveMemKey(e.target.value)}
                  placeholder="记忆键名（如：美元汇率快照）"
                />
                <button type="button" onClick={() => void saveToMemory()}>
                  保存
                </button>
                <button type="button" onClick={() => setSaveMemOpen(false)}>
                  取消
                </button>
                {saveMemMsg && <span className="mem-save-msg">{saveMemMsg}</span>}
              </div>
            )}
          </div>
        )}
        {state === 'running' && (
          <div className="task-running">
            <span className="spinner" /> agent 协作执行中{model ? ` · ${model}` : ''}
            {totalTokens > 0 ? ` · ⚡ ${fmtNum(totalTokens)} tokens` : ''}…
          </div>
        )}
        {state === 'done' && (
          <div className="task-done-banner">
            ✅ 任务完成 · {toolCalls} 次工具调用{model ? ` · ${model}` : ''}
            {finalTotal > 0 ? ` · ⚡ ${fmtNum(finalTotal)} tokens` : totalTokens > 0 ? ` · ⚡ ${fmtNum(totalTokens)} tokens` : ''}
          </div>
        )}
        {state === 'error' && <div className="task-done-banner err">✕ 任务失败</div>}
        <div ref={endRef} />
      </div>
      {preview && (
        <FilePreviewModal
          name={preview.name}
          path={preview.path}
          fallback={preview.fallback}
          onClose={() => setPreview(null)}
        />
      )}
    </div>
  )
}

// ---- grouped card rendering ----------------------------------------------------

export function GroupedCard({ item }: { item: GroupedItem }) {
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
          <MiniLoopGraph events={item.events} />
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
          <div className="step-title">
            ✅ 子任务完成：{item.title}
            {item.usage?.total_tokens !== undefined && (
              <span className="usage-tag">⚡ {fmtUsage(item.usage)}</span>
            )}
          </div>
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
            {item.usage?.total_tokens !== undefined && (
              <span className="usage-tag">⚡ {fmtUsage(item.usage)}</span>
            )}
          </div>
          <div
            className="step-markdown"
            dangerouslySetInnerHTML={{ __html: safeMarkdown(item.reply) }}
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

export function InlineEvent({ event, standalone }: { event: TaskEvent; standalone?: boolean }) {
  const d = event.data
  if (event.type === 'node_enter') {
    const node = String(d.node ?? '')
    const label = node === 'human_gate' ? '🚦 human_gate' : node === 'tools' ? '🛠 tools' : '🧠 agent'
    return (
      <div className={`inline-event inline-node${standalone ? ' standalone' : ''}`}>
        <span className="ie-badge">▶ {label}</span>
        {d.step != null && <span className="ie-res">step {String(d.step)}</span>}
      </div>
    )
  }
  if (event.type === 'route') {
    const to = String(d.to ?? '')
    const reason: Record<string, string> = {
      tool_calls: '有工具调用',
      approval_needed: '需人工审批',
      no_tool_calls: '无工具调用',
      max_steps: '已达 max_steps',
    }
    return (
      <div className={`inline-event inline-route${standalone ? ' standalone' : ''}`}>
        <span className="ie-badge">🔀 路由 → {to}</span>
        <span className="ie-res">
          {reason[String(d.reason ?? '')] ?? String(d.reason ?? '')}
          {d.max_steps != null && d.step != null
            ? ` · step ${String(d.step)}/${String(d.max_steps)}`
            : ''}
        </span>
      </div>
    )
  }
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

/** 本地时间戳，用于快照型记忆：YYYY-MM-DD HH:mm */
export function fmtNow(): string {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/** 从 markdown 文本提取第一个标题行（如「# 美元/人民币汇率报告」→ 美元/人民币汇率报告）。 */
export function extractTitle(md: string): string {
  const line = md
    .split('\n')
    .map((l) => l.trim())
    .find((l) => /^#{1,4}\s+\S/.test(l))
  return line ? line.replace(/^#+\s*/, '').trim().slice(0, 40) : ''
}

/** 去掉 markdown 的第一个标题行（及其所在行），返回正文。 */
export function stripTitle(md: string): string {
  const lines = md.split('\n')
  const idx = lines.findIndex((l) => /^#{1,4}\s+\S/.test(l.trim()))
  if (idx !== -1) lines.splice(idx, 1)
  return lines.join('\n').trim()
}

/** markdown → 纯文本：去掉加粗/斜体/链接/列表符/代码围栏等标记，留下可读内容。 */
export function mdToText(md: string): string {
  return md
    .replace(/```[a-z]*\s*/gi, '') // 代码块围栏
    .replace(/`([^`]+)`/g, '$1') // 行内代码
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1') // 图片 → alt
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1') // 链接 → 文字
    .replace(/(\*\*|__)(.*?)\1/g, '$2') // 粗体
    .replace(/(\*|_)(.*?)\1/g, '$2') // 斜体
    .replace(/~~(.*?)~~/g, '$1') // 删除线
    .replace(/^\s*[-*+]\s+/gm, '') // 无序列表符
    .replace(/^\s*\d+\.\s+/gm, '') // 有序列表符
    .replace(/^\s*>\s?/gm, '') // 引用
    .replace(/^\s*\|?[\s:|-]+\|[\s:|*-]*$/gm, '') // 表格分隔行
    .replace(/^#{1,6}\s+/gm, '') // 残留标题符
    .replace(/^\s*([-*_])\1{2,}\s*$/gm, '') // 水平线
    .replace(/[ \t]+\n/g, '\n') // 行尾空格
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

/** 迷你 LangGraph 拓扑图：START → agent ⇄ tools → END（含 human_gate 审批分支）。
 *  根据该子任务的 node_enter/route 事件高亮当前节点并显示 step 计数。 */
export function MiniLoopGraph({ events }: { events: TaskEvent[] }) {
  let current: string | null = null
  let step = 0
  let maxSteps = 0
  let hasGate = false
  let seenAgent = false
  let seenTools = false
  for (const ev of events) {
    if (ev.type === 'node_enter') {
      const node = String(ev.data.node ?? '')
      current = node
      step = Number(ev.data.step ?? 0)
      if (node === 'human_gate') hasGate = true
      if (node === 'agent') seenAgent = true
      if (node === 'tools') seenTools = true
    } else if (ev.type === 'route') {
      if (String(ev.data.to ?? '') === 'end') current = 'end'
      maxSteps = Number(ev.data.max_steps ?? 0)
    }
  }
  const stateOf = (name: string) => (current === name ? 'active' : current !== null ? 'past' : 'idle')
  return (
    <div className="loop-graph" title="LangGraph 节点流转（agent ⇄ tools 循环，human_gate 为审批分支）">
      <span className="loop-node past">START</span>
      <span className="loop-arrow">→</span>
      <span className={`loop-node ${stateOf('agent')}`}>agent</span>
      {hasGate && (
        <>
          <span className="loop-arrow dashed">⇢</span>
          <span className={`loop-node dashed ${stateOf('human_gate')}`}>human_gate</span>
        </>
      )}
      <span className="loop-arrow">{seenAgent && seenTools ? '⇄' : '→'}</span>
      <span className={`loop-node ${stateOf('tools')}`}>tools</span>
      <span className="loop-arrow">→</span>
      <span className={`loop-node ${stateOf('end')}`}>END</span>
      {step > 0 && (
        <span className="loop-step">
          step {step}
          {maxSteps ? `/${maxSteps}` : ''}
        </span>
      )}
    </div>
  )
}

/** 产出文件弹窗：图片/PDF 用 raw URL 直接渲染，markdown 用 marked，JSON 格式化+高亮，其余文本源码。 */
export function previewKind(path: string): 'image' | 'pdf' | 'markdown' | 'json' | 'text' {
  const lower = path.toLowerCase()
  if (/\.(png|jpe?g|gif|webp|svg|bmp|ico|avif)$/.test(lower)) return 'image'
  if (lower.endsWith('.pdf')) return 'pdf'
  if (/\.(md|markdown)$/.test(lower)) return 'markdown'
  if (lower.endsWith('.json')) return 'json'
  return 'text'
}

/** Pretty-print valid JSON, fall back to raw text. */
export function prettyJson(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2)
  } catch {
    return text
  }
}

export function FilePreviewModal({
  name,
  path,
  fallback,
  onClose,
}: {
  name: string
  path: string
  fallback?: string
  onClose: () => void
}) {
  const [content, setContent] = useState<string | null>(null)
  const [error, setError] = useState('')
  // <img>/<iframe> 带不了鉴权头：经 api 取回字节转 ObjectURL（含旧路径 fallback）
  const [mediaUrl, setMediaUrl] = useState<string | null>(null)
  const kind = previewKind(path)

  // pre-isolation history tasks wrote to the sandbox root; retry the bare path
  const tryRead = async (): Promise<string> => {
    try {
      const r = await api.sandboxFile(path)
      return r.content
    } catch (err) {
      if (fallback && fallback !== path) {
        const r = await api.sandboxFile(fallback)
        return r.content
      }
      throw err
    }
  }

  useEffect(() => {
    let cancelled = false
    let created: string | null = null
    setMediaUrl(null)
    if (kind === 'image' || kind === 'pdf') {
      // 二进制预览：鉴权取字节 → ObjectURL；图片失败时退回旧根路径
      const load = async (): Promise<void> => {
        try {
          const u = await api.sandboxRawObjectUrl(path)
          created = u
          if (!cancelled) setMediaUrl(u)
        } catch {
          if (kind === 'image' && fallback && fallback !== path) {
            try {
              const u2 = await api.sandboxRawObjectUrl(fallback)
              created = u2
              if (!cancelled) setMediaUrl(u2)
            } catch {
              /* 保持空态 */
            }
          }
        }
      }
      void load()
      return () => {
        cancelled = true
        if (created) URL.revokeObjectURL(created)
      }
    }
    tryRead()
      .then((c) => {
        if (!cancelled) setContent(c)
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, kind])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="file-modal-overlay" onClick={onClose}>
      <div className="file-modal" onClick={(e) => e.stopPropagation()}>
        <div className="file-modal-head">
          <span className="file-modal-path">{name}</span>
          <button type="button" className="file-modal-close" onClick={onClose} title="关闭 (Esc)">
            ✕
          </button>
        </div>
        <div className="file-modal-body">
          {error ? (
            <div className="file-modal-error">无法读取文件：{error}</div>
          ) : kind === 'image' ? (
            mediaUrl ? (
              <img src={mediaUrl} alt={name} />
            ) : (
              <div className="file-modal-loading">加载中…</div>
            )
          ) : kind === 'pdf' ? (
            <iframe src={mediaUrl ?? undefined} title={name} sandbox="" />
          ) : content === null ? (
            <div className="file-modal-loading">加载中…</div>
          ) : kind === 'markdown' ? (
            <div
              className="step-markdown"
              dangerouslySetInnerHTML={{ __html: safeMarkdown(content) }}
            />
          ) : kind === 'json' ? (
            <pre className="sandbox-highlight" style={{ margin: 0, maxHeight: '62vh' }}>
              <code
                className="hljs"
                dangerouslySetInnerHTML={{
                  __html: safeHighlight(prettyJson(content), 'json'),
                }}
              />
            </pre>
          ) : (
            <pre className="file-modal-pre">{content}</pre>
          )}
        </div>
      </div>
    </div>
  )
}
