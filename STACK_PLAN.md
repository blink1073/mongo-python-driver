# PYTHON-5947: plan for splitting PR #2964 into a stack of four

Scratch branch. Not part of any PR; delete once the stack is built.

The full change is `PYTHON-5947-otel-impl` (PR #2964, base `otel`). Build the
stack from it, then `gh stack init` + `gh stack submit`.

## The four slices, bottom to top

1. **Operations without getMore.** `_otel.py` span primitives and naming policy,
   `_telemetry.py`, `_retry_internal` wiring in `mongo_client.py`,
   killCursors/endSessions, unacknowledged client bulk write, the collection and
   database call sites, `client_options.py`, `helpers_shared.py`,
   `periodic_executor.py`, `bson/json_util.py`, `test_otel.py` (49 tests).
   Cursor-creating operations (`find`, `aggregate`) are included: their span
   covers the creating command only.
2. **Transactions.** `client_session.py`, plus `start_transaction_span` /
   `end_transaction_span` in `_otel.py` and the
   `parent_span = session._transaction.span` lookup in `_telemetry.py`.
   `test_otel_transactions.py` (10 tests).
3. **Unified test runner and vendored tests.** `unified_format.py`
   (`observeTracingMessages` / `expectTracingMessages` and
   `MatchEvaluatorUtil.match_span_attributes`), `test_open_telemetry_unified.py`,
   and 22 of the 23 fixtures in `test/open_telemetry/`.
4. **Operations with getMore.** `cursor_shared.py` getMore parts, `cursor.py`,
   `command_cursor.py`, `change_stream.py`, `aggregation.py`, `encryption.py`,
   `cursor_base.py`, and `test_otel_getmore.py` (14 tests), plus the pieces
   listed under "what belongs to getMore" below.

## Findings that shaped the ordering

- **`get_more.json` goes in slice 4, not 3.** It is the one fixture slice 3
  cannot pass on its own. The other 22 do not exercise getMore.
- **The transaction fixtures need slice 1, not slice 4.** All three run a `find`
  whose operation span comes from the cursor-*creating* path, and they set
  `ignoreExtraSpans: false`. Slice 1 provides that, so slice 2 is safe above it.
- **Slices 1 and 2 carry only hand-written tests**, which is what removes the
  ordering problem: neither has to satisfy a vendored fixture.
- Slice 3 is ~4,400 lines but only ~190 need review; the rest is
  `resync-specs.sh` output.

## What belongs to getMore (slice 4)

- `_otel.py`: `internal_cursor_iteration`, `is_internal_cursor_iteration`,
  `_INTERNAL_CURSOR_ITERATION`, `_set_operation_cursor_id` and its call from
  `end_command_span_success`, and the `cursor_id` parameter of
  `start_operation_span`.
- `_telemetry.py`: the `cursor_id` parameter on `_OperationTelemetry.__init__`
  and `_operation_telemetry_or_none`.
- `cursor_shared.py`: `_start_getmore_operation_telemetry`,
  `_attach_operation_telemetry`, `_reuse_current_span_for_getmore`. Keep
  `_operation_telemetry` and `_end_operation_telemetry` in slice 1: the
  cursor-creating span needs them.
- `cursor.py`: the getMore branch's span call, and
  `own_span = not is_internal_cursor_iteration()` (always True in slice 1).
- `mongo_client.py`: `is_internal_cursor_iteration()` and the
  `_attach_operation_telemetry` call in `_retryable_read_cursor`.
- `collection.py`, `database.py`, `encryption.py`: the
  `with internal_cursor_iteration():` wrappers.

`is_command_namespace` stays in slice 1: `_extract_collection_name` uses it.

## Build order

Build slice 1 first by reducing a copy of the full change, then add one concern
back per branch. **Slice 4's tree must end up identical to
`PYTHON-5947-otel-impl`** (`git diff --stat <b4> PYTHON-5947-otel-impl` empty),
which proves the decomposition neither lost nor duplicated anything.

Wholesale removals for slice 1 (the rest is hand-editing the files above):

```
git checkout upstream/otel -- pymongo/asynchronous/client_session.py \
    pymongo/synchronous/client_session.py \
    test/asynchronous/unified_format.py test/unified_format.py \
    test/unified_format_shared.py
git rm -rf test/open_telemetry test/asynchronous/test_open_telemetry_unified.py \
    test/test_open_telemetry_unified.py
git rm -f test/asynchronous/test_otel_transactions.py test/test_otel_transactions.py \
    test/asynchronous/test_otel_getmore.py test/test_otel_getmore.py
```

## Environment notes

- `just synchro` only regenerates mirrors for git-*tracked* changes, so
  `git add` new test modules before running it.
- Do not factor shared test scaffolding into a helper module under
  `test/asynchronous/`: synchro prefixes `Sync` onto symbols imported from
  there, so the generated import will not match the generated definition. Each
  split test module inlines its own exporter scaffolding for that reason.
- `just typing` re-resolves `.venv` and drops the test extras; re-run
  `uv sync --extra opentelemetry --extra test` and `uv pip install
  opentelemetry-sdk` before pytest.
