#!/bin/bash
# Set up development environment.
set -eu

HERE=$(dirname ${BASH_SOURCE:-$0})
HERE="$( cd -- "$HERE" > /dev/null 2>&1 && pwd )"
ROOT=$(dirname "$(dirname $HERE)")

# Source the env files to pick up common variables.
if [ -f $HERE/env.sh ]; then
  . $HERE/env.sh
fi

# Get variables defined in test-env.sh.
if [ -f $HERE/test-env.sh ]; then
  . $HERE/test-env.sh
fi

# Handle the value for UV_PYTHON.
. $HERE/setup-uv-python.sh

# Ensure dependencies are installed.
bash $HERE/install-dependencies.sh

# Re-source env.sh: install-dependencies.sh may have appended to it, e.g. when it
# had to install Python on an image that lacks a toolchain.
if [ -f $HERE/env.sh ]; then
  . $HERE/env.sh
fi

# UV_PYTHON is a bare version identifier (e.g. 3.14), never a path.  A download
# only happens when no toolchain Python matches it (PYTHON_FOUND unset): try
# uv's managed build first, and when uv has no build (e.g. a pre-release), fall
# back to python-build-standalone's latest release, putting the interpreter's
# bin directory on the path so uv resolves UV_PYTHON.
if [ -n "${UV_PYTHON:-}" ] && [ "${PYTHON_FOUND:-}" != "1" ]; then
  if ! uv python install "$UV_PYTHON" >/dev/null 2>&1; then
    _prefix="$(bash "$HERE/fetch-python.sh")" || {
      echo "Failed to obtain a Python $UV_PYTHON interpreter" >&2
      exit 1
    }

    if [ -f "$_prefix/python.exe" ]; then
      _path_dir="$_prefix"
    else
      _path_dir="$_prefix/bin"
    fi
    export PATH="$_path_dir:$PATH"
  fi
fi

# Add the default install path to the path if needed.
if [ -z "${PYMONGO_BIN_DIR:-}" ]; then
  export PATH="$PATH:$HOME/.local/bin"
fi

# Only run the next part if not running on CI.
if [ -z "${CI:-}" ]; then
  # Set up venv, making sure c extensions build unless disabled.
  if [ -z "${NO_EXT:-}" ]; then
    export PYMONGO_C_EXT_MUST_BUILD=1
  fi

  (
    cd $ROOT && uv sync
  )

  # Set up build utilities on Windows spawn hosts.
  if [ -f $HOME/.visualStudioEnv.sh ]; then
    set +u
    SSH_TTY=1 source $HOME/.visualStudioEnv.sh
    set -u
  fi

  # Only set up pre-commit if we are in a git checkout.
  if [ -f $HERE/.git ]; then
    if ! command -v pre-commit &>/dev/null; then
      uv tool install pre-commit
    fi

    if [ ! -f .git/hooks/pre-commit ]; then
      uvx pre-commit install
    fi
  fi
fi
