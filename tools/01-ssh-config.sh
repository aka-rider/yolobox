#!/usr/bin/env bash
# System-wide SSH client default. Under --ssh (1Password agent forwarding) the
# very first `git@github.com` connection from a fresh box would otherwise hang
# on an interactive host-key prompt: $HOME is a freshly-seeded volume with no
# ~/.ssh/known_hosts yet. Baking accept-new into /etc/ssh/ssh_config.d (system
# config, not per-user) fixes that first-connect hang for every user, before
# any known_hosts file exists.
TOOL_NAME=ssh-config

tool_install() {
    install -d -m 0755 /etc/ssh/ssh_config.d
    printf 'Host *\n    StrictHostKeyChecking accept-new\n' \
        > /etc/ssh/ssh_config.d/10-yolobox.conf
}
