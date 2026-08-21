import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type { TaskEvent, TaskSnapshot } from '../types'
import {
  GroupedCard,
  FilePreviewModal,
  extractTitle,
  fmtNow,
  groupEvents,
  mdToText,
  stripTitle,
} from './TaskPanel'

/** ISO → 本地 YYYY-MM-DD HH:mm（列表时间标签） */
function fmtTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/** 从事件流提取产出文件（含 fallback，兼容隔离前历史） */
function producedFilesOf(events: TaskEvent[]): { name: string; path: string; fallback: string }[] {
  const taskId = events[0] ? String(events[0].task_id) : null
  if (!taskId) return []
  const seen = new Set<string>()
  const out: { name: string; path: string; fallback: string }[] = []
  for (const ev of events) {
    if (ev.type !== 'tool_call' || String(ev.data.name ?? '') !== 'write_file') continue
    const p = String((ev.data.args as Record<string, unknown> | undefined)?.path ?? '')
    if (p && !seen.has(p)) {
      seen.add(p)
      out.push({ name: p, path: `tasks/${taskId}/${p}`, fallback: p })
    }
  }
  return out
}

function lastTaskEnd(events: TaskEvent[]): string {
  for (let i = events.length - 1; i >= 0; i--) {
    if (events[i].type === 'task_end') return String(events[i].data.reply ?? '')
  }
  return ''
}

export default function HistoryPanel() {
  const [tasks, setTasks] = useState<TaskSnapshot[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [preview, setPreview] = useState<{ name: string; path: string; fallback: string } | null>(null)
  const [saveOpen, setSaveOpen] = useState(false)
  const [saveKey, setSaveKey] = useState('')
  const [saveMsg, setSaveMsg] = useState('')
  const [saveSrc, setSaveSrc] = useState<{ title: string; body: string } | null>(null)

  const load = useCallback(async () => {
    try {
      const { tasks } = await api.listTasks()
      setTasks(tasks.filter((t) => t.status !== 'running'))
    } catch {
      /* backend down */
    }
  }, [])
  useEffect(() => {
    void load()
  }, [load])

  const selected = tasks.find((t) => t.task_id === selectedId) ?? null
  const events = selected?.events ?? []
  const items = groupEvents(events)
  const producedFiles = producedFilesOf(events)
  const finalReply = lastTaskEnd(events)

  // 找「报告文档」：第一个能提取出 markdown 标题的产出文件（含 fallback）
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

  const openSave = async () => {
    setSaveMsg('')
    setSaveOpen(true)
    const src = await findReportFile()
    setSaveSrc(src)
    setSaveKey(
      src?.title || extractTitle(finalReply) || String(selected?.objective ?? '').slice(0, 30) || '任务结果',
    )
  }

  const saveToMemory = async () => {
    const key = saveKey.trim()
    if (!key) return
    try {
      let body = saveSrc?.body ?? ''
      if (!body) body = finalReply
      await api.addMemory(key, `[${fmtNow()}] ${mdToText(body).slice(0, 500)}`)
      setSaveOpen(false)
      setSaveMsg('✅ 已存入长期记忆（带保存时间）')
    } catch (e) {
      setSaveMsg(`存入失败：${e instanceof Error ? e.message : String(e)}`)
    }
  }

  return (
    <div className="history-panel">
      <div className="hist-list">
        <div className="hist-head">📚 历史任务（{tasks.length}）· 点击回看</div>
        {tasks.length === 0 ? (
          <div className="empty">暂无历史记录 — 成功完成的任务会自动保存在这里</div>
        ) : (
          tasks.map((t) => (
            <button
              key={t.task_id}
              type="button"
              className={`hist-item${t.task_id === selectedId ? ' active' : ''}`}
              onClick={() => setSelectedId(t.task_id)}
            >
              <span className="hist-item-text">
                {t.objective.length > 46 ? `${t.objective.slice(0, 46)}…` : t.objective}
              </span>
              <span className="hist-item-meta">
                <span className={`hist-status ${t.status}`}>{t.status}</span>
                <span className="hist-time">🕐 {fmtTime(t.created_at)}</span>
              </span>
            </button>
          ))
        )}
      </div>

      <div className="hist-detail">
        {!selected ? (
          <div className="empty">从左侧选择一条历史任务，回看它的完整执行过程</div>
        ) : (
          <>
            <div className="hist-detail-head">
              <span className="hist-detail-title">{selected.objective}</span>
              <span className="hist-detail-time">🕐 {fmtTime(selected.created_at)}</span>
            </div>
            <div className="task-stream">
              {items.map((item, i) => (
                <GroupedCard key={i} item={item} />
              ))}
              {producedFiles.length > 0 && (
                <div className="produced-files">
                  <div className="produced-title">📄 产出文件（{producedFiles.length}）· 点击查看</div>
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
              {finalReply && (
                <div className="mem-save">
                  {!saveOpen ? (
                    <button type="button" className="mem-save-btn" onClick={() => void openSave()}>
                      💾 把本次结果存入长期记忆
                    </button>
                  ) : (
                    <div className="mem-save-inline">
                      <span className="mem-save-label">记忆 key</span>
                      <input
                        className="mem-save-input"
                        value={saveKey}
                        onChange={(e) => setSaveKey(e.target.value)}
                        placeholder="记忆键名"
                      />
                      <button type="button" onClick={() => void saveToMemory()}>
                        保存
                      </button>
                      <button type="button" onClick={() => setSaveOpen(false)}>
                        取消
                      </button>
                      {saveMsg && <span className="mem-save-msg">{saveMsg}</span>}
                    </div>
                  )}
                </div>
              )}
            </div>
          </>
        )}
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
