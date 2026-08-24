# Shortcuts for the things that get typed more than once.
# `make help` lists them.

.DEFAULT_GOAL := help
.PHONY: help install dev seed test lint check run harness clean

PYTHON ?= python

help:  ## Show this list
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install:  ## Runtime dependencies only (what Railway installs)
	$(PYTHON) -m pip install -r requirements.txt

dev:  ## Runtime + test + lint dependencies
	$(PYTHON) -m pip install -r requirements-dev.txt

seed:  ## Import the catalog and the governorate list into the database
	$(PYTHON) manage.py seed

test:  ## The full suite
	$(PYTHON) -m pytest tests/

lint:  ## Ruff, no autofix
	$(PYTHON) -m ruff check .

check: lint test  ## What CI runs

run:  ## The real app, with reload
	$(PYTHON) -m uvicorn app:app --reload

harness:  ## The local chat UI (unauthenticated; never expose it)
	HARNESS_ENABLED=1 $(PYTHON) -m assistant.harness.web

clean:  ## Remove caches and local databases
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache
	rm -f wanas.db wanas.db-shm wanas.db-wal test_wanas.db test_wanas.db-shm test_wanas.db-wal
