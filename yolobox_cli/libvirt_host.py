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
from typing import Dict, FrozenSet, List, NamedTuple, Optional, Tuple, Union

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
MARKER_TAG = "{%s}instance" % DOMAIN_NS
QEMU_NS = "{http://libvirt.org/schemas/domain/qemu/1.0}"
# The guest's address plan, Lima's (docs/linux-host.md, "The Lima mirror").
GUEST_ADDRESS = "192.168.5.15"
GUEST_PREFIX = "24"
GUEST_GATEWAY = "192.168.5.2"
GUEST_MTU = "65520"
GUEST_SSH_PORT = 22
LIBVIRT_POOL = "default"
# Where libvirt keeps a domain's EFI variable store, and where a firmware
# descriptor's variable-store template comes from (domain_shape).
NVRAM_DIR = "/var/lib/libvirt/qemu/nvram"
FIRMWARE_DIR = "/usr/share/"
PORT_RANGE_FILE = "/proc/sys/net/ipv4/ip_local_port_range"
MEMINFO_FILE = "/proc/meminfo"
MEMTOTAL_RE = re.compile(r"^MemTotal:\s+(\d+) kB$", re.M)
# The gap names (domain_gaps). The shape ones carry the element's path from
# the root: `element:devices/shmem`, `attribute:devices/interface/backend@logFile`.
GAP_ELEMENT = "element:%s"
GAP_REPEATED = "repeated:%s"
GAP_MISSING = "missing:%s"
GAP_ATTRIBUTE = "attribute:%s@%s"
GAP_VALUE = "value:%s@%s=%s"
GAP_TEXT = "text:%s=%s"
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


# A value rule: the strings admitted, or a pattern a value must match in full.
Rule = Union[FrozenSet[str], "re.Pattern[str]"]
ANY = re.compile(r".*", re.S)
DIGITS = re.compile(r"\d+")
HEX = re.compile(r"(0x)?[0-9a-fA-F]+")
PTY_PATH = re.compile(r"/dev/pts/\d+")
# libvirt's own SELinux process label: svirt_t with a category pair, nothing
# else (a <baselabel> would be a gap on its own, as an element not in shape).
SVIRT_LABEL = re.compile(r"system_u:system_r:svirt_t:s0:c\d+,c\d+")
DAC_LABEL = re.compile(r"\+\d+:\+\d+")


def one_of(*values: str) -> FrozenSet[str]:
    return frozenset(values)


def admits(rule: Rule, value: str) -> bool:
    if isinstance(rule, frozenset):
        return value in rule
    return rule.fullmatch(value) is not None


class Shape(NamedTuple):
    """What one element may hold: its attributes with the values each admits,
    its children by tag, and its text (None: it carries none). `many` admits
    the element more than once under its parent; `required` names children
    that must be there."""

    attrs: Dict[str, Rule]
    children: Dict[str, "Shape"]
    text: Optional[Rule]
    many: bool
    required: Tuple[str, ...]


def E(
    attrs: Optional[Dict[str, Rule]] = None,
    children: Optional[Dict[str, Shape]] = None,
    text: Optional[Rule] = None,
    many: bool = False,
    required: Tuple[str, ...] = (),
) -> Shape:
    return Shape(attrs or {}, children or {}, text, many, required)


# The elements libvirt adds to every device when it defines or runs a domain.
ALIAS = E(attrs={"name": ANY})
ADDRESS = E(
    attrs={
        "type": one_of("pci", "drive"),
        "domain": HEX,
        "bus": HEX,
        "slot": HEX,
        "function": HEX,
        "multifunction": one_of("on", "off"),
        "controller": DIGITS,
        "target": DIGITS,
        "unit": DIGITS,
    }
)
PLACED = {"alias": ALIAS, "address": ADDRESS}


class HostFirmware(NamedTuple):
    """What `virsh domcapabilities` says the host runs a q35 kvm domain with."""

    emulator: str
    loaders: FrozenSet[str]


def parse_domcapabilities(xml_text: str) -> HostFirmware:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as failure:
        raise YoError("virsh domcapabilities printed unparseable XML: %s" % failure)
    emulator = (root.findtext("path") or "").strip()
    loaders = frozenset(
        (value.text or "").strip() for value in root.findall("os/loader/value")
    )
    if not emulator or not loaders:
        raise YoError(
            "virsh domcapabilities names no emulator path or no firmware loader"
        )
    return HostFirmware(emulator, loaders)


def domain_shape(instance: str, firmware: HostFirmware) -> Shape:
    """The shape of libvirt/domain.xml for `instance`, as libvirt stores and
    runs it: the template's elements, attributes and values, plus what libvirt
    adds to a defined domain (uuid, currentMemory, resource, the firmware it
    resolved, the emulator, the q35 controllers, a ps2 input pair, a silent
    audio, the watchdog, aliases and addresses) and to a running one (the id,
    the pty paths, the labels). Everything else is a gap: a kernel, initrd or
    dtb from a host path, a loader or nvram outside the firmware's own
    directories, an smbios or acpi table from a file, another emulator, a disk
    from another pool or volume, a rom file, a backend log, a serial log, a
    base label, a second forwarded range, a backing store, a per-device
    seclabel. Each is a way to read or write a host file, or to widen what the
    host forwards or confines."""
    name = "yolobox-%s" % instance
    volumes = one_of("%s.qcow2" % name, "%s-cidata.iso" % name)
    return E(
        attrs={"type": one_of("kvm"), "id": DIGITS},
        children={
            "name": E(text=one_of(name)),
            "uuid": E(text=ANY),
            "metadata": E(children={MARKER_TAG: E(text=ANY)}),
            "memory": E(attrs={"unit": one_of("GiB", "KiB")}, text=DIGITS),
            "currentMemory": E(attrs={"unit": one_of("GiB", "KiB")}, text=DIGITS),
            "vcpu": E(attrs={"placement": one_of("static")}, text=DIGITS),
            "resource": E(children={"partition": E(text=one_of("/machine"))}),
            "os": E(
                attrs={"firmware": one_of("efi")},
                children={
                    "type": E(
                        attrs={
                            "arch": one_of("x86_64"),
                            "machine": re.compile(r"q35|pc-q35-[0-9.]+"),
                        },
                        text=one_of("hvm"),
                    ),
                    "firmware": E(
                        children={
                            "feature": E(
                                attrs={
                                    "enabled": one_of("no"),
                                    "name": one_of("secure-boot", "enrolled-keys"),
                                },
                                many=True,
                            )
                        }
                    ),
                    "loader": E(
                        attrs={
                            "readonly": one_of("yes"),
                            "type": one_of("pflash"),
                            "format": one_of("raw", "qcow2"),
                        },
                        text=firmware.loaders,
                    ),
                    "nvram": E(
                        attrs={
                            "template": re.compile(re.escape(FIRMWARE_DIR) + r"\S+"),
                            "templateFormat": one_of("raw", "qcow2"),
                            "format": one_of("raw", "qcow2"),
                        },
                        text=re.compile(
                            re.escape("%s/%s_VARS." % (NVRAM_DIR, name)) + r"[a-z0-9]+"
                        ),
                    ),
                    "boot": E(attrs={"dev": one_of("hd")}),
                },
                required=("type", "boot"),
            ),
            "features": E(children={"acpi": E(), "apic": E()}),
            "cpu": E(
                attrs={
                    "mode": one_of("host-passthrough"),
                    "check": one_of("none"),
                    "migratable": one_of("on", "off"),
                }
            ),
            "clock": E(attrs={"offset": one_of("utc")}),
            "on_poweroff": E(text=one_of("destroy")),
            "on_reboot": E(text=one_of("restart")),
            "on_crash": E(text=one_of("destroy")),
            "devices": E(
                children={
                    "emulator": E(text=one_of(firmware.emulator)),
                    "disk": E(
                        attrs={
                            "type": one_of("volume"),
                            "device": one_of("disk", "cdrom"),
                        },
                        children={
                            "driver": E(
                                attrs={
                                    "name": one_of("qemu"),
                                    "type": one_of("qcow2", "raw"),
                                    "discard": one_of("unmap"),
                                    "detect_zeroes": one_of("unmap"),
                                }
                            ),
                            "source": E(
                                attrs={
                                    "pool": one_of(LIBVIRT_POOL),
                                    "volume": volumes,
                                    "index": DIGITS,
                                }
                            ),
                            # Empty: a backing file would be a host path.
                            "backingStore": E(),
                            "target": E(
                                attrs={
                                    "dev": one_of("vda", "sda"),
                                    "bus": one_of("virtio", "sata"),
                                }
                            ),
                            "readonly": E(),
                            **PLACED,
                        },
                        many=True,
                        required=("driver", "source", "target"),
                    ),
                    "controller": E(
                        attrs={
                            "type": one_of("usb", "sata", "pci"),
                            "index": DIGITS,
                            "model": one_of("none", "pcie-root", "pcie-root-port"),
                        },
                        children={
                            "model": E(attrs={"name": one_of("pcie-root-port")}),
                            "target": E(attrs={"chassis": DIGITS, "port": HEX}),
                            **PLACED,
                        },
                        many=True,
                    ),
                    "interface": E(
                        attrs={"type": one_of("user")},
                        children={
                            "mac": E(attrs={"address": ANY}),
                            "backend": E(attrs={"type": one_of("passt")}),
                            "model": E(attrs={"type": one_of("virtio")}),
                            "mtu": E(attrs={"size": one_of(GUEST_MTU)}),
                            "ip": E(
                                attrs={
                                    "family": one_of("ipv4"),
                                    "address": one_of(GUEST_ADDRESS),
                                    "prefix": one_of(GUEST_PREFIX),
                                }
                            ),
                            "route": E(
                                attrs={
                                    "family": one_of("ipv4"),
                                    "gateway": one_of(GUEST_GATEWAY),
                                }
                            ),
                            "portForward": E(
                                attrs={
                                    "proto": one_of("tcp"),
                                    "address": one_of(LOOPBACK),
                                },
                                children={
                                    "range": E(
                                        attrs={
                                            "start": DIGITS,
                                            "to": one_of(str(GUEST_SSH_PORT)),
                                        }
                                    )
                                },
                                required=("range",),
                            ),
                            **PLACED,
                        },
                        required=("backend", "portForward"),
                    ),
                    "serial": E(
                        attrs={"type": one_of("pty")},
                        children={
                            "source": E(attrs={"path": PTY_PATH}),
                            "target": E(
                                attrs={"type": one_of("isa-serial"), "port": DIGITS},
                                children={
                                    "model": E(attrs={"name": one_of("isa-serial")})
                                },
                            ),
                            "alias": ALIAS,
                        },
                    ),
                    "console": E(
                        attrs={"type": one_of("pty"), "tty": PTY_PATH},
                        children={
                            "source": E(attrs={"path": PTY_PATH}),
                            "target": E(
                                attrs={"type": one_of("serial"), "port": DIGITS}
                            ),
                            "alias": ALIAS,
                        },
                    ),
                    "input": E(
                        attrs={
                            "type": one_of("mouse", "keyboard"),
                            "bus": one_of("ps2"),
                        },
                        children={"alias": ALIAS},
                        many=True,
                    ),
                    "audio": E(attrs={"id": DIGITS, "type": one_of("none")}),
                    "video": E(
                        children={
                            "model": E(attrs={"type": one_of("none")}),
                            "alias": ALIAS,
                        }
                    ),
                    "watchdog": E(
                        attrs={"model": one_of("itco"), "action": one_of("reset")},
                        children={"alias": ALIAS},
                    ),
                    "memballoon": E(attrs={"model": one_of("none")}),
                    "rng": E(
                        attrs={"model": one_of("virtio")},
                        children={
                            # `egd` would reach a host socket, and `random`
                            # reads whatever file its text names.
                            "backend": E(
                                attrs={"model": one_of("random")},
                                text=one_of("/dev/urandom", "/dev/random"),
                            ),
                            **PLACED,
                        },
                        required=("backend",),
                    ),
                }
            ),
            "seclabel": E(
                attrs={
                    "type": one_of("dynamic", "static"),
                    "model": one_of("selinux", "dac"),
                    "relabel": one_of("yes", "no"),
                },
                children={
                    "label": E(
                        text=re.compile(
                            "%s|%s" % (SVIRT_LABEL.pattern, DAC_LABEL.pattern)
                        )
                    ),
                    "imagelabel": E(text=ANY),
                },
                many=True,
            ),
        },
    )


def element_name(tag: str) -> str:
    """An element's tag as a person writes it: qemu:commandline, not {namespace}commandline."""
    if tag == MARKER_TAG:
        return "yolobox:instance"
    return "qemu:" + tag[len(QEMU_NS) :] if tag.startswith(QEMU_NS) else tag


def shape_gaps(element: ET.Element, shape: Shape, path: str) -> List[str]:
    """Every way `element` departs from `shape`, as gap names carrying `path`."""
    gaps = []
    here = path or "domain"
    for name, value in element.attrib.items():
        if name not in shape.attrs:
            gaps.append(GAP_ATTRIBUTE % (here, name))
        elif not admits(shape.attrs[name], value):
            gaps.append(GAP_VALUE % (here, name, value))
    text = (element.text or "").strip()
    if shape.text is None:
        if text:
            gaps.append(GAP_TEXT % (here, text))
    elif not admits(shape.text, text):
        gaps.append(GAP_TEXT % (here, text))
    seen: Dict[str, int] = {}
    for child in element:
        child_path = "/".join(filter(None, (path, element_name(child.tag))))
        child_shape = shape.children.get(child.tag)
        if child_shape is None:
            gaps.append(GAP_ELEMENT % child_path)
            continue
        seen[child.tag] = seen.get(child.tag, 0) + 1
        if seen[child.tag] == 2 and not child_shape.many:
            gaps.append(GAP_REPEATED % child_path)
        gaps += shape_gaps(child, child_shape, child_path)
    for tag in shape.required:
        if tag not in seen:
            gaps.append(GAP_MISSING % "/".join(filter(None, (path, tag))))
    return gaps


class Seclabel(NamedTuple):
    """One <seclabel> of a domain."""

    type: str
    model: str
    relabel: str
    label: str  # the <label> text, empty when there is none


class Interface(NamedTuple):
    """One <interface> of a domain."""

    type: str
    backend: str  # the <backend type=...>, empty when there is none


@dataclass(frozen=True)
class DomainInfo:
    """What yo reads from a domain's XML."""

    root: ET.Element
    instance: Optional[str]  # the yolobox:instance metadata, None when absent
    seclabels: Tuple[Seclabel, ...]
    interfaces: Tuple[Interface, ...]
    forward_addresses: Tuple[Optional[str], ...]  # of each <portForward>
    ssh_port: Optional[int]  # the host port forwarded to guest port 22


def domain_root(xml_text: str) -> ET.Element:
    try:
        return ET.fromstring(xml_text)
    except ET.ParseError as failure:
        raise YoError("virsh dumpxml printed unparseable XML: %s" % failure)


def domain_marker(root: ET.Element) -> Optional[str]:
    """The yolobox:instance metadata, None for a domain that is not a yolobox's."""
    marker = root.find("metadata/" + MARKER_TAG)
    return (marker.text or "").strip() if marker is not None else None


def domain_info(root: ET.Element) -> DomainInfo:
    forwards = root.findall("devices/interface/portForward")
    ranges = [rng for forward in forwards for rng in forward.findall("range")]
    try:
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
        root=root,
        instance=domain_marker(root),
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


def domain_gaps(
    info: DomainInfo, instance: str, uid: int, gid: int, firmware: HostFirmware
) -> List[str]:
    """How the domain differs from what libvirt/domain.xml makes, as gap names:
    first every departure from the template's shape (domain_shape), then the
    guarantees the shape alone cannot state (one relabelling seclabel of each
    model, the DAC one the vm account's; the tag; the forward on loopback; one
    passt interface)."""
    gaps = shape_gaps(info.root, domain_shape(instance, firmware), "")
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
