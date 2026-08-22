import DOMPurify from 'dompurify'
import { marked } from 'marked'
import hljs from 'highlight.js'

/**
 * AI 回复 / 沙箱文件内容属于「不可信输入」：fetch 抓取的网页可能携带注入指令，
 * 模型会原样回显进回复与交付物。marked 与 highlight.js 均不做安全过滤，
 * 渲染前必须经 DOMPurify 消毒，否则脚本可在应用同源上下文执行、调用 /api/*
 * 读取聊天记录 / 长期记忆 / 沙箱文件后外传。
 */

/** markdown → 消毒后的 HTML（AI 回复、任务报告、沙箱 md 预览）。 */
export function safeMarkdown(text: string): string {
  return DOMPurify.sanitize(marked.parse(text, { async: false }))
}

/** 已生成的 HTML 片段 → 消毒（highlight.js 输出等）。 */
export function safeHtml(html: string): string {
  return DOMPurify.sanitize(html)
}

/** 代码高亮 + 消毒，供 json/code 源码视图使用。 */
export function safeHighlight(code: string, language: string): string {
  let highlighted: string
  try {
    highlighted = hljs.highlight(code, { language, ignoreIllegals: true }).value
  } catch {
    highlighted = hljs.highlightAuto(code).value
  }
  return safeHtml(highlighted)
}
