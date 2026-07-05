PYTHON ?= ./venv/bin/python

.PHONY: verify verify-lite setup-check web-build test web-test api-smoke

verify:
	$(PYTHON) scripts/verify_local_stack.py --start-api

verify-lite: setup-check web-build test web-test

setup-check:
	$(PYTHON) scripts/check_local_setup.py --skip-runtime-files

web-build:
	npm --prefix apps/web run build

test:
	$(PYTHON) -m pytest -q

web-test:
	npm --prefix apps/web test

api-smoke:
	$(PYTHON) scripts/verify_local_stack.py --skip-setup-check --skip-web-build --skip-tests --start-api
