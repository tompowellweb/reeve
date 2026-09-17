#!/bin/sh
set -eu
. /run/toolbox/session-env
cd "$TOOLBOX_DIRECTORY"
if [ -n "${SSH_ORIGINAL_COMMAND:-}" ]; then
    exec /bin/bash -c "$SSH_ORIGINAL_COMMAND"
fi
exec /bin/bash -l
