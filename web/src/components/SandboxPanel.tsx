import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
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

export default function SandboxPanel() {
  const [files, setFiles] = useState<SandboxFile[]>([])
  const [selected, setSelected] = useState<SandboxFile | null>(null)
  const [content, setContent] = useState('')
  const [loadingContent, setLoadingContent] = useState(false)
  const [error, setError] = useState('')
  const [sandboxDir, setSandboxDir] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const res = await api.sandbox()
      setFiles(res.files)
      setSandboxDir(res.sandbox_dir)
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

  return (
    <div className="sandbox-panel">
      <div className="sandbox-head">
        <div className="sandbox-title">
          <span className="sandbox-title-main">沙箱文件</span>
          <span className="sandbox-title-sub">
            {sandboxDir ? sandboxDir : 'agent 可写的隔离目录'}
          </span>
        </div>
        <button type="button" className="ex-regen" onClick={() => void load()}>
          🔄 刷新
        </button>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="sandbox-body">
        <div className="sandbox-list">
          {files.length === 0 && !error && (
            <div className="empty">沙箱为空 — 运行一个「写文件」任务试试</div>
          )}
          {files.map((f) => (
            <button
              type="button"
              key={f.path}
              className={`sandbox-item ${selected?.path === f.path ? 'active' : ''}`}
              onClick={() => void openFile(f)}
            >
              <span className="sandbox-item-name" title={f.path}>
                {f.path}
              </span>
              <span className="sandbox-item-meta">
                {formatSize(f.size)} · {formatMtime(f.mtime)}
              </span>
            </button>
          ))}
        </div>

        <div className="sandbox-preview">
          {!selected && <div className="empty">选择左侧文件查看内容</div>}
          {selected && !selected.is_text && (
            <div className="empty">非文本文件（{formatSize(selected.size)}），无法预览</div>
          )}
          {selected?.is_text && (
            <pre className="sandbox-content">
              {loadingContent ? '加载中…' : content}
            </pre>
          )}
        </div>
      </div>
    </div>
  )
}
