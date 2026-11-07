"""Constants shared across modules, and the version and host OS of this run."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from .ui import YoError


# Rewritten at build time: nix substituteInPlace, Homebrew inreplace.
YO_VERSION = "dev"

DEFAULT_INSTANCE = "yolobox"
# One DNS label (RFC 1123, lowercase). flake.nix matches the same pattern against
# YOLOBOX_HOSTNAME; tests/test_consistency.py keeps the two equal. fullmatch only.
HOSTNAME_LABEL_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")

# The operator's uid inside the guest on every host: lima-init's cidata
# account, declared in nix/base.nix and checked by yolobox-guest rebuild.
OPERATOR_GUEST_UID = 501
# The shape of the operator's account name, which reaches sshd config, tmpfiles
# rules and shell scripts in the box. nix/base.nix asserts the same pattern at
# eval time (tests/test_consistency.py keeps the two equal); operator_account()
# refuses the name on the host first, in cli.main(), for both drivers.
OPERATOR_ACCOUNT_RE = re.compile(r"[a-z_][a-z0-9_-]*")
# The word the box's stage-2 sshd appends to its SSH banner (VersionAddendum in
# nix/base.nix). The initrd's sshd has none, so yo tells the two apart without
# logging in, and never offers stage 2 a user it rejects (nix/base.md).
STAGE2_SSH_BANNER_MARK = "yolobox-stage2"
# The one ~/.ssh/config line that reaches every machine on both hosts: libvirt's
# instance files and Lima's. An Include applies only to the Host or Match block
# it follows, so it belongs above the first one (ssh_config_reaches_box()).
SSH_CONFIG_INCLUDE_LINE = (
    "Include ~/.local/state/yolobox/instances/*/ssh.config ~/.lima/*/ssh.config"
)
AGENT_USER = "agent"


def operator_account(username: str) -> str:
    """The guest account the box creates for the operator, checked."""
    if not OPERATOR_ACCOUNT_RE.fullmatch(username):
        raise YoError(
            "the host account name %r cannot name the operator's account in the "
            "box: it must match %s" % (username, OPERATOR_ACCOUNT_RE.pattern)
        )
    if username == AGENT_USER:
        raise YoError(
            "the host account is named %r, which is the box's agent account; "
            "the operator needs a different name" % AGENT_USER
        )
    return username


AGENT_HOME = "/home/agent"
HELPER = "yolobox-guest"
HELPER_NO_SUBCOMMAND = 64
# EX_USAGE (sysexits.h): the command cannot do what was asked on this host.
USAGE_EXIT = 64
SSH_UNREACHABLE = 255

OP_GUEST_SOCK = "/run/yolobox-op/agent.sock"
AWS_BROKER_GUEST_SOCK = "/run/yolobox-op/aws-broker.sock"
AGENT_AWS_BROKER_SOCK = "/run/yolobox/aws-broker.sock"

# The encrypted root. The passphrase is derived from a 1Password-held key (see
# luks_passphrase) and never stored.
LUKS_DEVICE = "/dev/vda2"

# Title of the Ed25519 "SSH Key" item in 1Password that unlocks the disk.
LUKS_KEY_ITEM = "yolobox-disk"

PROXY_SOCKET_UNITS = ("yolobox-op-agent.socket", "yolobox-aws-broker.socket")


# The host OS as sys.platform spells it: the one place yo asks. main() turns it
# into a driver with driver_for(), and nothing else looks at the host OS.
HOST_PLATFORM = sys.platform

SCRIPT_DIR = Path(__file__).resolve().parent.parent

LOOPBACK = "127.0.0.1"

MAX_PORT = 65535

KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB

# The smallest box `yo up --memory/--disk` creates, and why
# (yo.md#several-machines). The reasons are printed with a refused flag.
MIN_MEMORY_GIB = 8
MIN_MEMORY_WHY = (
    "the box builds its own system, and with less the nix evaluator is OOM-killed"
)
MIN_DISK_GIB = 40
MIN_DISK_WHY = (
    "the box holds a 16 GiB swapfile plus the nix store of its first build, and "
    "a smaller disk fills up"
)

# Modes of the files yo writes: the owner alone, or world-readable (no secrets).
FILE_MODE_PRIVATE = 0o600
FILE_MODE_READABLE = 0o644
