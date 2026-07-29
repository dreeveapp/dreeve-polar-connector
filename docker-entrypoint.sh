#!/bin/sh
set -e

# Timezone first, so every log line and every comparison agrees with the host.
if [ -n "$TZ" ] && [ -f "/usr/share/zoneinfo/$TZ" ]; then
    ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime
    echo "$TZ" > /etc/timezone
fi

if [ -n "$UMASK" ]; then
    umask "$UMASK"
fi

# Only the state and the token store are ours to own. The watch folder belongs to Dreeve, and taking
# it over would be a good way to break the thing we are feeding.
if [ -n "$PUID" ] && [ -n "$PGID" ] && [ "$(id -u)" = "0" ]; then
    echo "Setting permissions for PUID=$PUID PGID=$PGID..."

    mkdir -p "${STATE_DIR:-/state}" "${POLAR_TOKENS:-/tokens}"
    chown -R "$PUID:$PGID" \
        "${STATE_DIR:-/state}" \
        "${POLAR_TOKENS:-/tokens}" || true

    echo "Permissions have been set, dropping to $PUID:$PGID"

    # Numeric ids straight to gosu: no user has to exist in /etc/passwd for this to work, and files
    # delivered into the watch folder end up owned by whatever uid Dreeve runs as.
    exec gosu "$PUID:$PGID" dreeve-polar-connector "$@"
fi

exec dreeve-polar-connector "$@"
