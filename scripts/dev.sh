#!/usr/bin/env bash
# One-shot dev launcher for resolve_harness.
#
# Order matters (and is enforced):
#   1. kill any leftover processes on :8899 / :5175
#   2. start the FastAPI backend  (:8899) and WAIT until /api/health is up
#   3. start the Vite frontend    (:5175)
# Ctrl-C stops both (trap cleanup).
#
# ---------------------------------------------------------------------------
# 三个踩过的坑，改动前先读懂，别改回去：
#
# 1) `set -m`（job control）必须开。
#    不开时后台 job 与本脚本同属一个进程组，`$!` 拿到的是「包装进程」——
#    `(cd web && pnpm dev)` 的子 shell、`uv run` 的 uv 本身。kill 这个 PID 只杀包装，
#    真正干活的 vite / uvicorn 是它们的子进程，会变成孤儿继续占着 8899 / 5175
#    （实测：kill 掉包装子 shell 后，里面的进程仍在跑）。结果就是「停了但端口还被占着」，
#    下次 make dev 只能靠 kill-dev.mjs 兜底清理。
#    开了 job control 后每个后台 job 独占进程组，`$!` 同时就是进程组 ID，
#    `kill -TERM -$PGID` 一次带走整棵进程树。
#
# 2) 健康检查的 curl 必须 `--noproxy '*'`。
#    本机有 http_proxy 时 curl 会把 127.0.0.1 的请求也交给代理：后端没起来时拿到的是
#    代理回的 502 而不是连接失败，判据失真（`-f` 勉强能挡住，但后端真起来了也可能因为
#    代理规则而探测不到，白等 15s）。
#
# 3) trap 必须在起任何后台 job 之前装。
#    装在 wait 之前的话，健康检查那 15s 里 Ctrl-C 会留下完全没人管的后端进程。
#
# 4) INT/TERM/HUP 只 exit，清理统一交给 EXIT trap —— 否则 cleanup 会被执行两次。
#    用户主动中断按 0 退出（不然 make 会打印 `*** [dev] Error 130`，看着像失败）；
#    服务自己崩了则把真实退出码抛出去，见文件末尾。
#
# 5) ⚠️ 不要在 `make dev` 跑着的时候编辑本脚本。
#    bash 不是一次性把脚本读进内存，而是按字节偏移边读边执行；文件被改长/改短后，
#    它会从错误的偏移继续读，于是执行到「跳过来的一半语句」——典型症状是明明在开头
#    赋过值的变量，到文件末尾却报 `未绑定的变量`（unbound variable），行号还对不上。
#    遇到这种报错：先停掉 dev，确认改完，再重新 `make dev`。
#
# 6) 后台 job 一律用「不 exec 的子壳」当组长。
#    外部命令（uv/node）直接放后台时，子进程可能抢在父 bash 的 setpgid 之前 exec
#    → 父进程报「child setpgid: Operation not permitted」EPERM 噪音（进程组实际已由
#    子进程自建成功，功能无碍，纯噪音）。子壳组长永不 exec，竞态窗口消失；$! 仍 = PGID。
# ---------------------------------------------------------------------------
set -euo pipefail
set -m   # job control: 每个后台 job 独立进程组，$! == PGID，便于整组清理

cd "$(dirname "$0")/.."

BACKEND_PGID=""
FRONTEND_PGID=""

kill_pgroup() {   # $1=SIG  $2=PGID
  # 只对自己拉起来的 job 进程组发信号。PGID 为空 / 非数字 / 等于本脚本 PID 时跳过：
  # 免得 `kill -TERM -` 打到自己所在的进程组，把自己也带走。
  case "${2:-}" in
    '' | *[!0-9]* ) return 0 ;;
  esac
  # 必须是 if 而不是 `[ x = y ] && return`：&& 列表整体返回非 0 时 set -e 会当场
  # 终止整个 cleanup（进程一个都没杀到，日志里连「已停止」都不会打印），
  # 只留下跑着的孤儿进程 —— 正是「停了但端口还被占着」的来源。
  if [ "$2" = "$$" ]; then
    return 0
  fi
  kill -"$1" -"$2" 2>/dev/null || true
}

cleanup() {
  # Ctrl-C / 关终端 / 有服务提前退出，都会落到这里（只走一次）
  kill_pgroup TERM "${BACKEND_PGID:-}"
  kill_pgroup TERM "${FRONTEND_PGID:-}"
  sleep 1
  kill_pgroup KILL "${BACKEND_PGID:-}"
  kill_pgroup KILL "${FRONTEND_PGID:-}"
  echo ""
  echo "[dev] 已停止前后端 (backend pgid=${BACKEND_PGID:-未启动} frontend pgid=${FRONTEND_PGID:-未启动})"
}

trap 'exit 0' INT TERM HUP
trap cleanup EXIT

node scripts/kill-dev.mjs

echo "[dev] 启动后端 uvicorn :8899 ..."
# 用「不 exec 的子壳」当组长：`uv run ... &` 直接把外部命令放后台时，子进程可能
# 抢在父 bash 的 setpgid 之前 exec（机器满载时尤甚）→ 父进程报
# 「child setpgid: Operation not permitted」EPERM 噪音（进程组实际已由子进程自建
# 成功，功能无碍）。子壳组长不 exec，竞态窗口消失；$! 仍 = PGID。
( uv run python -m uvicorn resolve_harness.api:app --reload --port 8899 ) &
BACKEND_PGID=$!

# wait for the backend to answer before launching the frontend
backend_ready=0
for _ in $(seq 1 30); do
  if curl -fsS --noproxy '*' --max-time 2 http://127.0.0.1:8899/api/health >/dev/null 2>&1; then
    backend_ready=1
    break
  fi
  # 进程已经死了就别再干等：多半是 uv 不在 PATH / 依赖没装 / 端口冲突。
  # 以前这里会硬等满 15s 再报个警告照样起前端，最后还是打印「就绪 → 打开 …」，
  # 看着像成功其实后端根本没起来，排查时极其误导（连 exit code 都是 0 之外的失败）。
  if ! kill -0 "$BACKEND_PGID" 2>/dev/null; then
    echo "[dev] ❌ 后端进程已退出（pid=$BACKEND_PGID），不再启动前端。请看上方输出。" >&2
    exit 1
  fi
  sleep 0.5
done
if [ "$backend_ready" -ne 1 ]; then
  echo "[dev] ⚠️  后端 15s 内未就绪（进程仍在），仍继续启动前端" >&2
fi

echo "[dev] 启动前端 vite :5175 ..."
(cd web && pnpm dev) &
FRONTEND_PGID=$!

echo "[dev] 就绪 → 打开 http://localhost:5175"
echo "[dev] 后端 API 文档 → http://127.0.0.1:8899/docs   （Ctrl-C 停止全部）"

# 正常情况下面这个循环会一直转到 Ctrl-C。
# 这里不用裸 `wait`：它要等「所有」后台 job 都退出才返回，于是前端崩了、后端还活着时
# dev 会一直挂着 —— 页面打不开却没有任何提示，最难排查的一种状态。
# macOS 自带 bash 3.2 没有 `wait -n`（能等任意一个），只能自己轮询存活性，1s 粒度。
while kill -0 "${BACKEND_PGID:-}" 2>/dev/null && kill -0 "${FRONTEND_PGID:-}" 2>/dev/null; do
  sleep 1
done

# 走到这里，说明至少一个服务自己退出了（Ctrl-C 走的是上面的 trap，不会到这里）。
# 先取回真实退出码，再决定要不要当成故障 —— 这一步很关键：
#   128+SIGTERM(15) / 128+SIGINT(2) / 128+SIGHUP(1)
#       = 被外部停掉（kill-dev 清端口、关终端、IDE 收会话都会这样）。这不是故障，
#         静默退出 0，否则 make 会打一行 `*** [dev] Error 1`，看着像 dev 崩了。
#   其它非 0（1 / 3 / 127 …）
#       = 服务真崩了，必须把退出码暴露出来，否则排查时一点线索都没有。
service_rc() {   # $1=pgid；还活着返回 0，已死则 wait 取真实退出码
  [ -n "${1:-}" ] || { echo 0; return 0; }
  if kill -0 "$1" 2>/dev/null; then
    echo 0
  else
    local _rc=0
    wait "$1" 2>/dev/null || _rc=$?
    echo "$_rc"
  fi
}
is_signal_stop() {   # $1=rc
  [ "$1" -gt 128 ] || return 1
  case $(( $1 - 128 )) in
    1 | 2 | 15) return 0 ;;
    *) return 1 ;;
  esac
}

backend_rc=$(service_rc "${BACKEND_PGID:-}")
frontend_rc=$(service_rc "${FRONTEND_PGID:-}")

if is_signal_stop "$backend_rc" || is_signal_stop "$frontend_rc"; then
  exit 0   # 被信号停掉，正常；cleanup 会打印「已停止前后端」
fi
if [ "$backend_rc" -ne 0 ]; then
  echo "[dev] ❌ 后端已退出（pgid=${BACKEND_PGID:-?}，退出码 $backend_rc），正在停止全部" >&2
fi
if [ "$frontend_rc" -ne 0 ]; then
  echo "[dev] ❌ 前端已退出（pgid=${FRONTEND_PGID:-?}，退出码 $frontend_rc），正在停止全部" >&2
fi
exit 1   # 服务异常退出要暴露出来，别让 make 以为一切正常
