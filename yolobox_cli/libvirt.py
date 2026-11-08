"""The libvirt driver: the domain, its network and storage, and the host checks."""

from __future__ import annotations

import base64
import contextlib
import errno
import hashlib
import json
import os
import re
import socket
import string
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from typing import (
    Callable,
    Dict,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
)

from . import (
    agent,
    aws,
    common,
    config,
    driver,
    forwards,
    libvirt_host,
    proc,
    release,
    sshx,
    state,
    ui,
    unlock,
)
from .config import (
    FILE_MODE_PRIVATE,
    FILE_MODE_READABLE,
    GIB,
    KIB,
    LOOPBACK,
    MIN_DISK_GIB,
    MIN_MEMORY_GIB,
    OPERATOR_GUEST_UID,
    OPERATOR_HOME_SUFFIX,
    SCRIPT_DIR,
)
from .driver import (
    BoxSize,
    DriverHints,
    NO_SIZE,
    NewBox,
    Removal,
    Size,
    SshFacts,
    StopRefused,
    VmState,
    VmStatus,
)
from .libvirt_host import LIBVIRT_POOL, LIBVIRT_URI, VM_USER
from .sshx import OPERATOR
from .ui import YoError


# The libvirt driver: Linux hosts. One KVM domain per machine on the system
# connection, with passt for the network and an SELinux/DAC label from the host
# layer. The domain is always what libvirt/domain.xml makes: a stopped domain
# is defined again from the template on every start, filled in from the facts
# the instance directory keeps (DomainFacts). The host's preflight is in
# libvirt_host.py. Pure helpers first, then the driver, which calls them and
# virsh by name at call time.
# The 1Password item whose public key is the operator's login, in cidata.
OPERATOR_KEY_ITEM = "yolobox-operator"
# A box's memory and disk when `yo up` is given no --memory or --disk
# (domain_size); lima/yolobox.yaml has the same. Its CPUs are default_cpus()
# in driver.py, which vm_up fills into the size.
LIBVIRT_MEMORY_GIB = 16
LIBVIRT_DISK_GIB = 100
# What an explicit --memory leaves the host itself: its kernel, page cache and
# desktop. A domain has no balloon (memballoon model='none'), so its qemu grows
# to the whole of its memory, and a host short of it OOM-kills a qemu.
HOST_MEMORY_RESERVE_GIB = 4
LIBVIRT_STOP_TIMEOUT = 300
# What `virsh event --timeout` prints, then exits 1, when no more events came.
EVENT_TIMEOUT_LINE = "event loop timed out"
# virsh domstate words. A guest is up in two of them ("blocked" is a running
# guest idling on I/O), and both read as VmState.RUNNING. "in shutdown" is a
# guest still going down, and the only other word a stop waits through.
STATE_OFF = "shut off"
STATE_SHUTTING_DOWN = "in shutdown"
RUNNING_STATES = ("running", "blocked")
STATES_STOPPING = RUNNING_STATES + (STATE_SHUTTING_DOWN,)
# What `virsh start` says when passt cannot bind in PASST_DIR: the ACL mask was
# recomputed by another passt domain's start after yo checked it.
PASST_BIND_DENIED = "Failed to bind UNIX domain socket"
# The states a guest stays in until someone acts: the verb and virsh command
# that bring it back (state_hint).
STATE_REMEDIES = {
    "paused": ("resume", "resume"),
    "pmsuspended": ("wake", "dompmwakeup"),
}
PORT_LOCK_NAME = ".port.lock"
# In the instance directory: what the template is filled in with, kept so every
# start defines the same domain (libvirt refuses a new uuid under a known name).
DOMAIN_FACTS_NAME = "domain.json"

SEED_NAME = "nixos-lima-v0.2.2-x86_64.qcow2"
SEED_URL = (
    "https://github.com/nixos-lima/nixos-lima/releases/download/v0.2.2/" + SEED_NAME
)
SEED_SHA512 = (
    "cdf15475c6f799d3a355da50147fa81316e43e159b1b2e76eb21046f9fe8499770840ef876daaf85a2f5eb"
    "3a73445c4fa87d0337533fdf6f116d80c40806fbc9"
)
SEED_CHUNK_BYTES = 1 << 20
SEED_CONNECT_TIMEOUT = 60
CIDATA_FILES = ("user-data", "meta-data", "lima.env", "etc_environment")
MASTER_PID_RE = re.compile(r"Master running \(pid=(\d+)\)")
SIZE_LINE_RE = re.compile(r"^(Capacity|Allocation):\s+(\d+) bytes\s*$", re.M)
MAX_MEMORY_LINE_RE = re.compile(r"^Max memory:\s+(\d+) KiB\s*$", re.M)


class DomainFacts(NamedTuple):
    """What a machine's domain is defined with, besides the template."""

    uuid: str
    ssh_port: int
    memory_gib: int
    cpus: int


def read_domain_facts(directory: str) -> Optional[DomainFacts]:
    """The facts kept in `directory`, None when there are none."""
    path = os.path.join(directory, DOMAIN_FACTS_NAME)
    text = proc.read_text_file(path)
    if text is None:
        return None
    try:
        facts = DomainFacts(**json.loads(text))
    except (ValueError, TypeError) as failure:
        raise YoError("%s is not a domain record: %s" % (path, failure))
    if not (
        isinstance(facts.uuid, str)
        and all(
            isinstance(field, int) and not isinstance(field, bool)
            for field in (facts.ssh_port, facts.memory_gib, facts.cpus)
        )
    ):
        raise YoError("%s is not a domain record: %r" % (path, text))
    return facts


def write_domain_facts(directory: str, facts: DomainFacts) -> None:
    proc.atomic_write(
        os.path.join(directory, DOMAIN_FACTS_NAME),
        json.dumps(facts._asdict(), indent=2) + "\n",
        FILE_MODE_READABLE,
    )


def domain_name() -> str:
    return state.box.vm_name


def disk_volume() -> str:
    return state.box.vm_name + ".qcow2"


def cidata_volume() -> str:
    return state.box.vm_name + "-cidata.iso"


def loopback_port_bound(port: int) -> bool:
    """Whether something already holds 127.0.0.1:port (a bind probe)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((LOOPBACK, port))
        except OSError as failure:
            if failure.errno == errno.EADDRINUSE:
                return True
            raise YoError(
                "cannot probe %s:%d: %s" % (LOOPBACK, port, ui.os_error_reason(failure))
            )
    return False


def lowest_free_port(
    window: range, taken: Sequence[int], is_bound: Callable[[int], bool]
) -> int:
    for port in window:
        if port not in taken and not is_bound(port):
            return port
    raise YoError(
        "no free host port in %d-%d: every one is bound or belongs to a box; stop a "
        "box or end a forward" % (window.start, window[-1])
    )


def domain_size(new_box: NewBox) -> BoxSize:
    """The size of a box yo creates: what was asked, else the defaults. The CPU
    count is the one vm_up decided, so there is no second default here."""
    return new_box.fill(LIBVIRT_MEMORY_GIB, LIBVIRT_DISK_GIB)


def domain_xml(instance: str, facts: DomainFacts, uid: int, gid: int) -> str:
    template = string.Template(
        (SCRIPT_DIR / "libvirt" / "domain.xml").read_text(encoding="utf-8")
    )
    return template.substitute(
        name=instance,
        uuid=facts.uuid,
        ssh_port=facts.ssh_port,
        uid=uid,
        gid=gid,
        memory_gib=facts.memory_gib,
        cpus=facts.cpus,
    )


def public_key_line(blob: bytes) -> str:
    return "%s %s %s" % (
        agent.key_type(blob),
        base64.b64encode(blob).decode("ascii"),
        OPERATOR_KEY_ITEM,
    )


def cidata_files(username: str, public_key: str, instance: str) -> Dict[str, str]:
    """The files of the cidata ISO, by name. The seed image is nixos-lima's, and
    its lima-init creates the operator from these LIMA_CIDATA_* variables and
    takes lima's instance name as its hostname; the initrd reads the key out of
    user-data. That is why a libvirt guest is told it is lima's."""
    account = config.operator_account(username)
    return {
        "user-data": (
            "#cloud-config\n"
            "users:\n"
            "  - name: %s\n"
            "    ssh-authorized-keys:\n"
            '      - "%s"\n' % (account, public_key)
        ),
        "meta-data": (
            "instance-id: yolobox-%s\nlocal-hostname: lima-%s\n" % (instance, instance)
        ),
        # Read line by line with `export "$line"`: no blank lines.
        "lima.env": (
            "LIMA_CIDATA_USER=%s\n"
            "LIMA_CIDATA_UID=%d\n"
            "LIMA_CIDATA_HOME=/home/%s%s\n"
            "LIMA_CIDATA_IID=yolobox-%s\n"
            % (account, OPERATOR_GUEST_UID, account, OPERATOR_HOME_SUFFIX, instance)
        ),
        "etc_environment": "",
    }


def file_size(path: str) -> int:
    try:
        return os.stat(path).st_size
    except OSError as failure:
        raise YoError("cannot stat %s: %s" % (path, ui.os_error_reason(failure)))


def file_sha512(path: str) -> str:
    digest = hashlib.sha512()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(SEED_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_seed(path: str) -> None:
    """Download the seed to `path`, through a file of its own, checked before it is moved."""
    fd, part = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            with urllib.request.urlopen(
                SEED_URL, timeout=SEED_CONNECT_TIMEOUT
            ) as reply:
                for chunk in iter(lambda: reply.read(SEED_CHUNK_BYTES), b""):
                    handle.write(chunk)
        digest = file_sha512(part)
        if digest != SEED_SHA512:
            raise YoError(
                "the download of %s has sha512 %s, not the pinned %s"
                % (SEED_URL, digest, SEED_SHA512)
            )
        os.replace(part, path)
    except (urllib.error.URLError, OSError) as failure:
        raise YoError("cannot download %s: %s" % (SEED_URL, failure))
    finally:
        proc.remove_file(part)


def ensure_seed() -> str:
    """The pinned seed image, downloaded once and verified on every use."""
    cache = os.path.join(state.home(), ".cache", "yolobox")
    proc.private_dir(cache)
    path = os.path.join(cache, SEED_NAME)
    if os.path.exists(path):
        if file_sha512(path) == SEED_SHA512:
            return path
        ui.err("yo: %s fails its sha512 check; downloading it again" % path)
        proc.remove_file(path)
    ui.err("yo: downloading the seed image %s" % SEED_URL)
    download_seed(path)
    return path


def names_listed(listing: str) -> List[str]:
    """The names `virsh list --name` printed, one per line."""
    return [line.strip() for line in listing.splitlines() if line.strip()]


def domain_names() -> List[str]:
    """Every defined domain, running or not."""
    return names_listed(libvirt_host.virsh("list", "--all", "--name").stdout)


def active_domain_names() -> List[str]:
    """The domains that hold their memory now: running, paused, shutting down."""
    return names_listed(libvirt_host.virsh("list", "--name").stdout)


def raise_unless_undefined(
    subcommand: str, name: str, failed: subprocess.CompletedProcess
) -> None:
    """Raise `failed`, the failure of `virsh SUBCOMMAND NAME`, unless that domain is no
    longer defined, which means it was undefined after it was listed."""
    if name in domain_names():
        proc.checked(failed, "virsh %s %s" % (subcommand, name))


def active_domains_memory() -> Dict[str, int]:
    """The max memory, in bytes, of every active domain, from `virsh dominfo`.

    A domain undefined between the listing and its dominfo is not counted; any
    other failure of dominfo is an error.
    """
    memory: Dict[str, int] = {}
    for name in active_domain_names():
        info = libvirt_host.virsh("dominfo", name, check=False)
        if info.returncode != 0:
            raise_unless_undefined("dominfo", name, info)
            continue
        found = MAX_MEMORY_LINE_RE.search(info.stdout)
        if found is None:
            raise YoError(
                "virsh dominfo %s printed no Max memory in KiB: %s"
                % (name, info.stdout.strip())
            )
        memory[name] = int(found.group(1)) * KIB
    return memory


def gib_text(size: int) -> str:
    return "%.1f GiB" % (size / GIB)


def memory_room(host: int, others: Dict[str, int]) -> int:
    """What is left for a new domain, in bytes: MemTotal less the host's reserve
    and the whole max memory of every active domain (none has a balloon)."""
    return host - HOST_MEMORY_RESERVE_GIB * GIB - sum(others.values())


def memory_refusal(memory_gib: int, host: int, others: Dict[str, int]) -> Optional[str]:
    """Why a new domain of `memory_gib` does not fit beside the active `others`
    on a host of `host` bytes, or None when it does."""
    room = memory_room(host, others)
    if memory_gib * GIB <= room:
        return None
    ways = []
    if room >= MIN_MEMORY_GIB * GIB:
        ways.append("ask for at most --memory %d" % (room // GIB))
    if others:
        ways.append("stop another domain")
    if not ways:
        ways.append("that is under the %d GiB a box needs" % MIN_MEMORY_GIB)
    return (
        "--memory %d does not fit on this host: %s reads MemTotal %s, the host "
        "keeps %d GiB for itself, and the other active domains may take %s (%s), "
        "which leaves %s for this box — %s"
        % (
            memory_gib,
            libvirt_host.MEMINFO_FILE,
            gib_text(host),
            HOST_MEMORY_RESERVE_GIB,
            gib_text(sum(others.values())),
            ", ".join(
                "%s %s" % (name, gib_text(others[name])) for name in sorted(others)
            )
            or "none",
            gib_text(max(room, 0)),
            " or ".join(ways),
        )
    )


def state_hint(domain_state: str) -> Optional[str]:
    """The full virsh command that brings the domain back from `domain_state`,
    or None when yo knows none."""
    remedy = STATE_REMEDIES.get(domain_state)
    if remedy is None:
        return None
    verb, command = remedy
    return "%s it with `virsh -c %s %s %s`" % (
        verb,
        LIBVIRT_URI,
        command,
        domain_name(),
    )


def virsh_removal(what: str, *args: str) -> Removal:
    def remove() -> None:
        libvirt_host.virsh(*args)

    return Removal(what, remove)


def domain_roots() -> List[ET.Element]:
    """The XML of every domain on the system connection, yolobox's or not.

    A domain undefined between the listing and its dumpxml is not counted; any
    other failure is an error. A foreign domain's XML is read leniently by
    whoever uses it, so one odd domain of another tool cannot stop yo.
    """
    roots = []
    for name in domain_names():
        dump = libvirt_host.virsh("dumpxml", name, check=False)
        if dump.returncode != 0:
            raise_unless_undefined("dumpxml", name, dump)
            continue
        roots.append(libvirt_host.domain_root(dump.stdout))
    return roots


def volume_names() -> List[str]:
    rows = libvirt_host.virsh("vol-list", "--pool", LIBVIRT_POOL).stdout.splitlines()
    separators = [number for number, row in enumerate(rows) if row.startswith("---")]
    if not separators:
        raise YoError("virsh vol-list printed no table: %s" % "\n".join(rows))
    return [row.split()[0] for row in rows[separators[0] + 1 :] if row.strip()]


class LibvirtDriver:
    """Linux: one libvirt domain per machine, on the system connection."""

    name = "libvirt"
    instances_dir = ".local/state/yolobox/instances"
    hints = DriverHints(
        keepalive_name="ssh master",
        login_fix="1Password running and unlocked, its prompt approved",
        force_stop_command="virsh -c %s destroy {vm_name}" % LIBVIRT_URI,
        # `virsh console` keeps no history and the initrd writes to tty0, which
        # no graphics show, so the domain's state is what the host can show.
        where_to_look="`virsh -c %s domstate --reason {vm_name}` says why (a guest "
        "that went off by itself was possibly killed by the host's OOM killer: "
        "check `journalctl -k -g oom`); a domain that runs while nothing answers "
        "never brought its initrd sshd up, which a missing or unreadable cidata "
        "disk ({vm_name}-cidata.iso) causes; then run this command again" % LIBVIRT_URI,
    )

    def require_tool(self) -> None:
        libvirt_host.check_libvirt_host()

    def op_sock(self) -> str:
        return os.path.join(state.home(), ".1password/agent.sock")

    def ssh_facts(self) -> SshFacts:
        # Both accounts log in with the operator's key (the agent account's
        # AuthorizedKeysCommand serves the operator's keys), and so does root
        # in the initrd; 1Password's agent holds it, the file is its public half.
        return SshFacts(
            self.ssh_port(),
            release.host_user(),
            os.path.join(state.instance_dir(), "operator.pub"),
            self.op_sock(),
        )

    def domain_exists(self) -> bool:
        """Whether this machine's domain is defined. A domain of that name that
        yo did not define for this machine (no yolobox:instance tag, or another
        instance's) is an error, never "exists": every command reaches the
        domain through here, so yo never stops, resizes or destroys a domain
        of another tool that happens to carry a box's name."""
        name = domain_name()
        if name not in domain_names():
            return False
        dump = libvirt_host.virsh("dumpxml", name, check=False)
        if dump.returncode != 0:
            raise_unless_undefined("dumpxml", name, dump)
            return False
        marker = libvirt_host.domain_marker(libvirt_host.domain_root(dump.stdout))
        if marker != state.box.name:
            raise YoError(
                "domain %s on %s is not %s's: yo defines a domain with a "
                "yolobox:instance tag holding the machine's name, and this one "
                "%s. yo never touches a domain it did not define — rename or "
                "remove it with virsh, or pick another machine name (-m)"
                % (
                    name,
                    LIBVIRT_URI,
                    state.box.name,
                    "has none" if marker is None else "says %r" % marker,
                )
            )
        return True

    def require_domain(self) -> None:
        if not self.domain_exists():
            raise YoError(
                "domain %s does not exist — create it with: %s"
                % (domain_name(), state.yo_cmd("up"))
            )

    def ssh_port(self) -> int:
        self.require_domain()
        return self.domain_facts().ssh_port

    def domain_facts(self) -> DomainFacts:
        """The facts the existing domain was defined with. Without them the
        domain cannot be defined again, so it is only good for a destroy."""
        facts = read_domain_facts(state.instance_dir())
        if facts is None:
            raise YoError(
                "domain %s exists but %s holds no record of its uuid, port and "
                "size, so yo cannot define it again — %s"
                % (
                    domain_name(),
                    os.path.join(state.instance_dir(), DOMAIN_FACTS_NAME),
                    self.recreate_command(NO_SIZE),
                )
            )
        return facts

    def allocate_host_port(self) -> int:
        # Every domain's forwards count, not only the boxes': a shut-off domain
        # of another tool holds no socket, so only its XML shows its port.
        taken = [
            port
            for root in domain_roots()
            for port in libvirt_host.forwarded_host_ports(root)
        ]
        return lowest_free_port(
            libvirt_host.read_port_window(), taken, loopback_port_bound
        )

    @contextlib.contextmanager
    def port_lock(self) -> Iterator[None]:
        """Hold the host-wide lock that makes picking a port and taking it one step.

        Two `yo` processes would otherwise both pick the lowest free port, and
        one of the two forwards would fail or, worse, land on the other's port.
        """
        directory = os.path.join(state.home(), self.instances_dir)
        with common.held_flock(os.path.join(directory, PORT_LOCK_NAME), wait=True):
            yield

    def forward_port(self, guest_port: int) -> int:
        forwards.ensure_session()
        with self.port_lock():
            host_port = self.allocate_host_port()
            proc.run(
                sshx.ssh_argv(
                    OPERATOR,
                    "-O",
                    "forward",
                    "-L",
                    "%s:%d:%s:%d" % (LOOPBACK, host_port, LOOPBACK, guest_port),
                    state.box.vm_host,
                    mux=True,
                ),
                check=True,
            )
        return host_port

    def keepalive_pid(self) -> Optional[int]:
        check = proc.run(
            sshx.ssh_argv(OPERATOR, "-O", "check", state.box.vm_host, mux=True)
        )
        found = MASTER_PID_RE.search(check.stderr + check.stdout)
        return aws.checked_pid(int(found.group(1))) if found else None

    def vm_status(self) -> Optional[VmStatus]:
        if not self.domain_exists():
            return None
        domain_state = self.domain_state()
        if domain_state in RUNNING_STATES:
            return VmStatus(VmState.RUNNING, domain_state)
        if domain_state == STATE_OFF:
            return VmStatus(VmState.OFF, domain_state)
        return VmStatus(VmState.OTHER, domain_state)

    def domain_state(self) -> str:
        """virsh's own word for the state: "running", "in shutdown", "shut off"..."""
        return libvirt_host.virsh("domstate", domain_name()).stdout.strip()

    def memory_fit_refusal(self, memory_gib: int) -> Optional[str]:
        """Why a domain of `memory_gib` does not fit beside the active ones."""
        return memory_refusal(
            memory_gib, libvirt_host.host_memory_bytes(), active_domains_memory()
        )

    def creation_refusal(self, size: Size) -> Optional[str]:
        # Explicit values only: the default must not refuse a 16 GiB host, whose
        # MemTotal is a little under 16 GiB.
        if size.memory_gib is not None:
            refusal = self.memory_fit_refusal(size.memory_gib)
            if refusal is not None:
                return refusal
        # start() adopts a finished volume as it is, so a --disk it would not get
        # is refused. A domain removed with `virsh undefine` leaves its volume.
        if size.disk_gib is None or not self.volume_exists(disk_volume()):
            return None
        capacity = self.volume_sizes()["Capacity"]
        if not self.disk_volume_finished(capacity) or capacity == size.disk_gib * GIB:
            return None
        if size.disk_gib * GIB > capacity:
            way = (
                "drop --disk to use that disk, then grow it, keeping what is on it: %s"
                % (state.yo_cmd("disk-grow", str(size.disk_gib)))
            )
        else:
            way = (
                "drop --disk to keep that disk (a disk cannot shrink), or delete it, "
                "and everything on it, to build a smaller one: virsh -c %s vol-delete "
                "--pool %s %s" % (LIBVIRT_URI, LIBVIRT_POOL, disk_volume())
            )
        return (
            "volume %s already holds a finished %g GiB disk, which yo would use as "
            "it is, not at the --disk %d asked for — %s"
            % (disk_volume(), capacity / GIB, size.disk_gib, way)
        )

    def disk_gib(self) -> int:
        self.require_domain()
        return driver.exact_gib(self.volume_sizes()["Capacity"], "disk")

    def verify_initrd_listener(self, port: int) -> None:
        # No listener at all is fine: ssh finds nothing and the passphrase goes
        # nowhere. Any listener on the port must be the domain's passt.
        listing = proc.run(["ss", "-ltnHe", "sport = :%d" % port], check=True).stdout
        uid, _ = libvirt_host.vm_account()
        strangers = [
            owner for owner in libvirt_host.listener_uids(listing) if owner != uid
        ]
        if strangers:
            raise YoError(
                "%s:%d is held by uid %s, not by the account %s (uid %d) that runs "
                "the box's passt, so the disk passphrase is not sent there. Find "
                "the holder with `ss -ltnp 'sport = :%d'`, stop it, and run %s again"
                % (
                    LOOPBACK,
                    port,
                    ", ".join(map(str, strangers)),
                    VM_USER,
                    uid,
                    port,
                    state.yo_cmd("up"),
                )
            )

    def operator_key(self) -> bytes:
        sock_path = self.op_sock()
        dead = (
            "1Password's SSH agent at %s is not running (or locked) — start or "
            "unlock 1Password; its %s key logs in to the box"
            % (sock_path, OPERATOR_KEY_ITEM)
        )
        if not os.path.exists(sock_path):
            raise YoError(dead)
        try:
            identities = agent.ssh_agent_identities(sock_path)
        except YoError as failure:
            raise YoError("%s (%s)" % (dead, failure.message))
        # An agent that lists no key at all cannot serve the login now, which
        # is the shape a locked or half-started 1Password may take; one that
        # lists others but not this key gets unique_key_titled's own message.
        if not identities:
            raise YoError(dead)
        return agent.unique_key_titled(identities, OPERATOR_KEY_ITEM)

    def volume_exists(self, volume: str) -> bool:
        return volume in volume_names()

    def create_volume(self, volume: str, size: int, fmt: str, source: str) -> None:
        """A pool volume holding the file `source`; never left half-filled.

        Not even by Ctrl-C: whatever ends the upload, the partial volume goes.
        """
        libvirt_host.virsh(
            "vol-create-as", LIBVIRT_POOL, volume, str(size), "--format", fmt
        )
        try:
            libvirt_host.virsh("vol-upload", "--pool", LIBVIRT_POOL, volume, source)
        except BaseException as failure:
            cleanup = libvirt_host.virsh(
                "vol-delete", "--pool", LIBVIRT_POOL, volume, check=False
            )
            removal = (
                ""
                if cleanup.returncode == 0
                else "removing the partial volume failed too: %s"
                % cleanup.stderr.strip()
            )
            if not isinstance(failure, YoError):
                if removal:
                    ui.err("yo: " + removal)
                raise
            raise YoError(
                (failure.message or "") + ("; " + removal if removal else ""),
                failure.code,
            )

    def disk_volume_finished(self, capacity: int) -> bool:
        """Whether the existing disk volume, of `capacity` bytes, was uploaded
        and resized.

        The volume is created at the seed's 4.7 GiB, and only the last step,
        the resize after a successful upload, takes it to `MIN_DISK_GIB` or
        more, the floor no box is created below. A smaller volume is one an
        earlier run did not finish (a Ctrl-C, a failed upload or resize).
        """
        return capacity >= MIN_DISK_GIB * GIB

    def ensure_disk_volume(self, disk_gib: int) -> None:
        """Make the disk volume exist, uploaded and resized to `disk_gib`, or fail.

        A finished volume is adopted as it is; an unfinished one is rebuilt.
        """
        if self.volume_exists(disk_volume()):
            if self.disk_volume_finished(self.volume_sizes()["Capacity"]):
                return
            ui.err(
                "yo: volume %s was never finished; deleting it to build it again"
                % disk_volume()
            )
            libvirt_host.virsh("vol-delete", "--pool", LIBVIRT_POOL, disk_volume())
        seed = ensure_seed()
        self.create_volume(disk_volume(), file_size(seed), "qcow2", seed)
        libvirt_host.virsh(
            "vol-resize", "--pool", LIBVIRT_POOL, disk_volume(), "%dG" % disk_gib
        )

    def sync_cidata(self, files: Dict[str, str]) -> None:
        """Make the pool's cidata volume carry `files`; rebuilt only on change.

        The staged copy in the instance dir is the cache key: what the volume
        was last built from. It is rewritten only after the upload succeeded,
        so a failed rebuild, or a rewrite cut short, leaves a key that does not
        match and the next run builds again.
        """
        staging = os.path.join(state.instance_dir(), "cidata")
        staged: Dict[str, str] = {}
        for name in CIDATA_FILES:
            text = proc.read_text_file(
                os.path.join(staging, name), errors="surrogateescape"
            )
            if text is None:
                break
            staged[name] = text
        if staged == files and self.volume_exists(cidata_volume()):
            return
        # The new ISO's tree and the ISO itself, side by side: xorriso packs
        # the tree, so the ISO cannot be written inside it.
        work = staging + ".new"
        tree = os.path.join(work, "tree")
        iso = os.path.join(work, "cidata.iso")
        proc.delete_tree(work)
        try:
            proc.private_dir(tree)
            for name, text in files.items():
                proc.atomic_write(os.path.join(tree, name), text, FILE_MODE_READABLE)
            proc.run(
                ["xorriso", "-as", "mkisofs", "-V", "cidata", "-J", "-R", "-o", iso]
                + [tree],
                check=True,
            )
            if self.volume_exists(cidata_volume()):
                libvirt_host.virsh(
                    "vol-delete", "--pool", LIBVIRT_POOL, cidata_volume()
                )
            self.create_volume(cidata_volume(), file_size(iso), "raw", iso)
            proc.delete_tree(staging)
            proc.private_dir(staging)
            for name, text in files.items():
                proc.atomic_write(os.path.join(staging, name), text, FILE_MODE_READABLE)
        finally:
            proc.delete_tree(work)

    def create_domain(self, memory_gib: int, cpus: int) -> DomainFacts:
        """Record and define the domain on the lowest free host port."""
        with self.port_lock():
            facts = DomainFacts(
                str(uuid.uuid4()), self.allocate_host_port(), memory_gib, cpus
            )
            write_domain_facts(state.instance_dir(), facts)
            self.define_domain(facts)
        return facts

    def define_domain(self, facts: DomainFacts) -> None:
        """`virsh define` the template filled in with `facts`: a new domain, or
        the stopped one again, which libvirt accepts under the same uuid."""
        uid, gid = libvirt_host.vm_account()
        path = os.path.join(state.instance_dir(), "domain.xml")
        proc.atomic_write(
            path, domain_xml(state.box.name, facts, uid, gid), FILE_MODE_READABLE
        )
        try:
            proc.checked(
                libvirt_host.virsh("define", path, check=False),
                "virsh define %s" % path,
            )
        except YoError as refused:
            raise YoError("%s — %s" % (refused.message, self.recreate_command(NO_SIZE)))

    def recreate_command(self, size: Size) -> str:
        return state.recreate_chain(flags=size.flags())

    def start(self, passphrase: bytes, new_box: Optional[NewBox]) -> None:
        public_key = public_key_line(self.operator_key())
        files = cidata_files(release.host_user(), public_key, state.box.name)
        proc.private_dir(state.instance_dir())
        # 0600 though it is public: when the agent is gone, ssh tries the
        # IdentityFile as a private key and warns UNPROTECTED PRIVATE KEY FILE
        # on any other mode, which misnames the failure.
        proc.atomic_write(
            os.path.join(state.instance_dir(), "operator.pub"),
            public_key + "\n",
            FILE_MODE_PRIVATE,
        )
        created = None if new_box is None else domain_size(new_box)
        if created is not None:
            self.ensure_disk_volume(created.disk_gib)
        self.sync_cidata(files)
        # The port is chosen last: the volume upload above is slow, and the port
        # lock is held from choosing until the domain claims the port.
        if created is not None:
            facts = self.create_domain(created.memory_gib, created.cpus)
        else:
            facts = self.domain_facts()
            # A stopped domain is always what this yo's template makes; a
            # domain that is up keeps the definition it runs.
            if self.domain_state() == STATE_OFF:
                self.define_domain(facts)
        # Only a start needs passt to bind its socket, so only a start is
        # refused for the directory's ACL; down, ssh and status work without it.
        gap = libvirt_host.passt_dir_gap()
        if gap is not None:
            raise YoError(gap)
        self.start_domain()
        # libvirt starts no foreground process: the domain is Running as soon as
        # `virsh start` returns, and unlock_disk judges it by its state alone.
        unlock.unlock_disk(passphrase)
        unlock.wait_for_operator()

    def start_domain(self) -> None:
        """`virsh start`, tried once more when passt could not bind its socket.

        Another passt domain starting between yo's ACL check and this start
        recomputes the directory's mask; the hook restores it at that domain's
        `started begin`. So
        the ACL is read again: short, and the start is refused with the remedy;
        restored, and the start is tried once more, whose failure is final.
        """
        started = libvirt_host.virsh("start", domain_name(), check=False)
        if started.returncode == 0:
            return
        if PASST_BIND_DENIED not in (started.stderr or ""):
            proc.checked(started, "virsh start %s" % domain_name())
        gap = libvirt_host.passt_dir_gap()
        if gap is not None:
            raise YoError(gap)
        libvirt_host.virsh("start", domain_name())

    def stop(self) -> None:
        if not self.domain_exists():
            raise YoError("domain %s does not exist" % domain_name())
        domain_state = self.domain_state()
        if domain_state == STATE_OFF:
            return
        self.require_stoppable(domain_state)
        # One watcher, armed before the shutdown is asked for and never re-armed:
        # a re-armed one can register after the stop it should have seen. Its
        # events only wake the loop, which judges by `domstate`, so a stop that
        # lands before the watcher registers is still seen by the first read.
        watcher = libvirt_host.spawn_virsh(
            "event",
            domain_name(),
            "lifecycle",
            "--loop",
            "--timeout",
            str(LIBVIRT_STOP_TIMEOUT),
        )
        try:
            if domain_state != STATE_SHUTTING_DOWN:
                libvirt_host.virsh("shutdown", domain_name())
            printed: List[str] = []
            domain_state = self.domain_state()
            while domain_state != STATE_OFF:
                self.require_stoppable(domain_state)
                line = watcher.stdout.readline()
                if line == "":
                    # The watcher is over, but the domain may have gone off
                    # since the last read: the state is the judge, not the stream.
                    domain_state = self.domain_state()
                    if domain_state == STATE_OFF:
                        return
                    raise self.event_stream_ended(watcher, printed)
                printed.append(line)
                domain_state = self.domain_state()
        finally:
            if watcher.poll() is None:
                watcher.terminate()
            watcher.communicate()

    def force_stop(self) -> None:
        self.require_domain()
        if self.domain_state() != STATE_OFF:
            libvirt_host.virsh("destroy", domain_name())

    def undefine_domain(self) -> None:
        """Remove the definition, after powering the domain off if it is active now.

        The state is read here, not when the steps were listed: a `yo up` can
        start the domain in between, and undefining a running one leaves qemu
        on the volume the next step deletes."""
        self.force_stop()
        libvirt_host.virsh("undefine", "--nvram", domain_name())

    def require_stoppable(self, domain_state: str) -> None:
        """Raise YoError for a state in which a guest shutdown will never finish.

        `yo down` reaches this without knowing whether the box is in its initrd,
        maybe encrypting its disk, so it names no forced stop: only a pending
        passphrase ask proves one safe (vm.cmd_down)."""
        if domain_state in STATES_STOPPING:
            return
        cause = ""
        if domain_state == "paused":
            # qemu pauses a guest whose disk image cannot grow (werror=enospc),
            # which the first boot's in-place encryption can cause. The cause
            # is part of the reason, so every caller, bootstrap's included,
            # says the host disk may be full before anyone resumes it.
            cause = (
                ". A domain pauses when its disk image cannot grow because the "
                "host's disk is full (check `virsh -c %s domblkerror %s`); free "
                "space on the host first" % (LIBVIRT_URI, domain_name())
            )
        no_force = "Do not force it off: it may be in its initrd, encrypting its disk"
        remedy = state_hint(domain_state)
        if remedy is None:
            # Naming `yo down` here would loop: it meets this same refusal.
            advice = (
                "yo does not know how to leave that state; `virsh -c %s domstate "
                "--reason %s` shows why. %s" % (LIBVIRT_URI, domain_name(), no_force)
            )
        else:
            advice = "%s%s, then run %s again. %s" % (
                remedy[:1].upper(),
                remedy[1:],
                state.yo_cmd("down"),
                no_force,
            )
        raise StopRefused(
            "domain %s is %r, so it will not shut down by itself%s"
            % (domain_name(), domain_state, cause),
            advice,
        )

    def event_stream_ended(
        self, watcher: subprocess.Popen, printed: Sequence[str]
    ) -> YoError:
        """Why `virsh event` ended before the domain stopped: its timeout, or a failure."""
        stderr = watcher.stderr.read()
        code = watcher.wait()
        if code == 1 and EVENT_TIMEOUT_LINE in (line.strip() for line in printed):
            return StopRefused(
                "%s did not shut down within %d s"
                % (domain_name(), LIBVIRT_STOP_TIMEOUT),
                "It may be in its initrd, which ignores the shutdown request, or busy "
                "shutting down. Run %s again: in the initrd it looks inside and "
                "powers off only when that is safe. If it still stays 'in shutdown', "
                "%s shows its state; do not force it off while its disk may be "
                "encrypting" % (state.yo_cmd("down"), state.yo_cmd("status")),
            )
        return YoError(proc.failed_message("virsh event", code, stderr.strip()))

    def close_master(self) -> None:
        sshx.stop_master(
            OPERATOR,
            "%s's ssh master is still running after `ssh -O exit`" % state.box.name,
        )

    def removals(self) -> List[Removal]:
        steps: List[Removal] = []
        if sshx.master_alive(OPERATOR):
            steps.append(Removal("close the operator's ssh master", self.close_master))
        if self.domain_exists():
            if self.domain_state() != STATE_OFF:
                steps.append(
                    Removal("power off domain %s" % domain_name(), self.force_stop)
                )
            steps.append(
                Removal(
                    "undefine domain %s with its nvram" % domain_name(),
                    self.undefine_domain,
                )
            )
        volumes = volume_names()
        for volume in (disk_volume(), cidata_volume()):
            if volume in volumes:
                steps.append(
                    virsh_removal(
                        "delete volume %s" % volume,
                        "vol-delete",
                        "--pool",
                        LIBVIRT_POOL,
                        volume,
                    )
                )
        if os.path.exists(state.instance_dir()):
            steps.append(state.tree_removal(state.instance_dir()))
        return steps

    def show_status(self) -> Optional[str]:
        shown = libvirt_host.virsh("dominfo", domain_name(), check=False, capture=False)
        if shown.returncode == 0:
            return None
        return "libvirt: %s, its own message is above" % proc.failed_message(
            "virsh -c %s dominfo %s" % (LIBVIRT_URI, domain_name()),
            shown.returncode,
            None,
        )

    def volume_sizes(self) -> Dict[str, int]:
        """Capacity and Allocation of the disk volume, in bytes."""
        info = libvirt_host.virsh(
            "vol-info", "--bytes", "--pool", LIBVIRT_POOL, disk_volume()
        )
        sizes = {name: int(size) for name, size in SIZE_LINE_RE.findall(info.stdout)}
        if set(sizes) != {"Capacity", "Allocation"}:
            raise YoError("virsh vol-info printed no sizes: %s" % info.stdout.strip())
        return sizes

    def disk_image(self) -> Optional[Tuple[str, int]]:
        if not self.volume_exists(disk_volume()):
            return None
        allocation = self.volume_sizes()["Allocation"]
        return "pool %s volume %s" % (LIBVIRT_POOL, disk_volume()), allocation // KIB

    def grow_disk(self, wanted_gib: int) -> None:
        # qemu holds the image of a running domain open, so only a stopped one
        # is resized.
        domain_state = self.domain_state()
        if domain_state != STATE_OFF:
            raise YoError(
                "%s is %s, and a disk is grown only while the domain is shut off"
                % (state.box.name, domain_state)
            )
        ui.out("libvirt: raising %s's disk to %dGiB" % (state.box.name, wanted_gib))
        libvirt_host.virsh(
            "vol-resize", "--pool", LIBVIRT_POOL, disk_volume(), "%dG" % wanted_gib
        )


LIBVIRT = LibvirtDriver()
