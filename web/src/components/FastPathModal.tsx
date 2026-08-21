/** Fast Path 说明弹窗：确定性查询为何"零模型秒回"。 */
export default function FastPathModal({ onClose }: { onClose: () => void }) {
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">⚡ Fast Path 代码直算</div>
        <div className="modal-body">
          <p>
            这条回答<b>没有经过大模型</b>：harness 先识别你的输入是不是「确定性查询」，
            命中就直接用内置代码算出结果——<b>零模型调用、零 token</b>。
          </p>
          <ul>
            <li>识别 + 求值由 <code>fastpath.py</code> 的 7 个内置匹配器完成（见插件 Tab → 内置核心）：算术 / 统计 / 单位换算 / 日期 / 进制 / 文本统计 / 时间</li>
            <li>Agent 运行中生成的检测器（codegen）命中时同样零模型，可在插件 Tab → 运行时管理</li>
            <li>需要最新 / 实时数据时请明确说「最新」——agent 会改走 <code>fetch</code> 工具（可能需人工审批）</li>
          </ul>
        </div>
        <div className="modal-foot">
          <button className="modal-close" onClick={onClose}>
            知道了
          </button>
        </div>
      </div>
    </div>
  )
}

/** Fast Path 匹配器的 method 名（fastpath.py / codegen 返回）。 */
export const FAST_PATH_METHODS = new Set([
  'arithmetic',
  'time',
  'statistics',
  'unit_convert',
  'date_math',
  'base_convert',
  'text_stats',
  'plugin',
  'promoted',
  'codegen',
])
