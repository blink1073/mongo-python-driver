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
  # TODO: fix this in drivers-tools:
  # Install once and export the variables.
  # Make it quieter
  export RUSTUP_HOME="${RUSTUP_HOME:-"${DRIVERS_TOOLS}/.rustup"}"
  export CARGO_HOME="${CARGO_HOME:-"${DRIVERS_TOOLS}/.cargo"}"
  export PATH="${RUSTUP_HOME}/bin:${CARGO_HOME}/bin:$PATH"
  [ ! -d ${CARGO_HOME} ] && ${DRIVERS_TOOLS}/.evergreen/install-rust.sh
  rustup default stable
fi

# Install "just" using the installer, falling back to using cargo.
# For platforms that do not have a compatible installer,
# rust needs to have been installed (Setting the USE_RUST variable).
CURL_ARGS="--retry 8 --tlsv1.2 -sSf https://just.systems/install.sh"
JUST_ARGS="--to ${BIN_DIR}"
if [ "${OS:-}" == "Windows_NT" ]; then
  JUST_ARGS="$JUST_ARGS --target x86_64-pc-windows-msvc"
fi
curl --proto '=https' $CURL_ARGS | bash -s -- ${JUST_ARGS} || {
  echo "Installing just using cargo..."
  cargo install -q just
  echo "Installing just using cargo... done."
  ln -s "${CARGO_HOME}/bin/just" ${BIN_DIR}/just
}

# Check just.
just --version

# Install virtualenv and add hatch.  For platforms that do not have a cryptography wheel,
# rust needs to have been installed (Setting the USE_RUST variable).
. .evergreen/utils.sh

if [ -z "${PYTHON_BINARY:-}" ]; then
    PYTHON_BINARY=$(find_python3)
fi
createvirtualenv "$PYTHON_BINARY" .venv
python -m pip install -q hatch

# Check python
python --version

# Check hatch.
hatch --version
