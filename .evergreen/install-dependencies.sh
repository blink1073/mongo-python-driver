#!/usr/bin/env bash
set -eu

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
source $SCRIPT_DIR/scripts/env.sh

# Ensure "just" is installed.
if ! command -v just > /dev/null; then
  # Install "just" using the installer, falling back to using cargo.
  echo "Installing just..."
  CURL_ARGS="--retry 8 --tlsv1.2 -sSf https://just.systems/install.sh"
  mkdir -p ${CARGO_HOME}/bin
  JUST_ARGS="--to ${CARGO_HOME}/bin"
  if [ "${OS:-}" == "Windows_NT" ]; then
    JUST_ARGS="$JUST_ARGS --target x86_64-pc-windows-msvc"
  fi
  curl --proto '=https' $CURL_ARGS | bash -s -- ${JUST_ARGS} || {
    # Install rust and install with cargo.
    export RUSTUP_HOME="${CARGO_HOME}/.rustup"
    ${DRIVERS_TOOLS}/.evergreen/install-rust.sh
    source "${CARGO_HOME}/env"
    cargo install -q just
  }
  echo "Installing just... done."
fi
just --version

# Ensure "hatch" is installed.
if ! command -v hatch > /dev/null; then
  # Ensure there is a python venv.
  if [ ! -d ${PYTHON_BIN} ]; then
    echo "Creating virtual environment..."
    . ${DRIVERS_TOOLS}/.evergreen/find-python3.sh
    PYTHON_BINARY=$(ensure_python3)
    $PYTHON_BINARY -m venv .venv
    echo "Creating virtual environment... done."
  fi
  python --version

  echo "Installing hatch..."
  python -m pip install -U pip
  python -m pip install hatch || {
    # Install rust and try again.
    export RUSTUP_HOME="${CARGO_HOME}/.rustup"
    ${DRIVERS_TOOLS}/.evergreen/install-rust.sh
    source "${CARGO_HOME}/env"
    python -m pip install hatch
  }
  echo "Installing hatch... done."
fi
hatch --version
