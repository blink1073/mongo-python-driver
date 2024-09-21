#!/bin/bash
set -eu

source .evergreens/scripts/env.sh
hatch run "$@"
