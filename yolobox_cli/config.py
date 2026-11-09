"""Constants shared across modules, and the version and host OS of this run."""

from __future__ import annotations

import re
import sys

from . import common
from .ui import YoError

# Where yo is installed, and the facts shipped beside it (common reads both).
SCRIPT_DIR = common.SCRIPT_DIR
FACTS = common.FACTS


# Rewritten at build time: nix substituteInPlace, Homebrew inreplace.
YO_VERSION = "dev"
# The commit this yo was built from, rewritten by nix alone (flake.nix passes
# self.rev; a dirty tree has none and stays "dev"). Brew builds from a release
# tarball with no git metadata, so a brew yo keeps "dev" here and builds its
# release tag instead (release.own_release_ref).
YO_SOURCE_REV = "dev"

DEFAULT_INSTANCE = FACTS["default_instance"]
# One DNS label (RFC 1123, lowercase); the flake matches the same pattern against
# YOLOBOX_HOSTNAME. fullmatch only.
HOSTNAME_LABEL_RE = re.compile(FACTS["hostname_label_re"])

# The operator's uid inside the guest on every host: lima-init's cidata account.
OPERATOR_GUEST_UID = FACTS["operator_uid"]
# What the operator's guest home adds to the account name: /home/<name>.guest.
OPERATOR_HOME_SUFFIX = FACTS["operator_home_suffix"]
# The shape of the operator's account name, which reaches sshd config, tmpfiles
# rules and shell scripts in the box; the box asserts the same pattern at eval
# time, and operator_account() refuses the name on the host first.
OPERATOR_ACCOUNT_RE = re.compile(FACTS["operator_account_re"])
# The words the box's stage-2 sshd and its initrd's sshd append to their SSH
# banners, so yo tells them, and the seed image's unmarked sshd, apart without
# logging in, and never offers a stage 2 a user it rejects.
STAGE2_SSH_BANNER_MARK = FACTS["stage2_ssh_banner_mark"]
INITRD_SSH_BANNER_MARK = FACTS["initrd_ssh_banner_mark"]
# The one ~/.ssh/config line that reaches every machine on both hosts: libvirt's
# instance files and Lima's. An Include applies only to the Host or Match block
# it follows, so it belongs above the first one (ssh_config_reaches_box()).
SSH_CONFIG_INCLUDE_LINE = (
    "Include ~/.local/state/yolobox/instances/*/ssh.config ~/.lima/*/ssh.config"
)
AGENT_USER = FACTS["agent_user"]


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


AGENT_HOME = FACTS["agent_home"]
HELPER = "yolobox-guest"
# What the box's build stamps; a box without it is not bootstrapped yet.
VERSION_STAMP = FACTS["version_stamp"]
# EX_USAGE (sysexits.h): the command cannot do what was asked on this host, and
# what the guest script answers an unknown subcommand with.
USAGE_EXIT = FACTS["usage_exit"]
SSH_UNREACHABLE = 255

OP_GUEST_SOCK = FACTS["op_guest_sock"]
AWS_BROKER_GUEST_SOCK = FACTS["aws_broker_guest_sock"]
AGENT_AWS_BROKER_SOCK = FACTS["agent_aws_broker_sock"]
# Where lima's hostagent listens on the Mac for the default instance.
SSH_LOCAL_PORT = FACTS["ssh_local_port"]
# The unit that loads the guest's egress table.
EGRESS_UNIT = FACTS["egress_unit"]

# The encrypted root: the partition and the name it is opened under. The
# passphrase is derived from a 1Password-held key (luks_passphrase) and never stored.
LUKS_DEVICE = FACTS["luks_device"]
LUKS_MAPPER = FACTS["luks_mapper"]

# Title of the Ed25519 "SSH Key" item in 1Password that unlocks the disk.
LUKS_KEY_ITEM = "yolobox-disk"

PROXY_SOCKET_UNITS = (
    FACTS["op_agent_proxy_unit"] + ".socket",
    FACTS["aws_broker_proxy_unit"] + ".socket",
)


# The host OS as sys.platform spells it: the one place yo asks. main() turns it
# into a driver with driver_for(), and nothing else looks at the host OS.
HOST_PLATFORM = sys.platform

LOOPBACK = "127.0.0.1"

MAX_PORT = 65535

KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB

# The smallest box `yo up --memory/--disk` creates, and why. The reasons are
# printed with a refused flag. The memory floor is measured: the first
# bootstrap's nix peaks at 1.78 GB, before the box has a swapfile.
MIN_MEMORY_GIB = 4
MIN_MEMORY_WHY = (
    "the box builds its own system, and with less the nix evaluator is OOM-killed "
    "during the first bootstrap, before the box has a swapfile"
)
MIN_DISK_GIB = 40
MIN_DISK_WHY = (
    "the box holds a 16 GiB swapfile plus the nix store of its first build, and "
    "a smaller disk fills up"
)

# Modes of the files yo writes: the owner alone, or world-readable (no secrets).
FILE_MODE_PRIVATE = 0o600
FILE_MODE_READABLE = 0o644
