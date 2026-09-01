#!/usr/bin/env bash
# Clean sanitizer rebuild of the C extensions, run the fixed BSON/client
# test files under the matching sanitizer runtime, and fail on any
# sanitizer diagnostic even if pytest itself exits 0.
set -eu

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

SANITIZER=${SANITIZER:?"SANITIZER must be set to 'asan' or 'tsan'"}
PYTHON_BIN=${PYTHON_BIN:-python3}
TEST_FILES=(test/test_bson.py test/test_raw_bson.py test/test_raw_bson_shared.py test/test_client.py)

# A stale build/ can leave a .so linked against the wrong sanitizer's
# runtime without a build error, so always start clean.
rm -rf build
rm -f bson/*.so pymongo/*.so

export CC=${CC:-clang}
export CXX=${CXX:-clang++}
export PYMONGO_C_EXT_MUST_BUILD=1
# Route CPython's own allocations through the system allocator so ASan's
# redzones can see them; pymalloc otherwise hides real bugs and adds noise.
export PYTHONMALLOC=malloc

case "$SANITIZER" in
  asan)
    export CFLAGS="-fsanitize=address,undefined -fno-omit-frame-pointer -g -O0"
    export LDFLAGS="-fsanitize=address,undefined"
    RUNTIME_LIB=$("$CC" -print-file-name=libasan.so)
    export ASAN_OPTIONS="detect_leaks=0"
    export UBSAN_OPTIONS="print_stacktrace=1:halt_on_error=1"
    ;;
  tsan)
    export CFLAGS="-fsanitize=thread -fno-omit-frame-pointer -g -O0"
    export LDFLAGS="-fsanitize=thread"
    RUNTIME_LIB=$("$CC" -print-file-name=libtsan.so)
    export TSAN_OPTIONS="halt_on_error=1"
    ;;
  *)
    echo "Unknown SANITIZER: $SANITIZER (expected 'asan' or 'tsan')" >&2
    exit 1
    ;;
esac

if [ ! -f "$RUNTIME_LIB" ]; then
  echo "Could not locate the $SANITIZER runtime library (got: $RUNTIME_LIB). Is the matching sanitizer runtime package installed?" >&2
  exit 1
fi

"$PYTHON_BIN" -m pip install -e . --force-reinstall --no-deps
"$PYTHON_BIN" -m pip install pytest

LOG_FILE=$(mktemp)
set +e
LD_PRELOAD="$RUNTIME_LIB" "$PYTHON_BIN" -m pytest -v "${TEST_FILES[@]}" 2>&1 | tee "$LOG_FILE"
PYTEST_STATUS=${PIPESTATUS[0]}
set -e

if grep -qE "ERROR: (AddressSanitizer|LeakSanitizer)|runtime error:|WARNING: ThreadSanitizer|SUMMARY: (Address|Undefined|ThreadSanitizer)" "$LOG_FILE"; then
  echo "Sanitizer diagnostic detected in test output, failing task" >&2
  exit 1
fi

exit "$PYTEST_STATUS"
