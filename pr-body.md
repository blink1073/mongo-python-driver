PYTHON-6084

## Changes in this PR

Reworks how Evergreen tasks select the Python interpreter used by `uv`, replacing the `TOOLCHAIN_VERSION`-based path translation with uv-native selection:

- `.evergreen/scripts/setup-uv-python.sh`: instead of translating `TOOLCHAIN_VERSION` into hardcoded per-platform interpreter paths (macOS frameworks, Windows `C:/python/...`, `/opt/python/...`), the script now exports:
  - `UV_PYTHON` — the requested Python (a version like `3.10` or `3.14t`, an implementation like `pypy3.11`, or an interpreter path), defaulting to CPython 3.10 when a task does not pin one;
  - `UV_PYTHON_SEARCH_PATH` — the toolchain bin dir for plain CPython versions, so uv uses the toolchain Python instead of downloading one;
  - `UV_PYTHON_PREFERENCE=system` — so the toolchain wins over managed installs.
  Anything the toolchain cannot provide (PyPy, other versions, exact patch versions on perf tasks) falls back to uv downloading it. Explicit interpreter paths (e.g. the FIPS host) are existence-checked with a fail-fast error.
- `.evergreen/scripts/generate_config.py` (and the regenerated `.evergreen/generated_configs/*.yml`): sweep of `TOOLCHAIN_VERSION=python` task vars to `UV_PYTHON=python`; the mod_wsgi tasks were dropped to match their earlier removal from `main`.
- `.evergreen/scripts/setup_tests.py`, `utils.py`, `run-tests.sh`: pass through / scrub the new environment variables alongside the existing `UV_PYTHON` handling.
- `CONTRIBUTING.md`: documents `UV_PYTHON` as the way to request a Python, the deterministic CPython 3.10 default, and that the uv binary version is pinned separately via `[tool.uv] required-version` (PYTHON-6091).

Builds on the PYTHON-6091 uv-pinning work already on `main`; no driver code is changed.

## Test Plan

- Regenerated the Evergreen config with `generate_config.py` and confirmed the committed `tasks.yml` / `functions.yml` / `variants.yml` are byte-identical to the generator output (per-version task counts match the old config exactly; the diff is the `TOOLCHAIN_VERSION` → `UV_PYTHON` rename).
- Exercised the new `setup-uv-python.sh` logic directly: default to 3.10 when unset; `3.14t` falls back to `uv python install` and resolves a free-threaded build via `UV_PYTHON_SEARCH_PATH` + `UV_PYTHON_PREFERENCE=system`; toolchain dirs are identical to the old hardcoded translations on Linux/macOS/Windows (`IS_WIN32`); path requests fail fast when missing.
- `bash -n` on the changed shell scripts and `py_compile` on the changed Python scripts.
- Evergreen tasks will run on this PR.

No unit tests were added: this change is CI tooling only, and its behavior is fully validated by config regeneration plus the Evergreen tasks themselves.

## Checklist
<!-- Do not delete the items provided on this checklist. -->

### Checklist for Author
- [x] Did you update the changelog (if necessary)? *(Not necessary — CI tooling only, no driver code changes.)*
- [x] Is there test coverage? *(Via config-regeneration verification and Evergreen task runs, as described above.)*
- [x] Is any followup work tracked in a JIRA ticket? If so, add link(s). *(No followup tracked.)*

### Checklist for Reviewer
- [ ] Does the title of the PR reference a JIRA Ticket?
- [ ] Do you fully understand the implementation? (Would you be comfortable explaining how this code works to someone else?)
- [ ] Is all relevant documentation (README or docstring) updated?
