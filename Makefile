# ===========================================================================
# agentpulse — developer task runner
#
#   make            # show this help
#   make install    # install Python deps (incl. dev)
#   make test       # run offline unit tests
#   make chat       # interactive REPL (terminal)
#   make demo       # scripted tool demo
#   make dev        # ONE-SHOT: clean :8000/:5173 -> backend first -> frontend
#   make api        # start FastAPI backend on :8000 only
#   make web-dev    # start Vite dev server on :5173 only (needs `cd web && pnpm install` first)
#   make web-build  # build frontend to web/dist
#   make env        # create .env from template (never overwrites)
#   make clean      # remove caches only
#   make clean-data # ALSO delete long-term memory DB (data/) — irreversible
#
# Web UI (make dev, or two terminals):
#   terminal 1: make api
#   terminal 2: make web-dev   -> open http://localhost:5173
# ===========================================================================

UV ?= uv
PNPM ?= pnpm

.PHONY: help install test chat demo dev api web-dev web-build env clean clean-data

help: ## 显示所有可用命令
	@echo "Usage: make <target>"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

install: ## 安装依赖（含 dev）
	$(UV) sync --extra dev

test: ## 运行离线单元测试（不需要 API key）
	$(UV) run pytest

chat: ## 交互式对话（REPL）
	$(UV) run agentpulse-chat

demo: ## 脚本演示：时间 / 计算 / 记忆
	$(UV) run python examples/tool_demo.py

dev: ## 一键启动前后端：先清理 :8000/:5173 残留，后端先起、健康检查通过后再起前端，Ctrl-C 全部停止
	@bash scripts/dev.sh

api: ## 启动 FastAPI 后端（http://127.0.0.1:8000，文档 /docs）
	$(UV) run uvicorn agentpulse.api:app --reload --port 8000

web-dev: ## 启动 Vite 前端（http://localhost:5173，需先 pnpm install）
	cd web && $(PNPM) dev

web-build: ## 构建前端到 web/dist
	cd web && $(PNPM) build

env: ## 从模板生成 .env（已存在则不覆盖）
	@test -f .env || cp .env.example .env
	@echo ".env ready — edit it to set your model & key"

clean: ## 清理缓存（__pycache__ / .pytest_cache）
	rm -rf .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

clean-data: ## ⚠️ 删除长期记忆 SQLite（data/，不可恢复）
	rm -rf data
	@echo "long-term memory deleted"
