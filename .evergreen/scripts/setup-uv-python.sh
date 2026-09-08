#!/bin/bash
# Set up the UV_PYTHON variable and put the toolchain pythons on the path.
set -eu

HERE=$(dirname ${BASH_SOURCE:-$0})
HERE="$( cd -- "$HERE" > /dev/null 2>&1 && pwd )"

# Use min supported version by default.
_python="3.10"

# Source the env files to pick up common variables.
if [ -f $HERE/env.sh ]; then
  . $HERE/env.sh
fi

# Get variables defined in test-env.sh.
if [ -f $HERE/test-env.sh ]; then
  . $HERE/test-env.sh
fi

# Prefer system/toolchain interpreters over uv-managed downloads.  Skip on
# Windows, where the first python3 on the path is a broken Chocolatey shim that
# uv cannot inspect.
if [ "Windows_NT" != "${OS:-}" ]; then
  export UV_PYTHON_PREFERENCE=system
fi

# UV_PYTHON is always a version identifier (e.g. 3.14), never a path.  uv
# discovers the interpreter itself, so a matching toolchain (system) Python is
# put on the path instead of pointing UV_PYTHON at it.
if [ -z "${UV_PYTHON:-}" ]; then
  if [ "${REQUIRE_FIPS:-}" = "1" ]; then
    # FIPS hosts provision a specific Python; put its directory first on the
    # path and leave UV_PYTHON unset so uv resolves the interpreter from PATH.
    export PATH="/usr/bin:$PATH"
  else
    export UV_PYTHON="$_python"
  fi
fi

# Whether a toolchain Python matching UV_PYTHON was found on the host.
PYTHON_FOUND=0
# Prefer a toolchain (system) python over a uv-managed download: when a
# matching toolchain interpreter is installed, put its bin directory on the
# path and let uv pick it up.  Versions uv must install itself (pre-releases)
# are left for the install/fetch fallback in setup-dev-env.sh.
if [ -n "${UV_PYTHON:-}" ] && [[ "$UV_PYTHON" =~ ^3\.[0-9]+t?$ ]]; then
  case "$(uname -s)" in
    Darwin)
      if [[ "$UV_PYTHON" == *"t"* ]]; then
        binary_name="python3t"
        framework_dir="PythonT"
      else
        binary_name="python3"
        framework_dir="Python"
      fi
      _version="${UV_PYTHON%t}"
      _bin_dir="/Library/Frameworks/${framework_dir}.Framework/Versions/$_version/bin"
      if [ -x "$_bin_dir/$binary_name" ]; then
        export PATH="$_bin_dir:$PATH"
        PYTHON_FOUND=1
      fi
      ;;
    *)
      if [ "Windows_NT" = "${OS:-}" ]; then
        _dir=$(echo "$UV_PYTHON" | cut -d. -f1,2 | sed 's/\.//g; s/t//g')
        if [[ "$UV_PYTHON" == *"t"* ]]; then
          _exe="python${UV_PYTHON}.exe"
        else
          _exe="python.exe"
        fi
        if [ -n "${IS_WIN32:-}" ]; then
          _bin_dir="C:/python/32/Python${_dir}"
        else
          _bin_dir="C:/python/Python${_dir}"
        fi
        if [ -f "$_bin_dir/$_exe" ]; then
          export PATH="$_bin_dir:$PATH"
          PYTHON_FOUND=1
        fi
      else
        _bin_dir="/opt/python/$UV_PYTHON/bin"
        if [ -x "$_bin_dir/python3" ]; then
          export PATH="$_bin_dir:$PATH"
          PYTHON_FOUND=1
        fi
      fi
      ;;
  esac
fi
export PYTHON_FOUND
