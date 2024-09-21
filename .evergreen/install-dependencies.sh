#!/bin/bash
set -eu

source .evergreen/scripts/env.sh
# Copy PyMongo's test certificates over driver-evergreen-tools'
# cp ${PROJECT_DIRECTORY}/test/certificates/* ${DRIVERS_TOOLS}/.evergreen/x509gen/

# # Replace MongoOrchestration's client certificate.
# cp ${PROJECT_DIRECTORY}/test/certificates/client.pem ${MONGO_ORCHESTRATION_HOME}/lib/client.pem

if [ -w /etc/hosts ]; then
  SUDO=""
else
  SUDO="sudo"
fi

# Add 'server' and 'hostname_not_in_cert' as a hostnames
echo "127.0.0.1 server" | $SUDO tee -a /etc/hosts
echo "127.0.0.1 hostname_not_in_cert" | $SUDO tee -a /etc/hosts

# Install binaries.
mkdir -p ${BIN_DIR}

# Install rust if need be.
# shellcheck disable=SC2154
if [ -n "${USE_RUST}" ]; then
  export RUSTUP_HOME="${RUSTUP_HOME:-"${DRIVERS_TOOLS}/.rustup"}"
  export CARGO_HOME="${CARGO_HOME:-"${DRIVERS_TOOLS}/.cargo"}"
  export PATH="${RUSTUP_HOME}/bin:${CARGO_HOME}/bin:$PATH"
  [ ! -d ${CARGO_HOME} ] && ${DRIVERS_TOOLS}/.evergreen/install-rust.sh
  rustup default stable
fi

# Install "just" using the installer, falling back to using cargo.
ARGS="--to ${BIN_DIR}"
if [ "${OS:-}" == "Windows_NT" ]; then
  ARGS="$ARGS --target x86_64-pc-windows-msvc"
fi
curl --proto '=https' --tlsv1.2 -sSf https://just.systems/install.sh | bash -s -- $ARGS || {
  # Install "just" using cargo
  echo "Installing just..."
  cargo install -q just
  echo "Installing just... done."
  ln -s "${CARGO_HOME}/bin/just" just
}

# Make "just" executable.
if [ "${OS:-}" == "Windows_NT" ]; then
  chmod+x just.exe
  mv just.exe ${BIN_DIR}
  ln -s "${BIN_DIR}/just.exe" "${BIN_DIR}/just"
else
  mv just ${BIN_DIR}
fi

# Check just.
just --version

# Install virtualenv and add hatch
. .evergreen/utils.sh

if [ -z "${PYTHON_BINARY:-}" ]; then
    PYTHON_BINARY=$(find_python3)
fi
createvirtualenv "$PYTHON_BINARY" .venv
trap "deactivate; rm -rf .venv" EXIT HUP
python -m pip install -q hatch
hatch --version

exit 1
