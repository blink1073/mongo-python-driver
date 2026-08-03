# PYTHON-5980: uv Lock File Management

Design for committing `uv.lock`, gating drift in CI, and automating weekly
refreshes with a reusable GitHub Action.

Ticket: https://jira.mongodb.org/browse/PYTHON-5980

## Background

PyMongo previously committed `uv.lock`. [PYTHON-5862](https://jira.mongodb.org/browse/PYTHON-5862)
removed it in two steps:

- `d150c687` deleted `uv.lock`, the `uv-lock` pre-commit hook, and the
  CONTRIBUTING guidance, added `uv.lock` to `.gitignore`, and set `UV_NO_LOCK=1`
  in Evergreen and the justfile.
- `74c011f9` finished the cleanup and removed `UV_NO_LOCK` from the justfile and
  `test-python.yml`.

This ticket reverses the lockfile removal and adds automation on top. Roughly
half the work is a targeted revert of `d150c687`; the new work is the action and
workflow.

Two existing facts shape the design:

- `pyproject.toml` sets `[tool.uv] exclude-newer = "7 days"`. uv records this in
  the lock as `exclude-newer-span = "P7D"`, not as an absolute timestamp, so the
  7 day cooldown is already native and `uv lock --check` is stable across dates.
- `APP_ID` and `APP_PRIVATE_KEY` exist only in the `release` environment, which
  also holds `AWS_ROLE_ARN`, `AWS_SECRET_ID`, and `ARTIFACTORY_USERNAME`. There
  are no repo level or org level variables, and the only repo secret is
  `CODECOV_TOKEN`.

## Goals

1. `uv.lock` tracked in version control and current as of the commit.
2. Dependabot limited to security updates for the uv ecosystem.
3. A weekly workflow that refreshes the lock and maintains one open PR.
4. CI fails when the lock drifts from `pyproject.toml`.
5. The refresh logic packaged so it can move to `mongodb-labs/drivers-github-tools`
   with a file copy and a `uses:` change.

## Non-Goals

These are deliberately excluded and tracked separately:

- **Evergreen lockfile use.** `.evergreen/scripts/configure-env.sh` keeps
  `UV_NO_LOCK=1`. The drift gate lives in GitHub Actions only. Changing Evergreen
  would affect the entire test matrix for no benefit to the stated acceptance
  criteria.
- **`set-uv-exclude-newer` cleanup.** `.github/actions/set-uv-exclude-newer` and
  the Evergreen `UV_EXCLUDE_NEWER` export are arguably redundant now that
  `pyproject.toml` carries `exclude-newer = "7 days"`. Unrelated to this ticket.
- **Restoring the `uv-lock` pre-commit hook.** Considered and rejected. CI
  reports drift; contributors fix it with a documented command.

## Architecture

Three separable pieces, in dependency order.

### A. Repo state

| File | Change |
| --- | --- |
| `.gitignore` | Remove the `# uv lockfiles` / `uv.lock` block |
| `uv.lock` | Generate with `uv lock --upgrade`, commit |
| `.github/dependabot.yml` | uv entry gets `applies-to: security-updates` and `open-pull-requests-limit: 0` |
| `.github/workflows/test-python.yml` | `static` job gains a `uv lock --check` step and a step running both action test scripts |
| `CONTRIBUTING.md` | Rewrite the "Dependabot updates" section |

The committed lock must be produced by `uv lock --upgrade`, not by committing
whatever a developer has on disk. A lock that merely satisfies `pyproject.toml`
can still pin stale versions, and the first scheduled run would then open a large
catch up PR. Generating with the same command the action runs makes that first
run a no-op.

`open-pull-requests-limit` is documented as governing version updates. Its
interaction with an `applies-to: security-updates` block is not clearly
specified. The acceptance criteria call for both keys, so both are set, and the
resulting behavior should be confirmed after merge.

### B. Composite action

```
.github/actions/uv-lock-update/
  action.yml
  diff_lock.py
  decide_pr_action.sh
  test_diff_lock.sh
  test_decide_pr_action.sh
```

Structure mirrors `Calysto/maintainer_tools/actions/poetry-lock-update`
file for file, including its shell based tests. Matching the reference layout is
what keeps the eventual move to drivers-github-tools a copy rather than a port.

The action assumes only that the repo is checked out and `uv` is on `PATH`. It
does not install uv, so it carries no pinned third party dependency into
drivers-github-tools; the consuming workflow controls the uv version.

#### Inputs

| Input | Default | Description |
| --- | --- | --- |
| `app-id` | `""` | GitHub App ID. Falls back to `github.token` when empty. |
| `app-private-key` | `""` | GitHub App private key. |
| `branch` | `uv-lock-update` | Fixed branch, force pushed each run. |
| `base` | `main` | PR base branch. Not present in the reference action, which lets `gh` infer it. Explicit is better for an action that will run unattended in repos with release branches. |
| `labels` | `dependencies` | Labels applied to the PR. |
| `dry-run` | `"false"` | Skip push and PR creation. |

There is no `min-release-age-days` input. The poetry action needs one because
`POETRY_SOLVER_MIN_RELEASE_AGE` is environment only. Here the cooldown is
`exclude-newer` in the locking repo's `pyproject.toml`, so an input would let the
action silently contradict the repo's own policy.

#### Steps

1. Generate an App token with `actions/create-github-app-token`, gated on both
   App inputs being non empty, requesting `contents: write` and
   `pull-requests: write`.
2. Copy `uv.lock` to `$RUNNER_TEMP/uv.lock.before`.
3. Run `uv lock --upgrade`.
4. Exit 0 early if `git diff --quiet uv.lock`.
5. Build the PR body from `diff_lock.py`.
6. Commit as `github-actions[bot]`, set an authenticated remote URL, force push
   the fixed branch.
7. Delegate to `decide_pr_action.sh`.

#### PR de-duplication

`decide_pr_action.sh` takes branch, title, body, labels, and dry-run, then
queries `gh pr list --head "$BRANCH" --state open`. An open PR on the branch is
updated in place with `gh pr edit`; otherwise a new PR is created. A merged or
manually closed PR is not "open", so it falls through to creating a fresh one
with no extra state to track.

This pairs with the force push in step 6. The branch is exclusively bot owned and
rebuilt from base every run, so overwriting whatever the remote holds is always
safe. Together they guarantee exactly one open PR and prevent the stale PR
accumulation a random suffix branch would cause.

Keeping this in its own script rather than inlining it in `action.yml` is what
makes it testable: `test_decide_pr_action.sh` puts a fake `gh` on `PATH` that
logs its invocations and answers `pr list` from a canned JSON file, then asserts
which of create or edit was called across four cases (no open PR, open PR, open
PR under dry-run, no open PR under dry-run).

#### Lock diff script

`diff_lock.py` reads both lock files with `tomllib`, falling back to `tomli` on
older interpreters as the poetry action does, and emits a markdown list of added,
removed, and changed packages. A `uv.lock` diff runs to hundreds of thousands of
lines, so the summary is what makes the PR reviewable.

**This is where the port diverges from the reference.** The reference builds
`dict[str, str]` keyed on package name, which is correct for poetry, where each
package appears once. uv emits one `[[package]]` entry per resolution fork, so a
package resolved differently across Python versions appears several times. In the
current lock, 129 entries cover 93 unique names, with `sphinx` appearing three
times and 35 other names twice. Keying on name would silently keep whichever
entry parsed last and report phantom upgrades while hiding real ones.

Our `load_versions` therefore maps each name to the sorted set of its versions,
and `diff_versions` compares those sets, rendering multi-version packages as
`- sphinx: 7.4.7, 8.1.3 → 7.4.7, 8.2.0`. This fix should be contributed back to
the reference action, since any uv consumer hits it.

`test_diff_lock.sh` follows the reference test, with added cases for a package
gaining a version, losing one, and changing one of several.

All four shell files need the executable bit set. The repo's `executable-shell`
pre-commit hook enforces the shebang and permission bit agreeing.

### C. Workflow

`.github/workflows/uv-lock-update.yml`, deliberately thin:

- `schedule: "0 12 * * 2"` (Tuesdays 12:00 UTC) plus `workflow_dispatch` with a
  `dry-run` boolean.
- `environment: automation`.
- `if: github.repository_owner == 'mongodb' || github.event_name == 'workflow_dispatch'`,
  matching `release-python.yml`, so forks do not run it on a schedule.
- `concurrency: uv-lock-update` with `cancel-in-progress: false`. Two runs force
  pushing the same branch concurrently is the one way this design corrupts
  itself.
- Workflow level `permissions: contents: read`. The action mints its own App
  token, so `GITHUB_TOKEN` never needs write access. The write permissions matter
  only for consumers who omit the App inputs.
- `actions/checkout` with `persist-credentials: false`. The action sets its own
  authenticated remote URL.
- `astral-sh/setup-uv` SHA pinned to `11f9893b081a58869d3b5fccaea48c9e9e46f990`
  (v8.3.2), identical to `test-python.yml`, keeping the uv version consistent
  across CI.

## Prerequisite: the `automation` environment

**Blocking, requires a repo admin, cannot be done in code.**

Create an `automation` environment on `mongodb/mongo-python-driver` holding:

- variable `APP_ID`, copied from the `release` environment
- secret `APP_PRIVATE_KEY`, copied from the `release` environment

The workflow cannot reuse `release` because that environment also exposes
`AWS_ROLE_ARN`, `AWS_SECRET_ID`, and Artifactory credentials, none of which a
weekly dependency bot needs. A separate environment is the boundary that keeps
release credentials away from scheduled automation.

The `environment: automation` line is inert until this exists, and the first run
fails on an empty `app-id`.

## Why a GitHub App token

Pull requests opened with `GITHUB_TOKEN` do not trigger workflow runs. A lock
update PR with no CI signal defeats the purpose, since the point is to learn
whether the upgraded dependency set still passes tests. An App token triggers CI
normally.

This is also why the design does not follow `sbom.yml`, which uses
`peter-evans/create-pull-request` with `GITHUB_TOKEN`. Moving the SBOM workflow
onto the same App token is a reasonable follow up.

## Verification

- `uv lock --upgrade` then `uv lock --check` passes.
- `rm uv.lock && uv lock --upgrade` reproduces byte identical output.
- `test_diff_lock.sh` and `test_decide_pr_action.sh` pass. Both are run against
  the real current lock as well as fixtures, so the multi-version handling is
  exercised on actual uv output rather than only on synthetic input.
- `just typing`, `just lint`, and `just lint-manual` pass. `lint-manual` runs
  shellcheck, which covers the two new shell scripts and the test scripts.
- The zizmor workflow passes on the new workflow and action. The `unpinned-uses`
  policy allows ref pins for `actions/*`, so tag pinning
  `actions/create-github-app-token` is acceptable; `astral-sh/setup-uv` is SHA
  pinned to match existing usage.
- After merge, a manual `workflow_dispatch` run with `dry-run: true` confirms the
  action end to end without opening a PR, then a second run without `dry-run`
  confirms PR creation. Scheduled triggers are frequently delayed or dropped by
  GitHub, so the first run is dispatched manually rather than waited on.

## Risks

| Risk | Mitigation |
| --- | --- |
| `automation` environment missing at merge time | Blocking prerequisite, called out above; first run is manual so the failure is immediate and visible |
| Dependabot `open-pull-requests-limit` semantics under `applies-to` | Confirm behavior after merge; adjust if security PRs are suppressed |
| Weekly PR fails CI and blocks the next week's update | Force push updates the same PR in place, so the newest lock is always what is under review |
| Lock churn from `exclude-newer` relative span | Span is recorded in the lock, so `uv lock --check` does not drift with the date; verified locally |
