#!/bin/bash
set -eu

# Copy PyMongo's test certificates over driver-evergreen-tools'
cp ${PROJECT_DIRECTORY}/test/certificates/* ${DRIVERS_TOOLS}/.evergreen/x509gen/

# Replace MongoOrchestration's client certificate.
cp ${PROJECT_DIRECTORY}/test/certificates/client.pem ${MONGO_ORCHESTRATION_HOME}/lib/client.pem

if [ -w /etc/hosts ]; then
  SUDO=""
else
  SUDO="sudo"
fi

# Add 'server' and 'hostname_not_in_cert' as a hostnames
echo "127.0.0.1 server" | $SUDO tee -a /etc/hosts
echo "127.0.0.1 hostname_not_in_cert" | $SUDO tee -a /etc/hosts

# Install just.
mkdir -p ${PROJECT_DIRECTORY}/.bin
ARGS="--to ${PROJECT_DIRECTORY}/.bin"
if [ "${OS:-}" == "Windows_NT" ]; then
  ARGS="$ARGS --target x86_64-pc-windows-msvc"
fi
curl --proto '=https' --tlsv1.2 -sSf https://just.systems/install.sh | bash -s -- $ARGS || {
    echo "'just' binary not available on this platform, installing using cargo..."
    # TODO: we should be able to just call install-rust and have it no-op, and set these values.
    export RUSTUP_HOME="${RUSTUP_HOME:-"${DRIVERS_TOOLS}/.rustup"}"
    export CARGO_HOME="${CARGO_HOME:-"${DRIVERS_TOOLS}/.cargo"}"
    export PATH="${RUSTUP_HOME}/bin:${CARGO_HOME}/bin:$PATH"
    [ ! -d ${CARGO_HOME} ] && ${DRIVERS_TOOLS}/.evergreen/install-rust.sh
    rustup default stable
    cargo install just
    mv ${CARGO_HOME}/bin/just .bin
}

if [ "${OS:-}" == "Windows_NT" ]; then
  chmod +x .bin/just.exe
  .bin/just.exe --version
else
  .bin/just --version
fi
