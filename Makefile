PYTHON ?= ./venv/bin/python

.PHONY: verify verify-lite setup-check web-build test web-test npm-audit lint typecheck test-coverage web-test-coverage api-smoke calibration-report cogvideox-smoke musicgen-smoke download-models release-artifact release-smoke third-party-notices

verify:
	$(PYTHON) scripts/verify_local_stack.py --start-api

verify-lite: setup-check web-build test web-test npm-audit lint typecheck test-coverage web-test-coverage

setup-check:
	$(PYTHON) scripts/check_local_setup.py --skip-runtime-files

web-build:
	npm --prefix apps/web run build

test:
	$(PYTHON) -m pytest -q

web-test:
	npm --prefix apps/web test

npm-audit:
	npm --prefix apps/web audit --audit-level=high

lint:
	npm --prefix apps/web run lint
	$(PYTHON) -m ruff check core generators

typecheck:
	$(PYTHON) -m mypy core generators

test-coverage:
	$(PYTHON) -m pytest --cov=core --cov=generators -q

web-test-coverage:
	npm --prefix apps/web run test:coverage

api-smoke:
	$(PYTHON) scripts/verify_local_stack.py --skip-setup-check --skip-web-build --skip-tests --skip-npm-audit --skip-eslint --skip-ruff --skip-mypy --skip-coverage --skip-web-coverage --start-api

calibration-report:
	$(PYTHON) scripts/build_calibration_report.py

download-models:
	./scripts/download_models.sh

cogvideox-smoke:
	$(PYTHON) scripts/smoke_cogvideox.py

musicgen-smoke:
	$(PYTHON) scripts/smoke_musicgen.py

# Build the release tarball (creative-ai-studio-v<VERSION>.tar.gz + SHA256SUMS)
# into artifacts/. Requires a clean working tree: the artifact is cut from HEAD.
release-artifact:
	./scripts/build_release_artifact.sh

# Unpack the built artifact into a throwaway directory, install it, run the
# Stable journey against it, and verify clean shutdown.
release-smoke:
	$(PYTHON) scripts/smoke_release_artifact.py

# Regenerate THIRD_PARTY_NOTICES.md from the installed dependencies. Requires a
# venv built from requirements.txt and apps/web/node_modules (npm ci), because
# it reads real package metadata rather than restating requirements.txt.
third-party-notices:
	$(PYTHON) scripts/collect_third_party_notices.py
