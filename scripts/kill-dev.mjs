#!/usr/bin/env node
/**
 * Kill leftover resolve_harness dev processes so `make dev` always starts from a
 * clean slate: anything bound to the backend port :8000 or the Vite port
 * :5173 (uvicorn / vite from previous runs).
 * Cross-platform (macOS / Linux / Windows).
 */
import { spawnSync } from 'node:child_process'

const PORTS = [8000, 5173]
const isWin = process.platform === 'win32'

function sh(cmd, args) {
  try {
    const r = spawnSync(cmd, args, { encoding: 'utf8', shell: isWin })
    return (r.stdout || '') + (r.stderr || '')
  } catch {
    return ''
  }
}

function pidsOnPort(port) {
  if (isWin) {
    const out = sh('netstat', ['-ano', '|', 'findstr', `:${port}`])
    const pids = out
      .split(/\r?\n/)
      .map((l) => l.trim().split(/\s+/).pop())
      .filter((p) => p && /^\d+$/.test(p) && Number(p) !== process.pid)
    return [...new Set(pids)].map(Number)
  }
  const out = sh('lsof', ['-ti', `tcp:${port}`])
  return out.split(/\r?\n/).filter(Boolean).map(Number)
}

function killPid(pid) {
  try {
    if (isWin) sh('taskkill', ['/PID', String(pid), '/T', '/F'])
    else process.kill(pid, 'SIGTERM')
    return true
  } catch {
    return false
  }
}

function sleep(ms) {
  try {
    const buf = new SharedArrayBuffer(4)
    Atomics.wait(new Int32Array(buf), 0, 0, ms)
  } catch {
    /* non-shared-array envs (rare) fall through */
  }
}

function collect() {
  return new Set(PORTS.flatMap(pidsOnPort))
}

let killed = 0
for (const pid of collect()) {
  if (killPid(pid)) killed += 1
}

sleep(300) // grace period so SIGTERM can finish before we hard-kill stragglers

for (const pid of collect()) {
  try {
    if (isWin) sh('taskkill', ['/PID', String(pid), '/T', '/F'])
    else process.kill(pid, 'SIGKILL')
    killed += 1
  } catch {
    /* already gone */
  }
}

if (killed) console.log(`[kill-dev] 已清理 ${killed} 个占用 :8000/:5173 的残留进程`)
else console.log('[kill-dev] 端口干净，无需清理')
process.exit(0)
