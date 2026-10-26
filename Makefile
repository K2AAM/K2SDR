# K2AAM WebSDR developer entry points. Never opens a physical SDR.
UV      ?= uv
PY      := $(UV) run --project control --frozen
SCHEMA  := protocol/schema/control-v1.json

.PHONY: help sync test test-a1 schema schema-check check
help:
	@echo "make sync          install pinned dev environment (control/uv.lock)"
	@echo "make test          run all control tests"
	@echo "make test-a1       A1: frequency/wire integer-Hz tests"
	@echo "make schema        regenerate $(SCHEMA)"
	@echo "make schema-check  A14: fail if $(SCHEMA) is stale"
	@echo "make check         schema-check + test"

sync:
	$(UV) sync --project control --frozen

test:
	$(PY) pytest control/tests

test-a1:
	$(PY) pytest control/tests/test_freq.py control/tests/test_models.py

schema:
	$(PY) python -m k2websdr.protocol.schema --write $(SCHEMA)

schema-check:
	$(PY) python -m k2websdr.protocol.schema --check $(SCHEMA)

check: schema-check test
