#!/bin/sh
# Stand-in for the official image's entrypoint, at the path the OpenBao Helm
# chart calls. The official script needs dumb-init and bash syntax; everything
# else it does (chown, setcap) the chart already switches off.
set -e

# Never write core dumps: they could hold key material.
# shellcheck disable=SC3045  # dash (Debian /bin/sh) supports ulimit -c
ulimit -c 0

# A bare flag means "arguments for bao server", as in the official image.
if [ "${1#-}" != "$1" ]; then
    set -- bao server "$@"
fi

exec "$@"
