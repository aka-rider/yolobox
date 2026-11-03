"""The libvirt driver: the domain, its network and storage, and the host checks."""

from __future__ import annotations

import base64
import contextlib
import errno
import fcntl
import hashlib
import os
import re
import shutil
import socket
import string
import subprocess
import tempfile
import urllib.error
import urllib.request
from typing import (
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
)

from . import (
    agent,
    aws,
    libvirt_host,
    proc,
    release,
    sshconfig,
    sshx,
    state,
    ui,
    unlock,
)
from .config import (
    AGENT_USER,
    FILE_MODE_PRIVATE,
    FILE_MODE_READABLE,
    GIB,
    KIB,
    LOOPBACK,
    MIN_DISK_GIB,
    SCRIPT_DIR,
)
from .driver import Size
from .libvirt_host import GUEST_SSH_PORT, LIBVIRT_URI, VM_USER, DomainInfo
from .sshx import OPERATOR
from .ui import YoError


# The libvirt driver: Linux hosts. One KVM domain per machine on the system
# connection, with passt for the network and an SELinux/DAC label from the host
# layer (see linux/host-setup.md). The host's preflight and the domain XML policy
# are in libvirt_host.py. Pure helpers first, then the driver, which calls them
# and virsh by name at call time.
LIBVIRT_POOL = "default"
# The 1Password item whose public key is the operator's login, in cidata.
OPERATOR_KEY_ITEM = "yolobox-operator"
# lima-init's cidata account: the operator inside the guest (nix/base.nix).
OPERATOR_GUEST_UID = 501
OPERATOR_ACCOUNT_RE = re.compile(r"[a-z_][a-z0-9_-]*")
# A box's size when `yo up` is given no --memory or --disk (domain_size).
LIBVIRT_MEMORY_GIB = 16
LIBVIRT_DISK_GIB = 100
LIBVIRT_STOP_TIMEOUT = 300
# What `virsh event --timeout` prints, then exits 1, when no more events came.
EVENT_TIMEOUT_LINE = "event loop timed out"
# virsh domstate words. "in shutdown" is a guest still going down, and the only
# word besides "shut off" that a stop waits through ("blocked" is a running guest
# idling on I/O).
STATE_OFF = "shut off"
STATE_SHUTTING_DOWN = "in shutdown"
STATES_STOPPING = ("running", "blocked", STATE_SHUTTING_DOWN)
STATE_HINTS = {
    "paused": "resume it with `virsh resume`",
    "pmsuspended": "wake it with `virsh dompmwakeup`",
}
PORT_LOCK_NAME = ".port.lock"

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


def domain_name() -> str:
    return state.box.vm_name


def disk_volume() -> str:
    return state.box.vm_name + ".qcow2"


def cidata_volume() -> str:
    return state.box.vm_name + "-cidata.iso"


def spawn_virsh(*args: str) -> subprocess.Popen:
    return proc.spawn(
        ["virsh", "-c", LIBVIRT_URI, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def loopback_port_bound(port: int) -> bool:
    """Whether something already holds 127.0.0.1:port (a bind probe)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((LOOPBACK, port))
        except OSError as failure:
            if failure.errno == errno.EADDRINUSE:
                return True
            raise YoError("cannot probe %s:%d: %s" % (LOOPBACK, port, failure.strerror))
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


def domain_size(size: Size) -> Tuple[int, int]:
    """The (memory, disk) GiB of a box yo creates: what was asked, else the defaults."""
    return (
        LIBVIRT_MEMORY_GIB if size.memory_gib is None else size.memory_gib,
        LIBVIRT_DISK_GIB if size.disk_gib is None else size.disk_gib,
    )


def domain_xml(
    instance: str, ssh_port: int, uid: int, gid: int, memory_gib: int
) -> str:
    template = string.Template(
        (SCRIPT_DIR / "libvirt" / "domain.xml").read_text(encoding="utf-8")
    )
    return template.substitute(
        name=instance, ssh_port=ssh_port, uid=uid, gid=gid, memory_gib=memory_gib
    )


def operator_account(username: str) -> str:
    """The guest account the cidata creates for the operator, checked."""
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


def public_key_line(blob: bytes) -> str:
    return "%s %s %s" % (
        agent.key_type(blob),
        base64.b64encode(blob).decode("ascii"),
        OPERATOR_KEY_ITEM,
    )


def cidata_files(username: str, public_key: str, instance: str) -> Dict[str, str]:
    """The files of the cidata ISO lima-init and the initrd read, by name."""
    account = operator_account(username)
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
            "LIMA_CIDATA_HOME=/home/%s.guest\n"
            "LIMA_CIDATA_IID=yolobox-%s\n"
            % (account, OPERATOR_GUEST_UID, account, instance)
        ),
        "etc_environment": "",
    }


def libvirt_ssh_config(
    host: str, port: int, user: str, identity_agent: str, instance_dir: str
) -> str:
    """The instance's ssh.config, in the shape lima writes for its own."""
    return "\n".join(
        [
            "Host %s" % host,
            "  Hostname %s" % LOOPBACK,
            "  Port %d" % port,
            "  User %s" % user,
            '  IdentityAgent "%s"' % identity_agent,
            '  IdentityFile "%s"' % os.path.join(instance_dir, "operator.pub"),
            "  IdentitiesOnly yes",
            "  BatchMode yes",
            "  PreferredAuthentications publickey",
            "  PasswordAuthentication no",
            "  KbdInteractiveAuthentication no",
            "  GSSAPIAuthentication no",
            "  AddKeysToAgent no",
            "  ForwardAgent no",
            "  Compression no",
            "  ControlMaster auto",
            '  ControlPath "%s"' % os.path.join(instance_dir, "ssh.sock"),
            "  ControlPersist yes",
            "  StrictHostKeyChecking no",
            "  UserKnownHostsFile /dev/null",
            "  NoHostAuthenticationForLocalhost yes",
            "",
        ]
    )


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
        if os.path.exists(part):
            os.unlink(part)


def ensure_seed() -> str:
    """The pinned seed image, downloaded once and verified on every use."""
    cache = os.path.join(state.home(), ".cache", "yolobox")
    os.makedirs(cache, exist_ok=True)
    path = os.path.join(cache, SEED_NAME)
    if os.path.exists(path):
        if file_sha512(path) == SEED_SHA512:
            return path
        ui.err("yo: %s fails its sha512 check; downloading it again" % path)
        os.unlink(path)
    ui.err("yo: downloading the seed image %s" % SEED_URL)
    download_seed(path)
    return path


def domain_names() -> List[str]:
    listing = libvirt_host.virsh("list", "--all", "--name").stdout.splitlines()
    return [line.strip() for line in listing if line.strip()]


def yolobox_domains() -> List[DomainInfo]:
    """Every domain on the system connection that carries yolobox's metadata.

    A foreign domain is only checked for the tag: its XML is never read strictly,
    so one odd domain of another tool cannot stop yo.
    """
    roots = [
        libvirt_host.domain_root(libvirt_host.virsh("dumpxml", name).stdout)
        for name in domain_names()
    ]
    return [
        libvirt_host.domain_info(root)
        for root in roots
        if libvirt_host.domain_marker(root) is not None
    ]


def volume_names() -> List[str]:
    rows = libvirt_host.virsh("vol-list", "--pool", LIBVIRT_POOL).stdout.splitlines()
    separators = [number for number, row in enumerate(rows) if row.startswith("---")]
    if not separators:
        raise YoError("virsh vol-list printed no table: %s" % "\n".join(rows))
    return [row.split()[0] for row in rows[separators[0] + 1 :] if row.strip()]


class LibvirtDriver:
    """Linux: one libvirt domain per machine, on the system connection."""

    host_os = "linux"
    name = "libvirt"
    instances_dir = ".local/state/yolobox/instances"
    start_command = "virsh start"
    # A domain that is not what the template made may hold a path to the host.
    gaps_are_fatal = True

    @property
    def initrd_identity(self) -> str:
        return "%s/%s/operator.pub" % (self.instances_dir, state.box.name)

    def initrd_identity_agent(self) -> str:
        return self.op_sock()

    def require_tool(self) -> None:
        libvirt_host.check_libvirt_host()

    def op_sock(self) -> str:
        return os.path.join(state.home(), ".1password/agent.sock")

    def domain_exists(self) -> bool:
        return domain_name() in domain_names()

    def require_domain(self) -> None:
        if not self.domain_exists():
            raise YoError(
                "domain %s does not exist — create it with: %s"
                % (domain_name(), state.yo_cmd("up"))
            )

    def ssh_port(self) -> int:
        self.require_domain()
        port = libvirt_host.parse_domain(
            libvirt_host.virsh("dumpxml", domain_name()).stdout
        ).ssh_port
        if port is None:
            raise YoError(
                "domain %s forwards no host port to guest port %d"
                % (domain_name(), GUEST_SSH_PORT)
            )
        return port

    def allocate_host_port(self) -> int:
        taken = [port for info in yolobox_domains() for port in info.forward_ports]
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
        os.makedirs(directory, exist_ok=True)
        fd = os.open(
            os.path.join(directory, PORT_LOCK_NAME),
            os.O_RDWR | os.O_CREAT,
            FILE_MODE_PRIVATE,
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing the descriptor drops the lock

    def forward_port(self, guest_port: int) -> int:
        self.ensure_master()
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

    def ensure_master(self) -> None:
        """Open the operator's multiplexed ssh connection, if it is not open."""
        sshx.ssh_run(OPERATOR, ["true"], mux=True, check=True)

    def keepalive_pid(self) -> Optional[int]:
        self.ensure_master()
        check = proc.run(
            sshx.ssh_argv(OPERATOR, "-O", "check", state.box.vm_host, mux=True)
        )
        found = MASTER_PID_RE.search(check.stderr + check.stdout)
        return aws.checked_pid(int(found.group(1))) if found else None

    def keepalive_pid_error(self) -> str:
        return state.not_up_hint(
            "cannot read the pid of %s's ssh master (ssh -O check)" % state.box.name
        )

    def vm_status(self) -> Optional[str]:
        if not self.domain_exists():
            return None
        domain_state = self.domain_state()
        return "Running" if domain_state == "running" else domain_state

    def domain_state(self) -> str:
        """virsh's own word for the state: "running", "in shutdown", "shut off"..."""
        return libvirt_host.virsh("domstate", domain_name()).stdout.strip()

    def creation_refusal(self, size: Size) -> Optional[str]:
        # Explicit values only: the default must not refuse a 16 GiB host, whose
        # MemTotal is a little under 16 GiB.
        if size.memory_gib is not None:
            host_bytes = libvirt_host.host_memory_bytes()
            if size.memory_gib * GIB > host_bytes:
                return (
                    "--memory %d is more than this host has: %s reads MemTotal "
                    "%.1f GiB"
                    % (size.memory_gib, libvirt_host.MEMINFO_FILE, host_bytes / GIB)
                )
        # start() adopts a finished volume as it is, so a --disk it would not get
        # is refused. A domain removed with `virsh undefine` leaves its volume.
        if size.disk_gib is None or not self.volume_exists(disk_volume()):
            return None
        capacity = self.volume_sizes()["Capacity"]
        if not self.disk_volume_finished(capacity) or capacity == size.disk_gib * GIB:
            return None
        return (
            "volume %s already holds a finished %g GiB disk, which yo would use as "
            "it is, not at the --disk %d asked for — drop --disk to keep that disk, "
            "or delete it, and everything on it, to build a new one: "
            "virsh -c %s vol-delete --pool %s %s"
            % (
                disk_volume(),
                capacity / GIB,
                size.disk_gib,
                LIBVIRT_URI,
                LIBVIRT_POOL,
                disk_volume(),
            )
        )

    def existing_size_refusal(self, size: Size) -> str:
        flags = " ".join(size.flags())
        if size.disk_gib is not None:
            return (
                "%s already exists, and its disk was sized when yo created it — drop "
                "%s, or delete the box, and everything on it is lost: %s, then run "
                "this command again" % (state.box.name, flags, self.delete_command())
            )
        return (
            "%s already exists, and its memory was set when yo defined its domain — "
            "drop %s, or define the domain again, keeping the disk: %s && virsh -c %s "
            "undefine --nvram %s, then run this command again, which adopts the disk"
            % (state.box.name, flags, state.yo_cmd("down"), LIBVIRT_URI, domain_name())
        )

    def host_gaps(self) -> List[str]:
        gap = libvirt_host.passt_dir_gap()
        return [gap] if gap else []

    def config_gaps(self) -> List[str]:
        if not self.domain_exists():
            return []
        uid, gid = libvirt_host.vm_account()
        return libvirt_host.domain_gaps(
            libvirt_host.parse_domain(
                libvirt_host.virsh("dumpxml", domain_name()).stdout
            ),
            state.box.name,
            uid,
            gid,
        )

    def config_gap_message(self, gaps: Sequence[str]) -> str:
        return (
            "domain %s breaks the box's guarantees (%s) — stop it with %s, remove its "
            "definition with `virsh -c %s undefine --nvram %s` (the disk volume "
            "stays), then run %s to define it again from libvirt/domain.xml"
            % (
                domain_name(),
                ", ".join(gaps),
                state.yo_cmd("down"),
                LIBVIRT_URI,
                domain_name(),
                state.yo_cmd("up"),
            )
        )

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
        if not os.path.exists(sock_path):
            raise YoError(
                "1Password is not running on this host (no socket at %s); it holds "
                "the %s key that logs in to the box" % (sock_path, OPERATOR_KEY_ITEM)
            )
        return agent.unique_key_titled(
            agent.ssh_agent_identities(sock_path), OPERATOR_KEY_ITEM
        )

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
        self.create_volume(disk_volume(), os.stat(seed).st_size, "qcow2", seed)
        libvirt_host.virsh(
            "vol-resize", "--pool", LIBVIRT_POOL, disk_volume(), "%dG" % disk_gib
        )

    def sync_cidata(self, files: Dict[str, str]) -> None:
        """Make the pool's cidata volume carry `files`; rebuilt only on change.

        The staged copy in the instance dir is the cache key: what the volume
        was last built from. It is replaced only after the upload succeeded, so
        a failed rebuild leaves the old key and the next run tries again.
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
        building = staging + ".new"
        iso = os.path.join(state.instance_dir(), "cidata.iso")
        if os.path.exists(building):
            shutil.rmtree(building)
        try:
            proc.private_dir(building)
            for name, text in files.items():
                proc.atomic_write(
                    os.path.join(building, name), text, FILE_MODE_READABLE
                )
            proc.run(
                ["xorriso", "-as", "mkisofs", "-V", "cidata", "-J", "-R", "-o", iso]
                + [building],
                check=True,
            )
            if self.volume_exists(cidata_volume()):
                libvirt_host.virsh(
                    "vol-delete", "--pool", LIBVIRT_POOL, cidata_volume()
                )
            self.create_volume(cidata_volume(), os.stat(iso).st_size, "raw", iso)
            if os.path.exists(staging):
                shutil.rmtree(staging)
            os.rename(building, staging)
        finally:
            if os.path.exists(iso):
                os.unlink(iso)
            if os.path.exists(building):
                shutil.rmtree(building)

    def write_ssh_config(self, port: int) -> None:
        wanted = libvirt_ssh_config(
            state.box.vm_host,
            port,
            release.host_user(),
            self.op_sock(),
            state.instance_dir(),
        )
        current = proc.read_text_file(
            state.ssh_cfg(), missing="", errors="surrogateescape"
        )
        if sshconfig.lima_part_of(current) != wanted:
            proc.atomic_write(state.ssh_cfg(), wanted, FILE_MODE_PRIVATE)

    def create_domain(self, memory_gib: int) -> int:
        """Define the domain on the lowest free host port; return that port."""
        uid, gid = libvirt_host.vm_account()
        with self.port_lock():
            port = self.allocate_host_port()
            path = os.path.join(state.instance_dir(), "domain.xml")
            proc.atomic_write(
                path,
                domain_xml(state.box.name, port, uid, gid, memory_gib),
                FILE_MODE_READABLE,
            )
            libvirt_host.virsh("define", path)
        return port

    def start(self, passphrase: bytes, size: Size) -> None:
        existing = self.domain_exists()
        if existing and size.asked:
            # The domain appeared after vm_up looked; it has a size already.
            raise YoError(self.existing_size_refusal(size))
        public_key = public_key_line(self.operator_key())
        files = cidata_files(release.host_user(), public_key, state.box.name)
        os.makedirs(os.path.dirname(state.instance_dir()), exist_ok=True)
        proc.private_dir(state.instance_dir())
        proc.atomic_write(
            os.path.join(state.instance_dir(), "operator.pub"),
            public_key + "\n",
            FILE_MODE_READABLE,
        )
        memory_gib, disk_gib = domain_size(size)
        if not existing:
            self.ensure_disk_volume(disk_gib)
        self.sync_cidata(files)
        # The port is chosen last: the volume upload above is slow, and the port
        # lock is held from choosing until the domain claims the port.
        port = self.ssh_port() if existing else self.create_domain(memory_gib)
        self.write_ssh_config(port)
        # Only a start needs passt to bind its socket, so only a start is
        # refused for the directory's ACL; down, ssh and status work without it.
        gap = libvirt_host.passt_dir_gap()
        if gap is not None:
            raise YoError(gap)
        libvirt_host.virsh("start", domain_name())
        # libvirt starts no foreground process: the domain is Running as soon as
        # `virsh start` returns, and unlock_disk judges it by its state alone.
        unlock.unlock_disk(passphrase)
        unlock.wait_for_operator()

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
        watcher = spawn_virsh(
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

    def require_stoppable(self, domain_state: str) -> None:
        """Raise YoError for a state in which a guest shutdown will never finish."""
        if domain_state in STATES_STOPPING:
            return
        raise YoError(
            "domain %s is %r, so it will not shut down by itself — %s; or force "
            "it off with: virsh -c %s destroy %s"
            % (
                domain_name(),
                domain_state,
                STATE_HINTS.get(
                    domain_state, "its state is not one yo knows how to stop"
                ),
                LIBVIRT_URI,
                domain_name(),
            )
        )

    def event_stream_ended(
        self, watcher: subprocess.Popen, printed: Sequence[str]
    ) -> YoError:
        """Why `virsh event` ended before the domain stopped: its timeout, or a failure."""
        stderr = watcher.stderr.read()
        code = watcher.wait()
        if code == 1 and EVENT_TIMEOUT_LINE in (line.strip() for line in printed):
            return YoError(
                "%s did not shut down within %d s — force it off with: "
                "virsh -c %s destroy %s"
                % (domain_name(), LIBVIRT_STOP_TIMEOUT, LIBVIRT_URI, domain_name())
            )
        return YoError("virsh event failed (exit %d): %s" % (code, stderr.strip()))

    def stop_command(self) -> str:
        return state.yo_cmd("down")

    def stop_failed(self, detail: str) -> str:
        return "virsh shutdown failed: %s" % detail

    def stop_recovery(self) -> List[str]:
        return [
            "Recover with: virsh -c %s destroy %s (a hard power-off)"
            % (LIBVIRT_URI, domain_name()),
            "then start it again with: %s" % state.yo_cmd("up"),
        ]

    def delete_command(self) -> str:
        virsh_ = "virsh -c %s" % LIBVIRT_URI
        return " && ".join(
            [
                state.yo_cmd("down"),
                "%s undefine --nvram %s" % (virsh_, domain_name()),
                "%s vol-delete --pool %s %s" % (virsh_, LIBVIRT_POOL, disk_volume()),
                "%s vol-delete --pool %s %s" % (virsh_, LIBVIRT_POOL, cidata_volume()),
                "rm -r ~/%s/%s" % (self.instances_dir, state.box.name),
            ]
        )

    def list_command(self) -> str:
        return "virsh -c %s list --all" % LIBVIRT_URI

    def boot_log(self) -> str:
        return "virsh -c %s console %s" % (LIBVIRT_URI, domain_name())

    def show_status(self) -> None:
        libvirt_host.virsh("dominfo", domain_name(), check=False, capture=False)

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

    def disk_ceiling_gib(self) -> int:
        raise self.disk_grow_unsupported()

    def disk_grow_unsupported(self) -> YoError:
        return YoError("growing a libvirt box's disk is not implemented yet (TODO.md)")

    def grow_disk(self, current_gib: int, wanted_gib: int) -> None:
        raise self.disk_grow_unsupported()


LIBVIRT = LibvirtDriver()
