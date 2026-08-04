#!/usr/bin/env bash
# Empty the box user's home. Several build steps (installers, smoke checks) write
# into $HOME while building the image, and $HOME is later mounted from an empty
# named volume that is seeded from the image exactly once. Anything left here at
# build time would be frozen into every future volume forever, so wipe it clean
# and re-chown it to the box user before the image is finalized.
set -uo pipefail
: "${USERNAME_ARG:?}" "${UID_ARG:?}" "${GID_ARG:?}"
h="/home/${USERNAME_ARG}"
rm -rf "${h:?}"/* "${h}"/.[!.]* "${h}"/..?* 2>/dev/null || true
chown "${UID_ARG}:${GID_ARG}" "${h}"
