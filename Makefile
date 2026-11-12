# job-copilot — one-command entry points.   `make help` lists them.
PY      ?= .venv/bin/python
PORT    ?= 8000
TORCH_CPU = --extra-index-url https://download.pytorch.org/whl/cpu

.DEFAULT_GOAL := help
.PHONY: help setup dev run test pii check ingest discover smoke-live facts-check facts-review answers-review llm-check docker-build docker-up daily daily-fixtures schedule-install schedule-uninstall schedule-status

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

ingest:  ## CLI ingest of companies + targets + boards -> jobs.jsonl
	$(PY) run.py

discover:  ## refresh discovered companies (yc-oss, Bangalore Startup Map, ATS detection)
	$(PY) -m jobcopilot.discovery all

smoke-live:  ## live ingest, prints per-source counts (no jsonl, no DB writes)
	$(PY) -m jobcopilot.ingest --smoke

facts-check:  ## validate profile/resume_facts.yaml
	$(PY) -m jobcopilot.facts check

facts-review:  ## list every résumé fact you still need to confirm
	$(PY) -m jobcopilot.facts review

answers-review:  ## list every value autofill could use + its status (approved/proposed)
	$(PY) -m jobcopilot.answers review

llm-check:  ## show which LLM provider/model is live
	$(PY) -c "from dotenv import load_dotenv; load_dotenv('.env'); from jobcopilot import llm; print(llm.health())"

docker-build:  ## build the container image
	docker build -t job-copilot:dev .

docker-up:  ## run the container locally (data/config/profile mounted from the host)
	docker compose up --build

daily:  ## nightly pipeline now: discover -> ingest -> evaluate -> score -> queue (ARGS=--skip-discovery)
	$(PY) -m jobcopilot.daily $(ARGS)

daily-fixtures:  ## the same on test fixtures, no network, into data/daily-fixtures.db
	$(PY) -m jobcopilot.daily --no-network

schedule-install:  ## install the 19:00 LaunchAgent (~/Library/LaunchAgents/com.jobcopilot.daily.plist)
	$(PY) scripts/schedule.py install

schedule-uninstall:  ## remove the LaunchAgent
	$(PY) scripts/schedule.py uninstall

schedule-status:  ## LaunchAgent installed/loaded, next run, last run
	$(PY) scripts/schedule.py status

# Coming in later phases (see PLAN.md): deploy-demo / stop-demo (Phase 3b).
