"""What the Linux host must hold and what a domain's XML may hold.

The preflight of the host (virsh, the daemon's version, the host unit, the
`yolobox-vm` account, the passt socket directory, the port window) and the
policy a defined domain is checked against (the allowlists and the gaps).
Only libvirt.py imports this module.
"""

from __future__ import annotations

import os
import pwd
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, NamedTuple, Optional, Tuple

from . import proc, ui
from .config import KIB, LOOPBACK, MAX_PORT, SCRIPT_DIR
from .ui import YoError


LIBVIRT_URI = "qemu:///system"
LIBVIRT_MIN_VERSION = (12, 2, 0)
# The static account every domain runs as, and the unit that loads its table.
VM_USER = "yolobox-vm"
HOST_UNIT = "yolobox-host.service"
# Where libvirt puts passt's sockets; host-setup gives VM_USER write access there
# (linux/host-setup.sh, step j).
PASST_DIR = "/run/libvirt/qemu/passt"
# The namespace of the domain metadata that marks a domain as a yolobox's.
DOMAIN_NS = "https://github.com/aka-rider/yolobox"
# What a domain may hold: the elements of libvirt/domain.xml and the few that
# libvirt adds when it defines the domain (read off `virsh dumpxml` of a defined
# one). Anything else may be a path between the guest and the host, and is a gap.
# The root's children:
DOMAIN_ELEMENTS_ALLOWED = frozenset(
    {
        # the template's
        "name",
        "metadata",
        "memory",
        "vcpu",
        "os",
        "features",
        "cpu",
        "clock",
        "on_poweroff",
        "on_reboot",
        "on_crash",
        "devices",
        "seclabel",
        # libvirt's
        "uuid",
        "currentMemory",
        "resource",
    }
)
# The children of <devices>, by tag: the `type` values allowed, or None when the
# tag is allowed with any. A disk is a pool volume, never a host path.
DEVICES_ALLOWED: Dict[str, Optional[FrozenSet[str]]] = {
    # the template's
    "disk": frozenset({"volume"}),
    "interface": None,  # judged whole by domain_gaps
    "serial": frozenset({"pty"}),
    "console": frozenset({"pty"}),
    "controller": frozenset({"usb", "sata", "pci"}),
    "video": None,
    "rng": None,
    "memballoon": None,
    # libvirt's: the emulator path, the ps2 pair, a silent audio, the q35 watchdog
    "emulator": None,
    "input": frozenset({"mouse", "keyboard"}),
    "audio": frozenset({"none"}),
    "watchdog": None,
}
# The <rng> has no type to tell a host source apart: `egd` over a unix socket
# reaches a host socket, and `random` reads whatever file its text names. Only
# the host's two entropy devices are admitted.
RNG_BACKENDS = frozenset({("random", "/dev/urandom"), ("random", "/dev/random")})
QEMU_NS = "{http://libvirt.org/schemas/domain/qemu/1.0}"

GUEST_SSH_PORT = 22
LIBVIRT_POOL = "default"
# Where libvirt keeps a domain's EFI variable store, and where a firmware
# descriptor's variable-store template comes from (domain_shape).
NVRAM_DIR = "/var/lib/libvirt/qemu/nvram"
FIRMWARE_DIR = "/usr/share/"
PORT_RANGE_FILE = "/proc/sys/net/ipv4/ip_local_port_range"
MEMINFO_FILE = "/proc/meminfo"
MEMTOTAL_RE = re.compile(r"^MemTotal:\s+(\d+) kB$", re.M)
GAP_DEVICE = "device:%s"
GAP_DEVICE_TYPE = "device:%s[type=%s]"
GAP_DEVICE_BACKEND = "device:%s[backend=%s]"
GAP_ELEMENT = "element:%s"
GAP_SELINUX_SECLABEL = "seclabel.selinux"
GAP_DAC_SECLABEL = "seclabel.dac"
GAP_METADATA = "metadata"
GAP_FORWARD_ADDRESS = "portForward.address"
GAP_INTERFACE = "interface"


def host_setup_hint() -> str:
    return "run: sudo bash %s" % (SCRIPT_DIR / "linux" / "host-setup.sh")


def virsh_env() -> Dict[str, str]:
    # virsh translates what it prints (`domstate` says "laufend" under de_DE),
    # and yo parses it, so every virsh runs in the C locale.
    return dict(os.environ, LC_ALL="C")


def virsh(*args: str, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    return proc.run(
        ["virsh", "-c", LIBVIRT_URI, *args], check=check, env=virsh_env(), **kwargs
    )


def spawn_virsh(*args: str) -> subprocess.Popen:
    """A virsh that outlives the call (`virsh event`), text on both pipes."""
    return proc.spawn(
        ["virsh", "-c", LIBVIRT_URI, *args],
        env=virsh_env(),
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


class Seclabel(NamedTuple):
    """One <seclabel> of a domain."""

    type: str
    model: str
    relabel: str
    label: str  # the <label> text, empty when there is none


class Device(NamedTuple):
    """One child of <devices>."""

    tag: str
    type: str  # the type attribute, empty when there is none
    backend: str  # the <backend model=...>, empty when there is none
    backend_source: str  # the <backend> text (the host file it reads), empty when none


class Interface(NamedTuple):
    """One <interface> of a domain."""

    type: str
    backend: str  # the <backend type=...>, empty when there is none


@dataclass(frozen=True)
class DomainInfo:
    """What yo reads from a domain's XML."""

    instance: Optional[str]  # the yolobox:instance metadata, None when absent
    elements: Tuple[str, ...]  # the tag of every child of the root
    devices: Tuple[Device, ...]
    seclabels: Tuple[Seclabel, ...]
    interfaces: Tuple[Interface, ...]
    forward_addresses: Tuple[Optional[str], ...]  # of each <portForward>
    forward_ports: Tuple[int, ...]  # the host port of each forwarded range
    ssh_port: Optional[int]  # the host port forwarded to guest port 22


def domain_root(xml_text: str) -> ET.Element:
    try:
        return ET.fromstring(xml_text)
    except ET.ParseError as failure:
        raise YoError("virsh dumpxml printed unparseable XML: %s" % failure)


def domain_marker(root: ET.Element) -> Optional[str]:
    """The yolobox:instance metadata, None for a domain that is not a yolobox's."""
    marker = root.find("metadata/{%s}instance" % DOMAIN_NS)
    return (marker.text or "").strip() if marker is not None else None


def domain_info(root: ET.Element) -> DomainInfo:
    forwards = root.findall("devices/interface/portForward")
    ranges = [rng for forward in forwards for rng in forward.findall("range")]
    try:
        ports = tuple(int(rng.get("start", "")) for rng in ranges)
        ssh = next(
            (
                int(rng.get("start", ""))
                for rng in ranges
                if rng.get("to") == str(GUEST_SSH_PORT)
            ),
            None,
        )
    except ValueError:
        raise YoError("a portForward range has no numeric start")
    return DomainInfo(
        instance=domain_marker(root),
        elements=tuple(child.tag for child in root),
        devices=tuple(
            Device(
                child.tag,
                child.get("type", ""),
                *device_backend(child),
            )
            for child in root.findall("devices/*")
        ),
        seclabels=tuple(
            Seclabel(
                label.get("type", ""),
                label.get("model", ""),
                label.get("relabel", ""),
                (label.findtext("label") or "").strip(),
            )
            for label in root.findall("seclabel")
        ),
        interfaces=tuple(
            Interface(
                interface.get("type", ""),
                (
                    interface.find("backend").get("type", "")
                    if interface.find("backend") is not None
                    else ""
                ),
            )
            for interface in root.findall("devices/interface")
        ),
        forward_addresses=tuple(forward.get("address") for forward in forwards),
        forward_ports=ports,
        ssh_port=ssh,
    )


def parse_domain(xml_text: str) -> DomainInfo:
    return domain_info(domain_root(xml_text))


def seclabel_holds(
    info: DomainInfo, kind: str, model: str, label: Optional[str]
) -> bool:
    """Whether the domain has exactly one such seclabel, relabelling, with `label`."""
    found = [
        seclabel
        for seclabel in info.seclabels
        if (seclabel.type, seclabel.model) == (kind, model)
    ]
    return (
        len(found) == 1
        and found[0].relabel == "yes"
        and (label is None or found[0].label == label)
    )


def device_backend(device: ET.Element) -> Tuple[str, str]:
    """A device's <backend> model and text; empty strings when it has none."""
    backend = device.find("backend")
    if backend is None:
        return "", ""
    return backend.get("model", ""), (backend.text or "").strip()


def element_name(tag: str) -> str:
    """An element's tag as a person writes it: qemu:commandline, not {namespace}commandline."""
    return "qemu:" + tag[len(QEMU_NS) :] if tag.startswith(QEMU_NS) else tag


def device_gap(device: Device) -> Optional[str]:
    """The gap a device is, or None when the allowlist admits it."""
    if device.tag not in DEVICES_ALLOWED:
        return GAP_DEVICE % element_name(device.tag)
    types = DEVICES_ALLOWED[device.tag]
    if types is not None and device.type not in types:
        return GAP_DEVICE_TYPE % (element_name(device.tag), device.type)
    if (
        device.tag == "rng"
        and (device.backend, device.backend_source) not in RNG_BACKENDS
    ):
        spelling = ":".join(filter(None, (device.backend, device.backend_source)))
        return GAP_DEVICE_BACKEND % (device.tag, spelling)
    return None


def domain_gaps(info: DomainInfo, instance: str, uid: int, gid: int) -> List[str]:
    """How the domain differs from what libvirt/domain.xml makes, as gap names."""
    gaps = [
        GAP_ELEMENT % element_name(tag)
        for tag in info.elements
        if tag not in DOMAIN_ELEMENTS_ALLOWED
    ]
    gaps += [gap for gap in map(device_gap, info.devices) if gap is not None]
    gaps = list(dict.fromkeys(gaps))
    if not seclabel_holds(info, "dynamic", "selinux", None):
        gaps.append(GAP_SELINUX_SECLABEL)
    if not seclabel_holds(info, "static", "dac", "+%d:+%d" % (uid, gid)):
        gaps.append(GAP_DAC_SECLABEL)
    if info.instance != instance:
        gaps.append(GAP_METADATA)
    if not info.forward_addresses or any(
        address != LOOPBACK for address in info.forward_addresses
    ):
        gaps.append(GAP_FORWARD_ADDRESS)
    if info.interfaces != (Interface("user", "passt"),):
        gaps.append(GAP_INTERFACE)
    return gaps


def vm_account() -> Tuple[int, int]:
    try:
        account = pwd.getpwnam(VM_USER)
    except KeyError:
        raise YoError(
            "the account %s does not exist — %s" % (VM_USER, host_setup_hint())
        )
    return account.pw_uid, account.pw_gid


LISTENER_UID_RE = re.compile(r"\buid:(\d+)\b")


def listener_uids(ss_output: str) -> List[int]:
    """The owner uid of each socket in `ss -ltnHe` output (root is not needed).

    ss prints no `uid:` for a root-owned socket, so a line without one is root's.
    """
    uids = []
    for line in ss_output.splitlines():
        if not line.strip():
            continue
        found = LISTENER_UID_RE.search(line)
        uids.append(int(found.group(1)) if found is not None else 0)
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
