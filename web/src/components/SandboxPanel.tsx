import { useCallback, useEffect, useMemo, useState } from 'react'
import 'highlight.js/styles/github-dark.css'
import { api } from '../api'
import { safeMarkdown, safeHighlight } from '../safeHtml'
import type { SandboxFile } from '../types'

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function formatMtime(mtime: number): string {
  const d = new Date(mtime * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** Renderable text kinds that get a rendered preview (with an edit fallback). */
const RENDERABLE_KINDS = new Set(['markdown', 'html', 'csv'])
/** Kinds displayed as syntax-highlighted source (still editable). */
const HIGHLIGHTED_KINDS = new Set(['json', 'code'])

/** 文件类型过滤分组（沙箱文件 kind 全集：text/markdown/html/image/pdf/csv/json/code/binary） */
type SandboxFilter = 'all' | 'image' | 'doc' | 'code' | 'other'
const FILTER_DEFS: { key: SandboxFilter; label: string; kinds: Set<SandboxFile['kind']> | null }[] = [
  { key: 'all', label: '全部', kinds: null },
  { key: 'image', label: '图片', kinds: new Set(['image']) },
  { key: 'doc', label: '文档', kinds: new Set(['markdown', 'html', 'csv', 'text']) },
  { key: 'code', label: '代码', kinds: new Set(['json', 'code']) },
  { key: 'other', label: '其他', kinds: new Set(['pdf', 'binary']) },
]

/** Extension -> highlight.js language (fallback: plaintext). */
const EXT_LANG: Record<string, string> = {
  py: 'python',
  js: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  ts: 'typescript',
  tsx: 'typescript',
  jsx: 'javascript',
  css: 'css',
  scss: 'scss',
  sh: 'bash',
  bash: 'bash',
  go: 'go',
  rs: 'rust',
  java: 'java',
  c: 'c',
  h: 'c',
  cpp: 'cpp',
  hpp: 'cpp',
  sql: 'sql',
  yaml: 'yaml',
  yml: 'yaml',
  toml: 'ini',
  ini: 'ini',
  cfg: 'ini',
  rb: 'ruby',
  php: 'php',
  swift: 'swift',
  json: 'json',
}

function languageFor(path: string, kind: string): string {
  const ext = path.split('.').pop()?.toLowerCase() ?? ''
  return (kind === 'json' ? 'json' : EXT_LANG[ext]) || 'plaintext'
}

/** Pretty-print valid JSON, fall back to raw text. */
function prettyJson(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2)
  } catch {
    return text
  }
}

/** Source view for json/code kinds: pretty-print JSON, then highlight + sanitize. */
function sourceHtml(file: SandboxFile, text: string): string {
  const code = file.kind === 'json' ? prettyJson(text) : text
  return safeHighlight(code, languageFor(file.path, file.kind))
}

/** Dead-simple CSV/TSV parser — good enough for a preview table. */
function parseTable(text: string): string[][] {
  return text
    .split(/\r?\n/)
    .filter((line) => line.trim() !== '')
    .map((line) => line.split(/[,\t]/).map((cell) => cell.trim()))
}

export default function SandboxPanel() {
  const [files, setFiles] = useState<SandboxFile[]>([])
  const [filter, setFilter] = useState<SandboxFilter>('all')
  const [selected, setSelected] = useState<SandboxFile | null>(null)
  const [content, setContent] = useState('')
  const [loadingContent, setLoadingContent] = useState(false)
  const [error, setError] = useState('')
  const [sandboxDir, setSandboxDir] = useState<string | null>(null)
  const [history, setHistory] = useState<string[]>([])
  const [newDir, setNewDir] = useState('')
  // 默认隐藏沙箱确立前就存在的「原有文件」，避免误显示/误删用户个人文件
  const [showAll, setShowAll] = useState(false)

  // edit mode / rendered preview mode
  const [editing, setEditing] = useState(false)
  const [previewing, setPreviewing] = useState(true)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  // bumped after every save so an HTML <iframe> re-renders with fresh content
  const [renderNonce, setRenderNonce] = useState(0)

  const load = useCallback(async () => {
    try {
      const res = await api.sandbox()
      setFiles(res.files)
      setSandboxDir(res.sandbox_dir)
      setHistory(res.sandbox_history ?? [])
      setError('')
    } catch {
      setError('无法读取沙箱（后端未连接？）')
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const openFile = useCallback(async (file: SandboxFile) => {
    setSelected(file)
    setEditing(false)
    setPreviewing(true)
    setDraft('')
    if (!file.is_text) {
      setContent('')
      return
    }
    setLoadingContent(true)
    setContent('')
    try {
      const res = await api.sandboxFile(file.path)
      setContent(res.content)
    } catch {
      setContent('（读取失败）')
    } finally {
      setLoadingContent(false)
    }
  }, [])

  const startEdit = useCallback(() => {
    setDraft(content)
    setEditing(true)
  }, [content])

  const cancelEdit = useCallback(() => {
    setEditing(false)
    setDraft('')
  }, [])

  const saveEdit = useCallback(async () => {
    if (!selected) return
    setSaving(true)
    try {
      await api.sandboxWrite(selected.path, draft)
      setContent(draft)
      setEditing(false)
      setPreviewing(true) // back to rendered preview for markdown/html/csv
      setRenderNonce((n) => n + 1)
      await load() // refresh size / mtime
    } catch {
      setError('保存失败')
    } finally {
      setSaving(false)
    }
  }, [selected, draft, load])

  const deleteFile = useCallback(
    async (file: SandboxFile) => {
      if (!window.confirm(`删除文件 “${file.path}”？此操作不可撤销。`)) return
      try {
        await api.sandboxDelete(file.path)
        if (selected?.path === file.path) {
          setSelected(null)
          setContent('')
          setEditing(false)
        }
        await load()
      } catch {
        setError('删除失败')
      }
    },
    [selected, load],
  )

  // Agent 创建的文件数 / 原有文件数（清空只删前者）
  const managedCount = useMemo(() => files.filter((f) => !f.preexisting).length, [files])
  const preexistingCount = useMemo(() => files.filter((f) => f.preexisting).length, [files])

  const clearAll = useCallback(async () => {
    if (managedCount === 0) return
    const hint = preexistingCount > 0 ? `（原有 ${preexistingCount} 个文件将保留）` : ''
    if (!window.confirm(`清空沙箱中 ${managedCount} 个 Agent 创建的文件？此操作不可撤销。${hint}`)) return
    try {
      await api.sandboxClear()
      setSelected(null)
      setContent('')
      setEditing(false)
      await load()
    } catch {
      setError('清空失败')
    }
  }, [managedCount, preexistingCount, load])

  const applyLocation = useCallback(
    async (path: string) => {
      const trimmed = path.trim()
      if (!trimmed) return
      try {
        const res = await api.sandboxSetLocation(trimmed)
        setSandboxDir(res.sandbox_dir)
        setHistory(res.sandbox_history)
        setNewDir('')
        await load()
      } catch {
        setError('切换沙箱目录失败')
      }
    },
    [load],
  )

  const removeHistory = useCallback(async (path: string) => {
    try {
      const res = await api.sandboxDeleteHistory(path)
      setHistory(res.sandbox_history)
    } catch {
      setError('移除历史目录失败')
    }
  }, [])

  const clearHistory = useCallback(async () => {
    try {
      const res = await api.sandboxClearHistory()
      setHistory(res.sandbox_history)
    } catch {
      setError('清空历史目录失败')
    }
  }, [])

  // 快捷目录：后端 set_sandbox_dir 已支持 ~ 展开与绝对化；「项目沙箱」发送哨兵复位默认。
  const QUICK_DIRS: { label: string; path: string }[] = [
    { label: 'ResolveHarness', path: '~/ResolveHarness' },
    { label: '桌面', path: '~/Desktop' },
    { label: '下载', path: '~/Downloads' },
    { label: '文稿', path: '~/Documents' },
    { label: '项目沙箱', path: '__default__' },
  ]

  // 类型过滤：选中分组只显示匹配 kind 的文件；默认隐藏「原有文件」（showAll 关闭时）。
  const filteredFiles = useMemo(() => {
    let list = showAll ? files : files.filter((f) => !f.preexisting)
    if (filter !== 'all') {
      const kinds = FILTER_DEFS.find((d) => d.key === filter)?.kinds
      if (kinds) list = list.filter((f) => kinds.has(f.kind))
    }
    return list
  }, [files, filter, showAll])
  const typeCounts = useMemo(() => {
    const counts: Record<SandboxFilter, number> = { all: files.length, image: 0, doc: 0, code: 0, other: 0 }
    for (const f of files) {
      for (const d of FILTER_DEFS) {
        if (d.key !== 'all' && d.kinds?.has(f.kind)) counts[d.key] += 1
      }
    }
    return counts
  }, [files])

  return (
    <div className="sandbox-panel">
      <div className="sandbox-head">
        <div className="sandbox-title">
          <span className="sandbox-title-main">沙箱文件</span>
          <span className="sandbox-title-sub">
            {sandboxDir ? sandboxDir : 'agent 可写的隔离目录'}
          </span>
        </div>
        <div className="sandbox-actions">
            <button
              type="button"
              className="ex-regen danger"
              onClick={() => void clearAll()}
              disabled={managedCount === 0}
              title={preexistingCount > 0 ? `清空只删 Agent 创建的文件，保留 ${preexistingCount} 个原有文件` : undefined}
            >
              🗑 清空
            </button>
              <button type="button" className="ex-regen" onClick={() => void load()}>
                🔄 刷新
              </button>
            </div>

            <div className="sandbox-locbar">
              <span className="sandbox-loc-label">沙箱位置</span>
              <input
                className="sandbox-loc-input"
                value={newDir}
                placeholder={sandboxDir ?? '输入目录绝对路径，如 /data/myproject'}
                onChange={(e) => setNewDir(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && newDir.trim()) void applyLocation(newDir.trim())
                }}
              />
              <button
                type="button"
                className="ex-regen"
                disabled={!newDir.trim()}
                onClick={() => newDir.trim() && void applyLocation(newDir.trim())}
              >
                📁 切换
              </button>
            </div>
            <div className="sandbox-history">
              <span className="sandbox-hist-label">快捷选择：</span>
              {QUICK_DIRS.map((d) => (
                <button
                  key={d.path}
                  type="button"
                  className="sandbox-hist-chip"
                  onClick={() => void applyLocation(d.path)}
                >
                  {d.label}
                </button>
              ))}
            </div>
            {history.length > 0 && (
              <div className="sandbox-history">
                <span className="sandbox-hist-label">已用目录：</span>
                {history.map((d) => (
                  <span
                    key={d}
                    className={`sandbox-hist-chip${d === sandboxDir ? ' active' : ''}`}
                    title={d}
                  >
                    <button
                      type="button"
                      className="sandbox-hist-name"
                      onClick={() => void applyLocation(d)}
                    >
                      {d}
                    </button>
                    <button
                      type="button"
                      className="sandbox-hist-del"
                      title="从历史中移除"
                      onClick={() => void removeHistory(d)}
                    >
                      ×
                    </button>
                  </span>
                ))}
                <button type="button" className="sandbox-hist-clear" onClick={() => void clearHistory()}>
                  清空
                </button>
              </div>
            )}
          </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="sandbox-body">
        <div className="sandbox-list">
          <div className="sandbox-filters">
            {preexistingCount > 0 && (
              <label className="sandbox-showall" title="显示沙箱目录确立前就存在的文件（清空时不会被删除）">
                <input
                  type="checkbox"
                  checked={showAll}
                  onChange={(e) => setShowAll(e.target.checked)}
                />
                显示原有文件（{preexistingCount}）
              </label>
            )}
            {FILTER_DEFS.map((d) => (
              <button
                key={d.key}
                type="button"
                className={`sandbox-filter${filter === d.key ? ' active' : ''}`}
                onClick={() => setFilter(d.key)}
                title={d.key === 'all' ? '显示全部文件' : `只显示${d.label}（${d.kinds ? [...d.kinds].join(' / ') : ''}）`}
              >
                {d.label}
                <span className="sandbox-filter-count">{typeCounts[d.key]}</span>
              </button>
            ))}
          </div>
          {files.length === 0 && !error && (
            <div className="empty">沙箱为空 — 运行一个「写文件」任务试试</div>
          )}
          {files.length > 0 && filteredFiles.length === 0 && (
            <div className="empty">
              {preexistingCount > 0 && !showAll
                ? `当前仅显示 Agent 创建的文件，无匹配；原有 ${preexistingCount} 个文件已隐藏`
                : '该类型下暂无文件'}
            </div>
          )}
          {filteredFiles.map((f) => (
            <div
              key={f.path}
              className={`sandbox-item ${selected?.path === f.path ? 'active' : ''}${f.preexisting ? ' preexisting' : ''}`}
            >
              <button type="button" className="sandbox-item-main" onClick={() => void openFile(f)}>
                <span className="sandbox-item-name" title={f.path}>
                  {f.path}
                  {f.preexisting && <span className="sandbox-item-origin" title="沙箱确立前已存在的原有文件">原</span>}
                </span>
                <span className="sandbox-item-meta">
                  {formatSize(f.size)} · {formatMtime(f.mtime)}
                </span>
              </button>
              <button
                type="button"
                className="sandbox-item-del"
                title="删除"
                onClick={(e) => {
                  e.stopPropagation()
                  void deleteFile(f)
                }}
              >
                ✕
              </button>
            </div>
          ))}
        </div>

        <div className="sandbox-preview">
          {!selected && <div className="empty">选择左侧文件查看内容</div>}

          {selected?.kind === 'image' && (
            <div className="sandbox-visual">
              <div className="sandbox-preview-bar">
                <span className="sandbox-preview-name">{selected.path}</span>
              </div>
              <img
                className="sandbox-img"
                src={api.sandboxRawUrl(selected.path)}
                alt={selected.path}
              />
            </div>
          )}

          {selected?.kind === 'pdf' && (
            <div className="sandbox-visual">
              <div className="sandbox-preview-bar">
                <span className="sandbox-preview-name">{selected.path}</span>
              </div>
              <iframe
                className="sandbox-iframe"
                src={api.sandboxRawUrl(selected.path)}
                title={selected.path}
                sandbox=""
              />
            </div>
          )}

          {selected && selected.kind === 'binary' && (
            <div className="empty">二进制文件（{formatSize(selected.size)}），无法预览或编辑</div>
          )}

          {selected?.is_text && !editing && (
            <>
              <div className="sandbox-preview-bar">
                <span className="sandbox-preview-name">{selected.path}</span>
                <div className="sandbox-actions">
                  {RENDERABLE_KINDS.has(selected.kind) && (
                    <button
                      type="button"
                      className={`ex-regen${previewing ? ' primary' : ''}`}
                      onClick={() => setPreviewing(true)}
                      title="渲染预览"
                    >
                      👁 预览
                    </button>
                  )}
                  <button type="button" className="ex-regen" onClick={startEdit}>
                    ✏️ 编辑
                  </button>
                </div>
              </div>
              {loadingContent ? (
                <pre className="sandbox-content">加载中…</pre>
              ) : previewing && selected.kind === 'markdown' ? (
                <div
                  className="step-markdown sandbox-markdown"
                  dangerouslySetInnerHTML={{ __html: safeMarkdown(content) }}
                />
              ) : previewing && selected.kind === 'html' ? (
                <iframe
                  key={renderNonce}
                  className="sandbox-iframe"
                  src={api.sandboxRawUrl(selected.path)}
                  title={selected.path}
                  sandbox=""
                />
              ) : previewing && selected.kind === 'csv' ? (
                <div className="sandbox-table-wrap">
                  <table className="sandbox-table">
                    <tbody>
                      {parseTable(content).map((row, i) => (
                        <tr key={i}>
                          {row.map((cell, j) => (
                            <td key={j}>{cell}</td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : HIGHLIGHTED_KINDS.has(selected.kind) ? (
                <pre className="sandbox-content sandbox-highlight">
                  <code
                    className="hljs"
                    dangerouslySetInnerHTML={{ __html: sourceHtml(selected, content) }}
                  />
                </pre>
              ) : (
                <pre className="sandbox-content">{content}</pre>
              )}
            </>
          )}

          {selected?.is_text && editing && (
            <>
              <div className="sandbox-preview-bar">
                <span className="sandbox-preview-name">编辑：{selected.path}</span>
                <div className="sandbox-actions">
                  <button
                    type="button"
                    className="ex-regen"
                    onClick={cancelEdit}
                    disabled={saving}
                  >
                    取消
                  </button>
                  <button
                    type="button"
                    className="ex-regen primary"
                    onClick={() => void saveEdit()}
                    disabled={saving}
                  >
                    {saving ? '保存中…' : '💾 保存'}
                  </button>
                </div>
              </div>
              <textarea
                className="sandbox-editor"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                spellCheck={false}
              />
            </>
          )}
        </div>
      </div>
    </div>
  )
}
