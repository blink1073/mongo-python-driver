#!/bin/bash
# Clean up resources at the end of an evergreen run.
set -eu

HERE=$(dirname ${BASH_SOURCE:-$0})

# Try to source the env file.
if [ -f $HERE/env.sh ]; then
  echo "Sourcing env file"
  source $HERE/env.sh
fi

# DRIVERS_TOOLS now points inside the checkout (the drivers-evergreen-tools
# submodule); deleting it would corrupt the workdir for later tasks on the
# same host, so it is intentionally not removed here.
rm -f $HERE/../../secrets-export.sh || true
