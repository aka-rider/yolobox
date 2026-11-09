"""What the Linux host must hold for a box to run on it.

The preflight of the host (virsh, the daemon's version, the host unit, the
`yolobox-vm` account, the port window), what only a start needs (the passt
socket directory, the resolver's label), and the two reads yo makes of a
domain's XML: its yolobox marker and the host ports it forwards. Only
libvirt.py imports this module.
"""

from __future__ import annotations

import errno
import os
import pwd
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from typing import List, Optional, Tuple

from . import proc, ui
from .config import FACTS, KIB, MAX_PORT, SCRIPT_DIR
from .ui import YoError


LIBVIRT_URI = "qemu:///system"
LIBVIRT_MIN_VERSION = (12, 2, 0)
# The static account every domain runs as, and the unit that loads its table.
VM_USER = FACTS["vm_user"]
HOST_UNIT = "yolobox-host.service"
# Where libvirt puts passt's sockets; host-setup gives VM_USER write access there.
PASST_DIR = FACTS["passt_dir"]
# The namespace of the domain metadata that marks a domain as a yolobox's.
DOMAIN_NS = FACTS["domain_ns"]
MARKER_TAG = "{%s}instance" % DOMAIN_NS
# The host resolver passt hands the guest, and the only SELinux type of it that
# passt_t may read.
RESOLV_CONF = FACTS["host_resolv_conf"]
RESOLV_CONF_TYPE = FACTS["host_resolv_conf_type"]
LIBVIRT_POOL = "default"
PORT_RANGE_FILE = "/proc/sys/net/ipv4/ip_local_port_range"
MEMINFO_FILE = "/proc/meminfo"
MEMTOTAL_RE = re.compile(r"^MemTotal:\s+(\d+) kB$", re.M)


def host_setup_hint() -> str:
    return "run: sudo bash %s" % (SCRIPT_DIR / "linux" / "host-setup.sh")


def virsh(*args: str, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    return proc.run(["virsh", "-c", LIBVIRT_URI, *args], check=check, **kwargs)


def spawn_virsh(*args: str) -> subprocess.Popen:
    """A virsh that outlives the call (`virsh event`), text on both pipes."""
    return proc.spawn(
        ["virsh", "-c", LIBVIRT_URI, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def parse_version(text: str) -> Tuple[int, ...]:
    word = text.strip()
    if not re.fullmatch(r"\d+(\.\d+)*", word):
        raise YoError("cannot read a version from %r" % text)
    return tuple(int(field) for field in word.split("."))


DAEMON_VERSION_RE = re.compile(r"^\s*Running against daemon:\s*(\S+)\s*$", re.M | re.I)


def parse_daemon_version(version_output: str) -> Tuple[int, ...]:
    """The libvirt daemon's version, from `virsh version --daemon`.

    The daemon is what starts passt and reads the domain XML, so its version is
    the one that counts; the client library may be newer than the daemon.
    """
    found = DAEMON_VERSION_RE.search(version_output)
    if found is None:
        raise YoError(
            "cannot find the daemon's version in `virsh version --daemon`: %r"
            % version_output
        )
    return parse_version(found.group(1))


# The first port the window may start at: IANA's registered ports end at
# 49151, and a host whose ephemeral range ends below that would otherwise hand
# yo a port a registered service may listen on.
DYNAMIC_PORTS_START = 49152


def host_port_window(range_text: str) -> range:
    """The host ports yo may bind: the ones above the kernel's ephemeral range.

    Nothing the kernel hands out as a source port, and no registered service,
    lives there, so a port picked from it cannot collide with either.
    """
    fields = range_text.split()
    if len(fields) != 2 or not all(proc.is_ascii_number(field) for field in fields):
        raise YoError(
            "%s holds %r, not two port numbers" % (PORT_RANGE_FILE, range_text)
        )
    # Never below the dynamic ports: a host whose range ends at 30000 would
    # otherwise offer 30001-49151, where registered services listen.
    first = max(int(fields[1]) + 1, DYNAMIC_PORTS_START)
    if first > MAX_PORT:
        raise YoError(
            "the kernel's ephemeral port range ends at %s (%s), so no host port is "
            "left above it for the box's ssh forward; lower the range's end with "
            "sysctl net.ipv4.ip_local_port_range" % (fields[1], PORT_RANGE_FILE)
        )
    return range(first, MAX_PORT + 1)


def read_port_window() -> range:
    try:
        with open(PORT_RANGE_FILE, "r") as handle:
            return host_port_window(handle.read())
    except OSError as failure:
        raise YoError(
            "cannot read %s: %s" % (PORT_RANGE_FILE, ui.os_error_reason(failure))
        )


def resolver_label_gap() -> Optional[str]:
    """What keeps passt from reading the host's resolver, or None when nothing does.

    passt_t may read only net_conf_t. A resolv.conf written by rename (a root
    editor over resolved's stub) keeps the editor's label, passt is denied, and
    the guest's DHCP lease then carries no DNS server. passt reads the file only
    when a domain starts. Without SELinux the kernel has no label to answer
    with, and then none can deny passt.
    """
    target = os.path.realpath(RESOLV_CONF)
    try:
        raw = os.getxattr(target, "security.selinux")
    except OSError as failure:
        if failure.errno in (errno.ENODATA, errno.EOPNOTSUPP):
            return None
        raise YoError(
            "cannot read the SELinux label of %s: %s"
            % (target, ui.os_error_reason(failure))
        )
    # `user:role:type:level`, NUL-terminated; the level holds colons itself.
    label = raw.rstrip(b"\0").decode("utf-8", "replace")
    fields = label.split(":", 3)
    if len(fields) >= 3 and fields[2] == RESOLV_CONF_TYPE:
        return None
    return (
        "passt cannot read the host resolver %s, labelled '%s' instead of type %s, "
        'so the guest would get no DNS — fix it with: sudo restorecon -F "$(readlink -f %s)"'
        % (target, ui.shown(label), RESOLV_CONF_TYPE, RESOLV_CONF)
    )


def forwarded_host_ports(root: ET.Element) -> List[int]:
    """Every host port any domain's <portForward> takes, read leniently: a
    range whose start or end is not a number is skipped, since a foreign
    domain's XML is never read strictly."""
    ports = []
    for rng in root.findall("devices/interface/portForward/range"):
        start, end = rng.get("start", ""), rng.get("end", "")
        if not proc.is_ascii_number(start):
            continue
        last = int(end) if proc.is_ascii_number(end) else int(start)
        ports.extend(range(int(start), last + 1))
    return ports


def host_memory_bytes() -> int:
    """The host's MemTotal: the most memory a domain can be given."""
    try:
        with open(MEMINFO_FILE, "r") as handle:
            text = handle.read()
    except OSError as failure:
        raise YoError(
            "cannot read %s: %s" % (MEMINFO_FILE, ui.os_error_reason(failure))
        )
    found = MEMTOTAL_RE.search(text)
    if found is None:
        raise YoError("%s has no MemTotal line in kB" % MEMINFO_FILE)
    return int(found.group(1)) * KIB


def domain_root(xml_text: str) -> ET.Element:
    try:
        return ET.fromstring(xml_text)
    except ET.ParseError as failure:
        raise YoError("virsh dumpxml printed unparseable XML: %s" % failure)


def domain_marker(root: ET.Element) -> Optional[str]:
    """The yolobox:instance metadata, None for a domain that is not a yolobox's."""
    marker = root.find("metadata/" + MARKER_TAG)
    return (marker.text or "").strip() if marker is not None else None


def vm_account() -> Tuple[int, int]:
    try:
        account = pwd.getpwnam(VM_USER)
    except KeyError:
        raise YoError(
            "the account %s does not exist — %s" % (VM_USER, host_setup_hint())
        )
    return account.pw_uid, account.pw_gid


LISTENER_UID_RE = re.compile(r"\buid:(\d+)\b")
LISTENER_INODE_RE = re.compile(r"\bino:\d+\b")


def listener_uids(ss_output: str) -> List[int]:
    """The owner uid of each socket in `ss -ltnHe` output (root is not needed).

    ss's -e details always carry `ino:`, and `uid:` only for an owner that is
    not root, so a line with its `ino:` and no `uid:` is root's. A line without
    the details is an answer yo has no name for, and an error showing it.
    """
    uids = []
    for line in ss_output.splitlines():
        if not line.strip():
            continue
        found = LISTENER_UID_RE.search(line)
        if found is not None:
            uids.append(int(found.group(1)))
        elif LISTENER_INODE_RE.search(line) is not None:
            uids.append(0)
        else:
            raise YoError(
                "`ss -ltnHe` printed a listener without its details (no ino:), so "
                "yo cannot tell who owns it: %s" % ui.shown(line.strip())
            )
    return uids


def acl_effective_permissions(getfacl_output: str, user: str) -> Optional[str]:
    """The permissions `user` really has through the ACL entry `getfacl` printed.

    The entry is ANDed with the mask, as the kernel does; the `#effective:`
    comment is ignored. None when the ACL has no entry for the user. Default
    entries (`default:user:...`) are other lines and never match.
    """
    entry: Optional[str] = None
    mask: Optional[str] = None
    for line in getfacl_output.splitlines():
        fields = line.split("#", 1)[0].strip().split(":")
        if fields[:2] == ["user", user] and len(fields) == 3:
            entry = fields[2]
        elif fields[:2] == ["mask", ""] and len(fields) == 3:
            mask = fields[2]
    if entry is None:
        return None
    if mask is None:
        raise YoError(
            "getfacl printed an entry for %s but no mask:\n%s" % (user, getfacl_output)
        )
    return "".join(
        letter if letter in entry and letter in mask else "-" for letter in "rwx"
    )


def passt_dir_gap() -> Optional[str]:
    """What keeps VM_USER from writing in PASST_DIR, or None when it can.

    libvirt recomputes the directory's ACL mask each time it starts a passt
    domain, and the entry host-setup grants is only as wide as the mask.
    """
    proc.require_tool("getfacl", "install the acl package")
    listing = proc.run(["getfacl", "--absolute-names", PASST_DIR])
    remedy = (
        "fix it with: sudo setfacl -m u:%s:rwx,m::rwx %s; to keep it fixed when "
        "libvirt starts another passt domain, install the libvirt hook: %s"
        % (VM_USER, PASST_DIR, host_setup_hint())
    )
    if listing.returncode != 0:
        return "cannot read the ACL of %s (%s) — %s" % (
            PASST_DIR,
            (listing.stderr or "").strip() or "getfacl exit %d" % listing.returncode,
            host_setup_hint(),
        )
    effective = acl_effective_permissions(listing.stdout, VM_USER)
    if effective is None:
        return (
            "%s has no ACL entry for %s, so passt cannot bind its socket there; %s"
            % (
                PASST_DIR,
                VM_USER,
                remedy,
            )
        )
    if "w" not in effective:
        return (
            "%s gives %s only %s (the ACL mask cuts the entry down), so passt cannot "
            "bind its socket there; %s" % (PASST_DIR, VM_USER, effective, remedy)
        )
    return None


def check_libvirt_host() -> None:
    """Raise YoError, naming the setup script, for the first thing the host lacks.

    Cheap presence checks only: every command that talks to a box runs this.
    """
    setup = host_setup_hint()
    if shutil.which("virsh") is None:
        raise YoError("virsh not found — %s" % setup)
    connect = virsh("uri", check=False)
    if connect.returncode != 0:
        raise YoError(
            "cannot connect to %s (%s) — %s"
            % (LIBVIRT_URI, (connect.stderr or "").strip(), setup)
        )
    version = parse_daemon_version(virsh("version", "--daemon").stdout)
    if version < LIBVIRT_MIN_VERSION:
        raise YoError(
            "the libvirt daemon %s is older than %s; passt needs the interface route "
            "option (after an upgrade, restart virtqemud) — %s"
            % (
                ".".join(map(str, version)),
                ".".join(map(str, LIBVIRT_MIN_VERSION)),
                setup,
            )
        )
    unit = proc.run(["systemctl", "is-active", HOST_UNIT])
    if unit.stdout.strip() != "active":
        raise YoError(
            "%s is %s, so the host's firewall table for the box is not loaded — %s"
            % (HOST_UNIT, unit.stdout.strip() or "not active", setup)
        )
    vm_account()
    read_port_window()
