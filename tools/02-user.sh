#!/usr/bin/env bash
# Create the non-root box user with a zsh login shell. No sudo is installed
# and root has no login — this box cannot self-escalate. Handles the case
# where the requested GID/UID already exist in the base image.
#
# Uses tool_prepare(), NOT tool_install(): the box user has to exist before
# tool_apt_repo() runs `gpg` with $HOME pointing at its home directory.
TOOL_NAME=user

tool_prepare() {
    : "${USERNAME_ARG:?}" "${UID_ARG:?}" "${GID_ARG:?}"

    if ! getent group "${GID_ARG}" >/dev/null; then
        groupadd -g "${GID_ARG}" "${USERNAME_ARG}"
    fi
    existing_group="$(getent group "${GID_ARG}" | cut -d: -f1)"
    if ! getent passwd "${UID_ARG}" >/dev/null; then
        useradd -m -u "${UID_ARG}" -g "${GID_ARG}" -s /usr/bin/zsh "${USERNAME_ARG}"
    fi
    box_user="$(getent passwd "${UID_ARG}" | cut -d: -f1)"
    usermod -d "/home/${USERNAME_ARG}" -s /usr/bin/zsh "${box_user}"
    groupmod -n "${USERNAME_ARG}" "${existing_group}" 2>/dev/null || true
    if [ "${box_user}" != "${USERNAME_ARG}" ]; then
        if getent passwd "${USERNAME_ARG}" >/dev/null; then
            echo "tools/02-user.sh: cannot rename uid ${UID_ARG} to '${USERNAME_ARG}':" \
                 "that name is already taken by a different base-image account" \
                 "(uid $(getent passwd "${USERNAME_ARG}" | cut -d: -f3)). Pick a" \
                 "different host username or run with a non-colliding -u override." >&2
            exit 1
        fi
        usermod -l "${USERNAME_ARG}" "${box_user}"
    fi

    # That empty, correctly-owned dir is what seeds ownership into a fresh
    # /home named volume — removing it would leave the box user with an
    # unwritable $HOME.
    mkdir -p "/home/${USERNAME_ARG}"
    chown "${UID_ARG}:${GID_ARG}" "/home/${USERNAME_ARG}"
}
