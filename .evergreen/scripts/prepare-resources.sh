#!/bin/bash

. src/.evergreen/scripts/env.sh
set -o xtrace
rm -rf $DRIVERS_TOOLS
if [ "$PROJECT" = "drivers-tools" ]; then
    # If this was a patch build, doing a fresh clone would not actually test the patch
    cp -R $PROJECT_DIRECTORY/ $DRIVERS_TOOLS
else
    git clone --branch DRIVERS-2743-2 https://github.com/blink1073/drivers-evergreen-tools.git $DRIVERS_TOOLS
fi
echo "{ \"releases\": { \"default\": \"$MONGODB_BINARIES\" }}" >$MONGO_ORCHESTRATION_HOME/orchestration.config
