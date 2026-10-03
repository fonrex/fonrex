# Makefile for Fonrex (FonRex Pro) API
SHELL := /bin/bash
PYTHON := python3
DOCKER_COMPOSE := docker-compose
CONTAINER_NAME := fonrex-api
PORT := 5000

# Colors
GREEN := \033[0;32m
BLUE := \033[0;34m
CYAN := \033[0;36m
YELLOW := \033[1;33m
RED := \033[0;31m
NC := \033[0m

.PHONY: help install install-dev lock lock-upgrade clean test test-db test-cov lint typecheck syntax migration-check quality ci run dev docker-build docker-run docker-stop logs health status db-reset cache-clear example-quote example-quotes example-client info

# The API requires a key by default: export FONREX_API_KEY so the curl-based
# targets below can authenticate (e.g. `FONREX_API_KEY=frx_live_... make example-quote`).
AUTH_HEADER := $(if $(FONREX_API_KEY),-H "X-API-KEY: $(FONREX_API_KEY)",)

TYPED_BOUNDARIES := use_cases/ports.py use_cases/fundamentals.py cache/adapters.py cache/technical.py technical/contracts.py technical/catalog.py technical/calculation_engine.py technical/indicator_service.py database/technical.py realtime/connection_manager.py financials/enrichment/adapters.py schemas/technical.py schemas/realtime.py

.DEFAULT_GOAL := help

help: ## Display this help message
	@echo -e "$(CYAN)🚀 Fonrex (FonRex Pro) API - Make Commands$(NC)"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  $(GREEN)%-20s$(NC) %s\n", $$1, $$2}'

install: ## Install the locked runtime dependencies
	@echo -e "$(BLUE)📦 Installing dependencies...$(NC)"
	$(PYTHON) -m pip install --require-hashes -r requirements.lock
	@echo -e "$(GREEN)✅ Dependencies installed$(NC)"

install-dev: ## Install the locked dev and quality dependencies
	$(PYTHON) -m pip install --require-hashes -r requirements-dev.lock

# requirements*.txt list what the project needs, as ranges; requirements*.lock
# record the exact versions installed by Docker, the CI and the two targets above.
LOCK_FLAGS := --universal --python-version 3.12 --generate-hashes --custom-compile-command "make lock" --quiet

lock: ## Refresh the lock files after editing requirements*.txt (needs uv)
	uv pip compile requirements.txt $(LOCK_FLAGS) -o requirements.lock
	uv pip compile requirements-dev.txt $(LOCK_FLAGS) -c requirements.lock -o requirements-dev.lock

lock-upgrade: ## Move every dependency to its latest allowed version (then run `make ci`)
	uv pip compile requirements.txt $(LOCK_FLAGS) --upgrade -o requirements.lock
	uv pip compile requirements-dev.txt $(LOCK_FLAGS) --upgrade -c requirements.lock -o requirements-dev.lock

setup: ## Initial setup (create logs, instance, backup dirs)
	@echo -e "$(BLUE)🔧 Running initial setup...$(NC)"
	mkdir -p logs instance backups static/logos
	touch logs/app.log
	chmod +x entrypoint.sh start.sh
	@echo -e "$(GREEN)✅ Project configured$(NC)"

clean: ## Clean cache files and __pycache__
	@echo -e "$(YELLOW)🧹 Cleaning cache files and __pycache__...$(NC)"
	find . -name "*.pyc" -delete
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache
	@echo -e "$(GREEN)✅ Cleanup completed$(NC)"

test: ## Run unit and integration tests with pytest
	@echo -e "$(BLUE)🧪 Running pytest test suite...$(NC)"
	PYTHONPATH=. pytest
	@echo -e "$(GREEN)✅ Tests completed$(NC)"

# Throwaway TimescaleDB for the database tests: the image of docker-compose.yml,
# on its own port, without volume. The CI runs the same tests on the same image.
TEST_DB_IMAGE := timescale/timescaledb-ha:pg16
TEST_DB_CONTAINER := fonrex-test-db
TEST_DB_PORT := 54329
TEST_DB_URL := postgresql://fonrex:fonrex_password@127.0.0.1:$(TEST_DB_PORT)/postgres

test-db: ## Run the database tests on a throwaway TimescaleDB container (needs Docker)
	@docker rm -f -v $(TEST_DB_CONTAINER) >/dev/null 2>&1 || true
	docker run -d --name $(TEST_DB_CONTAINER) -e POSTGRES_USER=fonrex -e POSTGRES_PASSWORD=fonrex_password -e POSTGRES_DB=postgres -p 127.0.0.1:$(TEST_DB_PORT):5432 $(TEST_DB_IMAGE) >/dev/null
	@echo "Waiting for the test database..."
	@ready=0; for attempt in $$(seq 1 90); do \
		if docker exec $(TEST_DB_CONTAINER) pg_isready -h 127.0.0.1 -U fonrex -d postgres >/dev/null 2>&1; then ready=1; break; fi; \
		sleep 1; \
	done; \
	if [ $$ready -ne 1 ]; then \
		echo "The test database did not start:"; docker logs --tail 30 $(TEST_DB_CONTAINER); \
		docker rm -f -v $(TEST_DB_CONTAINER) >/dev/null; exit 1; \
	fi; \
	FONREX_TEST_DATABASE_URL=$(TEST_DB_URL) PYTHONWARNINGS=error PYTHONPATH=. $(PYTHON) -m pytest tests/test_timescale_integration.py --no-cov; status=$$?; \
	docker rm -f -v $(TEST_DB_CONTAINER) >/dev/null; \
	exit $$status

test-cov: ## Run tests with coverage reporting
	PYTHONWARNINGS=error PYTHONPATH=. $(PYTHON) -m pytest --cov=. --cov-branch --cov-report=term-missing --cov-report=xml --cov-report=json:coverage.json
	$(PYTHON) scripts/check_coverage_distribution.py coverage.json

lint: ## Lint Python source code with Ruff
	$(PYTHON) -m ruff check .

typecheck: ## Check typed boundaries with Ruff
	$(PYTHON) -m ruff check --select ANN $(TYPED_BOUNDARIES)

syntax: ## Compile Python sources without writing bytecode
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m compileall -q -x '/(venv|node_modules|scratch)/' .

migration-check: ## Verify single Alembic migration head
	@test "$$($(PYTHON) -m alembic heads | grep -c '(head)')" -eq 1 || (echo "Alembic must have exactly one head" && exit 1)

quality: lint typecheck syntax migration-check test-cov ## Run complete local quality pipeline

ci: quality ## CI pipeline entrypoint

run: ## Run FastAPI application locally with uvicorn (loads .env when present)
	@echo -e "$(BLUE)🚀 Starting application...$(NC)"
	@echo -e "$(CYAN)🔗 API: http://localhost:$(PORT)$(NC)"
	@echo -e "$(CYAN)🔗 Docs: http://localhost:$(PORT)/docs$(NC)"
	set -a; [ -f .env ] && . ./.env; set +a; uvicorn main:app --reload --port $(PORT)

docker-build: ## Build Docker images
	@echo -e "$(BLUE)🐳 Building Docker images...$(NC)"
	$(DOCKER_COMPOSE) build
	@echo -e "$(GREEN)✅ Images built successfully$(NC)"

docker-run: ## Start Docker infrastructure (API + TimescaleDB + Redis)
	@echo -e "$(BLUE)🐳 Starting Docker services...$(NC)"
	$(DOCKER_COMPOSE) up -d
	@echo -e "$(GREEN)✅ Docker services started$(NC)"
	@echo -e "$(CYAN)🔗 API: http://localhost:$(PORT)$(NC)"
	@echo -e "$(CYAN)🔗 Docs: http://localhost:$(PORT)/docs$(NC)"

docker-stop: ## Stop and remove Docker containers
	@echo -e "$(YELLOW)🛑 Stopping Docker services...$(NC)"
	$(DOCKER_COMPOSE) down
	@echo -e "$(GREEN)✅ Docker services stopped$(NC)"

docker-restart: docker-stop docker-run ## Restart Docker infrastructure

docker-logs: ## Tail Docker API container logs
	@echo -e "$(BLUE)📋 Tailing Docker API logs...$(NC)"
	$(DOCKER_COMPOSE) logs -f $(CONTAINER_NAME)

docker-clean: ## Clean Docker environment completely (including volumes)
	@echo -e "$(YELLOW)🧹 Full Docker cleanup...$(NC)"
	$(DOCKER_COMPOSE) down -v --remove-orphans
	docker system prune -f
	@echo -e "$(GREEN)✅ Full Docker cleanup completed$(NC)"

logs: ## Tail all Docker logs (API, DB, Redis)
	@echo -e "$(BLUE)📋 Tailing all Docker logs...$(NC)"
	$(DOCKER_COMPOSE) logs -f

status: ## Check Docker service status
	@echo -e "$(BLUE)📊 Service status...$(NC)"
	$(DOCKER_COMPOSE) ps

health: ## Check API health endpoint
	@echo -e "$(BLUE)🏥 Performing health check...$(NC)"
	@curl -s http://localhost:$(PORT)/health | python3 -m json.tool 2>/dev/null || echo -e "$(RED)❌ API unavailable$(NC)"

db-reset: ## Reset PostgreSQL/TimescaleDB database in Docker
	@echo -e "$(YELLOW)🗄️ Resetting and applying migrations...$(NC)"
	$(DOCKER_COMPOSE) exec fonrex-api alembic downgrade base || true
	$(DOCKER_COMPOSE) exec fonrex-api alembic upgrade head
	@echo -e "$(GREEN)✅ Database reset and up to date$(NC)"

db-seed: ## Seed default asset catalog into database
	@echo -e "$(BLUE)🌱 Seeding asset catalog...$(NC)"
	$(DOCKER_COMPOSE) exec fonrex-api python scripts/seed_database.py --enrich
	@echo -e "$(GREEN)✅ Seeding completed$(NC)"

cache-clear: ## Clear global Redis cache
	@echo -e "$(BLUE)🗑️ Clearing Redis cache...$(NC)"
	@curl -X POST $(AUTH_HEADER) http://localhost:$(PORT)/cache/clear 2>/dev/null || echo -e "$(RED)❌ Error clearing cache$(NC)"

example-quote: ## Example: Fetch real-time price snapshot (REST) for AAPL
	@echo -e "$(BLUE)📈 Fetching quote for AAPL...$(NC)"
	@curl -s $(AUTH_HEADER) "http://localhost:$(PORT)/quote/AAPL" | python3 -m json.tool

example-quotes: ## Example: Batch fetch quotes for AAPL, AIR.PA, BNP.PA (REST)
	@echo -e "$(BLUE)📈 Batch fetching quotes...$(NC)"
	@curl -s $(AUTH_HEADER) "http://localhost:$(PORT)/quotes?tickers=AAPL,AIR.PA,BNP.PA" | python3 -m json.tool

example-client: ## Example: Run real-time python client (WebSocket/REST) for AAPL
	@echo -e "$(BLUE)⚡ Starting real-time client (Ctrl+C to quit)...$(NC)"
	python3 scripts/example_realtime_client.py AAPL

info: ## Project information
	@echo -e "$(CYAN)🚀 Fonrex (FonRex Pro) API$(NC)"
	@echo -e "Port: $(PORT)"
	@echo -e "Container: $(CONTAINER_NAME)"
	@echo ""
	@echo -e "$(BLUE)Main endpoints:$(NC)"
	@echo -e "  Health: http://localhost:$(PORT)/health"
	@echo -e "  Documentation (Swagger): http://localhost:$(PORT)/docs"
	@echo -e "  Latest price AAPL: http://localhost:$(PORT)/quote/AAPL"
	@echo -e "  WebSocket stream AAPL: ws://localhost:$(PORT)/ws/realtime/AAPL"

# Shortcuts
start: docker-run
stop: docker-stop
restart: docker-restart
build: docker-build
up: docker-run
down: docker-stop
