#!/usr/bin/env bash
# Clean sanitizer rebuild of the C extensions, run the fixed BSON/client
# test files under the matching sanitizer runtime, and fail on any
# sanitizer diagnostic even if pytest itself exits 0.
#
# ASan runs against a prebuilt interpreter with libasan.so LD_PRELOADed.
# TSan builds a fully instrumented free-threaded CPython from source
# instead: TSan cannot see synchronization in code compiled without
# -fsanitize=thread, so LD_PRELOADing it onto a prebuilt interpreter
# reports false races inside CPython's own free-threading internals.
set -eu

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

SANITIZER=${SANITIZER:?"SANITIZER must be set to 'asan' or 'tsan'"}
UV_PYTHON=${UV_PYTHON:-3.13}
TEST_FILES=(test/test_bson.py test/test_raw_bson.py test/test_raw_bson_shared.py test/test_client.py)

# The free-threaded CPython the TSan task builds. Kept in step with the
# "3.14t" entry in .evergreen/scripts/generate_config_utils.py's CPYTHONS.
CPYTHON_TAG=v3.14.0
CPYTHON_SRC=.tsan-cpython-src
CPYTHON_INSTALL=.tsan-cpython-install

# A stale build/ or venv can leave a .so or install linked against the wrong
# sanitizer's runtime without a build error, so always start clean.
rm -rf build
rm -f bson/*.so pymongo/*.so

export CC=${CC:-clang}
export CXX=${CXX:-clang++}
export PYMONGO_C_EXT_MUST_BUILD=1

case "$SANITIZER" in
  asan)
    rm -rf .sanitizer-venv
    export CFLAGS="-fsanitize=address,undefined -fno-omit-frame-pointer -g -O0"
    export LDFLAGS="-fsanitize=address,undefined"
    RUNTIME_LIB=$("$CC" -print-file-name=libasan.so)
    export ASAN_OPTIONS="detect_leaks=0"
    export UBSAN_OPTIONS="print_stacktrace=1:halt_on_error=1"
    # Route CPython's own allocations through the system allocator so ASan's
    # redzones can see them; pymalloc otherwise hides real bugs and adds
    # noise. The free-threaded TSan interpreter doesn't accept this value,
    # so it's scoped to ASan only.
    export PYTHONMALLOC=malloc

    if [ ! -f "$RUNTIME_LIB" ]; then
      echo "Could not locate the $SANITIZER runtime library (got: $RUNTIME_LIB). Is the matching sanitizer runtime package installed?" >&2
      exit 1
    fi

    uv venv --python "$UV_PYTHON" .sanitizer-venv
    VENV_PYTHON=.sanitizer-venv/bin/python3
    uv pip install --python "$VENV_PYTHON" -e . --reinstall
    uv pip install --python "$VENV_PYTHON" -r requirements/test.txt

    PYTEST_CMD=(env "LD_PRELOAD=$RUNTIME_LIB" "$VENV_PYTHON" -m pytest)
    ;;
  tsan)
    rm -rf "$CPYTHON_SRC" "$CPYTHON_INSTALL"

    # TSan's shadow mapping can fail to reserve its address ranges under the
    # default ASLR entropy on recent kernels, which crashes the process at
    # startup. CPython's own CI lowers the entropy the same way. Evergreen
    # hosts may not permit it, so this is best effort.
    sudo sysctl -w vm.mmap_rnd_bits=28 || true

    CPYTHON_INSTALL_ABS="$(pwd)/$CPYTHON_INSTALL"
    git clone --depth 1 --branch "$CPYTHON_TAG" https://github.com/python/cpython.git "$CPYTHON_SRC"
    # Flags mirror CPython's own TSan CI job (.github/workflows/reusable-san.yml).
    # CFLAGS/LDFLAGS are deliberately left unset here: --with-thread-sanitizer
    # and --with-pydebug already supply the right flags, and overriding them
    # would fight configure.
    #
    # Unlike CPython's CI this does not rebuild OpenSSL with TSan, which is
    # only needed to keep the ssl tests quiet. The tests below don't use ssl,
    # but pip does need it to reach PyPI, so _ssl still has to build against
    # the system OpenSSL.
    (
      cd "$CPYTHON_SRC"
      ./configure \
        --with-thread-sanitizer \
        --with-pydebug \
        --disable-gil \
        --prefix="$CPYTHON_INSTALL_ABS"
      make -j"$(nproc 2>/dev/null || echo 4)"
      make install
    )

    # Free-threaded builds install as pythonX.Yt; fall back to python3 in case
    # a future release drops the suffix.
    TSAN_PYTHON=""
    for candidate in "$CPYTHON_INSTALL"/bin/python3.*t "$CPYTHON_INSTALL"/bin/python3; do
      if [ -x "$candidate" ]; then
        TSAN_PYTHON="$candidate"
        break
      fi
    done
    if [ -z "$TSAN_PYTHON" ]; then
      echo "Could not find the interpreter built from source under $CPYTHON_INSTALL/bin:" >&2
      ls -l "$CPYTHON_INSTALL/bin" >&2 || true
      exit 1
    fi
    echo "Using TSan-instrumented interpreter: $TSAN_PYTHON"
    "$TSAN_PYTHON" -VV
    "$TSAN_PYTHON" -c 'import sysconfig, sys; sys.exit(0 if sysconfig.get_config_var("Py_GIL_DISABLED") else "interpreter is not free-threaded")'
    if ! "$TSAN_PYTHON" -c 'import ssl' >/dev/null 2>&1; then
      echo "Warning: the interpreter built from source has no working ssl module, so pip cannot reach PyPI. Install the system OpenSSL development headers on this host." >&2
    fi

    # Build PyMongo's C extensions with the same instrumentation as the
    # interpreter. These are plain shell env vars so pip's isolated build
    # subprocess inherits them; build isolation is left on so pip resolves
    # hatchling's build dependencies itself.
    export CFLAGS="-fsanitize=thread -fno-omit-frame-pointer -g -O0"
    export LDFLAGS="-fsanitize=thread"
    "$TSAN_PYTHON" -m ensurepip --upgrade
    "$TSAN_PYTHON" -m pip install -e .
    "$TSAN_PYTHON" -m pip install -r requirements/test.txt

    # Set after the build: halt_on_error=1 would abort the CPython build and
    # the installs on any diagnostic raised by those tools themselves.
    TSAN_OPTIONS="halt_on_error=1:suppressions=$(pwd)/.evergreen/tsan-suppressions.txt"
    export TSAN_OPTIONS

    # No LD_PRELOAD: both the interpreter and the extensions link the TSan
    # runtime at build time.
    PYTEST_CMD=("$TSAN_PYTHON" -m pytest)
    ;;
  *)
    echo "Unknown SANITIZER: $SANITIZER (expected 'asan' or 'tsan')" >&2
    exit 1
    ;;
esac

LOG_FILE=$(mktemp)
set +e
"${PYTEST_CMD[@]}" -v --capture=no "${TEST_FILES[@]}" 2>&1 | tee "$LOG_FILE"
PYTEST_STATUS=${PIPESTATUS[0]}
set -e

if grep -qE "ERROR: (AddressSanitizer|LeakSanitizer)|runtime error:|WARNING: ThreadSanitizer|SUMMARY: (Address|Undefined|ThreadSanitizer)" "$LOG_FILE"; then
  echo "Sanitizer diagnostic detected in test output, failing task" >&2
  exit 1
fi

exit "$PYTEST_STATUS"
