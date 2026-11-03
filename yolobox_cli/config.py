"""Constants shared across modules, and the version and host OS of this run."""

from __future__ import annotations

import re
import sys
from pathlib import Path


# Rewritten at build time: nix substituteInPlace, Homebrew inreplace.
YO_VERSION = "dev"

DEFAULT_INSTANCE = "yolobox"
# One DNS label (RFC 1123, lowercase). flake.nix matches the same pattern against
# YOLOBOX_HOSTNAME; tests/test_consistency.py keeps the two equal. fullmatch only.
HOSTNAME_LABEL_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")

AGENT_USER = "agent"
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
