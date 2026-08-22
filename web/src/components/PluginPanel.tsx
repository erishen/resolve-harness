import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type { PluginItem } from '../types'

type FilterKey = 'all' | 'core' | 'promoted' | 'runtime'

export default function PluginPanel() {
  const [plugins, setPlugins] = useState<PluginItem[]>([])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [filter, setFilter] = useState<FilterKey>('all')

  const filteredPlugins = plugins.filter((p) => {
    if (filter === 'core') return p.builtin && p.kind === '内置核心'
    if (filter === 'promoted') return p.builtin && p.kind === '内置晋升'
    if (filter === 'runtime') return !p.builtin
    return true
  })

  const load = useCallback(async () => {
    try {
      const { plugins } = await api.plugins()
      setPlugins(plugins)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const toggle = (name: string) => {
    const p = plugins.find((x) => x.name === name)
    if (p?.builtin) return // built-in detectors cannot be promoted
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  const toggleExpand = (name: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  const remove = async (name: string) => {
    setError('')
    try {
      await api.deletePlugin(name)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const promote = async () => {
    if (selected.size === 0 || busy) return
    setError('')
    setNotice('')
    setBusy(true)
    try {
      const res = await api.promotePlugins([...selected])
      setNotice(
        `已晋升 ${res.promoted} 个检测器到 ${res.file}（已从运行时插件目录移除，需 git commit 保留）`,
      )
      setSelected(new Set())
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="plugin-panel">
      <div className="plugin-header">
        <div className="plugin-intro">
          <div className="plugin-title">Fast-path 插件（运行时 + 内置）</div>
          <div className="plugin-sub">
            内置核心 = fastpath.py 自带匹配器（只读）；内置晋升 = 已晋升进源码的检测器（只读）；
            运行时 = Agent 生成的检测器，勾选后「晋升到源码」可合并写回 src/resolve_harness/generated_detectors.py。
          </div>
        </div>
        <div className="plugin-actions">
          <button onClick={() => void promote()} disabled={selected.size === 0 || busy}>
            晋升选中到源码（{selected.size}）
          </button>
          <button onClick={() => void load()} disabled={busy}>
            刷新
          </button>
        </div>
      </div>

      {notice && <div className="plugin-notice">{notice}</div>}
      {error && <div className="error-banner">{error}</div>}

      <div className="tools-filters plugin-filters">
        {(() => {
          const counts = {
            all: plugins.length,
            core: plugins.filter((p) => p.builtin && p.kind === '内置核心').length,
            promoted: plugins.filter((p) => p.builtin && p.kind === '内置晋升').length,
            runtime: plugins.filter((p) => !p.builtin).length,
          }
          const defs: { key: FilterKey; label: string; count: number }[] = [
            { key: 'all', label: '全部', count: counts.all },
            { key: 'core', label: '内置核心', count: counts.core },
            { key: 'promoted', label: '内置晋升', count: counts.promoted },
            { key: 'runtime', label: '运行时', count: counts.runtime },
          ]
          return defs.map((f) => (
            <button
              key={f.key}
              type="button"
              className={`tool-filter ${filter === f.key ? 'active' : ''}`}
              onClick={() => setFilter(f.key)}
            >
              {f.label}（{f.count}）
            </button>
          ))
        })()}
      </div>

      <div className="plugin-list">
        {filteredPlugins.length === 0 ? (
          <div className="empty">
            暂无插件 —— 在任务 tab 问一个内置未覆盖的确定性任务（如「计算 8 的阶乘」），Agent 会生成插件出现在这里
          </div>
        ) : (
          filteredPlugins.map((p) => (
            <div key={p.name} className="plugin-item">
              <label className="plugin-item-head">
                {p.builtin ? (
                  <span className="plugin-builtin">📦</span>
                ) : (
                  <input
                    type="checkbox"
                    checked={selected.has(p.name)}
                    onChange={() => toggle(p.name)}
                  />
                )}
                <span className="plugin-name">{p.name}</span>
                {p.builtin && <span className="plugin-badge">{p.kind ?? '内置'}</span>}
                {p.trigger && <span className="plugin-trigger"># {p.trigger}</span>}
                <span className="plugin-size">{p.size} B</span>
                <button
                  type="button"
                  className="plugin-expand"
                  onClick={() => toggleExpand(p.name)}
                >
                  {expanded.has(p.name) ? '收起' : '代码'}
                </button>
                {!p.builtin && (
                  <button
                    type="button"
                    className="plugin-delete"
                    onClick={() => void remove(p.name)}
                    title="删除插件"
                  >
                    ✕
                  </button>
                )}
              </label>
              {expanded.has(p.name) && (
                <pre className="plugin-source">{p.source}</pre>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  )
}
