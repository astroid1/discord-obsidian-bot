# Works from Git Bash / WSL / Linux / macOS. Each target is a plain command you can also run by hand.
PY ?= python
VENV ?= .venv
BIN := $(VENV)/bin
ifeq ($(OS),Windows_NT)
  BIN := $(VENV)/Scripts
endif

.PHONY: venv dev smoke test lint fmt build up down logs backfill sync precommit-install

venv:                 ## create venv + install dev deps (no GPU extras)
	$(PY) -m venv $(VENV)
	$(BIN)/python -m pip install -U pip
	$(BIN)/python -m pip install -e ".[dev]"

dev:                  ## run the bot natively
	$(BIN)/python -m dob run

smoke:                ## make smoke FILE=path-or-url [ARGS="--fake-llm --fake-whisper"]
	$(BIN)/python scripts/smoke.py $(FILE) --vault ./tmp-vault $(ARGS)

test:
	$(BIN)/python -m pytest -q

lint:
	$(BIN)/ruff check . && $(BIN)/ruff format --check .

fmt:
	$(BIN)/ruff check --fix . && $(BIN)/ruff format .

build:
	docker compose build

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=200 bot

backfill:             ## full history backfill of all archived channels
	docker compose exec bot dob backfill

sync:                 ## one incremental sync
	docker compose exec bot dob sync

precommit-install:
	$(BIN)/pre-commit install
