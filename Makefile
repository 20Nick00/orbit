ifeq ($(OS),Windows_NT)
PYTHON ?= .venv/Scripts/python.exe
else
PYTHON ?= .venv/bin/python
endif

.PHONY: demo demo-10k run test

demo:
	"$(PYTHON)" -m alembic upgrade head
	"$(PYTHON)" -m core.demo

demo-10k:
	"$(PYTHON)" -m alembic upgrade head
	"$(PYTHON)" -m core.demo --transactions 10000

run:
	"$(PYTHON)" -m alembic upgrade head
	"$(PYTHON)" -m uvicorn app.main:app --host 127.0.0.1 --port 8000

test:
	"$(PYTHON)" -m pytest
