# K2AAM WebSDR developer entry points. Never opens a physical SDR.
UV      ?= uv
PY      := $(UV) run --project control --frozen
SCHEMA  := protocol/schema/control-v1.json

.PHONY: web web-test help sync liquid dsp-deps dsp-bench worker worker-san probe rx888-stream test test-a1 test-worker-san schema schema-check fixtures fixtures-check ledger ledger-check run-dev check
help:
	@echo "make sync          install pinned dev environment (control/uv.lock)"
	@echo "make liquid        fetch (pinned, verified) and build liquid-dsp into build/third_party (no FFTW)"
	@echo "make dsp-deps      prove the worker and liquid-dsp contain no FFTW and use the pinned build"
	@echo "make dsp-bench     benchmark M1 FFT sizes and channel chains on this machine"
	@echo "make worker        build the C++ worker (build/worker/ksdr-worker)"
	@echo "make probe         build the Airspy HF+ probe (needs libairspyhf-dev, pkg-config)"
	@echo "make test-worker-san  worker + DSP pipeline tests against an ASan/UBSan build"
	@echo "make test          run all control tests"
	@echo "make test-a1       A1: frequency/wire integer-Hz tests"
	@echo "make schema        regenerate $(SCHEMA)"
	@echo "make schema-check  A14: fail if $(SCHEMA) is stale"
	@echo "make fixtures      regenerate fixtures/iq (synthetic SigMF)"
	@echo "make fixtures-check fail if fixtures/iq differs from the generator"
	@echo "make ledger        regenerate ledger/modes.v1.json from the outline appendix"
	@echo "make run-dev       run the service on 127.0.0.1:8075 with replay fixtures"
	@echo "make web           build the browser UI into web/dist (needs node >= 20.19 and npm)"
	@echo "make web-test      browser UI: type check + unit tests (shared protocol vectors)"
	@echo "make check         schema-check + fixtures-check + ledger-check + test"

sync:
	$(UV) sync --project control --frozen

LIQUID := build/third_party/liquid-dsp/lib/libliquid.a

liquid:
	tools/build_liquid.sh

$(LIQUID):
	tools/build_liquid.sh

rx888-stream:
	tools/rx888/build_rx888_stream.sh

worker: $(LIQUID)
	cmake -S worker -B build/worker -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo
	cmake --build build/worker

probe: $(LIQUID)
	cmake -S worker -B build/worker -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo -DK2_REQUIRE_AIRSPYHF=ON
	cmake --build build/worker

worker-san: $(LIQUID)
	cmake -S worker -B build/worker-san -G Ninja -DK2_SANITIZE=ON
	cmake --build build/worker-san

dsp-deps: worker
	python3 tools/check_dsp_deps.py build/worker build/third_party/liquid-dsp

dsp-bench: worker
	build/worker/ksdr-dsp-check bench

test: worker
	$(PY) pytest control/tests

test-worker-san: worker-san
	K2_WORKER_BIN=$(CURDIR)/build/worker-san/ksdr-worker \
	ASAN_OPTIONS=detect_leaks=1:abort_on_error=1 UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1 \
	$(PY) pytest control/tests/test_worker.py control/tests/test_worker_dsp.py
	K2_WORKER_AIRSPYHF_MOCK_BIN=$(CURDIR)/build/worker-san/ksdr-worker-airspyhf-mock \
	ASAN_OPTIONS=detect_leaks=1:abort_on_error=1 UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1 \
	$(PY) pytest control/tests/test_worker_airspyhf.py
	K2_DSP_RUN_BIN=$(CURDIR)/build/worker-san/ksdr-dsp-run \
	ASAN_OPTIONS=detect_leaks=1:abort_on_error=1 UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1 \
	$(PY) pytest control/tests/test_dsp_pipeline.py

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

ledger:
	python3 tools/import_ledger.py docs/outline/WebSDR_Project_Outline-v0.1.md ledger/modes.v1.json

ledger-check:
	python3 tools/import_ledger.py --check docs/outline/WebSDR_Project_Outline-v0.1.md ledger/modes.v1.json

web:
	cd web && npm ci --no-audit --no-fund && npm run build

web-test:
	cd web && npm ci --no-audit --no-fund && npm run check && npm test

run-dev: worker
	$(PY) k2websdr-server --config dev/k2websdr.dev.toml

check: schema-check fixtures-check ledger-check test
