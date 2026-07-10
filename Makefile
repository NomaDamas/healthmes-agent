# HealthMes Agent — mac-native tooling (primary run path).
# Thin wrappers around scripts/dev_mac.sh; docker compose remains the
# alternative full-stack path (see docs/DEVELOPMENT.md).

DEV_MAC := bash scripts/dev_mac.sh

.PHONY: help mac-setup mac-services-start mac-services-stop mac-services-status \
	mac-run mac-test mac-ow mac-ow-worker compose-config

help: ## List targets
	@grep -E '^[a-z][a-zA-Z_-]*:.*##' $(MAKEFILE_LIST) | \
		awk -F ':.*## ' '{printf "  %-22s %s\n", $$1, $$2}'

mac-setup: ## brew install pg16+redis if missing, initdb ./data/pg, create DBs, uv sync
	$(DEV_MAC) setup

mac-services-start: ## Start ephemeral postgres (pg_ctl) + redis (pidfiles under ./data/)
	$(DEV_MAC) services-start

mac-services-stop: ## Stop the ephemeral postgres + redis
	$(DEV_MAC) services-stop

mac-services-status: ## Show whether the ephemeral services are running
	$(DEV_MAC) services-status

mac-run: ## alembic upgrade head (if present) + uvicorn healthmes on 8100
	$(DEV_MAC) run

mac-test: ## uv run pytest -q
	$(DEV_MAC) test

mac-ow: ## Best-effort native boot of vendor/open-wearables backend (needs mac-services-start)
	$(DEV_MAC) ow

mac-ow-worker: ## open-wearables celery worker (needs redis from mac-services-start)
	$(DEV_MAC) ow-worker

compose-config: ## Validate the docker compose file (no daemon required)
	docker compose config -q
