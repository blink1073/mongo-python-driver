#!/usr/bin/env bash
# Bootstrap the system environment.
set -eu

# Set up default environment variables.
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

PROJECT_DIRECTORY=$(dirname $SCRIPT_DIR)
ROOT_DIR=$(dirname $PROJECT_DIRECTORY)
DRIVERS_TOOLS=${DRIVERS_TOOLS:-${ROOT_DIR}/drivers-evergreen-tools}
MONGO_ORCHESTRATION_HOME="${DRIVERS_TOOLS}/.evergreen/orchestration"
MONGODB_BINARIES="${DRIVERS_TOOLS}/mongodb/bin"
CARGO_HOME=${CARGO_HOME:-${DRIVERS_TOOLS}/.cargo}
PYTHON_BIN="${PROJECT_DIRECTORY}/.venv/bin"
# shellcheck disable=SC2154
PROJECT="${project:-mongo-python-driver}"

# Handle paths on Windows.
if [ "Windows_NT" = "${OS:-}" ]; then # Magic variable in cygwin
  DRIVERS_TOOLS=$(cygpath -m $DRIVERS_TOOLS)
  PROJECT_DIRECTORY=$(cygpath -m $PROJECT_DIRECTORY)
  MONGO_ORCHESTRATION_HOME=$(cygpath -m $MONGO_ORCHESTRATION_HOME)
  MONGODB_BINARIES=$(cygpath -m $MONGODB_BINARIES)
  CARGO_HOME=$(cygpath -m $CARGO_HOME)
  PYTHON_BIN="${PROJECT_DIRECTORY}/.venv/Scripts"
fi

# Add binaries to the path.
PATH="${PYTHON_BIN}:${CARGO_HOME}/bin:${MONGODB_BINARIES}:${PATH}"

# Ensure a checkout of drivers-tools.
if [ ! -d "$DRIVERS_TOOLS" ]; then
  git clone --branch rust-robust https://github.com/blink1073/drivers-evergreen-tools $DRIVERS_TOOLS
fi

# Get the current unique version of this checkout
# shellcheck disable=SC2154
if [ "${is_patch:-}" = "true" ]; then
    # shellcheck disable=SC2154
    CURRENT_VERSION="$(git describe)-patch-${version_id:-}"
else
    CURRENT_VERSION=latest
fi

# Write our own environment file.
cat <<EOT > ${SCRIPT_DIR}/scripts/env.sh
export PROJECT_DIRECTORY="$PROJECT_DIRECTORY"
export CURRENT_VERSION="$CURRENT_VERSION"
export DRIVERS_TOOLS="$DRIVERS_TOOLS"
export CARGO_HOME="${CARGO_HOME}"
export PYTHON_BIN="${PYTHON_BIN}"
export PATH="$PATH"
export PROJECT="{$PROJECT}"
export PIP_QUIET=1
EOT

# Ensure hatch uses the desired python binary.
if [ -n "${PYTHON_BINARY:-}" ]; then
  echo "HATCH_PYTHON=${PYTHON_BINARY}" >> ${SCRIPT_DIR}/scripts/env.sh
fi

# Write the .env file for drivers-tools.
cat <<EOT > ${DRIVERS_TOOLS}/.env
PROJECT_DIRECTORY="$PROJECT_DIRECTORY"
SKIP_LEGACY_SHELL=1
DRIVERS_TOOLS="$DRIVERS_TOOLS"
MONGO_ORCHESTRATION_HOME="$MONGO_ORCHESTRATION_HOME"
MONGODB_BINARIES="$MONGODB_BINARIES"
TMPDIR="$MONGO_ORCHESTRATION_HOME/db"
EOT

# Use our own certificates for csfle.
cat <<EOT > ${DRIVERS_TOOLS}/.evergreen/csfle/.env
CSFLE_TLS_CA_FILE="${PROJECT_DIRECTORY}/test/certificates/ca-ec.pem"
CSFLE_TLS_CERT_FILE="${PROJECT_DIRECTORY}/test/certificates/server-ec.pem"
CSFLE_TLS_CLIENT_CERT_FILE="${PROJECT_DIRECTORY}/test/certificates/client.pem"
EOT

# Set up drivers-tools.  This will call install-dependencies.sh.
bash ${DRIVERS_TOOLS}/.evergreen/setup.sh

# Add 'server' and 'hostname_not_in_cert' as a hostnames.
[ -w /etc/hosts ] && SUDO="" || SUDO="sudo"
cat /etc/hosts | grep hostname_not_in_cert > /dev/null || {
  echo "Adding localhost aliases..."
  echo "127.0.0.1 server" | $SUDO tee -a /etc/hosts
  echo "127.0.0.1 hostname_not_in_cert" | $SUDO tee -a /etc/hosts
  echo "Adding localhost aliases... done."
}

# Add these expansions to make it easier to call out tests scripts from the EVG yaml.
cat <<EOT > expansion.yml
DRIVERS_TOOLS: "$DRIVERS_TOOLS"
PROJECT_DIRECTORY: "$PROJECT_DIRECTORY"
TASK_BIN: "$(which just)"
EOT
