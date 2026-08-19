#!/usr/bin/env bash
# One-shot dev launcher for agentpulse.
#
# Order matters (and is enforced):
#   1. kill any leftover processes on :8000 / :5173
#   2. start the FastAPI backend  (:8000) and WAIT until /api/health is up
#   3. start the Vite frontend    (:5173)
# Ctrl-C stops both (trap cleanup).
set -euo pipefail
cd "$(dirname "$0")/.."

node scripts/kill-dev.mjs

echo "[dev] 启动后端 uvicorn :8000 ..."
uv run uvicorn agentpulse.api:app --reload --port 8000 &
BACKEND_PID=$!

# wait for the backend to answer before launching the frontend
backend_ready=0
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
    backend_ready=1
    break
  fi
  sleep 0.5
done
if [ "$backend_ready" -ne 1 ]; then
  echo "[dev] ⚠️  后端 15s 内未就绪（日志见上方），仍继续启动前端" >&2
fi

echo "[dev] 启动前端 vite :5173 ..."
(cd web && pnpm dev) &
FRONTEND_PID=$!

cleanup() {
  echo ""
  echo "[dev] 停止前后端 (backend=$BACKEND_PID frontend=$FRONTEND_PID)"
  kill "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "[dev] 就绪 → 打开 http://localhost:5173"
echo "[dev] 后端 API 文档 → http://127.0.0.1:8000/docs   （Ctrl-C 停止全部）"
wait
