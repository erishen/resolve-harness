# ===========================================================================
# agentpulse — developer task runner
#
#   make            # show this help
#   make install    # install deps (incl. dev)
#   make test       # run offline unit tests
#   make chat       # interactive REPL
#   make demo       # scripted tool demo
#   make env        # create .env from template (never overwrites)
#   make clean      # remove caches only
#   make clean-data # ALSO delete long-term memory DB (data/) — irreversible
# ===========================================================================

UV ?= uv

.PHONY: help install test chat demo env clean clean-data

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

env: ## 从模板生成 .env（已存在则不覆盖）
	@test -f .env || cp .env.example .env
	@echo ".env ready — edit it to set your model & key"

clean: ## 清理缓存（__pycache__ / .pytest_cache）
	rm -rf .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

clean-data: ## ⚠️ 删除长期记忆 SQLite（data/，不可恢复）
	rm -rf data
	@echo "long-term memory deleted"
