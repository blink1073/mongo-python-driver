#!/bin/bash
# Smoke test the mod_wsgi tests. On Linux the test runs against the host's
# Apache (apache2 on Debian/Ubuntu, httpd on Fedora/RHEL). On other hosts it
# runs in an ubuntu:24.04 container so the test is still available on macOS.
set -eu

SCRIPT_DIR=$(dirname ${BASH_SOURCE:-$0})
ROOT=$(dirname "$(dirname "$SCRIPT_DIR")")

# Only use a container when the host cannot run Apache and mongod natively.
if [ "$(uname -s)" != "Linux" ]; then
  if ! command -v docker >/dev/null; then
    echo "docker is required to run the mod_wsgi smoke test on non-Linux hosts"
    exit 1
  fi
  exec docker run --rm -v "$ROOT":/src:ro ubuntu:24.04 bash /src/.evergreen/scripts/mod_wsgi_smoke_test.sh
fi

install_apache() {
  if command -v apt-get >/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq apache2 apache2-dev build-essential curl jq git tar gzip >/dev/null
  elif command -v dnf >/dev/null; then
    dnf install -y gcc gcc-c++ make httpd httpd-devel curl jq git tar gzip which >/dev/null
  elif command -v yum >/dev/null; then
    yum install -y gcc gcc-c++ make httpd httpd-devel curl jq git tar gzip which >/dev/null
  else
    echo "Unsupported package manager; install Apache and build tools manually"
    exit 1
  fi
}

if [ "$(id -u)" = "0" ]; then
  # Apache and mongod must not run as root. Install system packages, copy the
  # checkout into a home directory, and re-run this script unprivileged.
  install_apache
  useradd -m smoke 2>/dev/null || true
  mkdir -p /home/smoke/src
  # Leave out the generated env files so they cannot override the versions
  # set below.
  tar -C "$ROOT" -cf - --exclude=.git --exclude=.venv \
    --exclude=.evergreen/scripts/env.sh --exclude=.evergreen/scripts/test-env.sh . \
    | tar -C /home/smoke/src -xf -
  chown -R smoke:smoke /home/smoke/src
  # The Apache user must be able to traverse the home directory to read the
  # Python installation and virtualenv.
  chmod 755 /home/smoke
  exec su - smoke -c "cd /home/smoke/src && bash /home/smoke/src/.evergreen/scripts/mod_wsgi_smoke_test.sh"
fi

# apache2/httpd live in sbin, which is not on PATH for non-root users.
export PATH="/usr/sbin:/sbin:$PATH"

if ! command -v apache2 >/dev/null && ! command -v httpd >/dev/null; then
  echo "Apache is required: install apache2/apache2-dev (Debian) or httpd/httpd-devel (Fedora/RHEL)"
  exit 1
fi

# Install the Python interpreter in a world-readable directory so the Apache
# user can read it regardless of the home directory permissions.
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-/tmp/mod-wsgi-uv-python}"

if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
fi
export PATH="$HOME/.local/bin:$PATH"
if ! command -v just >/dev/null; then
  uv tool install rust-just >/dev/null
fi

cd "$ROOT"

# mongod inherits the soft nofile limit; the 1024 default is exhausted by the
# connection storm the parallel test generates.
ulimit -n 65536

# Mirror the GHA job and test the newest supported CPython.
LATEST_PYTHON=$(uv run --no-project --with 'shrub.py>=3.10.0' python .evergreen/scripts/mod_wsgi_matrix.py | jq -r '.[-1]."python-version"')
echo "Testing with CPython $LATEST_PYTHON"
uv python install "$LATEST_PYTHON" >/dev/null

export UV_PYTHON=$LATEST_PYTHON
export PYMONGO_C_EXT_MUST_BUILD=1
just install
uv sync --group mod_wsgi

# Start a single-node replica set.
case "$(uname -m)" in
  aarch64|arm64) MARCH=aarch64 ;;
  x86_64|amd64) MARCH=x86_64 ;;
  *)
    echo "Unsupported architecture: $(uname -m)"
    exit 1
    ;;
esac

# Pick a MongoDB build that matches the host distribution. Fedora and RHEL
# share the rhel targets.
if [ -z "${MONGODB_TARGET:-}" ]; then
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
  fi
  case "${ID:-}${ID_LIKE:-}:${VERSION_ID:-}" in
    *ubuntu*24.04*) MONGODB_TARGET=ubuntu2404 ;;
    *ubuntu*22.04*) MONGODB_TARGET=ubuntu2204 ;;
    *ubuntu*20.04*) MONGODB_TARGET=ubuntu2004 ;;
    *debian*12*) MONGODB_TARGET=debian12 ;;
    *fedora*|*rhel*) MONGODB_TARGET=rhel93 ;;
    *) MONGODB_TARGET=ubuntu2404 ;;
  esac
fi

MONGODB_URL=$(curl -fsSL https://downloads.mongodb.org/current.json | jq -r "
  .versions[] | select(.current and .production_release) | .downloads[] |
  select(.target==\"$MONGODB_TARGET\" and .arch==\"$MARCH\") | .archive.url" | grep -v enterprise | head -1)
WORK_DIR=$(mktemp -d)
curl -fsSL "$MONGODB_URL" -o "$WORK_DIR/mongo.tgz"
MEMBER=$(tar -tzf "$WORK_DIR/mongo.tgz" | grep "bin/mongod$")
tar -xz -C "$WORK_DIR" --strip-components=2 -f "$WORK_DIR/mongo.tgz" "$MEMBER"
mkdir -p "$WORK_DIR/db"

# Stop Apache/httpd directly, without relying on the test harness. The direct
# stop only needs the Apache binary, so it also works when teardown-tests.sh
# cannot run (for example if uv or the generated test-env.sh is unavailable).
# Idempotent: safe to call when Apache is not running.
stop_apache() {
  local apache="${APACHE_BINARY:-}" config="${APACHE_CONFIG:-}"
  if [ -z "$apache" ]; then
    if command -v apache2 >/dev/null; then
      apache=apache2
    elif [ -x /usr/lib/apache2/mpm-prefork/apache2 ]; then
      apache=/usr/lib/apache2/mpm-prefork/apache2
    elif command -v httpd >/dev/null; then
      apache=httpd
    elif [ -x /usr/sbin/httpd ]; then
      apache=/usr/sbin/httpd
    else
      return 0
    fi
  fi
  if [ -z "$config" ]; then
    case "$apache" in
      *httpd*) config=httpd24fedora.conf ;;
      *) config=apache24ubuntu.conf ;;
    esac
  fi
  "$apache" -k stop -f "$ROOT/test/mod_wsgi_test/$config" >/dev/null 2>&1 || true
}

# Always stop Apache and mongod, even if setup or the tests fail or the script
# is interrupted, so repeated runs are idempotent and don't leave servers
# behind. teardown-tests.sh is a no-op if Apache was never started, so it is
# run for the harness bookkeeping and Apache is stopped directly as well in
# case that teardown fails.
cleanup() {
  local status=$?
  # Pick up APACHE_BINARY/APACHE_CONFIG written by setup-tests.sh so the direct
  # stop uses the same binary and config as the running server.
  if [ -f "$ROOT/.evergreen/scripts/test-env.sh" ]; then
    # shellcheck disable=SC1090,SC1091
    . "$ROOT/.evergreen/scripts/test-env.sh"
  fi
  stop_apache
  bash .evergreen/scripts/teardown-tests.sh || true
  "$WORK_DIR/mongod" --dbpath "$WORK_DIR/db" --shutdown >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

"$WORK_DIR/mongod" --replSet rs0 --bind_ip 127.0.0.1 --port 27017 --dbpath "$WORK_DIR/db" --fork --logpath "$WORK_DIR/mongod.log"
uv run python -c "
from pymongo import MongoClient
client = MongoClient('127.0.0.1:27017', directConnection=True)
client.admin.command('replSetInitiate', {'_id': 'rs0', 'members': [{'_id': 0, 'host': '127.0.0.1:27017'}]})
MongoClient().admin.command('hello')
"

# The mod_wsgi group is part of the synced environment, so both modes can run
# without reinstalling.
for MODE in standalone embedded; do
  bash .evergreen/scripts/setup-tests.sh mod_wsgi $MODE
  # Stop Apache even if the tests fail so the next mode can bind port 8080.
  STATUS=0
  bash .evergreen/run-tests.sh || STATUS=$?
  bash .evergreen/scripts/teardown-tests.sh || true
  [ "$STATUS" -eq 0 ] || exit "$STATUS"
done
echo "mod_wsgi smoke test passed"
