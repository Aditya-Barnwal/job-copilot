# job-copilot — one-command entry points.   `make help` lists them.
PY      ?= .venv/bin/python
PORT    ?= 8000
TORCH_CPU = --extra-index-url https://download.pytorch.org/whl/cpu

.DEFAULT_GOAL := help
.PHONY: help setup dev run test pii check ingest facts-check facts-review llm-check docker-build docker-up

help:  ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[1m%-14s\033[0m %s\n", $$1, $$2}'

.venv/bin/python:
	python3.13 -m venv .venv || python3 -m venv .venv

setup: .venv/bin/python  ## create venv + install everything (incl. dev + autofill deps)
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q $(TORCH_CPU) -r requirements-dev.txt
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example — add your keys")

dev:  ## run the app with auto-reload on http://127.0.0.1:8000 (PORT=… to change)
	$(PY) -m uvicorn server.app:app --reload --host 127.0.0.1 --port $(PORT)

run:  ## run the app without reload
	$(PY) -m uvicorn server.app:app --host 127.0.0.1 --port $(PORT)

test:  ## offline test suite
	$(PY) -m pytest tests/ -q

pii:  ## fail if personal data is in tracked files (repo is public)
	$(PY) scripts/check_pii.py

check: test pii  ## everything CI runs (minus the docker build)

ingest:  ## CLI ingest of config/companies.yaml -> jobs.jsonl
	$(PY) run.py

facts-check:  ## validate profile/resume_facts.yaml
	$(PY) -m jobcopilot.facts check

facts-review:  ## list every résumé fact you still need to confirm
	$(PY) -m jobcopilot.facts review

llm-check:  ## show which LLM provider/model is live
	$(PY) -c "from dotenv import load_dotenv; load_dotenv('.env'); from jobcopilot import llm; print(llm.health())"

docker-build:  ## build the container image
	docker build -t job-copilot:dev .

docker-up:  ## run the container locally (data/config/profile mounted from the host)
	docker compose up --build

# Coming in later phases (see PLAN.md): daily, schedule-install, schedule-uninstall,
# smoke-live (Phase 1–2), deploy-demo / stop-demo (Phase 3b).
