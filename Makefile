export DATABASE_URL ?= postgresql://app:app-dev-only@127.0.0.1:55109/app
PY = ./venv/bin/python
BASE = http://127.0.0.1:8109

bootstrap:      ## local virtualenv + database container
	python3 -m venv venv && ./venv/bin/pip install -q -r requirements-dev.txt
	docker compose up -d db
seed:           ## migrate and load the synthetic world (no-op if already seeded)
	$(PY) -m ws.seed --if-empty
dev:            ## API with reload on :8109, jobs run by a local worker
	($(PY) -m core.jobs ws.api &) && $(PY) -m uvicorn ws.api:app --reload --port 8109
test:
	$(PY) -m pytest --cov=ws --cov=core --cov-report=term-missing
demo:           ## the full stack in Docker, then the seven-step scenario against it
	docker compose up --build -d --wait && $(PY) -m core.scenario $(BASE)
load-test:      ## run `make demo` first; reads the schedule the demo produced
	$(PY) -m core.loadtest $(BASE) acme-viewer-demo 32 10 "GET /v1/locations" "GET /v1/schedules/$$($(PY) -c "import json; print(json.load(open('web/demo.json'))['opt']['body']['result']['schedule_id'])")" "GET /v1/fairness?schedule_id=$$($(PY) -c "import json; print(json.load(open('web/demo.json'))['opt']['body']['result']['schedule_id'])")"
reset:          ## drop all data (volume included)
	docker compose down -v
.PHONY: bootstrap seed dev test demo load-test reset
