# PYTHON-5980: uv Lock File Management

Design for committing `uv.lock`, gating drift in CI, and automating weekly
refreshes with a shared GitHub Action.

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
half the PyMongo work is a targeted revert of `d150c687`; the new work is the
action.

Three existing facts shape the design:

- `pyproject.toml` sets `[tool.uv] exclude-newer = "7 days"`. uv records this in
  the lock as `exclude-newer-span = "P7D"`, not as an absolute timestamp, so the
  7 day cooldown is already native and `uv lock --check` is stable across dates.
- `APP_ID` and `APP_PRIVATE_KEY` exist only in PyMongo's `release` environment,
  which also holds `AWS_ROLE_ARN`, `AWS_SECRET_ID`, and `ARTIFACTORY_USERNAME`.
  There are no repo level or org level variables, and the only repo secret is
  `CODECOV_TOKEN`.
- `uv.lock` is roughly 715KB and `check-added-large-files` defaults to a 500KB
  limit, so the hook needs an exemption for that one file.

## Goals

1. `uv.lock` tracked in version control and current as of the commit.
2. Dependabot limited to security updates for the uv ecosystem.
3. A weekly workflow that refreshes the lock and maintains one open PR.
4. CI fails when the lock drifts from `pyproject.toml`.
5. The refresh logic shared, so other driver repos using uv get it for free.

## Repository Split

The action is built in `mongodb-labs/drivers-github-tools` first and consumed
from PyMongo. Building it in PyMongo and upstreaming later would mean writing
the action, its two scripts, and its two test scripts into PyMongo only to
delete them again.

| Repo | Contents |
| --- | --- |
| `drivers-github-tools` | `python/uv-lock-update/` action, scripts, tests, CI job, README section |
| `mongo-python-driver` | Lockfile, gitignore, dependabot, drift gate, thin workflow, CONTRIBUTING |

**Sequencing:** the drivers-github-tools PR lands and is tagged first. The
PyMongo PR then references the action at that SHA with a `# v3` comment,
matching how PyMongo already references `secure-checkout` and `codeql`. PyMongo
does not merge a branch reference.

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

## Part A: the shared action

```
python/uv-lock-update/
  action.yml
  diff_lock.py
  decide_pr_action.sh
  test_diff_lock.sh
  test_decide_pr_action.sh
```

Placed under `python/` alongside `pre-publish` and `post-publish`, because uv is
Python-specific tooling and CONTRIBUTING directs opinionated per-language actions
into their own folder. Python scripts beside an action are an established pattern
there (`python/post-publish/handle_following_version.py`).

The action assumes only that the repo is checked out and `uv` is on `PATH`. It
does not install uv, so it carries no pinned third party dependency and the
consuming workflow controls the uv version.

### Conventions

Inputs use `snake_case` (`app_id`, `private_key`, `dry_run`), matching every
existing action in the repo. Scripts are invoked as
`${{ github.action_path }}/script.sh` with values passed through an `env:` block
rather than positional arguments, matching `create-branch`.

### Inputs

| Input | Default | Description |
| --- | --- | --- |
| `app_id` | `""` | GitHub App ID. Falls back to `github.token` when empty. |
| `private_key` | `""` | GitHub App private key. |
| `branch` | `uv-lock-update` | Fixed branch, force pushed each run. |
| `base` | `main` | PR base branch. Explicit, so repos with release branches behave predictably. |
| `labels` | `dependencies` | Labels applied to the PR. |
| `dry_run` | `"false"` | Report intended actions without pushing or mutating PRs. |

There is no cooldown input. The cooldown is `exclude-newer` in the consuming
repo's `pyproject.toml`, so an input would let the action silently contradict
that repo's own policy.

### Steps

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

### PR de-duplication

`decide_pr_action.sh` queries `gh pr list --head "$BRANCH" --base "$BASE"
--state open`. An open PR on the branch is updated in place with `gh pr edit`;
otherwise a new PR is created. A merged or manually closed PR is not "open", so
it falls through to creating a fresh one with no extra state to track.

This pairs with the force push in step 6. The branch is exclusively bot owned and
rebuilt from base every run, so overwriting whatever the remote holds is always
safe. Together they guarantee exactly one open PR and prevent the stale PR
accumulation a random suffix branch would cause.

**Dry-run branches before any mutating `gh` call** rather than passing
`--dry-run` through. `gh pr create --dry-run` documents that it "may still push
git changes", which would break the guarantee that a dry run has no side effects.
Listing is read only and still runs. This diverges from the reference action,
which has the same hole.

### Lock diff script

`diff_lock.py` reads both lock files with `tomllib`, falling back to `tomli` on
older interpreters, and emits a markdown list of added, removed, and changed
packages. A `uv.lock` diff runs to hundreds of thousands of lines, so the summary
is what makes the PR reviewable.

The naive implementation keys package name to version, which is correct for
poetry, where each package appears once. uv emits one `[[package]]` entry per
resolution fork, so a package resolved differently across Python versions appears
several times. In PyMongo's lock, 129 entries cover 93 unique names, with
`sphinx` appearing three times and 35 other names twice. Keying on name would
silently keep whichever entry parsed last and report phantom upgrades while
hiding real ones.

`load_versions` therefore maps each name to the sorted set of its versions, and
`diff_versions` compares those sets, rendering multi-version packages as
``- sphinx: `7.4.7`, `8.1.3` → `7.4.7`, `8.2.0` ``.

### Testing

`test.yml` in drivers-github-tools runs pre-commit only, and `ci.yml` runs
TypeScript tests for a single directory. Neither covers bash, so most of that
repo's shell actions are untested. This adds a shell-test job to `ci.yml` that
runs both scripts, establishing a pattern the other bash actions can adopt.

`test_decide_pr_action.sh` puts a fake `gh` on `PATH` that logs invocations and
answers `pr list` from a canned JSON file, then asserts create-versus-edit across
five cases. It is not a general `gh` emulator; it handles only the call shapes
the script makes.

### Documentation

Actions are documented in the root `README.md`, not per-action READMEs. The new
action gets a section in the existing style.

## Part B: PyMongo

| File | Change |
| --- | --- |
| `.gitignore` | Remove the `# uv lockfiles` / `uv.lock` block |
| `uv.lock` | Generate with `uv lock --upgrade`, commit |
| `.pre-commit-config.yaml` | `exclude: ^uv\.lock$` on `check-added-large-files` |
| `.github/dependabot.yml` | uv entry gets `applies-to: security-updates` and `open-pull-requests-limit: 0` |
| `.github/workflows/test-python.yml` | `static` job gains a `uv lock --check` step |
| `.github/workflows/uv-lock-update.yml` | New, references the shared action |
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

### Workflow

`.github/workflows/uv-lock-update.yml`, deliberately thin:

- `schedule: "0 12 * * 2"` (Tuesdays 12:00 UTC) plus `workflow_dispatch` with a
  `dry_run` boolean.
- `environment: automation`.
- `if: github.repository_owner == 'mongodb' || github.event_name == 'workflow_dispatch'`,
  matching `release-python.yml`, so forks do not run it on a schedule.
- `concurrency: uv-lock-update` with `cancel-in-progress: false`. Two runs force
  pushing the same branch concurrently is the one way this design corrupts
  itself.
- Workflow level `permissions: contents: read`. The action mints its own App
  token, so `GITHUB_TOKEN` never needs write access.
- `actions/checkout` with `persist-credentials: false`. The action sets its own
  authenticated remote URL.
- `astral-sh/setup-uv` SHA pinned to `11f9893b081a58869d3b5fccaea48c9e9e46f990`
  (v8.3.2), identical to `test-python.yml`.

## Prerequisite: the `automation` environment

**Blocking for Part B, requires a repo admin, cannot be done in code.**

Create an `automation` environment on `mongodb/mongo-python-driver` holding:

- variable `APP_ID`, copied from the `release` environment
- secret `APP_PRIVATE_KEY`, copied from the `release` environment

The workflow cannot reuse `release` because that environment also exposes
`AWS_ROLE_ARN`, `AWS_SECRET_ID`, and Artifactory credentials, none of which a
weekly dependency bot needs. A separate environment is the boundary that keeps
release credentials away from scheduled automation.

## Why a GitHub App token

Pull requests opened with `GITHUB_TOKEN` do not trigger workflow runs. A lock
update PR with no CI signal defeats the purpose, since the point is to learn
whether the upgraded dependency set still passes tests. An App token triggers CI
normally.

This is also why the design does not follow PyMongo's `sbom.yml`, which uses
`peter-evans/create-pull-request` with `GITHUB_TOKEN`. Moving the SBOM workflow
onto the same App token is a reasonable follow up.

## Verification

**Part A:**

- `test_diff_lock.sh` and `test_decide_pr_action.sh` pass locally and in `ci.yml`.
- `pre-commit run --all-files --hook-stage manual` passes, covering shellcheck
  and `check-github-actions`.

**Part B:**

- `uv lock --upgrade` then `uv lock --check` passes.
- `rm uv.lock && uv lock --upgrade` reproduces byte identical output.
- The drift gate demonstrably fails on a modified `pyproject.toml`.
- `just typing`, `just lint`, and `just lint-manual` pass.
- The zizmor workflow passes on the new workflow.

**Post-merge**, which cannot be done earlier because scheduled and dispatched
workflows only run from the default branch:

- Dispatch with `dry_run: true`, expecting no branch pushed and no PR opened.
- Dispatch with `dry_run: false`, expecting one labeled PR with an
  `## Updated packages` body and CI running on it. CI running is the specific
  thing to confirm, since it is why the design uses an App token.
- Dispatch a third time, expecting the same PR updated rather than a second one.
- Confirm Dependabot still opens uv security PRs.

## Risks

| Risk | Mitigation |
| --- | --- |
| `automation` environment missing at merge time | Blocking prerequisite; first run is manual so the failure is immediate and visible |
| Dependabot `open-pull-requests-limit` semantics under `applies-to` | Confirm behavior after merge; adjust if security PRs are suppressed |
| Weekly PR fails CI and blocks the next week's update | Force push updates the same PR in place, so the newest lock is always what is under review |
| Lock churn from `exclude-newer` relative span | Span is recorded in the lock, so `uv lock --check` does not drift with the date; verified locally |
| PyMongo blocked on drivers-github-tools review | Part B is independently useful; only the workflow file depends on Part A |
