# K2AAM WebSDR developer entry points. Never opens a physical SDR.
UV      ?= uv
PY      := $(UV) run --project control --frozen
SCHEMA  := protocol/schema/control-v1.json

.PHONY: help sync worker worker-san test test-a1 test-worker-san schema schema-check fixtures fixtures-check check
help:
	@echo "make sync          install pinned dev environment (control/uv.lock)"
	@echo "make worker        build the C++ worker (build/worker/ksdr-worker)"
	@echo "make test-worker-san  worker tests against an ASan/UBSan build"
	@echo "make test          run all control tests"
	@echo "make test-a1       A1: frequency/wire integer-Hz tests"
	@echo "make schema        regenerate $(SCHEMA)"
	@echo "make schema-check  A14: fail if $(SCHEMA) is stale"
	@echo "make fixtures      regenerate fixtures/iq (synthetic SigMF)"
	@echo "make fixtures-check fail if fixtures/iq differs from the generator"
	@echo "make check         schema-check + fixtures-check + test"

sync:
	$(UV) sync --project control --frozen

worker:
	cmake -S worker -B build/worker -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo
	cmake --build build/worker

worker-san:
	cmake -S worker -B build/worker-san -G Ninja -DK2_SANITIZE=ON
	cmake --build build/worker-san

test: worker
	$(PY) pytest control/tests

test-worker-san: worker-san
	K2_WORKER_BIN=$(CURDIR)/build/worker-san/ksdr-worker \
	ASAN_OPTIONS=detect_leaks=1:abort_on_error=1 UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1 \
	$(PY) pytest control/tests/test_worker.py

test-a1:
	$(PY) pytest control/tests/test_freq.py control/tests/test_models.py

schema:
	$(PY) python -m k2websdr.protocol.schema --write $(SCHEMA)

schema-check:
	$(PY) python -m k2websdr.protocol.schema --check $(SCHEMA)

fixtures:
	python3 fixtures/gen/make_synthetic.py --write fixtures/iq

fixtures-check:
	python3 fixtures/gen/make_synthetic.py --check fixtures/iq

check: schema-check fixtures-check test
