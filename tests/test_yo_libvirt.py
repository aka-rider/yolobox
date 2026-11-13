"""The libvirt driver: what yo does on a Linux host, with virsh faked.

The tests drive `yo` through its public functions and `main()`, on a copy of
the module that holds a LibvirtDriver. `FakeHost` stands in for virsh and the
other host tools: it keeps domains, volumes and states, answers the way virsh
prints, and records every argv so a test can check the order of the calls.

Tools whose absence skips tests: gawk, or nix to fetch it (the initrd's key
program).
"""

import argparse
import base64
import contextlib
import fcntl
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from shell_harness import require_tool
from test_yo import (
    ACCEPT_TIMEOUT_SECONDS,
    NOT_LISTENING,
    FakeHelper,
    FakeHome,
    box_lock_held_elsewhere,
    box_lock_is_held,
    guest_words,
    load_yo,
    locked,
    ssh_ends,
    ssh_log_of,
    unload_yo,
    refuse_tcp_to_the_host,
)


def setUpModule():
    refuse_tcp_to_the_host()


REPO = Path(__file__).resolve().parent.parent
# LibvirtCase.setUp patches `which` on the stdlib module itself, which yo shares,
# so a test that looks for a real tool needs the function as it was at import.
REAL_WHICH = shutil.which
FACTS = json.loads((REPO / "facts.json").read_text())
INITRD_SSH_BANNER_MARK = FACTS["initrd_ssh_banner_mark"]
VERSION_STAMP = FACTS["version_stamp"]
INITRD_KEYS_SCRIPT = REPO / "nix" / "luks" / "yolobox-initrd-authorized-keys.sh"

VM_UID, VM_GID = 990, 989
OPERATOR = "alice"
KEY_BLOB = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(32))
OTHER_BLOB = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(1, 33))
PUBLIC_KEY = "ssh-ed25519 %s yolobox-operator" % base64.b64encode(KEY_BLOB).decode()
EPHEMERAL_RANGE = "32768\t60999\n"
# Found before any test patches shutil.which, which LibvirtCase does.
READLINK = shutil.which("readlink")
BASH = shutil.which("bash")
GOOD_LABEL = b"system_u:object_r:net_conf_t:s0\0"
WRONG_LABEL = b"unconfined_u:object_r:systemd_resolved_var_run_t:s0\0"
FIRST_PORT = 61000
DAEMON_VERSION_TEXT = (
    "Compiled against library: libvirt 12.8.0\n"
    "Using library: libvirt 12.8.0\n"
    "Using API: QEMU 12.8.0\n"
    "Running hypervisor: QEMU 10.2.2\n"
    "Running against daemon: %s\n"
)
PASST_DIR = "/run/libvirt/qemu/passt"
HERDR = "/opt/herdr/bin/herdr"
HOST_MEMORY_GIB = 64
DOMAIN_CPUS = 8
# The virsh subcommands that only look.
READS = frozenset(
    {
        "list",
        "dumpxml",
        "domstate",
        "dominfo",
        "vol-list",
        "vol-info",
    }
)
# What ssh prints when nothing listens behind the box's port: a domain that is off.


def getfacl_listing(entry="rwx", mask="rwx", comment=True):
    """What `getfacl --absolute-names` prints for the passt directory."""
    effective = "\t#effective:%s" % "".join(
        letter if letter in (entry or "") and letter in mask else "-"
        for letter in "rwx"
    )
    lines = [
        "# file: run/libvirt/qemu/passt",
        "# owner: qemu",
        "# group: qemu",
        "user::rwx",
    ]
    if entry is not None:
        lines.append("user:yolobox-vm:%s%s" % (entry, effective if comment else ""))
    lines += [
        "group::r-x",
        "mask::%s" % mask,
        "other::r-x",
        "default:user::rwx",
        "default:user:yolobox-vm:rwx",
        "default:mask::r-x",
        "",
    ]
    return "\n".join(lines)


def load_libvirt_yo(instance="box"):
    """A copy of yo driving libvirt, as main() sets it up on a Linux host."""
    name = "yo_libvirt_%s" % instance.replace("-", "_")
    module = load_yo(name)
    module.config.HOST_PLATFORM = "linux"
    module.set_box(module.driver_for("linux"), instance)
    return module, name


def as_libvirt_prints(xml):
    """A domain's definition as virsh prints it: the memory always in KiB."""
    return re.sub(
        r"<memory unit='GiB'>(\d+)</memory>",
        lambda match: "<memory unit='KiB'>%d</memory>" % (int(match.group(1)) << 20),
        xml,
    )


def completed(stdout="", returncode=0, stderr=""):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


class FakeHost:
    """virsh, systemctl, xorriso and ssh, as far as the driver talks to them."""

    def __init__(self, box):
        self.box = box
        self.calls = []
        self.domains = {}  # name -> domain XML
        self.states = {}  # name -> domstate word
        self.volumes = {}  # name -> bytes uploaded
        self.fail = {}  # virsh subcommand -> stderr it fails with
        self.fail_once = {}  # the same, for the next call of the subcommand only
        self.interrupted = {}  # virsh subcommand -> the exception that ends it
        self.capacity = {}  # volume name -> bytes, as vol-create-as and vol-resize set
        self.iso_inputs = []  # (directory, file names) at each xorriso call
        self.master_output = ""
        self.master_running = True  # whether `ssh -O check` finds the master
        # The same for the agent account's master, which no session opens here.
        self.agent_master_running = False
        # A master that answers `ssh -O exit` and keeps running.
        self.master_survives_exit = False
        # What a session through that master gets: (code, stderr). A master
        # whose connection died still answers -O check, and its sessions fail.
        self.master_session = (0, "")
        self.forwarded = []  # the -O forward requests a live master accepted
        self.listeners = ""  # what `ss -ltnHe` prints
        self.acl = getfacl_listing()  # what `getfacl` prints for the passt dir
        self.shutdown_state = "in shutdown"  # the state `virsh shutdown` leaves
        self.event_script = ([], "hang")  # what the next `virsh event` prints
        self.off_at_eof = False  # the domain goes off as that stream ends
        self.watchers = []
        self.max_memory_kib = {}  # domain -> the Max memory `virsh dominfo` prints
        self.cpus = {}  # domain -> the CPU(s) `virsh dominfo` prints, default 8
        self.dominfo = {}  # domain -> what `virsh dominfo` prints instead
        # Runs as `virsh dominfo NAME` starts, given NAME: another tool acting.
        self.before_dominfo = lambda name: None
        # The same as `virsh dumpxml NAME` starts.
        self.before_dumpxml = lambda name: None
        # The guest's sshds, for the `ssh ... true` probes: argv -> (code, stderr).
        self.sshd = lambda argv: (0, "")
        # What the box's port sends before any login: the initrd's sshd,
        # unless a test says the port is silent ("") or another sshd's.
        self.banner = "SSH-2.0-OpenSSH_10.6 " + INITRD_SSH_BANNER_MARK
        # The initrd's yolobox-unlock session: (stdout lines, exit code, stderr).
        self.helper = None
        # What `herdr machine list --json` prints, entry by entry.
        self.herdr_machines = []
        self.herdr_remove_fails = False

    def subcommands(self):
        return [call[3] for call in self.calls if call[0] == "virsh" and len(call) > 3]

    def calls_of(self, subcommand):
        return [
            call
            for call in self.calls
            if call[:1] == ["virsh"] and call[3:4] == [subcommand]
        ]

    def run(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "virsh":
            result = self.virsh(argv[3], argv[4:])
        elif argv[0] == "xorriso":
            self.iso_inputs.append((argv[-1], sorted(os.listdir(argv[-1]))))
            Path(argv[argv.index("-o") + 1]).write_bytes(b"ISO" * 100)
            result = completed()
        elif argv[0] == "ssh" and "check" in argv:
            running = (
                self.agent_master_running
                if "User=agent" in argv
                else self.master_running
            )
            result = completed(
                returncode=0 if running else 255,
                stderr=self.master_output
                or (
                    "Master running (pid=4242)\r\n"
                    if running
                    else "Control socket connect(x): No such file or directory\r\n"
                ),
            )
        elif argv[0] == "ssh" and "-O" in argv and "forward" in argv:
            if self.master_running and self.master_session[0] == 0:
                self.forwarded.append(argv)
                result = completed()
            else:
                result = completed(returncode=255, stderr="mux_client_forward: failed")
        elif argv[0] == "ssh" and "exit" in argv:
            if "User=agent" in argv:
                result = completed(returncode=0 if self.agent_master_running else 255)
                self.agent_master_running &= self.master_survives_exit
            else:
                result = completed(returncode=0 if self.master_running else 255)
                self.master_running &= self.master_survives_exit
        elif argv[0] == "ssh" and "-O" not in argv:
            # A session without ControlPath=none rides the master when one runs;
            # any other one logs in to the box's sshd, and a mux one that does
            # becomes the master.
            fresh = "ControlPath=none" in argv
            if self.master_running and not fresh:
                code, stderr = self.master_session
            else:
                code, stderr = self.sshd(argv)
                if not fresh and code == 0:
                    self.master_running = True
                    self.master_session = (0, "")
            # The box's build stamped it, and holds nothing else an exists
            # probe asks about.
            stdout = ""
            if code == 0 and "echo present" in argv[-1]:
                stamp = argv[-1].endswith(" " + VERSION_STAMP)
                stdout = "present\n" if stamp else "absent\n"
            stdout, stderr = ssh_ends(
                guest_words(argv[-1]), ssh_log_of(argv), code, stdout, stderr
            )
            result = completed(stdout, returncode=code, stderr=stderr)
        elif argv[0] == HERDR and argv[1:3] == ["machine", "list"]:
            result = completed(json.dumps(self.herdr_machines))
        elif argv[0] == HERDR and argv[1:3] == ["machine", "remove"]:
            if self.herdr_remove_fails:
                result = completed(returncode=1, stderr="no such machine")
            else:
                self.herdr_machines = [
                    m for m in self.herdr_machines if m["id"] != argv[3]
                ]
                result = completed()
        elif argv[0] == "ss":
            result = completed(self.listeners)
        elif argv[0] == "getfacl":
            result = completed(self.acl)
        else:
            result = completed()
        if kwargs.get("check") and result.returncode != 0:
            raise self.box.YoError(
                "%s failed (exit %d): %s"
                % (" ".join(argv), result.returncode, result.stderr)
            )
        return result

    def spawn(self, argv, **kwargs):
        """Start `virsh event`, or the ssh session of the initrd's helper."""
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "ssh" and argv[-1] == "yolobox-unlock":
            return FakeHelper(*self.helper())
        assert argv[0] == "virsh" and argv[3] == "event", argv
        assert not self.watchers, "virsh event was armed a second time"
        events, ending = self.event_script
        watcher = FakeEventStream(self, argv[4], events, ending, self.off_at_eof)
        self.watchers.append(watcher)
        return watcher

    def virsh(self, sub, args):
        if sub in self.interrupted:
            raise self.interrupted[sub]
        if sub in self.fail:
            return completed(returncode=1, stderr=self.fail[sub])
        if sub in self.fail_once:
            return completed(returncode=1, stderr=self.fail_once.pop(sub))
        if sub == "shutdown":
            self.states[args[0]] = self.shutdown_state
            return completed()
        if sub == "destroy":
            self.states[args[0]] = "shut off"
            return completed()
        if sub == "undefine":
            assert args[0] == "--nvram", args
            del self.domains[args[1]]
            del self.states[args[1]]
            return completed()
        if sub == "list":
            # Without --all virsh lists the active domains only.
            names = [
                name
                for name in self.domains
                if "--all" in args or self.states[name] != "shut off"
            ]
            return completed("".join(name + "\n" for name in names) + "\n")
        if sub == "dominfo":
            self.before_dominfo(args[0])
            if args[0] not in self.domains:
                return completed(
                    returncode=1,
                    stderr="error: failed to get domain '%s'\n" % args[0],
                )
            if args[0] in self.dominfo:
                return completed(self.dominfo[args[0]])
            return completed(
                "Id:             7\nName:           %s\nState:          %s\n"
                "Max memory:     %d KiB\nUsed memory:    1048576 KiB\n"
                "CPU(s):         %d\n\n"
                % (
                    args[0],
                    self.states[args[0]],
                    self.max_memory_kib[args[0]],
                    self.cpus.get(args[0], DOMAIN_CPUS),
                )
            )
        if sub == "dumpxml":
            name = args[-1]
            self.before_dumpxml(name)
            if name not in self.domains:
                return completed(
                    returncode=1,
                    stderr="error: failed to get domain '%s'\n" % name,
                )
            return completed(self.domains[name])
        if sub == "domstate":
            return completed(self.states[args[0]] + "\n")
        if sub == "define":
            xml = Path(args[0]).read_text()
            root = ET.fromstring(xml)
            name = root.findtext("name")
            # libvirt keeps a name to its uuid: a definition under a known name
            # must carry that domain's uuid, and only replaces it while it is off.
            if name in self.domains:
                known = ET.fromstring(self.domains[name]).findtext("uuid")
                if known != root.findtext("uuid"):
                    return completed(
                        returncode=1,
                        stderr="error: operation failed: domain '%s' already "
                        "exists with uuid %s" % (name, known),
                    )
                if self.states[name] != "shut off":
                    return completed()
            self.domains[name] = as_libvirt_prints(xml)
            self.states[name] = "shut off"
            memory = root.find("memory")
            assert memory.get("unit") == "GiB", memory.get("unit")
            self.max_memory_kib[name] = int(memory.text) << 20
            self.cpus[name] = int(root.findtext("vcpu"))
            return completed()
        if sub in ("start", "resume", "dompmwakeup"):
            self.states[args[0]] = "running"
            return completed()
        if sub == "vol-list":
            rows = "".join(" %s  /var/lib/%s\n" % (name, name) for name in self.volumes)
            return completed(" Name   Path\n-----------------\n" + rows + "\n")
        if sub == "vol-create-as":
            self.volumes[args[1]] = b""
            self.capacity[args[1]] = int(args[2])
            return completed()
        if sub == "vol-resize":
            gib = re.fullmatch(r"(\d+)G", args[3])
            assert gib, args
            self.capacity[args[2]] = int(gib.group(1)) << 30
            return completed()
        if sub == "vol-upload":
            self.volumes[args[2]] = Path(args[3]).read_bytes()
            return completed()
        if sub == "vol-delete":
            del self.volumes[args[2]]
            return completed()
        if sub == "vol-info":
            return completed(
                "Name:           x\nType:           file\n"
                "Capacity:       %d bytes\n"
                "Allocation:     2147483648 bytes\n"
                % self.capacity.get(args[-1], 100 << 30)
            )
        return completed()

    def tagged_domain(self, name, port, memory_gib=None, cpus=DOMAIN_CPUS):
        """A domain yo defined for the instance `name`, running, beside the
        instance directory yo made for it."""
        facts = self.box.DomainFacts(
            str(uuid.uuid4()), port, memory_gib or self.box.LIBVIRT_MEMORY_GIB, cpus
        )
        os.makedirs(
            os.path.join(
                self.box.state.home(), self.box.box.driver.instances_dir, name
            ),
            exist_ok=True,
        )
        self.domains["yolobox-" + name] = as_libvirt_prints(
            self.box.domain_xml(name, facts, VM_UID, VM_GID)
        )
        self.states["yolobox-" + name] = "running"
        self.max_memory_kib["yolobox-" + name] = facts.memory_gib << 20
        self.cpus["yolobox-" + name] = cpus
        return facts

    def foreign_domain(self, name, memory_kib, domain_state="running"):
        """A domain of another tool, which yo only lists and asks for its memory."""
        self.domains[name] = "<domain type='kvm'><name>%s</name></domain>" % name
        self.states[name] = domain_state
        self.max_memory_kib[name] = memory_kib


def fake_which(name):
    """The tools FakeHost.run answers for are installed; nothing else is, so a
    test never depends on what the machine running it has (getfacl is absent on
    macOS, `aws` is present on some CI images)."""
    return "/usr/bin/" + name if name in ("virsh", "getfacl", "ss") else None


class LibvirtCase(FakeHome):
    instance = "box"

    def setUp(self):
        super().setUp()
        self.box, name = load_libvirt_yo(self.instance)
        self.addCleanup(unload_yo, name)
        self.host = FakeHost(self.box)
        for patcher in (
            mock.patch.object(self.box.proc, "run", self.host.run),
            mock.patch.object(
                self.box.pwd,
                "getpwnam",
                return_value=mock.Mock(pw_uid=VM_UID, pw_gid=VM_GID),
            ),
            mock.patch.object(self.box.release, "host_user", return_value=OPERATOR),
            mock.patch.object(self.box.shutil, "which", fake_which),
            mock.patch.object(
                self.box.unlock, "ssh_banner", lambda endpoint: self.host.banner
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fake_resolver()
        self.range_file = Path(self.home) / "port_range"
        self.range_file.write_text(EPHEMERAL_RANGE)
        self.meminfo = Path(self.home) / "meminfo"
        self.host_memory(HOST_MEMORY_GIB << 20)
        for name, path in (
            ("PORT_RANGE_FILE", self.range_file),
            ("MEMINFO_FILE", self.meminfo),
        ):
            patcher = mock.patch.object(self.box.libvirt_host, name, str(path))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.driver = self.box.box.driver
        # Every ssh checks the operator's key is on offer first, so the host has
        # a 1Password agent that holds it; TestOperatorKey takes it away.
        sock = Path(self.driver.op_sock())
        sock.parent.mkdir(parents=True)
        sock.touch()
        self.real_agent_identities = self.box.agent.ssh_agent_identities
        patcher = mock.patch.object(
            self.box.agent,
            "ssh_agent_identities",
            return_value=[(KEY_BLOB, "yolobox-operator"), (OTHER_BLOB, "yolobox-disk")],
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_resolver(self):
        """/etc/resolv.conf is a symlink to resolved's stub, as on Fedora: the
        label that counts is the stub's, which `self.resolver_label` is (bytes
        as the kernel answers, or the OSError it raises)."""
        stub = Path(self.home) / "stub-resolv.conf"
        stub.write_text("nameserver 127.0.0.53\n")
        self.resolv_stub = os.path.realpath(stub)
        self.resolv_conf = Path(self.home) / "resolv.conf"
        self.resolv_conf.symlink_to(stub)
        self.resolver_label = GOOD_LABEL

        def getxattr(path, name):
            self.assertEqual((path, name), (self.resolv_stub, "security.selinux"))
            if isinstance(self.resolver_label, OSError):
                raise self.resolver_label
            return self.resolver_label

        for patcher in (
            mock.patch.object(
                self.box.libvirt_host, "RESOLV_CONF", str(self.resolv_conf)
            ),
            # Not every host has getxattr (macOS has none), and no host's
            # kernel answers with the label a test needs.
            mock.patch.object(
                self.box.libvirt_host.os, "getxattr", getxattr, create=True
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def kept_volume(self, gib, record="built"):
        """A disk volume of `gib` GiB left from an earlier run, and this host's
        record of building it (None: no record)."""
        self.host.volumes["yolobox-box.qcow2"] = b"a whole disk"
        self.host.capacity["yolobox-box.qcow2"] = gib << 30
        if record is not None:
            os.makedirs(self.box.instance_dir(), exist_ok=True)
            Path(self.box.instance_dir(), "disk-volume").write_text(record + "\n")

    def derived(self, passphrase):
        """A passphrase source that derived `passphrase` earlier in the command."""
        source = self.box.agent.PassphraseSource()
        source.derived = bytearray(passphrase)
        return source

    def dead_agent(self):
        """The real agent client, against a socket file nobody listens on:
        connect() is refused, as it is when 1Password quits and leaves its
        socket behind."""
        patcher = mock.patch.object(
            self.box.agent, "ssh_agent_identities", self.real_agent_identities
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        sock = Path(self.driver.op_sock())
        sock.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock))
        server.close()

    def host_memory(self, total_kib, available_kib=None):
        """The host's MemTotal and MemAvailable (the same, unless a test says
        the host has less free than it has), as /proc/meminfo prints them."""
        self.meminfo.write_text(
            "MemTotal:       %d kB\nMemFree:         1048576 kB\n"
            "MemAvailable:   %d kB\n"
            % (total_kib, total_kib if available_kib is None else available_kib)
        )

    def vm_word(self):
        """virsh's own word for the domain's state, None for no domain."""
        status = self.driver.vm_status()
        return status.word if status is not None else None

    def force_stop_command(self):
        return self.box.state.hint(self.driver.hints.force_stop_command)

    def lock_held(self, path=None):
        """Whether another process would have to wait for the lock at `path`,
        the host-wide port lock by default."""
        path = path or os.path.join(
            self.home, self.driver.instances_dir, self.box.PORT_LOCK_NAME
        )
        fd = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)  # drops the lock when this probe got it
        return False


class TestDomainTemplate(LibvirtCase):
    """What libvirt/domain.xml gives every box: the guarantees the box's
    confinement rests on are properties of the template, which every start of
    a stopped domain defines again."""

    def render(self, name="box", port=FIRST_PORT, memory_gib=16, cpus=DOMAIN_CPUS):
        facts = self.box.DomainFacts("a-uuid", port, memory_gib, cpus)
        return self.box.domain_xml(name, facts, VM_UID, VM_GID)

    def test_the_facts_fill_the_template(self):
        root = ET.fromstring(self.render(port=61234, memory_gib=24, cpus=3))
        self.assertEqual(root.findtext("uuid"), "a-uuid")
        self.assertEqual(root.find("memory").get("unit"), "GiB")
        self.assertEqual(root.findtext("memory"), "24")
        self.assertEqual(root.findtext("vcpu"), "3")
        forward = root.find("devices/interface/portForward")
        self.assertEqual(forward.get("address"), "127.0.0.1")
        self.assertEqual(forward.get("proto"), "tcp")
        self.assertEqual(forward.find("range").get("start"), "61234")
        self.assertEqual(forward.find("range").get("to"), str(FACTS["guest_ssh_port"]))

    def test_no_device_reaches_the_host(self):
        root = ET.fromstring(self.render())
        devices = {child.tag for child in root.find("devices")}
        for tag in ("graphics", "vsock", "filesystem", "channel", "hostdev", "tpm"):
            self.assertNotIn(tag, devices)
        interfaces = root.findall("devices/interface")
        self.assertEqual([i.get("type") for i in interfaces], ["user"])

    def test_both_seclabels_are_there_and_the_dac_one_relabels_to_the_vm_account(self):
        root = ET.fromstring(self.render())
        labels = {
            (label.get("type"), label.get("model")): label
            for label in root.findall("seclabel")
        }
        self.assertEqual(set(labels), {("dynamic", "selinux"), ("static", "dac")})
        for label in labels.values():
            self.assertEqual(label.get("relabel"), "yes")
        dac = labels[("static", "dac")]
        self.assertEqual(dac.findtext("label"), "+%d:+%d" % (VM_UID, VM_GID))

    def test_the_network_mirrors_limas(self):
        root = ET.fromstring(self.render())
        interface = root.find("devices/interface")
        self.assertEqual(interface.get("type"), "user")
        self.assertEqual(interface.find("backend").get("type"), "passt")
        self.assertEqual(interface.find("ip").get("address"), "192.168.5.15")
        self.assertEqual(interface.find("ip").get("prefix"), "24")
        self.assertEqual(interface.find("route").get("gateway"), "192.168.5.2")
        address = interface.find("address")
        self.assertEqual(
            (address.get("bus"), address.get("slot"), address.get("function")),
            ("0x00", "0x01", "0x0"),
        )

    def test_the_domain_is_named_and_tagged_after_the_instance(self):
        root = ET.fromstring(self.render(name="e2e"))
        self.assertEqual(root.findtext("name"), "yolobox-e2e")
        marker = root.find("metadata/{%s}instance" % self.box.DOMAIN_NS)
        self.assertEqual(marker.text, "e2e")
        volumes = [
            source.get("volume") for source in root.findall("devices/disk/source")
        ]
        self.assertEqual(volumes, ["yolobox-e2e.qcow2", "yolobox-e2e-cidata.iso"])

    def test_the_machine_is_the_unprivileged_shape(self):
        root = ET.fromstring(self.render())
        self.assertEqual(root.find("devices/controller").get("model"), "none")
        self.assertEqual(root.find("devices/memballoon").get("model"), "none")
        self.assertEqual(
            root.find("devices/disk[@device='cdrom']/readonly") is not None, True
        )
        self.assertEqual(root.find("os").get("firmware"), "efi")
        self.assertEqual(root.find("os/firmware/feature").get("enabled"), "no")

    def test_unparseable_xml_from_virsh_is_an_error_not_a_missing_domain(self):
        self.host.domains["yolobox-box"] = "<domain"
        self.host.states["yolobox-box"] = "running"
        with self.assertRaisesRegex(self.box.YoError, "unparseable XML"):
            self.driver.vm_status()


class TestHostPortWindow(LibvirtCase):
    def test_the_window_starts_above_the_kernels_ephemeral_range(self):
        window = self.box.host_port_window("32768\t60999\n")
        self.assertEqual((window.start, window.stop), (61000, 65536))

    def test_a_range_ending_at_the_top_leaves_no_window(self):
        with self.assertRaises(self.box.YoError) as caught:
            self.box.host_port_window("32768 65535")
        self.assertIn("no host port is left", caught.exception.message)

    def test_text_that_is_not_two_port_numbers_is_refused(self):
        for text in (
            "",
            "lizard",
            "1",
            "1 2 3",
            "a b",
            "-1 5",
            "1 99999999999x",
            "1 \u00b2",
            "\uff11\uff12 \uff13\uff14",
        ):
            with self.subTest(text=text):
                with self.assertRaisesRegex(self.box.YoError, "not two port numbers"):
                    self.box.host_port_window(text)

    def test_the_window_is_read_from_the_kernels_file(self):
        self.range_file.write_text("1024 49151\n")
        self.assertEqual(self.box.read_port_window().start, 49152)

    def test_the_window_never_dips_into_the_registered_ports(self):
        # A host whose ephemeral range ends early does not hand yo port 30001,
        # where a registered service may listen.
        for text, first in (("1024 30000", 49152), ("1024 49150", 49152)):
            with self.subTest(text=text):
                self.assertEqual(self.box.host_port_window(text).start, first)

    def test_an_unreadable_file_is_an_error(self):
        self.range_file.unlink()
        with self.assertRaises(self.box.YoError) as caught:
            self.box.read_port_window()
        self.assertIn("cannot read", caught.exception.message)


class TestPortAllocation(LibvirtCase):
    def never_bound(self, port):
        return False

    def test_the_lowest_free_port_wins(self):
        port = self.box.lowest_free_port(range(61000, 61010), [], self.never_bound)
        self.assertEqual(port, 61000)

    def test_taken_and_bound_ports_are_skipped(self):
        port = self.box.lowest_free_port(
            range(61000, 61010), [61000, 61002], lambda port: port == 61001
        )
        self.assertEqual(port, 61003)

    def test_a_full_window_is_an_error(self):
        with self.assertRaises(self.box.YoError) as caught:
            self.box.lowest_free_port(
                range(61000, 61003), [61000, 61001], lambda p: p == 61002
            )
        self.assertIn("no free host port in 61000-61002", caught.exception.message)

    def test_a_port_bound_on_loopback_is_seen_as_bound(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen()
            port = holder.getsockname()[1]
            self.assertTrue(self.box.loopback_port_bound(port))
        self.assertFalse(self.box.loopback_port_bound(port))

    def test_a_port_the_probe_may_not_bind_is_an_error_not_a_free_port(self):
        with mock.patch.object(self.box.socket, "socket") as fake:
            fake.return_value.__enter__.return_value.bind.side_effect = PermissionError(
                13, "Permission denied"
            )
            with self.assertRaisesRegex(self.box.YoError, "cannot probe 127.0.0.1:80"):
                self.box.loopback_port_bound(80)

    def test_the_driver_skips_ports_every_domain_forwards_boxes_or_not(self):
        # A shut-off domain of another tool holds no socket, so only its XML
        # shows the port it will take when it starts.
        self.host.tagged_domain("first", FIRST_PORT)
        self.host.tagged_domain("second", FIRST_PORT + 1)
        untagged = self.box.domain_xml(
            "x", self.box.DomainFacts("u", FIRST_PORT + 2, 16, 2), VM_UID, VM_GID
        )
        self.host.domains["plain"] = re.sub(
            r"<metadata>.*</metadata>", "", untagged, flags=re.S
        )
        self.host.states["plain"] = "shut off"
        self.host.domains["span"] = (
            "<domain><name>span</name><devices><interface><portForward>"
            "<range start='%d' end='%d'/></portForward></interface></devices></domain>"
            % (FIRST_PORT + 3, FIRST_PORT + 5)
        )
        self.host.states["span"] = "shut off"
        with mock.patch.object(
            self.box.driver, "loopback_port_bound", return_value=False
        ):
            self.assertEqual(self.driver.allocate_host_port(), FIRST_PORT + 6)

    def test_a_domain_undefined_while_yo_reads_the_others_is_skipped(self):
        self.host.tagged_domain("first", FIRST_PORT)
        self.host.tagged_domain("second", FIRST_PORT + 1)
        self.host.before_dumpxml = lambda name: self.host.domains.pop(
            "yolobox-first", None
        )
        with mock.patch.object(
            self.box.driver, "loopback_port_bound", return_value=False
        ):
            self.assertEqual(self.driver.allocate_host_port(), FIRST_PORT)

    def test_a_dumpxml_that_fails_for_a_domain_still_there_is_an_error(self):
        self.host.tagged_domain("first", FIRST_PORT)
        self.host.fail["dumpxml"] = "error: internal error: lizard"
        with self.assertRaisesRegex(
            self.box.YoError,
            r"virsh dumpxml yolobox-first failed \(exit 1\): .*lizard",
        ):
            self.driver.allocate_host_port()

    FOREIGN = (
        "<domain><name>foreign</name><devices><interface>"
        "<portForward><range start='not-a-port' to='22'/></portForward>"
        "</interface></devices></domain>"
    )

    def test_a_foreign_domain_is_never_read_strictly(self):
        self.host.domains["foreign"] = self.FOREIGN
        self.host.tagged_domain("first", FIRST_PORT)
        with mock.patch.object(
            self.box.driver, "loopback_port_bound", return_value=False
        ):
            self.assertEqual(self.driver.allocate_host_port(), FIRST_PORT + 1)

    def test_the_driver_skips_a_port_bound_on_the_host(self):
        with mock.patch.object(
            self.box.driver,
            "loopback_port_bound",
            side_effect=lambda port: port == FIRST_PORT,
        ):
            self.assertEqual(self.driver.allocate_host_port(), FIRST_PORT + 1)

    def test_a_domains_facts_are_read_from_its_inactive_definition(self):
        made = self.host.tagged_domain("box", 61234, memory_gib=24, cpus=3)
        self.assertEqual(self.driver.domain_facts(), made)
        self.assertEqual(self.driver.ssh_port(), 61234)
        self.assertEqual(
            self.host.calls_of("dumpxml")[-1][3:],
            ["dumpxml", "--inactive", "yolobox-box"],
        )

    def test_the_ssh_port_of_a_missing_domain_says_to_create_it(self):
        with self.assertRaises(self.box.YoError) as caught:
            self.driver.ssh_port()
        self.assertIn(self.box.yo_cmd("up"), caught.exception.message)


class TestYoPort(LibvirtCase):
    def run_main(self, *argv):
        stdout = io.StringIO()
        with (
            mock.patch.object(self.box.box.driver, "require_tool"),
            mock.patch.object(
                self.box.driver, "loopback_port_bound", return_value=False
            ),
            contextlib.redirect_stdout(stdout),
        ):
            self.box.main(["-m", "box"] + list(argv))
        return stdout.getvalue()

    def test_the_chosen_host_port_is_printed_and_forwarded_over_the_operator_master(
        self,
    ):
        printed = self.run_main("port", "5173")
        self.assertEqual(printed, "127.0.0.1:%d\n" % FIRST_PORT)
        (forward,) = [call for call in self.host.calls if "-L" in call]
        self.assertEqual(forward[forward.index("-O") + 1], "forward")
        self.assertEqual(
            forward[forward.index("-L") + 1], "127.0.0.1:%d:127.0.0.1:5173" % FIRST_PORT
        )
        self.assertEqual(forward[-1], "lima-box")

    def test_the_port_is_chosen_and_taken_under_the_lock(self):
        held = []
        run = self.host.run

        def spy(argv, **kwargs):
            if "-L" in argv:
                held.append(self.lock_held())
            return run(argv, **kwargs)

        with (
            mock.patch.object(self.box.proc, "run", spy),
            mock.patch.object(
                self.box.driver,
                "loopback_port_bound",
                side_effect=lambda port: held.append(self.lock_held()) or False,
            ),
        ):
            self.driver.forward_port(5173)
        self.assertEqual(held, [True, True])
        self.assertFalse(self.lock_held())

    def test_a_guest_port_outside_one_to_65535_is_refused(self):
        for value in ("0", "65536", "-1", "lizard", "", "\u00b2", "\uff11\uff12"):
            with self.subTest(value=value):
                with (
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as caught,
                ):
                    self.box.build_parser().parse_args(["port", value])
                self.assertEqual(caught.exception.code, 2)

    def test_the_edges_of_the_range_are_accepted(self):
        for value in ("1", "65535"):
            with self.subTest(value=value):
                args = self.box.build_parser().parse_args(["port", value])
                self.assertEqual(args.guest_port, int(value))

    def test_a_full_window_is_an_error(self):
        self.range_file.write_text("32768 65534\n")
        self.host.tagged_domain("other", 65535)
        with self.assertRaisesRegex(self.box.YoError, "no free host port in"):
            self.run_main("port", "80")

    def test_on_a_mac_it_exits_64_saying_lima_mirrors_ports(self):
        box = load_yo("yo_mac_port")
        self.addCleanup(unload_yo, "yo_mac_port")
        with (
            mock.patch.object(box.box.driver, "require_tool"),
            self.assertRaises(box.YoError) as caught,
        ):
            box.main(["port", "5173"])
        self.assertEqual(caught.exception.code, 64)
        self.assertIn(
            "mirrors the guest's TCP ports 1024-49151", caught.exception.message
        )


class TestOperatorKey(LibvirtCase):
    """The operator's key, read from a real unix socket that plays the agent."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(
            self.box.agent, "ssh_agent_identities", self.real_agent_identities
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sock = Path(self.driver.op_sock())
        self.sock.unlink()

    def agent(self, identities):
        """An agent on the socket that answers one key listing with `identities`."""
        ssh_string = self.box.agent.ssh_string
        reply = (
            bytes([self.box.agent.SSH_AGENT_IDENTITIES_ANSWER])
            + struct.pack(">I", len(identities))
            + b"".join(
                ssh_string(blob) + ssh_string(comment.encode())
                for blob, comment in identities
            )
        )
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(self.sock))
        server.listen(1)
        # Bounded, so code that never asks fails its test instead of hanging
        # the suite in the cleanup's join.
        server.settimeout(ACCEPT_TIMEOUT_SECONDS)

        def answer():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                return
            with conn:
                (length,) = struct.unpack(">I", conn.recv(4))
                conn.recv(length)
                conn.sendall(ssh_string(reply))

        thread = threading.Thread(target=answer)
        thread.start()
        self.addCleanup(thread.join)

    def assert_named_dead(self, message):
        self.assertTrue(
            message.startswith(
                "1Password's SSH agent at %s is not running (or locked) — start or "
                "unlock 1Password" % self.sock
            ),
            message,
        )

    def test_the_one_key_with_the_title_is_chosen(self):
        self.agent([(OTHER_BLOB, "yolobox-disk"), (KEY_BLOB, "yolobox-operator")])
        self.assertEqual(self.driver.operator_key(), KEY_BLOB)

    def test_no_key_with_the_title_is_an_error_naming_it(self):
        self.agent([(OTHER_BLOB, "yolobox-disk")])
        with self.assertRaises(self.box.YoError) as caught:
            self.driver.operator_key()
        self.assertIn("no key titled yolobox-operator", caught.exception.message)

    def test_two_keys_with_the_title_are_an_error_not_a_guess(self):
        self.agent([(KEY_BLOB, "yolobox-operator"), (OTHER_BLOB, "yolobox-operator")])
        with self.assertRaises(self.box.YoError) as caught:
            self.driver.operator_key()
        self.assertIn("offers 2 keys titled yolobox-operator", caught.exception.message)

    def test_a_missing_agent_socket_is_a_dead_agent(self):
        with self.assertRaises(self.box.YoError) as caught:
            self.driver.operator_key()
        self.assert_named_dead(caught.exception.message)

    def test_an_agent_that_refuses_connections_is_named_dead_with_the_cause(self):
        self.dead_agent()
        with self.assertRaises(self.box.YoError) as caught:
            self.driver.operator_key()
        self.assert_named_dead(caught.exception.message)
        self.assertIn("Connection refused", caught.exception.message)

    def test_an_agent_that_lists_no_key_is_named_dead(self):
        self.agent([])
        with self.assertRaises(self.box.YoError) as caught:
            self.driver.operator_key()
        self.assert_named_dead(caught.exception.message)

    def test_the_public_key_line_carries_the_key_and_the_title_only(self):
        self.assertEqual(self.box.public_key_line(KEY_BLOB), PUBLIC_KEY)


def gawk_argv():
    if REAL_WHICH("gawk"):
        return ["gawk"]
    if REAL_WHICH("nix"):
        return ["nix", "shell", "nixpkgs#gawk", "-c", "gawk"]
    require_tool(
        ["gawk", "nix"],
        have=False,
        why="the initrd's key program needs gawk, or nix to fetch it",
    )


def initrd_awk_program():
    text = INITRD_KEYS_SCRIPT.read_text()
    found = re.search(r"gawk '(.*?)' \"\$mnt/user-data\"", text, re.S)
    assert found, "the initrd script no longer runs gawk over user-data"
    return found.group(1)


class TestCidata(LibvirtCase):
    def files(self, username=OPERATOR):
        return self.box.cidata_files(username, PUBLIC_KEY, "box")

    def test_lima_env_has_no_blank_line_and_the_operators_identity(self):
        text = self.files()["lima.env"]
        self.assertNotIn("\n\n", "\n" + text)
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(
            text.splitlines(),
            [
                "LIMA_CIDATA_USER=alice",
                "LIMA_CIDATA_UID=501",
                "LIMA_CIDATA_HOME=/home/alice.guest",
                "LIMA_CIDATA_IID=yolobox-box",
            ],
        )

    def test_the_other_files_lima_init_reads_are_there(self):
        files = self.files()
        self.assertEqual(set(files), set(self.box.CIDATA_FILES))
        self.assertEqual(files["etc_environment"], "")
        self.assertIn("instance-id: yolobox-box", files["meta-data"])

    def test_the_initrds_key_program_reads_the_key_out_of_user_data(self):
        with tempfile.NamedTemporaryFile("w", suffix="user-data") as user_data:
            user_data.write(self.files()["user-data"])
            user_data.flush()
            printed = subprocess.run(
                gawk_argv() + [initrd_awk_program(), user_data.name],
                capture_output=True,
                text=True,
                timeout=120,
            )
        self.assertEqual(printed.returncode, 0, printed.stderr)
        self.assertEqual(printed.stdout, PUBLIC_KEY + "\n")

    def test_an_empty_operator_name_is_refused(self):
        with self.assertRaisesRegex(self.box.YoError, "cannot name the operator"):
            self.files("")

    def test_the_agent_account_name_is_refused_for_the_operator(self):
        with self.assertRaises(self.box.YoError) as caught:
            self.files("agent")
        self.assertIn("agent account", caught.exception.message)

    def test_names_the_guest_cannot_create_are_refused(self):
        for name in ("Alice", "a b", "a\nb", "alice\n", "1alice", "a;b", "-a"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(
                    self.box.YoError, "cannot name the operator"
                ):
                    self.files(name)


class TestSshConfig(LibvirtCase):
    """The ssh facts a libvirt box is reached with, and the file written from them."""

    def test_the_facts_name_the_domains_port_the_public_key_and_the_agent(self):
        self.host.tagged_domain("box", 61234)
        facts = self.driver.ssh_facts()
        self.assertEqual(
            facts,
            self.box.SshFacts(
                61234,
                OPERATOR,
                os.path.join(self.box.instance_dir(), "operator.pub"),
                self.driver.op_sock(),
            ),
        )

    def test_the_written_file_carries_the_agent_for_both_names(self):
        self.host.tagged_domain("box", 61234)
        self.box.sshconfig.write_ssh_config()
        with open(self.box.ssh_cfg()) as handle:
            text = handle.read()
        self.assertEqual(
            text.count('  IdentityAgent "%s"\n' % self.driver.op_sock()), 2
        )
        self.assertEqual(text.count("  Port 61234\n"), 2)
        self.assertIn("Host lima-box\n", text)
        self.assertIn("Host box\n", text)

    def test_the_initrd_probe_uses_the_agent_and_the_public_key(self):
        self.host.tagged_domain("box", 61234)
        argv = self.box.unlock.operator_argv("true", self.driver.ssh_facts())
        self.assertIn("IdentityAgent=%s" % self.driver.op_sock(), argv)
        self.assertEqual(
            argv[argv.index("-i") + 1],
            os.path.join(self.box.instance_dir(), "operator.pub"),
        )
        self.assertIn("IdentitiesOnly=yes", argv)
        self.assertIn("Port=61234", argv)

    def test_without_a_domain_there_are_no_facts_and_the_remedy_is_yo_up(self):
        with self.assertRaisesRegex(self.box.YoError, "does not exist.*yo -m box up"):
            self.driver.ssh_facts()

    def test_lima_keeps_its_private_key_and_no_agent(self):
        mac = load_yo("yo_mac_initrd")
        self.addCleanup(unload_yo, "yo_mac_initrd")
        with mock.patch.object(mac.box.driver, "ssh_port", return_value=60022):
            argv = mac.unlock.operator_argv("true", mac.box.driver.ssh_facts())
        self.assertIn("IdentityAgent=none", argv)
        self.assertTrue(argv[argv.index("-i") + 1].endswith(".lima/_config/user"))


class TestPreconditions(LibvirtCase):
    def check(
        self,
        which="/usr/bin/virsh",
        uri=0,
        version="12.8.0",
        unit="active\n",
        user=True,
        window=EPHEMERAL_RANGE,
    ):
        self.range_file.write_text(window)

        def run(argv, **kwargs):
            self.assertNotEqual(argv[0], "getfacl", "require_tool judges the ACL")
            if argv[3:] == ["version", "--daemon"]:
                return completed(DAEMON_VERSION_TEXT % version)
            if argv[0] == "systemctl":
                return completed(unit, returncode=0 if unit == "active\n" else 3)
            return completed("qemu:///system\n", returncode=uri, stderr="boom")

        account = mock.Mock(pw_uid=VM_UID, pw_gid=VM_GID) if user else None
        with (
            mock.patch.object(self.box.shutil, "which", return_value=which),
            mock.patch.object(self.box.proc, "run", run),
            mock.patch.object(
                self.box.pwd,
                "getpwnam",
                side_effect=None if user else KeyError("yolobox-vm"),
                return_value=account,
            ),
        ):
            self.driver.require_tool()

    def assertRefused(self, fragment, remedy="host-setup.sh", **kwargs):
        with self.assertRaises(self.box.YoError) as caught:
            self.check(**kwargs)
        message = caught.exception.message
        self.assertIn(fragment, message)
        self.assertIn(remedy, message)
        return message

    def test_a_ready_host_passes(self):
        self.check()

    def test_a_mislabelled_resolver_does_not_fail_the_commands_that_never_start(
        self,
    ):
        # passt reads the resolver only when a domain starts; start_refusal
        # judges the label, so ssh, status, down and destroy still run.
        self.resolver_label = WRONG_LABEL
        self.check()

    def test_each_missing_piece_has_its_own_message_naming_the_setup_script(self):
        messages = {
            self.assertRefused("virsh not found", which=None),
            self.assertRefused("cannot connect to qemu:///system", uri=1),
            self.assertRefused("older than 12.2.0", version="12.1.9"),
            self.assertRefused("yolobox-host.service is inactive", unit="inactive\n"),
            self.assertRefused("account yolobox-vm does not exist", user=False),
        }
        self.assertEqual(len(messages), 5)

    def test_a_host_with_no_port_above_the_ephemeral_range_is_refused(self):
        with self.assertRaises(self.box.YoError) as caught:
            self.check(window="32768 65535\n")
        self.assertIn("no host port is left", caught.exception.message)

    def test_libvirt_versions_compare_per_field(self):
        self.check(version="12.2.0")
        self.check(version="12.10.0")
        self.check(version="13.0.0")
        with self.assertRaisesRegex(self.box.YoError, "older than 12.2.0"):
            self.check(version="11.99.99")

    def test_a_version_that_is_not_a_version_is_an_error(self):
        with self.assertRaisesRegex(self.box.YoError, "cannot read a version"):
            self.check(version="lizard")

    def test_the_daemons_version_counts_not_the_clients_library(self):
        text = DAEMON_VERSION_TEXT % "12.1.0"
        self.assertEqual(self.box.parse_daemon_version(text), (12, 1, 0))
        self.assertIn("Using library: libvirt 12.8.0", text)
        self.assertRefused("older than 12.2.0", version="12.1.0")

    def test_the_daemon_line_is_found_whatever_the_spacing_or_neighbours(self):
        for text in (
            "Running against daemon: 12.8.0",
            "  running against daemon:   12.8.0  \r\n",
            "Using API: QEMU 1.0.0\nRunning against daemon: 12.8.0\nmore\n",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.box.parse_daemon_version(text), (12, 8, 0))

    def test_output_without_a_daemon_line_is_an_error(self):
        for text in ("", "Using library: libvirt 12.8.0\n", "daemon: 12.8.0\n"):
            with self.subTest(text=text):
                with self.assertRaisesRegex(
                    self.box.YoError, "cannot find the daemon's version"
                ):
                    self.box.parse_daemon_version(text)

    def test_the_installed_script_path_is_the_one_next_to_yo(self):
        message = self.assertRefused("virsh not found", which=None)
        self.assertIn(str(self.box.SCRIPT_DIR / "linux" / "host-setup.sh"), message)


class TestPasstDirectory(LibvirtCase):
    def gap(self, acl=None, acl_exit=0, getfacl=True):
        acl = getfacl_listing() if acl is None else acl

        def run(argv, **kwargs):
            self.assertEqual(argv, ["getfacl", "--absolute-names", PASST_DIR])
            if acl_exit:
                return completed(
                    returncode=acl_exit,
                    stderr="getfacl: %s: No such file or directory\n" % PASST_DIR,
                )
            return completed(acl)

        with (
            mock.patch.object(
                self.box.shutil,
                "which",
                return_value="/usr/bin/getfacl" if getfacl else None,
            ),
            mock.patch.object(self.box.proc, "run", run),
        ):
            return self.box.passt_dir_gap()

    def test_a_passt_directory_the_vm_user_can_write_in_has_no_gap(self):
        for comment in (True, False):
            with self.subTest(comment=comment):
                self.assertIsNone(self.gap(acl=getfacl_listing(comment=comment)))

    def assertPasstGap(self, fragment, **kwargs):
        message = self.gap(**kwargs)
        self.assertIn(fragment, message)
        self.assertIn(PASST_DIR, message)
        self.assertIn("sudo setfacl -m u:yolobox-vm:rwx,m::rwx " + PASST_DIR, message)
        # The fix lasts only with the hook, which re-running host-setup installs.
        self.assertIn("install the libvirt hook: run: sudo bash", message)
        self.assertIn("host-setup.sh", message)

    def test_a_mask_that_cuts_the_entry_to_read_only_is_a_gap(self):
        # What libvirt leaves behind after it starts a second passt domain.
        self.assertPasstGap(
            "yolobox-vm only r-x", acl=getfacl_listing(entry="rwx", mask="r-x")
        )

    def test_the_effective_permission_is_computed_not_read_from_the_comment(self):
        self.assertPasstGap(
            "yolobox-vm only r-x",
            acl=getfacl_listing(mask="r-x", comment=False),
        )
        liar = getfacl_listing(mask="r-x").replace("#effective:r-x", "#effective:rwx")
        self.assertPasstGap("yolobox-vm only r-x", acl=liar)

    def test_an_entry_that_is_itself_read_only_is_a_gap(self):
        self.assertPasstGap("only r-x", acl=getfacl_listing(entry="r-x", mask="rwx"))

    def test_a_directory_with_no_entry_for_the_vm_user_is_a_gap(self):
        # Default entries name the user too; only the access entry counts.
        self.assertPasstGap(
            "no ACL entry for yolobox-vm", acl=getfacl_listing(entry=None)
        )

    def test_a_missing_passt_directory_is_a_gap_fixed_by_the_setup_script(self):
        message = self.gap(acl_exit=1)
        self.assertIn("cannot read the ACL of " + PASST_DIR, message)
        self.assertIn("No such file or directory", message)
        self.assertIn("host-setup.sh", message)

    def test_an_entry_without_a_mask_is_an_error(self):
        broken = getfacl_listing().replace("mask::rwx\n", "")
        with self.assertRaisesRegex(self.box.YoError, "no mask"):
            self.gap(acl=broken)

    def test_a_host_without_getfacl_is_told_so(self):
        with self.assertRaisesRegex(self.box.YoError, "getfacl not found"):
            self.gap(getfacl=False)


class TestResolverLabel(LibvirtCase):
    """passt reads the host's resolver when a domain starts, so a start is what
    its label refuses, and nothing else is."""

    def refusal(self, label):
        self.resolver_label = label
        return self.driver.start_refusal()

    def test_a_resolver_not_labelled_net_conf_t_is_refused_with_the_relabel_command(
        self,
    ):
        for label in (
            b"unconfined_u:object_r:systemd_resolved_var_run_t:s0\0",
            b"system_u:object_r:net_conf_t_x:s0\0",
            b"system_u:object_r:xnet_conf_t:s0\0",
            b"system_u:object_r:etc_t:net_conf_t\0",
            b"net_conf_t:object_r:etc_t:s0\0",
        ):
            with self.subTest(label=label):
                message = self.refusal(label)
                self.assertIn("labelled '%s'" % label.rstrip(b"\0").decode(), message)
                self.assertIn(
                    'sudo restorecon -F "$(readlink -f %s)"' % self.resolv_conf,
                    message,
                )
                self.assertIn(self.resolv_stub, message)
                self.assertIn("no DNS", message)

    def test_a_label_with_no_type_field_is_refused_and_shown(self):
        for label, shown in (
            (b"lizard\0", "lizard"),
            (b"system_u:object_r\0", "system_u:object_r"),
            (b"", ""),
            (b"\0", ""),
            (b":::", ":::"),
            (b"\xff\xfe\0", "\ufffd\ufffd"),
            (b"a:b:\xffnet_conf_t\0", "a:b:\ufffdnet_conf_t"),
            (b"a\nb:c:d\0", "a\\nb:c:d"),
        ):
            with self.subTest(label=label):
                self.assertIn(
                    "labelled '%s' instead of type net_conf_t" % shown,
                    self.refusal(label),
                )

    def test_the_level_of_a_good_label_may_hold_colons(self):
        for label in (
            b"system_u:object_r:net_conf_t:s0:c0.c1023\0",
            b"system_u:object_r:net_conf_t",
        ):
            with self.subTest(label=label):
                self.assertIsNone(self.refusal(label))

    def test_a_host_without_selinux_has_no_label_to_deny_passt(self):
        for code in (
            self.box.libvirt_host.errno.ENODATA,
            self.box.libvirt_host.errno.EOPNOTSUPP,
        ):
            with self.subTest(code=code):
                self.assertIsNone(self.refusal(OSError(code, os.strerror(code))))

    def test_a_resolver_that_cannot_be_read_is_an_error_naming_why(self):
        for code in (
            self.box.libvirt_host.errno.ENOENT,
            self.box.libvirt_host.errno.EACCES,
        ):
            with self.subTest(code=code):
                with self.assertRaises(self.box.YoError) as caught:
                    self.assertIsNone(self.refusal(OSError(code, os.strerror(code))))
                message = caught.exception.message
                self.assertIn(
                    "cannot read the SELinux label of %s: %s"
                    % (self.resolv_stub, os.strerror(code)),
                    message,
                )
                self.assertNotIn("restorecon", message)

    def test_the_relabel_command_it_prints_relabels_the_stub_the_link_points_at(self):
        message = self.refusal(WRONG_LABEL)
        remedy = message.split("fix it with: ", 1)[1]
        fakes = Path(self.home) / "fakes"
        fakes.mkdir()
        record = Path(self.home) / "restorecon-args"
        for name, body in (
            ("sudo", 'exec "$@"'),
            ("restorecon", 'printf "%%s\\n" "$@" >%s' % shlex.quote(str(record))),
        ):
            script = fakes / name
            script.write_text("#!/bin/sh\n%s\n" % body)
            script.chmod(0o755)
        # The fakes alone are on PATH, so the host's own sudo and restorecon
        # are out of reach whatever goes wrong with a fake.
        (fakes / "readlink").symlink_to(READLINK)
        env = {"PATH": str(fakes), "LC_ALL": "C"}
        subprocess.run([BASH, "-c", remedy], env=env, check=True)
        self.assertEqual(record.read_text().splitlines(), ["-F", self.resolv_stub])

    def test_a_directory_gap_is_named_before_a_resolver_gap(self):
        self.host.acl = getfacl_listing(mask="r-x")
        self.assertIn("sudo setfacl", self.refusal(WRONG_LABEL))


class TestStatusAndSize(LibvirtCase):
    def test_a_domain_that_is_not_defined_has_no_status(self):
        self.assertIsNone(self.driver.vm_status())

    def test_running_and_blocked_are_running_and_other_states_keep_their_words(self):
        # "blocked" is a running guest idling on I/O; the same word set keeps
        # a stop waiting through it (require_stoppable).
        self.host.tagged_domain("box", FIRST_PORT)
        for word, state in (
            ("running", self.box.VmState.RUNNING),
            ("blocked", self.box.VmState.RUNNING),
            ("shut off", self.box.VmState.OFF),
            ("paused", self.box.VmState.OTHER),
            ("in shutdown", self.box.VmState.OTHER),
        ):
            with self.subTest(word=word):
                self.host.states["yolobox-box"] = word
                self.assertEqual(
                    self.driver.vm_status(), self.box.VmStatus(state, word)
                )
        self.driver.require_stoppable("blocked")

    def test_a_dominfo_that_worked_is_left_to_the_terminal(self):
        self.host.tagged_domain("box", FIRST_PORT)
        self.assertIsNone(self.driver.show_status())

    def test_a_dominfo_that_failed_is_a_line_with_its_exit_code(self):
        self.host.tagged_domain("box", FIRST_PORT)
        self.host.fail["dominfo"] = "error: internal error: lizard"
        self.assertEqual(
            self.driver.show_status(),
            "libvirt: virsh -c qemu:///system dominfo yolobox-box failed (exit 1), "
            "its own message is above",
        )

    def test_another_instances_domain_is_not_this_one(self):
        self.host.tagged_domain("other", FIRST_PORT)
        self.assertIsNone(self.driver.vm_status())

    def refusal(self, memory_gib=None, disk_gib=None):
        return self.driver.creation_refusal(self.box.Size(memory_gib, disk_gib))

    def test_any_valid_name_and_size_is_created_where_there_is_no_volume(self):
        self.assertIsNone(self.refusal())
        self.assertIsNone(self.refusal(memory_gib=24, disk_gib=250))

    def test_memory_that_fits_beside_the_reserve_and_the_active_domains_is_created(
        self,
    ):
        self.host.foreign_domain("win", 6 << 20)
        self.host_memory((24 + 6 + self.box.HOST_MEMORY_RESERVE_GIB) << 20)
        self.assertIsNone(self.refusal(memory_gib=24))

    def test_one_kib_over_is_refused_naming_every_number(self):
        self.host.foreign_domain("win", 6 << 20)
        self.host_memory(((24 + 6 + self.box.HOST_MEMORY_RESERVE_GIB) << 20) - 1)
        refusal = self.refusal(memory_gib=24)
        self.assertIn("--memory 24 does not fit on this host", refusal)
        self.assertIn("MemTotal 33.9 GiB", refusal)
        self.assertIn("keeps 4 GiB for itself", refusal)
        self.assertIn("may take 6.0 GiB (win 6.0 GiB)", refusal)
        self.assertIn("leaves 23.9 GiB for this box", refusal)
        self.assertIn("ask for at most --memory 23 or stop another domain", refusal)

    def test_memory_under_memtotal_is_refused_when_another_domain_holds_the_rest(
        self,
    ):
        self.host.foreign_domain("other", 8 << 20)
        self.host_memory(32 << 20)
        refusal = self.refusal(memory_gib=24)
        self.assertIn("ask for at most --memory 20", refusal)

    def test_every_active_domain_counts_and_a_shut_off_one_does_not(self):
        self.host.tagged_domain("other", FIRST_PORT + 1)  # 16 GiB, running
        self.host.foreign_domain("paused", 2 << 20, "paused")
        self.host.foreign_domain("off", 512 << 20, "shut off")
        self.host_memory((8 + 16 + 2 + self.box.HOST_MEMORY_RESERVE_GIB) << 20)
        self.assertIsNone(self.refusal(memory_gib=8))
        refusal = self.refusal(memory_gib=9)
        self.assertIn("(paused 2.0 GiB, yolobox-other 16.0 GiB)", refusal)

    def test_a_domain_undefined_while_yo_reads_the_others_is_not_counted(self):
        self.host.foreign_domain("win", 64 << 20)

        def undefine(name):
            self.host.domains.pop(name)

        self.host.before_dominfo = undefine
        self.host_memory((8 + self.box.HOST_MEMORY_RESERVE_GIB) << 20)
        self.assertIsNone(self.refusal(memory_gib=8))

    def test_a_dominfo_that_fails_for_a_domain_still_there_is_an_error(self):
        self.host.foreign_domain("win", 6 << 20)
        self.host.fail["dominfo"] = "error: internal error: lizard"
        with self.assertRaisesRegex(
            self.box.YoError, r"virsh dominfo win failed \(exit 1\): .*lizard"
        ):
            self.refusal(memory_gib=8)

    def test_a_host_with_no_room_for_the_smallest_box_says_so(self):
        floor = self.box.MIN_MEMORY_GIB
        under = floor - 1
        self.host_memory((self.box.HOST_MEMORY_RESERVE_GIB + under) << 20)
        refusal = self.refusal(memory_gib=floor)
        self.assertIn("other active domains may take 0.0 GiB (none)", refusal)
        self.assertIn(
            "leaves %d.0 GiB for this box — that is under the %d GiB a box needs"
            % (under, floor),
            refusal,
        )
        self.assertNotIn("at most", refusal)
        self.host.foreign_domain("win", under << 20)
        refusal = self.refusal(memory_gib=floor)
        self.assertIn("leaves 0.0 GiB for this box — stop another domain", refusal)

    def test_a_dominfo_without_a_max_memory_in_kib_is_an_error(self):
        self.host.foreign_domain("win", 6 << 20)
        for text in (
            "",
            "Max memory:     lizard KiB\n",
            "Max memory:     6 GiB\n",
            "Max memory:      KiB\n",
            "Max memory:     -1 KiB\n",
        ):
            with self.subTest(text=text):
                self.host.dominfo["win"] = text
                with self.assertRaisesRegex(
                    self.box.YoError, "virsh dominfo win printed no Max memory in KiB"
                ):
                    self.refusal(memory_gib=8)

    def test_the_default_memory_is_not_held_to_the_host(self):
        # A 16 GiB host reports a little under 16 GiB.
        self.host.foreign_domain("win", 6 << 20)
        self.host_memory((self.box.LIBVIRT_MEMORY_GIB << 20) - 1)
        self.assertIsNone(self.refusal())

    def test_a_host_memory_yo_cannot_read_is_an_error(self):
        for text in ("", "MemTotal: lizard kB\n", "MemTotal: 1 MB\n"):
            with self.subTest(text=text):
                self.meminfo.write_text(text)
                with self.assertRaisesRegex(self.box.YoError, "no MemTotal line"):
                    self.refusal(memory_gib=24)
        self.meminfo.unlink()
        with self.assertRaisesRegex(self.box.YoError, "cannot read"):
            self.refusal(memory_gib=24)

    def test_a_bigger_disk_than_the_kept_volume_is_refused_pointing_at_disk_grow(self):
        self.kept_volume(100)
        refusal = self.refusal(disk_gib=250)
        self.assertIn("100 GiB", refusal)
        self.assertIn("--disk 250", refusal)
        self.assertIn("drop --disk", refusal)
        self.assertIn("keeping what is on it: yo -m box disk-grow 250", refusal)
        self.assertNotIn("vol-delete", refusal)

    def test_a_smaller_disk_than_the_kept_volume_is_refused_since_a_disk_cannot_shrink(
        self,
    ):
        self.kept_volume(250)
        refusal = self.refusal(disk_gib=100)
        self.assertIn("a disk cannot shrink", refusal)
        self.assertIn(
            "virsh -c qemu:///system vol-delete --pool default yolobox-box.qcow2",
            refusal,
        )

    def test_the_kept_volumes_own_size_or_memory_alone_is_created(self):
        self.kept_volume(100)
        self.assertIsNone(self.refusal(disk_gib=100))
        self.assertIsNone(self.refusal(memory_gib=24))

    def test_an_unfinished_volume_is_rebuilt_at_any_disk_size(self):
        self.kept_volume(100, record="building")
        self.assertIsNone(self.refusal(disk_gib=250))

    def test_the_disk_image_is_the_pool_volumes_allocation(self):
        self.assertIsNone(self.driver.disk_image())
        self.host.volumes["yolobox-box.qcow2"] = b""
        label, kib = self.driver.disk_image()
        self.assertIn("yolobox-box.qcow2", label)
        self.assertEqual(kib, 2 * 1024 * 1024)

    def test_the_disk_is_what_the_volume_reports(self):
        self.host.tagged_domain("box", FIRST_PORT, memory_gib=24, cpus=6)
        self.host.states["yolobox-box"] = "shut off"
        self.host.volumes["yolobox-box.qcow2"] = b"a whole disk"
        self.host.capacity["yolobox-box.qcow2"] = 120 << 30
        self.assertEqual(self.driver.disk_gib(), 120)

    def test_a_disk_that_is_not_whole_gib_is_an_error_not_a_rounded_one(self):
        self.host.tagged_domain("box", FIRST_PORT)
        self.host.volumes["yolobox-box.qcow2"] = b"a whole disk"
        self.host.capacity["yolobox-box.qcow2"] = (100 << 30) + 512
        with self.assertRaisesRegex(self.box.YoError, "disk is .* not a whole number"):
            self.driver.disk_gib()

    def test_a_shut_off_domain_gets_its_volume_resized_and_a_running_one_does_not(self):
        self.host.tagged_domain("box", FIRST_PORT)
        self.host.volumes["yolobox-box.qcow2"] = b"a whole disk"
        with (
            mock.patch.object(self.box.ui, "out"),
            self.assertRaisesRegex(
                self.box.YoError, "only while the domain is shut off"
            ),
        ):
            locked(self.box, self.driver.grow_disk, 200)
        self.assertEqual(self.host.calls_of("vol-resize"), [])
        self.host.states["yolobox-box"] = "shut off"
        with mock.patch.object(self.box.ui, "out"):
            locked(self.box, self.driver.grow_disk, 200)
        self.assertEqual(
            self.host.calls_of("vol-resize")[0][3:],
            ["vol-resize", "--pool", "default", "yolobox-box.qcow2", "200G"],
        )
        self.assertEqual(self.host.capacity["yolobox-box.qcow2"], 200 << 30)


class TestOwnDomain(LibvirtCase):
    """A domain yo reaches by name must carry its own tag."""

    def test_a_domain_of_this_name_that_yo_did_not_define_is_an_error_not_a_box(self):
        # Every command reaches the domain through domain_exists, so none
        # stops, resizes or destroys a domain of another tool.
        tagged = self.host.domains.get
        self.host.tagged_domain("box", FIRST_PORT)
        foreign = re.sub(
            r"<metadata>.*</metadata>", "", tagged("yolobox-box"), flags=re.S
        )
        other = tagged("yolobox-box").replace(
            ">box</yolobox:instance>", ">other</yolobox:instance>"
        )
        for xml, said in ((foreign, "has none"), (other, "says 'other'")):
            with self.subTest(said=said):
                self.host.domains["yolobox-box"] = xml
                for look in (
                    self.driver.vm_status,
                    self.driver.ssh_port,
                    lambda: locked(self.box, self.driver.stop),
                    self.driver.removals,
                ):
                    with self.assertRaises(self.box.YoError) as caught:
                        look()
                    self.assertIn("domain yolobox-box", caught.exception.message)
                    self.assertIn("is not box's", caught.exception.message)
                    self.assertIn(said, caught.exception.message)
        self.assertEqual(
            [sub for sub in self.host.subcommands() if sub not in READS], []
        )

    def test_a_domain_undefined_between_the_listing_and_its_tag_read_does_not_exist(
        self,
    ):
        self.host.tagged_domain("box", FIRST_PORT)
        self.host.before_dumpxml = lambda name: self.host.domains.pop(name, None)
        self.assertFalse(self.driver.domain_exists())


class TestKeepalive(LibvirtCase):
    def pid(self, output):
        self.host.master_output = output
        return self.driver.keepalive_pid()

    def test_the_pid_is_the_masters(self):
        self.assertEqual(self.pid("Master running (pid=4242)\r\n"), 4242)

    def test_no_master_means_no_pid(self):
        self.host.master_running = False
        self.assertIsNone(self.pid("Control socket connect(/x): No such file\n"))

    def test_an_answer_with_no_pid_and_no_missing_socket_is_an_error_showing_it(self):
        for running, output in ((True, "lizard\n"), (False, "Bad option: lizard\n")):
            with self.subTest(running=running):
                self.host.master_running = running
                with self.assertRaisesRegex(self.box.YoError, "lizard"):
                    self.pid(output)

    def test_a_pid_that_would_hit_init_is_not_a_pid(self):
        self.assertIsNone(self.pid("Master running (pid=1)\n"))

    def test_reading_the_pid_opens_no_master(self):
        self.host.master_running = False
        self.assertIsNone(self.driver.keepalive_pid())
        self.assertFalse(self.host.master_running)
        self.assertEqual([call for call in self.host.calls if "-O" not in call], [])


class FakeEventStream:
    """`virsh event DOM lifecycle --loop --timeout N`, as the Popen the driver reads.

    Each of `events` is the state the domain reaches, delivered as one printed
    line; the host's state follows the line out. Then the process ends the way
    `ending` says: "timeout" prints virsh's timeout lines and exits 1, "failed"
    exits 1 with a message on stderr, "clean" exits 0, and "hang" never ends, so
    a read that would wait for it fails the test instead of blocking it.
    With `off_at_eof` the domain goes off just as the stream ends, after the
    last line was printed.
    """

    def __init__(self, host, domain, events, ending, off_at_eof=False):
        self.host = host
        self.domain = domain
        self.pending = list(events)
        self.trailer = (
            ["event loop timed out\n", "events received: %d\n" % len(events)]
            if ending == "timeout"
            else []
        )
        self.hangs = ending == "hang"
        self.off_at_eof = off_at_eof
        self.exit_code = 0 if ending == "clean" else 1
        self.stderr = io.StringIO("lost connection\n" if ending == "failed" else "")
        self.stdout = self
        self.ended = False
        self.terminated = False

    def readline(self):
        if self.pending:
            state = self.pending.pop(0)
            self.host.states[self.domain] = state
            return "event 'lifecycle' for domain '%s': %s\n" % (self.domain, state)
        if self.trailer:
            return self.trailer.pop(0)
        assert not self.hangs, "read from a virsh event that never prints again"
        self.ended = True
        if self.off_at_eof:
            self.host.states[self.domain] = "shut off"
        return ""

    def poll(self):
        return self.exit_code if self.ended or self.terminated else None

    def wait(self):
        assert self.ended, "waited for a virsh event that is still running"
        return self.exit_code

    def terminate(self):
        self.terminated = True

    def communicate(self):
        return "", ""


class TestStop(LibvirtCase):
    def setUp(self):
        super().setUp()
        self.host.tagged_domain("box", FIRST_PORT)
        patcher = mock.patch.object(self.box.proc, "spawn", self.host.spawn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def stop(self, events=(), ending="hang"):
        self.host.event_script = (list(events), ending)
        locked(self.box, self.driver.stop)

    def test_the_watcher_is_armed_once_before_the_shutdown_is_asked_for(self):
        self.stop(["shut off"])
        watch = [
            call
            for call in self.host.calls
            if call[:1] == ["virsh"] and "event" in call
        ]
        self.assertEqual(
            watch,
            [
                [
                    "virsh",
                    "-c",
                    "qemu:///system",
                    "event",
                    "yolobox-box",
                    "lifecycle",
                    "--loop",
                    "--timeout",
                    "300",
                ]
            ],
        )
        order = [sub for sub in self.host.subcommands() if sub in ("event", "shutdown")]
        self.assertEqual(order, ["event", "shutdown"])
        self.assertEqual(self.vm_word(), "shut off")

    def test_events_that_are_not_the_stop_are_waited_past_by_the_same_watcher(self):
        self.stop(["in shutdown", "running", "shut off"])
        self.assertEqual(len(self.host.watchers), 1)
        self.assertEqual(self.vm_word(), "shut off")

    def test_a_stop_that_lands_before_any_event_is_seen_does_not_wait_for_one(self):
        self.host.shutdown_state = "shut off"
        self.stop()
        self.assertEqual(self.vm_word(), "shut off")

    def test_the_watcher_is_ended_once_the_guest_is_off(self):
        self.stop(["shut off"])
        self.assertTrue(self.host.watchers[0].terminated)

    def test_a_guest_already_in_shutdown_is_waited_for_without_a_second_request(self):
        self.host.states["yolobox-box"] = "in shutdown"
        self.stop(["shut off"])
        self.assertNotIn("shutdown", self.host.subcommands())

    def test_a_timeout_is_an_error_that_never_offers_a_power_cut(self):
        # yo down reaches this without proof the box is past its initrd.
        with self.assertRaises(self.box.YoError) as caught:
            self.stop(["in shutdown"], "timeout")
        message = caught.exception.message
        self.assertIn("yolobox-box did not shut down within 300 s", message)
        # No "once ssh answers", which never comes true in the initrd or in a
        # stage 2 hung in shutdown; a repeat is made visible by yo status.
        self.assertIn("Run yo -m box down again", message)
        self.assertIn("yo -m box status shows its state", message)
        self.assertNotIn("ssh answers", message)
        self.assertNotIn("destroy", message)
        self.assertEqual(self.vm_word(), "in shutdown")

    def test_a_domain_that_went_off_as_the_event_stream_ended_is_stopped(self):
        self.host.off_at_eof = True
        self.stop(["in shutdown"], "timeout")
        self.assertEqual(self.vm_word(), "shut off")

    def test_a_virsh_that_fails_is_an_error_with_its_message(self):
        with self.assertRaisesRegex(
            self.box.YoError, r"virsh event failed \(exit 1\): lost connection"
        ) as caught:
            self.stop([], "failed")
        self.assertNotIn("did not shut down", caught.exception.message)

    def test_a_virsh_that_ends_cleanly_without_the_stop_is_an_error(self):
        with self.assertRaisesRegex(self.box.YoError, r"virsh event failed \(exit 0\)"):
            self.stop(["in shutdown"], "clean")

    def test_a_refused_shutdown_is_surfaced_and_the_watcher_ended(self):
        self.host.fail["shutdown"] = "domain is not running"
        with self.assertRaisesRegex(self.box.YoError, "domain is not running"):
            self.stop()
        self.assertTrue(self.host.watchers[0].terminated)

    def test_a_guest_that_pauses_while_stopping_is_an_error_not_a_stop(self):
        for state, wake in (("paused", "resume"), ("pmsuspended", "dompmwakeup")):
            with self.subTest(state=state):
                self.host.watchers.clear()
                self.host.states["yolobox-box"] = "running"
                with self.assertRaises(self.box.YoError) as caught:
                    self.stop([state])
                message = caught.exception.message
                self.assertIn(
                    "`virsh -c qemu:///system %s yolobox-box`" % wake, message
                )
                self.assertIn("then run yo -m box down again", message)
                self.assertNotIn("destroy", message)

    def test_a_paused_guest_is_explained_by_a_full_host_disk(self):
        # qemu pauses on ENOSPC, as during an in-place encryption that fills
        # the host: free space and resume, never a power cut.
        self.host.states["yolobox-box"] = "paused"
        with self.assertRaises(self.box.YoError) as caught:
            locked(self.box, self.driver.stop)
        message = caught.exception.message
        self.assertIn("host's disk is full", message)
        self.assertIn("domblkerror yolobox-box", message)
        self.assertIn("Resume it with", message)
        self.assertNotIn("destroy", message)

    def test_bootstrap_paused_mid_reboot_says_the_disk_is_full_and_its_own_way(self):
        # cmd_bootstrap on the fake host: the build ran (so the box is in
        # stage 2), then the domain paused on a full host disk as it was told
        # to shut down. The abort names the cause and bootstrap's recovery,
        # never yo down's "do not force" beside it.
        self.host.states["yolobox-box"] = "paused"
        args = self.box.build_parser().parse_args(
            ["-m", "box", "bootstrap", "--version", "github:x/y/z"]
        )
        errors = []
        with (
            mock.patch.object(self.box.vm, "vm_up"),
            mock.patch.object(
                self.box.vm, "luks_device_fstype", return_value="crypto_LUKS"
            ),
            mock.patch.object(
                self.box.vm, "guest_generations", return_value=("g1", "g0")
            ),
            mock.patch.object(
                self.box.release,
                "locked_rev",
                return_value=self.box.release.LockedSource(
                    "0" * 40, "sha256-" + "A" * 43 + "="
                ),
            ),
            mock.patch.object(
                self.box.sshx,
                "ssh_run",
                return_value=mock.Mock(returncode=0, stdout="", stderr=""),
            ),
            mock.patch.object(self.box.ui, "out"),
            mock.patch.object(self.box.ui, "err", errors.append),
        ):
            with self.assertRaises(self.box.YoError):
                self.box.cmd_bootstrap(args)
        text = " ".join(errors)
        self.assertIn(
            "the box did not stop: domain yolobox-box is 'paused', so it will "
            "not shut down by itself. A domain pauses when its disk image cannot "
            "grow because the host's disk is full",
            text,
        )
        self.assertIn("domblkerror yolobox-box", text)
        self.assertIn(self.force_stop_command(), text)
        self.assertIn("It runs stage 2", text)
        self.assertNotIn("Do not force it off", text)

    def test_no_stop_message_down_can_reach_names_a_power_cut(self):
        for state in ("paused", "pmsuspended", "crashed", "lizard"):
            with self.subTest(state=state):
                self.host.states["yolobox-box"] = state
                with self.assertRaises(self.box.YoError) as caught:
                    locked(self.box, self.driver.stop)
                self.assertNotIn("destroy", caught.exception.message)
                self.assertNotIn(self.force_stop_command(), caught.exception.message)

    def test_a_state_yo_cannot_leave_does_not_send_the_user_back_to_down(self):
        # `yo down` would meet the same refusal, so the message names what
        # shows the cause instead.
        for state in ("crashed", "lizard"):
            with self.subTest(state=state):
                self.host.states["yolobox-box"] = state
                with self.assertRaises(self.box.YoError) as caught:
                    locked(self.box, self.driver.stop)
                message = caught.exception.message
                self.assertIn("does not know how to leave that state", message)
                self.assertIn(
                    "virsh -c qemu:///system domstate --reason yolobox-box", message
                )
                self.assertNotIn(self.box.yo_cmd("down"), message)

    def test_a_guest_that_is_paused_or_in_a_state_yo_does_not_know_is_not_asked_to_stop(
        self,
    ):
        for state in ("paused", "pmsuspended", "crashed", "lizard"):
            with self.subTest(state=state):
                self.host.states["yolobox-box"] = state
                with self.assertRaisesRegex(
                    self.box.YoError, "will not shut down by itself"
                ):
                    self.stop()
                self.assertEqual(self.host.watchers, [])
                self.assertNotIn("shutdown", self.host.subcommands())

    def test_a_stopped_domain_needs_nothing(self):
        self.host.states["yolobox-box"] = "shut off"
        self.stop()
        self.assertEqual(self.host.watchers, [])

    def test_a_domain_that_does_not_exist_is_an_error(self):
        del self.host.domains["yolobox-box"]
        with self.assertRaisesRegex(self.box.YoError, "does not exist"):
            locked(self.box, self.driver.stop)


class TestUpWithASize(LibvirtCase):
    """`yo up --memory/--disk` on a host where the box has no domain yet."""

    def up(self, *flags):
        with (
            mock.patch.object(self.driver, "require_tool"),
            mock.patch.object(self.box.agent, "luks_passphrase") as passphrase,
            self.assertRaises(self.box.YoError) as caught,
        ):
            self.box.main(["-m", "box", "up", *flags])
        passphrase.assert_not_called()
        for write in ("vol-delete", "vol-create-as", "define", "start"):
            self.assertNotIn(write, self.host.subcommands())
        return caught.exception.message

    def test_a_disk_the_kept_volume_does_not_have_is_refused_before_the_passphrase(
        self,
    ):
        self.kept_volume(100)
        refusal = self.up("--disk", "250")
        self.assertIn("finished 100 GiB disk", refusal)
        self.assertIn("--disk 250", refusal)
        self.assertEqual(self.host.volumes["yolobox-box.qcow2"], b"a whole disk")

    def test_more_memory_than_the_host_has_is_refused_before_the_passphrase(self):
        self.host.foreign_domain("win", 6 << 20)
        refusal = self.up("--memory", str(HOST_MEMORY_GIB - 4 - 6 + 1))
        self.assertIn("--memory %d does not fit" % (HOST_MEMORY_GIB - 9), refusal)
        self.assertIn("(win 6.0 GiB)", refusal)


class TestDown(LibvirtCase):
    # The initrd has no ACPI handler, so `virsh shutdown` on a box parked there
    # would wait out the whole stop timeout.
    def setUp(self):
        super().setUp()
        self.host.tagged_domain("box", FIRST_PORT)
        for patcher in (
            mock.patch.object(self.box.proc, "spawn", self.host.spawn),
            mock.patch.object(self.box.ui, "out"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def down(self, stage, ask_pending=True):
        self.host.event_script = (["shut off"], "hang")
        with (
            mock.patch.object(
                self.box.unlock, "boot_stage", return_value=self.box.Stage(stage, "")
            ),
            mock.patch.object(
                self.box.unlock, "initrd_ask_pending", return_value=ask_pending
            ),
        ):
            self.box.cmd_down(None)

    def test_a_box_waiting_in_its_initrd_is_powered_off_without_a_shutdown_request(
        self,
    ):
        self.down(self.box.BootStage.INITRD)
        self.assertEqual(self.host.calls_of("destroy")[0][4:], ["yolobox-box"])
        self.assertEqual(self.host.watchers, [])
        self.assertNotIn("shutdown", self.host.subcommands())
        self.assertEqual(self.vm_word(), "shut off")

    def test_a_box_that_is_up_or_unanswering_is_asked_to_shut_down(self):
        for stage in (self.box.BootStage.STAGE2, self.box.BootStage.NONE):
            with self.subTest(stage=stage):
                self.host.watchers.clear()
                self.host.states["yolobox-box"] = "running"
                self.down(stage)
                self.assertIn("shutdown", self.host.subcommands())
                self.assertEqual(self.host.calls_of("destroy"), [])

    def test_a_box_with_no_ask_pending_in_its_initrd_is_neither_destroyed_nor_asked_to_shut_down(
        self,
    ):
        with self.assertRaises(self.box.YoError) as caught:
            self.down(self.box.BootStage.INITRD, ask_pending=False)
        self.assertIn("not stopping it", caught.exception.message)
        self.assertIn("may be encrypting its disk", caught.exception.message)
        # Never a dead end: the operator who knows nothing encrypts has the command.
        self.assertIn(
            "virsh -c qemu:///system destroy yolobox-box", caught.exception.message
        )
        self.assertEqual(self.host.calls_of("destroy"), [])
        self.assertNotIn("shutdown", self.host.subcommands())

    def test_a_domain_already_off_is_not_destroyed(self):
        self.host.states["yolobox-box"] = "shut off"
        self.down(self.box.BootStage.INITRD)
        self.assertEqual(self.host.calls_of("destroy"), [])

    def test_a_refused_destroy_is_surfaced(self):
        self.host.fail["destroy"] = "operation failed"
        with self.assertRaisesRegex(self.box.YoError, "operation failed"):
            self.down(self.box.BootStage.INITRD)

    def test_a_domain_that_does_not_exist_is_an_error(self):
        del self.host.domains["yolobox-box"]
        with self.assertRaisesRegex(self.box.YoError, "does not exist"):
            self.down(self.box.BootStage.INITRD)


class TestDestroy(LibvirtCase):
    """`yo destroy` lists what this machine has, and with --yes removes exactly that."""

    TILDE_INSTANCE_DIR = "~/.local/state/yolobox/instances/box"

    def setUp(self):
        super().setUp()
        self.host.tagged_domain("box", FIRST_PORT)
        self.host.tagged_domain("other", FIRST_PORT + 1)
        self.host.agent_master_running = True
        self.host.volumes = {
            "yolobox-box.qcow2": b"",
            "yolobox-box-cidata.iso": b"",
            "yolobox-other.qcow2": b"",
        }
        self.host.herdr_machines = [
            {"id": "m-7", "label": "ops", "target": "box"},
            {"id": "m-8", "label": "ops", "target": "other"},
        ]
        self.instance_dir = Path(self.box.state.instance_dir())
        (self.instance_dir / "operator.pub").write_text(PUBLIC_KEY)
        self.lock_file = Path(self.box.state.state_dir()) / "box.box.lock"
        self.lines = []
        for patcher in (
            mock.patch.object(self.box.ui, "out", side_effect=self.lines.append),
            mock.patch.object(self.box.shutil, "which", return_value=HERDR),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def destroy(self, yes):
        self.box.cmd_destroy(argparse.Namespace(yes=yes))

    def forget_the_box(self):
        """Take away everything this machine had, leaving the other one."""
        del self.host.domains["yolobox-box"], self.host.states["yolobox-box"]
        del self.host.volumes["yolobox-box.qcow2"]
        del self.host.volumes["yolobox-box-cidata.iso"]
        self.host.herdr_machines.pop(0)
        self.host.master_running = False
        self.host.agent_master_running = False
        shutil.rmtree(self.instance_dir)
        self.host.calls.clear()

    def assert_the_other_machine_is_untouched(self):
        self.assertIn("yolobox-other", self.host.domains)
        self.assertEqual(list(self.host.volumes), ["yolobox-other.qcow2"])
        self.assertEqual([m["id"] for m in self.host.herdr_machines], ["m-8"])

    def mutations(self):
        return [s for s in self.host.subcommands() if s not in READS]

    def test_yes_removes_every_piece_of_a_running_box_and_only_that_box(self):
        def assert_lock_held_while_it_mutates(argv, **kwargs):
            if argv[0] == "virsh" and argv[3] not in READS:
                self.assertTrue(box_lock_is_held(self.box.state))
            return self.host.run(argv, **kwargs)

        with mock.patch.object(
            self.box.proc, "run", side_effect=assert_lock_held_while_it_mutates
        ):
            self.destroy(yes=True)
        self.assertNotIn("yolobox-box", self.host.domains)
        self.assertFalse(self.instance_dir.exists())
        self.assertFalse(self.host.master_running)
        self.assertFalse(self.host.agent_master_running)
        self.assert_the_other_machine_is_untouched()
        self.assertEqual(
            self.mutations(), ["destroy", "undefine", "vol-delete", "vol-delete"]
        )
        self.assertIn("delete volume yolobox-box.qcow2", self.lines)

    def test_the_agents_master_quits_before_the_domain_is_touched(self):
        self.destroy(yes=True)
        first_virsh_change = next(
            i
            for i, call in enumerate(self.host.calls)
            if call[:1] == ["virsh"] and call[3] in ("destroy", "undefine")
        )
        agent_exit = next(
            i
            for i, call in enumerate(self.host.calls)
            if "exit" in call and "User=agent" in call
        )
        self.assertLess(agent_exit, first_virsh_change)

    def test_the_boxs_broker_stops_while_its_master_and_directory_are_there(self):
        # The broker watches the operator's master: closed first, the broker
        # would end by itself and unlink its sockets while the directory goes.
        seen = []

        def stop(pid):
            seen.append((pid, self.host.master_running, self.instance_dir.exists()))

        self.host.master_running = True
        with (
            mock.patch.object(
                self.box.aws, "aws_broker_health", return_value={"pid": 77}
            ),
            mock.patch.object(self.box.aws, "stop_broker", side_effect=stop),
        ):
            self.destroy(yes=True)
        self.assertEqual(seen, [(77, True, True)])
        self.assertFalse(self.instance_dir.exists())

    def test_a_box_that_is_already_off_is_not_powered_off_again(self):
        self.host.states["yolobox-box"] = "shut off"
        self.destroy(yes=True)
        self.assertEqual(self.host.calls_of("destroy"), [])
        self.assertNotIn("yolobox-box", self.host.domains)

    def test_a_domain_another_yo_starts_while_destroy_waits_is_powered_off_first(self):
        # The list is made under the lock: a yo up that wins the lock first may
        # start the box that was off, and undefining it running leaves qemu on a
        # deleted volume.
        self.host.states["yolobox-box"] = "shut off"
        take_the_lock = self.box.state.box_lock

        @contextlib.contextmanager
        def lock_after_a_yo_up_started_the_box(wait):
            self.host.states["yolobox-box"] = "running"
            with take_the_lock(wait) as held:
                yield held

        with mock.patch.object(
            self.box.state, "box_lock", lock_after_a_yo_up_started_the_box
        ):
            self.destroy(yes=True)
        self.assertEqual(self.mutations()[:2], ["destroy", "undefine"])

    def test_a_domain_started_after_the_steps_were_listed_is_powered_off_before_its_undefine(
        self,
    ):
        # The lock does not keep a `yo up` from `virsh start`: the state read when
        # the list was made is stale by the time the undefine runs.
        self.host.states["yolobox-box"] = "shut off"
        steps = self.box.destruction_steps()
        self.assertNotIn("power off domain yolobox-box", [step.what for step in steps])
        self.host.states["yolobox-box"] = "running"
        self.host.calls.clear()
        undefine = next(step for step in steps if step.what.startswith("undefine"))
        locked(self.box, undefine.remove)
        self.assertEqual(self.mutations(), ["destroy", "undefine"])
        self.assertNotIn("yolobox-box", self.host.domains)

    def test_without_yes_nothing_changes_and_each_piece_is_named(self):
        self.destroy(yes=False)
        self.assertLessEqual(set(self.host.subcommands()), READS)
        self.assertIn("yolobox-box", self.host.domains)
        self.assertEqual(
            set(self.host.volumes),
            {"yolobox-box.qcow2", "yolobox-box-cidata.iso", "yolobox-other.qcow2"},
        )
        self.assertTrue(self.instance_dir.exists())
        self.assertTrue(self.host.master_running)
        self.assertTrue(self.host.agent_master_running)
        self.assertEqual([m["id"] for m in self.host.herdr_machines], ["m-7", "m-8"])
        listing = "\n".join(self.lines)
        for piece in (
            "close the operator's ssh master",
            "close the agent's ssh master",
            "power off domain yolobox-box",
            "undefine domain yolobox-box",
            "delete volume yolobox-box.qcow2",
            "delete volume yolobox-box-cidata.iso",
            "delete directory " + self.TILDE_INSTANCE_DIR,
            "delete the herdr machine box",
        ):
            with self.subTest(piece=piece):
                self.assertIn(piece, listing)
        self.assertNotIn("yolobox-other", listing)
        self.assertNotIn("box.lock", listing)
        self.assertIn("yo -m box destroy --yes", self.lines[-1])

    def test_a_box_with_nothing_left_has_nothing_to_destroy(self):
        self.forget_the_box()
        for yes in (False, True):
            with self.subTest(yes=yes):
                self.lines.clear()
                self.destroy(yes=yes)
                self.assertEqual(self.lines, ["box has nothing to destroy"])
                self.assertLessEqual(set(self.host.subcommands()), READS)
        self.assert_the_other_machine_is_untouched()

    def test_the_lock_file_stays_so_a_waiting_yo_and_the_next_one_share_it(self):
        with self.box.state.box_lock(wait=True):
            pass
        waiting_yo = os.open(self.lock_file, os.O_RDWR)
        self.addCleanup(os.close, waiting_yo)
        self.destroy(yes=True)
        self.assertTrue(self.lock_file.exists())
        self.assertEqual(os.fstat(waiting_yo).st_nlink, 1)

    def test_pieces_that_are_gone_are_skipped_and_the_rest_removed(self):
        del self.host.volumes["yolobox-box-cidata.iso"]
        self.host.herdr_machines.pop(0)
        self.destroy(yes=True)
        self.assertEqual(self.mutations().count("vol-delete"), 1)
        self.assertNotIn("yolobox-box", self.host.domains)
        self.assertFalse(self.instance_dir.exists())

    def test_a_master_that_survives_its_exit_stops_the_run(self):
        self.host.master_survives_exit = True
        with self.assertRaisesRegex(self.box.YoError, "still running after"):
            self.destroy(yes=True)
        self.assertEqual(self.mutations(), [])
        self.assertTrue(self.instance_dir.exists())

    def test_a_failing_step_is_raised_and_stops_the_run(self):
        self.host.fail["undefine"] = "domain is busy"
        with self.assertRaisesRegex(self.box.YoError, "domain is busy"):
            self.destroy(yes=True)
        self.assertIn("yolobox-box", self.host.domains)
        self.assertEqual(self.host.calls_of("vol-delete"), [])
        self.assertTrue(self.instance_dir.exists())

    def test_a_failing_herdr_removal_is_raised(self):
        self.host.herdr_remove_fails = True
        with self.assertRaisesRegex(
            self.box.YoError, "machine remove m-7 failed.*no such machine"
        ):
            self.destroy(yes=True)

    def test_a_box_another_yo_is_acting_on_is_not_destroyed(self):
        with box_lock_held_elsewhere(self.box.state):
            with self.assertRaisesRegex(
                self.box.YoError, "another yo is acting on box"
            ):
                self.destroy(yes=True)
        self.assertLessEqual(set(self.host.subcommands()), READS)
        self.assertIn("yolobox-box", self.host.domains)
        self.assertTrue(self.instance_dir.exists())

    def test_the_listing_does_not_wait_for_a_yo_acting_on_the_box(self):
        with box_lock_held_elsewhere(self.box.state):
            self.destroy(yes=False)
        self.assertIn("yo -m box destroy --yes", self.lines[-1])


class TestADeadAgent(LibvirtCase):
    """1Password's agent is gone. What needs no login still works, through
    main() and the real boot_stage; a start names the agent before anything
    is created, and a session reports what ssh said."""

    DEAD = "1Password's SSH agent at %s is not running (or locked)"
    REFUSED = "Connection closed by authenticating user op 127.0.0.1 port 1 [preauth]"

    def setUp(self):
        super().setUp()
        self.host.tagged_domain("box", FIRST_PORT)
        self.dead_agent()
        # What a login without the key gets, from either sshd.
        self.host.sshd = lambda argv: (255, self.REFUSED)
        self.lines = []
        for patcher in (
            mock.patch.object(self.box.box.driver, "require_tool"),
            mock.patch.object(self.box.proc, "spawn", self.host.spawn),
            mock.patch.object(self.box.ui, "out", self.lines.append),
            mock.patch.object(
                self.box.unlock,
                "ssh_banner",
                return_value="SSH-2.0-OpenSSH_10.6 " + self.box.STAGE2_SSH_BANNER_MARK,
            ),
            mock.patch.object(
                self.box.driver, "loopback_port_bound", return_value=False
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def dead(self):
        return self.DEAD % self.driver.op_sock()

    def ssh_calls(self):
        return [argv for argv in self.host.calls if argv[0] == "ssh"]

    def test_down_stops_the_box_it_cannot_log_in_to(self):
        self.host.event_script = (["shut off"], "hang")
        self.box.main(["-m", "box", "down"])
        self.assertIn("shutdown", self.host.subcommands())
        self.assertEqual(self.vm_word(), "shut off")
        self.assertFalse(
            [argv for argv in self.ssh_calls() if argv[-1] != "true"],
            "down ran more than the boot-stage probes",
        )

    def test_destroy_deletes_the_box_it_cannot_log_in_to(self):
        instance_dir = Path(self.box.state.instance_dir())
        instance_dir.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(self.box.shutil, "which", return_value=None):
            self.box.main(["-m", "box", "destroy", "--yes"])
        self.assertNotIn("yolobox-box", self.host.domains)
        self.assertFalse(instance_dir.exists())
        self.assertFalse(self.host.master_running)
        self.assertFalse(
            [argv for argv in self.ssh_calls() if argv[-1] == "true"],
            "destroy logged in",
        )

    def test_destroy_without_yes_only_lists(self):
        with mock.patch.object(self.box.shutil, "which", return_value=None):
            self.box.main(["-m", "box", "destroy"])
        self.assertIn("yolobox-box", self.host.domains)
        self.assertLessEqual(set(self.host.subcommands()), READS)
        self.assertIn("undefine domain yolobox-box", "\n".join(self.lines))
        self.assertIn("yo -m box destroy --yes", self.lines[-1])

    def test_down_refuses_an_sshd_it_cannot_identify_and_names_the_login(self):
        # The initrd's banner and root refused: yo cannot look inside, and the
        # initrd may be encrypting the disk, so neither stop is safe.
        self.host.states["yolobox-box"] = "running"
        with mock.patch.object(
            self.box.unlock,
            "ssh_banner",
            return_value="SSH-2.0-OpenSSH_10.6 " + INITRD_SSH_BANNER_MARK,
        ):
            with self.assertRaises(self.box.YoError) as caught:
                self.box.main(["-m", "box", "down"])
        message = caught.exception.message
        self.assertTrue(message.startswith("not stopping it: "), message)
        self.assertIn(self.driver.hints.login_fix, message)
        # The passphrase may have gone in earlier this boot: no force offered.
        self.assertNotIn(self.force_stop_command(), message)
        self.assertNotIn("shutdown", self.host.subcommands())
        self.assertNotIn("destroy", self.host.subcommands())
        self.assertEqual(self.vm_word(), "running")

    def test_a_start_with_a_cached_passphrase_names_the_agent_before_anything(self):
        # bootstrap's second vm_up: the box stopped, the passphrase in hand.
        self.host.states["yolobox-box"] = "shut off"
        with self.assertRaises(self.box.YoError) as caught:
            locked(
                self.box,
                self.box.vm_up,
                run_services=False,
                passphrase=self.derived(b"cached"),
            )
        self.assertTrue(
            caught.exception.message.startswith(self.dead()), caught.exception.message
        )
        self.assertNotIn("start", self.host.subcommands())
        self.assertEqual(self.ssh_calls(), [])

    def test_status_reports_the_box_unreachable_and_the_rest(self):
        with self.assertRaises(self.box.YoError) as caught:
            self.box.main(["-m", "box", "status"])
        self.assertEqual(caught.exception.code, 1)
        # The operator's refused login is shown in ssh's own words.
        self.assertEqual(
            self.lines[0],
            "vm: unreachable (its sshd did not let the operator in; ssh said: "
            "Connection closed by authenticating user op 127.0.0.1 port 1 "
            "[preauth]) — is it up? start it with: yo -m box up",
        )
        self.assertTrue(
            any(line.startswith("aws: ") for line in self.lines), self.lines
        )

    def test_yo_ssh_still_reaches_the_box_and_says_why_the_forwards_are_down(self):
        self.host.master_running = False
        errors = []
        with (
            mock.patch.object(self.box.proc, "exec_process") as exec_ssh,
            mock.patch.object(self.box.ui, "err", errors.append),
        ):
            self.box.main(["-m", "box", "ssh"])
        (warning,) = errors
        self.assertIn("box does not take the operator's login", warning)
        self.assertIn(self.REFUSED, warning)
        self.assertEqual(self.host.forwarded, [])
        (argv,), _ = exec_ssh.call_args
        self.assertEqual(argv[0], "ssh")

    def test_port_rides_a_live_master_without_the_agent(self):
        self.box.main(["-m", "box", "port", "5173"])
        self.assertEqual(len(self.lines), 1)
        self.assertRegex(self.lines[0], r"^127\.0\.0\.1:\d+$")
        (port_forward,) = [argv for argv in self.host.forwarded if "-L" in argv]
        self.assertIn(
            "127.0.0.1:%s:127.0.0.1:5173" % self.lines[0].split(":")[1], port_forward
        )

    def test_port_without_a_master_reports_what_ssh_said(self):
        self.host.master_running = False
        with self.assertRaises(self.box.Unreachable) as caught:
            self.box.main(["-m", "box", "port", "5173"])
        self.assertIn(self.REFUSED, caught.exception.message)
        self.assertEqual(self.host.forwarded, [])


class TestAnInitrdThatNeverAnswersPointsAtWhatTheHostShows(LibvirtCase):
    def test_the_timeout_names_the_domain_state_and_status_not_a_console(self):
        # virsh console keeps no history and the initrd writes to tty0, so
        # it would show nothing; the domain state and the cidata disk do.
        self.host.tagged_domain("box", FIRST_PORT)
        last = self.box.unlock.Round("no banner", self.box.unlock.where_to_look())
        text = self.box.unlock.initrd_timeout_error(last).message
        self.assertIn("did not let the operator in", text)
        self.assertIn("virsh -c qemu:///system domstate --reason yolobox-box", text)
        self.assertIn("yolobox-box-cidata.iso", text)
        self.assertNotIn("console", text)


class TestAnSshdYoCannotIdentify(LibvirtCase):
    """The key is on offer, yet the initrd's sshd refused root: yo cannot tell
    an initrd waiting for its passphrase from one encrypting the disk, so it
    neither starts nor stops."""

    def setUp(self):
        super().setUp()
        self.host.tagged_domain("box", FIRST_PORT)
        self.host.sshd = lambda argv: (255, "Permission denied (publickey).")
        for patcher in (
            mock.patch.object(self.box.proc, "spawn", self.host.spawn),
            mock.patch.object(
                self.box.unlock,
                "ssh_banner",
                return_value="SSH-2.0-OpenSSH_10.6 " + INITRD_SSH_BANNER_MARK,
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def assert_names_the_way_out(self, text):
        # Fix the login and run again; never a force (the initrd may be
        # encrypting), never "see yo status", which would only refuse again.
        self.assertIn("Fix the login", text)
        self.assertIn("run the command again", text)
        self.assertNotIn("console", text)
        self.assertNotIn(self.force_stop_command(), text)
        self.assertNotIn("destroy", text)
        self.assertNotIn("yo -m box status", text)
        self.assertIn(self.driver.hints.login_fix, text)

    def test_the_unknown_text_names_no_force(self):
        # A listed key may not sign, and root's probe may only have timed out:
        # the initrd may be encrypting whatever the login state says.
        self.assert_names_the_way_out(
            self.box.unlock.unknown_stage_message(
                self.box.Stage(self.box.BootStage.UNKNOWN, "")
            )
        )

    def test_down_refuses_names_the_way_out_and_stops_nothing(self):
        with self.assertRaises(self.box.YoError) as caught:
            self.box.cmd_down(None)
        self.assertIn(
            "not stopping it: the initrd's sshd answers", caught.exception.message
        )
        self.assert_names_the_way_out(caught.exception.message)
        self.assertNotIn("shutdown", self.host.subcommands())
        self.assertNotIn("destroy", self.host.subcommands())

    def test_up_refuses_instead_of_taking_it_for_stage2(self):
        with self.assertRaises(self.box.YoError) as caught:
            locked(self.box, self.box.vm_up, passphrase=self.derived(b"cached"))
        self.assertIn("refused root's key", caught.exception.message)
        self.assert_names_the_way_out(caught.exception.message)

    def test_status_names_the_way_out_and_runs_no_pinned_probe(self):
        lines = []
        with (
            mock.patch.object(self.box.box.driver, "require_tool"),
            mock.patch.object(self.box.ui, "out", lines.append),
        ):
            with self.assertRaises(self.box.YoError) as caught:
                self.box.main(["-m", "box", "status"])
        self.assertEqual(caught.exception.code, 1)
        vm_lines = [line for line in lines if line.startswith("vm: ")]
        self.assertEqual(len(vm_lines), 1, lines)
        self.assertIn("the initrd's sshd answers", vm_lines[0])
        self.assert_names_the_way_out(vm_lines[0])
        self.assertNotIn("start it with", vm_lines[0])
        # Only the unpinned boot-stage probes ran; the pinned agent probe, which
        # reads the ssh.config and its known_hosts, did not.
        pinned = [
            argv
            for argv in self.host.calls
            if argv[0] == "ssh" and "-F" in argv and "/dev/null" not in argv
        ]
        self.assertEqual(pinned, [])


class TestEnsureSession(LibvirtCase):
    """`yo port` rides the operator's master alone: it forwards no reverse
    socket and probes nothing, whether a master answered or a login opened one.
    A session command declares both reverse forwards."""

    def setUp(self):
        super().setUp()
        self.host.tagged_domain("box", FIRST_PORT)

    def port(self):
        """`yo port 5173`, and the host port it printed."""
        lines = []
        with (
            mock.patch.object(self.box.box.driver, "require_tool"),
            mock.patch.object(self.box.ui, "out", lines.append),
            mock.patch.object(
                self.box.driver, "loopback_port_bound", return_value=False
            ),
        ):
            self.box.main(["-m", "box", "port", "5173"])
        (line,) = lines
        return line

    def reverse_forwards(self):
        return [
            argv[argv.index("-R") + 1] for argv in self.host.forwarded if "-R" in argv
        ]

    def assert_forwarded_only_the_port(self, printed):
        (forward,) = self.host.forwarded
        self.assertEqual(self.reverse_forwards(), [])
        self.assertIn("127.0.0.1:%s:127.0.0.1:5173" % printed.split(":")[1], forward)

    def test_a_master_that_answers_gets_only_the_port_forward(self):
        self.assert_forwarded_only_the_port(self.port())
        self.assertEqual([argv for argv in self.host.calls if "exit" in argv], [])

    def test_a_dead_1password_forward_does_not_keep_the_port_from_forwarding(self):
        self.host.sshd = lambda argv: (
            (2, "agent failed") if "ssh-add" in argv[-1] else (0, "")
        )
        self.host.master_session = (0, "")
        self.assert_forwarded_only_the_port(self.port())
        asked = [argv[-1] for argv in self.host.calls if argv[0] == "ssh"]
        self.assertEqual([a for a in asked if "ssh-add" in a or "systemctl" in a], [])

    def test_a_master_whose_connection_died_fails_with_ssh_s_words(self):
        self.host.master_session = (255, "Connection to lima-box closed")
        with self.assertRaisesRegex(self.box.YoError, "Connection to lima-box closed"):
            self.port()
        self.assertEqual([argv for argv in self.host.calls if "exit" in argv], [])

    def test_a_session_command_logs_in_over_the_masters_alone(self):
        # Every session command declares the services; on libvirt each fresh
        # login is a 1Password signature, so all of it rides the masters.
        self.host.master_running = False
        with (
            mock.patch.object(self.box.box.driver, "require_tool"),
            mock.patch.object(self.box.proc, "exec_process"),
            mock.patch.object(self.box.ui, "err"),
        ):
            self.box.main(["-m", "box", "ssh"])
        fresh = [
            argv
            for argv in self.host.calls
            if argv[0] == "ssh" and "ControlPath=none" in argv
        ]
        self.assertEqual(fresh, [])
        self.assertEqual(len(self.reverse_forwards()), 2)

    def test_with_no_master_a_login_opens_one_and_only_the_port_is_forwarded(self):
        self.host.master_running = False
        self.assert_forwarded_only_the_port(self.port())
        self.assertTrue(self.host.master_running)


class TestDefinition(LibvirtCase):
    """A stopped domain is defined again from libvirt/domain.xml on every start,
    with the uuid, port and size libvirt holds of it; one that is up is left as
    it runs."""

    def setUp(self):
        super().setUp()
        self.facts = self.host.tagged_domain("box", FIRST_PORT)
        self.host.states["yolobox-box"] = "shut off"
        self.host.volumes["yolobox-box.qcow2"] = b"a whole disk"
        self.tampered = self.host.domains["yolobox-box"].replace(
            "<video>", "<tpm model='tpm-crb'/><video>"
        )
        self.host.domains["yolobox-box"] = self.tampered
        for patcher in (
            mock.patch.object(
                self.box.shutil, "which", return_value="/usr/bin/getfacl"
            ),
            mock.patch.object(self.driver, "operator_key", return_value=KEY_BLOB),
            mock.patch.object(self.box.unlock, "unlock_disk"),
            mock.patch.object(self.box.unlock, "wait_for_operator"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def start(self):
        locked(self.box, self.driver.start, lambda: b"pass")

    def test_a_stopped_domain_is_defined_again_from_the_template_on_up(self):
        self.start()
        subs = self.host.subcommands()
        self.assertLess(subs.index("define"), subs.index("start"))
        self.assertEqual(
            self.host.domains["yolobox-box"],
            as_libvirt_prints(self.box.domain_xml("box", self.facts, VM_UID, VM_GID)),
        )
        self.assertNotIn("tpm", self.host.domains["yolobox-box"])
        self.assertEqual(self.driver.ssh_port(), FIRST_PORT)
        self.assertEqual(self.host.states["yolobox-box"], "running")

    def test_a_domain_that_is_up_is_left_as_it_runs(self):
        for domain_state in ("running", "paused"):
            with self.subTest(domain_state=domain_state):
                self.host.states["yolobox-box"] = domain_state
                self.host.calls.clear()
                self.start()
                self.assertNotIn("define", self.host.subcommands())
                self.assertEqual(self.host.domains["yolobox-box"], self.tampered)

    def test_a_definition_that_is_not_a_boxs_is_refused_before_anything_is_defined(
        self,
    ):
        range_line = "<range start='%d' to='22'/>" % FIRST_PORT
        cases = (
            ("no range to port 22", range_line, "", "has 0 portForward ranges"),
            (
                "a range to another port",
                range_line,
                "<range start='%d' to='23'/>" % FIRST_PORT,
                "has 0 portForward ranges",
            ),
            (
                "two ranges to port 22",
                range_line,
                range_line + "<range start='61001' to='22'/>",
                "has 2 portForward ranges",
            ),
            (
                "a start that is no port",
                range_line,
                "<range start='lizard' to='22'/>",
                "start='lizard', which is not a port",
            ),
            (
                "a start of 0",
                range_line,
                "<range start='0' to='22'/>",
                "start='0', which is not a port",
            ),
            (
                "memory that is not whole GiB",
                "<memory unit='KiB'>16777216<",
                "<memory unit='KiB'>16777217<",
                "<memory> '16777217' KiB, which is not a positive whole number",
            ),
            (
                "memory in MiB",
                "<memory unit='KiB'>",
                "<memory unit='MiB'>",
                "<memory> in unit='MiB'",
            ),
            (
                "memory with no unit",
                "<memory unit='KiB'>",
                "<memory>",
                "<memory> in unit=None",
            ),
            (
                "memory of 0",
                "<memory unit='KiB'>16777216<",
                "<memory unit='KiB'>0<",
                "<memory> '0' KiB",
            ),
            (
                "memory of -1",
                "<memory unit='KiB'>16777216<",
                "<memory unit='KiB'>-1<",
                "<memory> '-1' KiB",
            ),
            (
                "memory of a lizard",
                "<memory unit='KiB'>16777216<",
                "<memory unit='KiB'>lizard<",
                "<memory> 'lizard' KiB",
            ),
            (
                "vcpu of a lizard",
                "'static'>8<",
                "'static'>lizard<",
                "<vcpu> 'lizard'",
            ),
            ("vcpu of 0", "'static'>8<", "'static'>0<", "<vcpu> '0'"),
            ("vcpu of -1", "'static'>8<", "'static'>-1<", "<vcpu> '-1'"),
            ("no vcpu", "<vcpu placement='static'>8</vcpu>", "", "<vcpu> ''"),
            (
                "no uuid",
                "<uuid>%s</uuid>" % self.facts.uuid,
                "",
                "domain yolobox-box has no <uuid>",
            ),
        )
        for label, old, new, said in cases:
            with self.subTest(label):
                self.assertIn(old, self.tampered)
                self.host.domains["yolobox-box"] = self.tampered.replace(old, new)
                self.host.calls.clear()
                with self.assertRaises(self.box.YoError) as caught:
                    self.start()
                self.assertIn("domain yolobox-box", caught.exception.message)
                self.assertIn(said, caught.exception.message)
                self.assertIn(
                    "yo -m box destroy --yes && yo -m box bootstrap",
                    caught.exception.message,
                )
                for step in ("define", "start", "vol-create-as", "vol-upload"):
                    self.assertNotIn(step, self.host.subcommands())

    def test_a_domain_of_the_boxs_name_that_is_not_the_boxs_is_refused_untouched(self):
        untagged = re.sub(r"<metadata>.*?</metadata>", "", self.tampered, flags=re.S)
        self.assertNotEqual(untagged, self.tampered)
        self.host.domains["yolobox-box"] = untagged
        for act in (self.start, self.driver.ssh_port):
            with self.subTest(act=act.__name__):
                self.host.calls.clear()
                with self.assertRaisesRegex(self.box.YoError, "is not box's.*has none"):
                    act()
                for step in ("define", "start", "vol-create-as", "vol-upload"):
                    self.assertNotIn(step, self.host.subcommands())
                self.assertEqual(self.host.domains["yolobox-box"], untagged)

    def test_a_definition_libvirt_refuses_is_quoted_with_the_way_out(self):
        self.host.fail["define"] = (
            "error: operation failed: domain 'yolobox-box' already exists with "
            "uuid another"
        )
        with self.assertRaises(self.box.YoError) as caught:
            self.start()
        self.assertIn("already exists with uuid another", caught.exception.message)
        self.assertIn(
            "yo -m box destroy --yes && yo -m box bootstrap", caught.exception.message
        )
        self.assertNotIn("start", self.host.subcommands())


class TestVmUpOnAnExistingBox(LibvirtCase):
    """`yo up` on a box whose domain exists: its size flags resize it while it
    is off, and every start is judged against the host's free memory."""

    DISK_GIB = 100

    def setUp(self):
        super().setUp()
        self.facts = self.host.tagged_domain("box", FIRST_PORT)
        self.host.states["yolobox-box"] = "shut off"
        self.host.volumes["yolobox-box.qcow2"] = b"a whole disk"
        self.host.capacity["yolobox-box.qcow2"] = self.DISK_GIB << 30
        self.starts = []

    @contextlib.contextmanager
    def stubbed(self):
        """Everything vm_up reaches around the box's own definition and start.
        The start is stubbed: it records the virsh calls made before it, and
        leaves the domain running."""
        stubs = {}

        def start(held, source):
            self.starts.append(self.host.subcommands())
            self.host.states["yolobox-box"] = "running"

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(self.box.ui, "out"))
            stack.enter_context(mock.patch.object(self.driver, "require_tool"))
            for target, name in (
                (self.box.agent, "luks_passphrase"),
                (self.box.agent, "luks_key"),
                (self.driver, "start"),
                (self.driver, "create"),
                (self.box.sshconfig, "write_ssh_config"),
                (self.box.guest, "ensure_guest_known_hosts"),
                (self.box.forwards, "vm_services"),
                (self.box.sshx, "bootstrapped"),
                (self.box.sshx, "broken_agent_login"),
            ):
                stubs[name] = stack.enter_context(mock.patch.object(target, name))
            stubs["start"].side_effect = start
            stubs["broken_agent_login"].return_value = None
            yield stubs

    def vm_up(self, size, restart=False):
        """Run vm_up; return the error it raised (or None) and its stubs."""
        with self.stubbed() as stubs:
            try:
                locked(self.box, self.box.vm_up, size=size, restart=restart)
            except self.box.YoError as failure:
                return failure, stubs
        return None, stubs

    def up(self, *flags):
        """`yo -m box up FLAGS`, as the operator types it."""
        with self.stubbed() as stubs:
            try:
                self.box.main(["-m", "box", "up", *flags])
            except self.box.YoError as failure:
                return failure, stubs
        return None, stubs

    def writes(self):
        return [sub for sub in self.host.subcommands() if sub not in READS]

    def defined(self):
        """The domain's definition, as libvirt holds it now."""
        return self.host.domains["yolobox-box"]

    def assert_refused_before_anything(self, error, stubs, says):
        self.assertIn(says, error.message)
        # Nothing was asked of 1Password: not even a signature, which only an
        # initrd's ask derives.
        stubs["luks_passphrase"].assert_not_called()
        stubs["luks_key"].assert_not_called()
        stubs["start"].assert_not_called()
        stubs["create"].assert_not_called()
        self.assertEqual(self.writes(), [])

    # What a resize does.

    def test_a_stopped_box_is_redefined_at_the_asked_size_and_then_started(self):
        error, stubs = self.vm_up(self.box.Size(24, None, 2))
        self.assertIsNone(error)
        self.assertEqual(
            self.defined(),
            as_libvirt_prints(
                self.box.domain_xml(
                    "box", self.facts._replace(memory_gib=24, cpus=2), VM_UID, VM_GID
                )
            ),
        )
        # Defined before the start, and only by `virsh define`: no set* call.
        (before_start,) = self.starts
        self.assertEqual([s for s in before_start if s not in READS], ["define"])
        # The key is listed before anything changes; no signature is asked.
        stubs["luks_key"].assert_called_once_with(self.driver.op_sock())
        stubs["luks_passphrase"].assert_not_called()

    def test_the_redefinition_keeps_the_uuid_the_port_and_the_templates_devices(self):
        self.host.domains["yolobox-box"] = self.defined().replace(
            "<video>", "<tpm model='tpm-crb'/><video>"
        )
        self.vm_up(self.box.Size(24, None, None))
        root = ET.fromstring(self.defined())
        self.assertEqual(root.findtext("uuid"), self.facts.uuid)
        self.assertEqual(self.driver.ssh_port(), FIRST_PORT)
        self.assertNotIn("tpm", self.defined())

    def test_memory_alone_leaves_the_cpus_alone_and_the_reverse(self):
        for size, memory, cpus in (
            (self.box.Size(24, None, None), 24, DOMAIN_CPUS),
            (self.box.Size(None, None, 2), self.facts.memory_gib, 2),
        ):
            with self.subTest(size=size):
                self.host.states["yolobox-box"] = "shut off"
                self.vm_up(size)
                got = self.driver.domain_facts()
                self.assertEqual((got.memory_gib, got.cpus), (memory, cpus))
                self.host.domains["yolobox-box"] = as_libvirt_prints(
                    self.box.domain_xml("box", self.facts, VM_UID, VM_GID)
                )

    def test_a_failed_definition_keeps_the_old_size_and_starts_nothing(self):
        before = self.defined()
        self.host.fail["define"] = "error: internal error: lizard"
        error, stubs = self.vm_up(self.box.Size(24, None, None))
        self.assertIn("lizard", error.message)
        self.assertIn("box keeps its old size", error.message)
        # Never a reason to destroy the box.
        self.assertNotIn("destroy", error.message)
        self.assertEqual(self.defined(), before)
        stubs["luks_passphrase"].assert_not_called()
        stubs["start"].assert_not_called()

    def test_the_same_flags_run_again_after_a_failed_start_resize_the_box_as_before(
        self,
    ):
        size = self.box.Size(24, None, 2)
        with self.stubbed() as stubs:
            stubs["start"].side_effect = self.box.YoError("virsh start failed")
            with self.assertRaisesRegex(self.box.YoError, "virsh start failed"):
                locked(self.box, self.box.vm_up, size=size)
        resized = self.defined()
        self.assertEqual(self.vm_word(), "shut off")
        error, _ = self.vm_up(size)
        self.assertIsNone(error)
        self.assertEqual(self.defined(), resized)
        self.assertEqual(self.vm_word(), "running")

    def test_memory_below_the_smallest_box_is_refused_when_the_flag_is_read(self):
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            self.box.build_parser().parse_args(["up", "--memory", "3"])
        self.assertEqual(caught.exception.code, 2)

    def test_bootstrap_resizes_a_stopped_box_the_same_way(self):
        class Stop(Exception):
            """Bootstrap's first step, vm_up, is over."""

        args = self.box.build_parser().parse_args(
            ["bootstrap", "--version", "github:x/y/z", "--memory", "24", "--cpus", "2"]
        )
        with (
            self.stubbed(),
            mock.patch.object(self.box.vm, "luks_device_fstype", side_effect=Stop),
            self.assertRaises(Stop),
        ):
            self.box.cmd_bootstrap(args)
        got = self.driver.domain_facts()
        self.assertEqual((got.memory_gib, got.cpus), (24, 2))
        self.assertEqual(len(self.starts), 1)

    # What a resize refuses, before anything changes.

    def test_memory_the_active_domains_leave_no_room_for_is_refused_naming_the_most_that_fits(
        self,
    ):
        # Plenty is free right now, but the other domain may grow to 40 GiB.
        self.host.foreign_domain("win", 40 << 20)
        error, stubs = self.up("--memory", "24")
        self.assert_refused_before_anything(error, stubs, "does not fit on this host")
        self.assertIn("reads MemTotal 64.0 GiB", error.message)
        self.assertIn("ask for at most --memory 20", error.message)

    def test_a_larger_disk_is_left_to_disk_grow_and_refused_before_anything(self):
        error, stubs = self.up("--memory", "24", "--disk", "200")
        self.assert_refused_before_anything(
            error, stubs, "only yo -m box disk-grow 200"
        )
        self.assertNotIn("--memory", error.message)

    def test_the_disk_grow_command_it_names_is_one_yo_runs(self):
        error, _ = self.up("--disk", "200")
        words = shlex.split(error.message.split("only ")[1].split(" does that")[0])
        self.assertEqual(words[:3], ["yo", "-m", "box"])
        args = self.box.build_parser().parse_args(words[3:])
        self.assertIs(args.func, self.box.cmd_disk_grow)
        self.assertEqual(args.size, 200)

    def test_a_smaller_disk_is_refused_before_anything(self):
        error, stubs = self.up("--disk", "50")
        self.assert_refused_before_anything(error, stubs, "ask for at least 100")

    def test_the_disk_the_box_has_is_no_change_and_the_box_starts(self):
        error, stubs = self.up("--disk", str(self.DISK_GIB))
        self.assertIsNone(error)
        self.assertEqual(self.host.calls_of("vol-resize"), [])
        stubs["start"].assert_called_once()

    # A box that is not off.

    def test_a_size_flag_on_a_box_that_is_up_or_stuck_is_refused_naming_its_state(
        self,
    ):
        for word in ("running", "blocked", "in shutdown", "paused", "pmsuspended"):
            with self.subTest(state=word):
                self.host.states["yolobox-box"] = word
                self.host.calls.clear()
                before = self.defined()
                error, stubs = self.up("--memory", "24", "--cpus", "2")
                self.assert_refused_before_anything(
                    error,
                    stubs,
                    "box is %s, so --memory 24 --cpus 2 cannot apply to it" % word,
                )
                self.assertEqual(self.defined(), before)

    def test_a_disk_the_box_already_has_on_a_running_box_is_refused_like_any_size_flag(
        self,
    ):
        self.host.states["yolobox-box"] = "running"
        error, stubs = self.up("--disk", str(self.DISK_GIB))
        self.assert_refused_before_anything(error, stubs, "cannot apply to it")

    def test_the_printed_remedy_runs_from_every_state_that_prints_it_and_ends_resized(
        self,
    ):
        for word in ("running", "blocked", "in shutdown", "paused", "pmsuspended"):
            with self.subTest(state=word):
                self.host.states["yolobox-box"] = word
                self.host.domains["yolobox-box"] = as_libvirt_prints(
                    self.box.domain_xml("box", self.facts, VM_UID, VM_GID)
                )
                error, _ = self.up("--memory", "24")
                # The remedy is pasted as printed: the command that brings a
                # stuck box back, then each command of the chain.
                for resume in error.message.split("`")[1:2]:
                    self.host.run(shlex.split(resume))
                down, up = [
                    shlex.split(command)
                    for command in error.message.rsplit(": ", 1)[1].split(" && ")
                ]
                self.assertEqual(down, ["yo", "-m", "box", "down"])
                self.host.event_script = (["shut off"], "hang")
                self.host.watchers.clear()
                with (
                    self.stubbed(),
                    mock.patch.object(self.box.proc, "spawn", self.host.spawn),
                    mock.patch.object(
                        self.box.unlock,
                        "boot_stage",
                        return_value=self.box.Stage(self.box.BootStage.STAGE2, ""),
                    ),
                ):
                    self.box.main(down[1:])
                self.assertEqual(self.vm_word(), "shut off")
                error, stubs = self.up(*up[4:])
                self.assertIsNone(error)
                self.assertEqual(self.driver.domain_facts().memory_gib, 24)
                self.assertEqual(self.vm_word(), "running")

    def test_a_paused_box_is_told_to_resume_it_first_with_the_command_that_does(self):
        self.host.states["yolobox-box"] = "paused"
        error, _ = self.up("--memory", "24")
        self.assertIn(
            "resume it with `virsh -c qemu:///system resume yolobox-box`, then stop it",
            error.message,
        )
        self.host.states["yolobox-box"] = "pmsuspended"
        error, _ = self.up("--memory", "24")
        self.assertIn(
            "wake it with `virsh -c qemu:///system dompmwakeup", error.message
        )

    # The start itself, at a size the host's free memory may not hold.

    def test_a_start_the_host_cannot_serve_refuses_before_any_change(self):
        # A refusal after the cidata sync or the definition would leave a host
        # changed for a box that never started.
        defined = self.defined()
        for gap, remedy in (
            ("passt directory", "sudo setfacl -m u:yolobox-vm"),
            ("resolver label", "sudo restorecon -F"),
        ):
            for domain in ("defined", "missing", "paused"):
                with self.subTest(gap=gap, domain=domain):
                    self.host.domains["yolobox-box"] = defined
                    self.host.states["yolobox-box"] = "shut off"
                    self.host.acl = getfacl_listing()
                    self.resolver_label = GOOD_LABEL
                    if gap == "passt directory":
                        self.host.acl = getfacl_listing(mask="r-x")
                    else:
                        self.resolver_label = WRONG_LABEL
                    if domain == "missing":
                        self.host.domains.clear()
                        self.host.states.clear()
                    if domain == "paused":
                        self.host.states["yolobox-box"] = "paused"
                    error, stubs = self.vm_up(self.box.NO_SIZE)
                    self.assert_refused_before_anything(error, stubs, remedy)

    def test_a_box_asked_for_nothing_is_started_as_it_is(self):
        error, stubs = self.vm_up(self.box.NO_SIZE)
        self.assertIsNone(error)
        ((held, source), _) = stubs["start"].call_args
        self.assertIsInstance(held, self.box.state.HeldBox)
        self.assertIsInstance(source, self.box.agent.PassphraseSource)
        # The key is listed before the start; it is derived only when an
        # initrd asks.
        stubs["luks_key"].assert_called_once_with(self.driver.op_sock())
        stubs["luks_passphrase"].assert_not_called()
        self.assertEqual(self.writes(), [])

    def free(self, gib, kib=0):
        """The host has `gib` GiB (less `kib`) of memory available, of its 64."""
        self.host_memory(HOST_MEMORY_GIB << 20, (gib << 20) - kib)

    def test_a_start_that_fits_exactly_beside_the_hosts_reserve_passes(self):
        self.free(self.facts.memory_gib + self.box.HOST_MEMORY_RESERVE_GIB)
        error, _ = self.vm_up(self.box.NO_SIZE)
        self.assertIsNone(error)

    def test_one_kib_short_is_refused_naming_every_number_and_the_way_out(self):
        self.free(self.facts.memory_gib + self.box.HOST_MEMORY_RESERVE_GIB, kib=1)
        error, stubs = self.vm_up(self.box.NO_SIZE)
        self.assert_refused_before_anything(
            error, stubs, "16 GiB of memory does not fit"
        )
        # The figures are rounded down, so a box that is short never reads as
        # one that fits, and no domain is named when none is active.
        self.assertEqual(
            error.message,
            "16 GiB of memory does not fit on this host now: %s reads MemAvailable "
            "19.9 GiB, the host keeps 4 GiB for itself, which leaves 15.9 GiB for "
            "this box — ask for at most --memory 15 or free host memory"
            % self.box.libvirt_host.MEMINFO_FILE,
        )

    def test_the_guard_judges_the_size_the_box_will_start_with_not_the_one_it_has(self):
        self.free(22)
        # The box has 16 GiB, which fits; 20 GiB, asked, does not.
        error, stubs = self.up("--memory", "20")
        self.assert_refused_before_anything(
            error, stubs, "20 GiB of memory does not fit"
        )
        self.assertIn("ask for at most --memory 18", error.message)
        # The box is defined at 32 GiB, which does not fit; 16 GiB, asked, does.
        self.host.domains["yolobox-box"] = as_libvirt_prints(
            self.box.domain_xml(
                "box", self.facts._replace(memory_gib=32), VM_UID, VM_GID
            )
        )
        error, _ = self.vm_up(self.box.NO_SIZE)
        self.assertIn("32 GiB of memory does not fit", error.message)
        error, _ = self.vm_up(self.box.Size(16, None, None))
        self.assertIsNone(error)

    def test_a_host_with_too_little_free_to_name_a_smaller_box_says_to_free_memory(
        self,
    ):
        self.free(self.box.HOST_MEMORY_RESERVE_GIB + self.box.MIN_MEMORY_GIB - 1)
        error, _ = self.vm_up(self.box.NO_SIZE)
        self.assertIn("leaves 3.0 GiB for this box", error.message)
        self.assertIn("— free host memory", error.message)
        self.assertNotIn("at most", error.message)

    def test_swap_is_not_free_memory(self):
        self.meminfo.write_text(
            "MemTotal: %d kB\nMemAvailable: %d kB\nSwapFree: %d kB\n"
            % (HOST_MEMORY_GIB << 20, 8 << 20, 64 << 20)
        )
        error, _ = self.vm_up(self.box.NO_SIZE)
        self.assertIn("MemAvailable 8.0 GiB", error.message)

    def test_a_meminfo_without_a_readable_available_line_is_an_error_not_a_guess(self):
        total = "MemTotal: %d kB\n" % (HOST_MEMORY_GIB << 20)
        for text in (
            "",
            total,
            total + "MemAvailable: lizard kB\n",
            total + "MemAvailable: 1 MB\n",
            total + "MemAvailable: -1 kB\n",
            total + "MemAvailable:\n1 kB\n",
        ):
            with self.subTest(text=text):
                self.meminfo.write_text(text)
                error, stubs = self.vm_up(self.box.NO_SIZE)
                self.assert_refused_before_anything(
                    error, stubs, "has no MemAvailable line in kB"
                )
        self.meminfo.unlink()
        error, stubs = self.vm_up(self.box.NO_SIZE)
        self.assert_refused_before_anything(error, stubs, "cannot read")

    def test_a_box_that_is_up_is_not_judged_and_the_host_memory_is_not_read(self):
        self.host.states["yolobox-box"] = "running"
        self.meminfo.unlink()
        with mock.patch.object(
            self.box.unlock,
            "boot_stage",
            return_value=self.box.Stage(self.box.BootStage.STAGE2, ""),
        ):
            error, _ = self.vm_up(self.box.NO_SIZE)
        self.assertIsNone(error)

    def test_the_restart_of_a_box_that_ran_a_moment_ago_is_not_judged_again(self):
        self.free(1)
        error, _ = self.vm_up(self.box.NO_SIZE, restart=True)
        self.assertIsNone(error)
        self.host.states["yolobox-box"] = "shut off"
        error, _ = self.vm_up(self.box.NO_SIZE)
        self.assertIn("does not fit", error.message)

    def test_the_other_domains_are_read_and_named_only_to_explain_a_refusal(self):
        self.host.foreign_domain("win", 6 << 20)
        error, _ = self.vm_up(self.box.NO_SIZE)
        self.assertIsNone(error)
        self.assertEqual(self.host.calls_of("dominfo"), [])
        self.host.states["yolobox-box"] = "shut off"
        self.free(8)
        error, _ = self.vm_up(self.box.NO_SIZE)
        self.assertIn("may take 6.0 GiB (win 6.0 GiB)", error.message)
        self.assertIn("stop another domain", error.message)

    def test_a_box_that_does_not_exist_is_judged_at_the_default_memory(self):
        self.host.domains.clear()
        self.host.states.clear()
        self.free(self.box.LIBVIRT_MEMORY_GIB + self.box.HOST_MEMORY_RESERVE_GIB, kib=1)
        error, stubs = self.vm_up(self.box.NO_SIZE)
        self.assert_refused_before_anything(
            error, stubs, "16 GiB of memory does not fit"
        )
        self.assertIn("ask for at most --memory 15", error.message)

    def test_a_default_box_does_not_fit_a_host_of_its_own_size(self):
        # A 16 GiB host reports a little under 16 GiB, and keeps 4 for itself.
        self.host.domains.clear()
        self.host.states.clear()
        self.host_memory((self.box.LIBVIRT_MEMORY_GIB << 20) - 1)
        error, stubs = self.vm_up(self.box.NO_SIZE)
        self.assert_refused_before_anything(error, stubs, "does not fit")
        self.assertIn("ask for at most --memory 11", error.message)

    def test_a_new_box_is_refused_by_the_total_the_host_has_before_what_it_has_free(
        self,
    ):
        self.host.domains.clear()
        self.host.states.clear()
        self.free(2)
        error, stubs = self.vm_up(self.box.Size(HOST_MEMORY_GIB, None, None))
        self.assert_refused_before_anything(error, stubs, "reads MemTotal")
        self.assertNotIn("MemAvailable", error.message)


class TestVmUpCreatesAtTheAskedSize(LibvirtCase):
    def up(self, host_cpus=16, *flags):
        with (
            mock.patch.object(self.driver, "require_tool"),
            mock.patch.object(self.box.os, "cpu_count", return_value=host_cpus),
            mock.patch.object(self.box.agent, "luks_passphrase") as passphrase,
            mock.patch.object(self.driver, "create") as create,
            mock.patch.object(self.driver, "start"),
        ):
            try:
                self.box.main(["-m", "box", "up", *flags])
            except self.box.YoError as failure:
                return failure, passphrase, create
        return None, passphrase, create

    def test_all_three_flags_reach_the_creation(self):
        with (
            mock.patch.object(self.box.sshconfig, "write_ssh_config"),
            mock.patch.object(self.box.guest, "ensure_guest_known_hosts"),
            mock.patch.object(self.box.forwards, "vm_services"),
            mock.patch.object(self.box.sshx, "bootstrapped", return_value=False),
            mock.patch.object(self.box.ui, "err") as err,
        ):
            error, passphrase, create = self.up(
                16, "--memory", "24", "--disk", "250", "--cpus", "3"
            )
        self.assertIsNone(error)
        err.assert_called_once_with("yo: " + self.box.sshx.not_bootstrapped_message())
        self.assertEqual(self.host.calls_of("vol-resize"), [])
        create.assert_called_once_with(mock.ANY, self.box.NewBox(24, 250, 3))
        # A new box is the plaintext seed: 1Password is not asked for its disk.
        passphrase.assert_not_called()

    def test_a_host_that_cannot_give_a_default_creates_nothing(self):
        for host in (1, None):
            with self.subTest(host=host):
                error, passphrase, create = self.up(host)
                self.assertIn("CPU", error.message)
                passphrase.assert_not_called()
                create.assert_not_called()


SS_LINE = (
    "LISTEN 0      4096       127.0.0.1:61000      0.0.0.0:*    uid:%d ino:98765 "
    "sk:2f cgroup:/system.slice/virtqemud.service <->"
)


class TestInitrdListener(LibvirtCase):
    def verify(self, *listeners, port=61000):
        self.host.listeners = "".join(line + "\n" for line in listeners)
        self.driver.verify_vm_listener(port)

    def test_the_owner_is_read_from_every_listing_line(self):
        self.assertEqual(
            self.box.listener_uids(SS_LINE % 990 + "\n\n" + SS_LINE % 1000 + "\n"),
            [990, 1000],
        )
        self.assertEqual(self.box.listener_uids(""), [])

    def test_a_line_with_its_details_and_no_owner_is_a_root_listener_and_a_stranger(
        self,
    ):
        # ss prints uid: only for an owner that is not root; ino: always.
        line = "LISTEN 0 4096 127.0.0.1:61000 0.0.0.0:* ino:4242 sk:1 <->"
        self.assertEqual(self.box.listener_uids(line + "\n"), [0])
        with self.assertRaisesRegex(self.box.YoError, "uid 0"):
            self.verify(line)

    def test_a_line_without_its_details_is_an_error_showing_it(self):
        for line in ("LISTEN 0 4096 127.0.0.1:61000 0.0.0.0:*", "lizard"):
            with self.subTest(line=line):
                with self.assertRaisesRegex(self.box.YoError, "no ino:.*" + line[:6]):
                    self.box.listener_uids(line + "\n")

    def test_the_vm_accounts_own_listener_is_trusted(self):
        self.verify(SS_LINE % VM_UID)
        argv = [call for call in self.host.calls if call[0] == "ss"][0]
        self.assertEqual(argv, ["ss", "-ltnHe", "sport = :61000"])

    def test_no_listener_yet_is_not_a_refusal(self):
        self.verify()

    def test_another_accounts_listener_is_refused_naming_the_port_and_owner(self):
        for owner in (0, 501, 1000, VM_UID + 1):
            with self.subTest(owner=owner):
                with self.assertRaises(self.box.YoError) as caught:
                    self.verify(SS_LINE % owner)
                self.assertIn("127.0.0.1:61000", caught.exception.message)
                self.assertIn("uid %d" % owner, caught.exception.message)

    def test_one_stranger_among_the_vm_accounts_listeners_is_enough(self):
        with self.assertRaisesRegex(self.box.YoError, "held by uid 1000"):
            self.verify(SS_LINE % VM_UID, SS_LINE % 1000)

    def send_passphrase(self, passphrase=lambda: b"secret"):
        """The initrd's gate, then its helper with `passphrase`, as unlock_disk
        runs them; whether ssh was started for the helper."""

        def gate_then_helper(held):
            gate = self.box.unlock.initrd_endpoint(held)
            self.box.run_initrd_helper(gate, passphrase)

        self.host.tagged_domain("box", 61000)
        with mock.patch.object(
            self.box.subprocess, "Popen", side_effect=FileNotFoundError
        ) as popen:
            with self.assertRaises(self.box.YoError) as caught:
                locked(self.box, gate_then_helper)
        return popen.called, caught.exception.message

    def test_the_passphrase_is_not_sent_when_the_listener_is_a_strangers(self):
        self.host.listeners = SS_LINE % 1000 + "\n"
        started, message = self.send_passphrase()
        self.assertFalse(started)
        self.assertIn("yo does not connect", message)

    def test_a_stranger_that_takes_the_port_after_the_gate_gets_no_session(self):
        # Nothing held the port at the gate, which is fine there; the check
        # right before ssh dials sees who took it since.
        self.host.listeners = ""

        def passphrase():
            self.host.listeners = SS_LINE % 1000 + "\n"
            return b"secret"

        started, message = self.send_passphrase(passphrase)
        self.assertFalse(started)
        self.assertIn("held by uid 1000", message)

    def test_the_vm_accounts_listener_lets_the_helper_run(self):
        self.host.listeners = SS_LINE % VM_UID + "\n"
        started, message = self.send_passphrase()
        self.assertTrue(started)
        self.assertIn("ssh not found", message)


class TestStart(LibvirtCase):
    def start(self, memory_gib=None, disk_gib=None, cpus=None, existing=False):
        """driver.create() of the flags as vm_up decides a creation size from
        them (unless the domain exists), then driver.start()."""
        seed = Path(self.home) / "seed.qcow2"
        seed.write_bytes(b"Q" * 4096)
        with (
            mock.patch.object(
                self.box.shutil, "which", return_value="/usr/bin/getfacl"
            ),
            mock.patch.object(self.box.libvirt, "ensure_seed", return_value=str(seed)),
            mock.patch.object(self.driver, "operator_key", return_value=KEY_BLOB),
            mock.patch.object(self.box.unlock, "unlock_disk") as unlock,
            mock.patch.object(self.box.unlock, "wait_for_operator"),
            mock.patch.object(
                self.box.driver, "loopback_port_bound", return_value=False
            ),
        ):
            if not existing:
                locked(
                    self.box,
                    self.driver.create,
                    self.box.Size(memory_gib, disk_gib, cpus).for_creation(),
                )
            locked(self.box, self.driver.start, self.passphrase)
        return unlock

    def passphrase(self):
        return b"pass"

    def test_a_new_instance_gets_its_volumes_then_its_domain_then_starts(self):
        unlock = self.start()
        subs = self.host.subcommands()
        order = [
            "vol-create-as",
            "vol-upload",
            "vol-resize",
            "vol-create-as",
            "vol-upload",
            "define",
            # start() defines the stopped domain from the template again.
            "define",
            "start",
        ]
        picked = [sub for sub in subs if sub in set(order)]
        self.assertEqual(picked, order)
        resize = self.host.calls_of("vol-resize")[0]
        self.assertEqual(resize[-2:], ["yolobox-box.qcow2", "100G"])
        self.assertEqual(
            self.host.calls_of("vol-create-as")[0][-2:], ["--format", "qcow2"]
        )
        self.assertEqual(
            self.host.calls_of("vol-create-as")[1][-2:], ["--format", "raw"]
        )
        self.assertEqual(
            set(self.host.volumes), {"yolobox-box.qcow2", "yolobox-box-cidata.iso"}
        )
        # The unlock gets the source, which derives only if an initrd asks.
        unlock.assert_called_once_with(mock.ANY, self.passphrase)

    def memory(self):
        """The <memory> of the defined domain, as (unit, GiB)."""
        element = ET.fromstring(self.host.domains["yolobox-box"]).find("memory")
        return element.get("unit"), int(element.text) >> 20

    def test_a_box_given_no_size_gets_the_default_memory_and_disk(self):
        self.start()
        self.assertEqual(self.memory(), ("KiB", self.box.LIBVIRT_MEMORY_GIB))
        self.assertEqual(
            self.host.capacity["yolobox-box.qcow2"], self.box.LIBVIRT_DISK_GIB << 30
        )

    def test_a_box_is_created_at_the_size_asked_for(self):
        self.start(memory_gib=24, disk_gib=250)
        self.assertEqual(self.memory(), ("KiB", 24))
        self.assertEqual(self.host.capacity["yolobox-box.qcow2"], 250 << 30)

    def test_memory_alone_leaves_the_disk_at_its_default(self):
        self.start(memory_gib=24)
        self.assertEqual(self.memory(), ("KiB", 24))
        self.assertEqual(
            self.host.capacity["yolobox-box.qcow2"], self.box.LIBVIRT_DISK_GIB << 30
        )

    def vcpus(self):
        return ET.fromstring(self.host.domains["yolobox-box"]).findtext("vcpu")

    def test_an_existing_domain_starts_on_a_host_whose_default_is_unavailable(self):
        self.host.tagged_domain("box", FIRST_PORT)
        self.host.states["yolobox-box"] = "shut off"
        for host in (1, None):
            with self.subTest(host=host):
                with mock.patch.object(self.box.os, "cpu_count", return_value=host):
                    unlock = self.start(existing=True)
                unlock.assert_called_once_with(mock.ANY, self.passphrase)
                self.assertNotIn("yolobox-box.qcow2", self.host.volumes)

    def test_a_box_is_created_with_the_cpus_asked_for(self):
        self.start(cpus=3)
        self.assertEqual(self.vcpus(), "3")

    def test_a_box_created_without_cpus_gets_one_less_than_the_host_up_to_four(self):
        for host, expected in ((2, "1"), (5, "4"), (64, "4")):
            with self.subTest(host=host):
                self.host.domains.clear()
                self.host.states.clear()
                with mock.patch.object(self.box.os, "cpu_count", return_value=host):
                    self.start()
                self.assertEqual(self.vcpus(), expected)

    BIND_DENIED = (
        "error: Failed to start domain 'yolobox-box'\nerror: internal error: "
        "Could not start 'passt': Failed to bind UNIX domain socket: Permission denied"
    )

    def test_a_passt_that_could_not_bind_is_started_once_more_after_the_acl_held(
        self,
    ):
        # Another passt domain's start reset the mask between yo's check and
        # the start; the hook restored it at that domain's `started begin`.
        self.host.fail_once["start"] = self.BIND_DENIED
        self.start()
        self.assertEqual(len(self.host.calls_of("start")), 2)
        self.assertEqual(self.host.states["yolobox-box"], "running")

    def test_a_passt_that_could_not_bind_with_the_acl_still_short_is_the_remedy(self):
        # The ACL was whole when vm_up judged the host; another passt domain's
        # start narrowed it since.
        self.host.fail_once["start"] = self.BIND_DENIED
        self.host.acl = getfacl_listing(mask="r-x")
        with self.assertRaisesRegex(self.box.YoError, "sudo setfacl -m u:yolobox-vm"):
            self.start()
        self.assertEqual(len(self.host.calls_of("start")), 1)

    def test_a_start_that_fails_for_any_other_reason_is_not_retried(self):
        self.host.fail_once["start"] = "error: internal error: lizard"
        with self.assertRaisesRegex(
            self.box.YoError, r"virsh start .*failed \(exit 1\): .*lizard"
        ):
            self.start()
        self.assertEqual(len(self.host.calls_of("start")), 1)

    def test_a_second_bind_failure_is_final(self):
        self.host.fail["start"] = self.BIND_DENIED
        with self.assertRaisesRegex(self.box.YoError, "Failed to bind"):
            self.start()
        self.assertEqual(len(self.host.calls_of("start")), 2)

    def test_the_port_is_chosen_and_the_domain_defined_under_the_lock(self):
        held = []
        virsh = self.host.virsh

        def spy(sub, args):
            if sub == "define":
                held.append(self.lock_held())
            return virsh(sub, args)

        self.host.virsh = spy
        allocate = self.driver.allocate_host_port

        def spy_allocate():
            held.append(self.lock_held())
            return allocate()

        with mock.patch.object(self.driver, "allocate_host_port", spy_allocate):
            self.start()
        # The first define is the creation's, under the lock; start() defines
        # the stopped domain again from its record, which needs no lock.
        self.assertEqual(held, [True, True, False])
        self.assertFalse(self.lock_held())

    def test_every_virsh_call_names_the_system_connection(self):
        self.start()
        for call in self.host.calls:
            if call[0] == "virsh":
                self.assertEqual(call[1:3], ["-c", "qemu:///system"])

    def test_the_domain_forwards_the_port_the_ssh_facts_name(self):
        self.start()
        self.assertEqual(self.driver.ssh_facts().port, FIRST_PORT)
        self.assertIn(
            "<range start='%d' to='22'/>" % FIRST_PORT, self.host.domains["yolobox-box"]
        )

    def test_the_iso_is_built_under_the_cidata_label_and_the_files_are_staged_after(
        self,
    ):
        self.start()
        argv = [call for call in self.host.calls if call[0] == "xorriso"]
        self.assertEqual(
            argv[0][:7], ["xorriso", "-as", "mkisofs", "-V", "cidata", "-J", "-R"]
        )
        ((built_from, listing),) = self.host.iso_inputs
        self.assertEqual(listing, sorted(self.box.CIDATA_FILES))
        self.assertFalse(os.path.exists(built_from))
        self.assertEqual(
            sorted(os.listdir(self.box.instance_dir())),
            ["cidata", "disk-volume", "domain.xml", "operator.pub"],
        )
        staged = Path(self.box.instance_dir()) / "cidata"
        self.assertEqual(
            sorted(path.name for path in staged.iterdir()),
            sorted(self.box.CIDATA_FILES),
        )

    def test_only_the_public_key_is_written(self):
        self.start()
        self.assertEqual(
            (Path(self.box.instance_dir()) / "operator.pub").read_text(),
            PUBLIC_KEY + "\n",
        )
        for path in Path(self.box.instance_dir()).rglob("*"):
            if path.is_file():
                self.assertNotIn(
                    base64.b64encode(OTHER_BLOB).decode(), path.read_text()
                )

    def test_the_public_key_is_private_to_the_user_so_ssh_never_calls_it_unprotected(
        self,
    ):
        # ssh reads an IdentityFile as a private key when its agent is gone,
        # and warns UNPROTECTED PRIVATE KEY FILE on any mode but 0600.
        self.start()
        mode = (Path(self.box.instance_dir()) / "operator.pub").stat().st_mode
        self.assertEqual(stat.S_IMODE(mode), 0o600)

    def test_a_second_start_keeps_the_volumes_and_defines_the_same_domain(self):
        self.start()
        defined = self.host.domains["yolobox-box"]
        self.host.states["yolobox-box"] = "shut off"
        self.host.calls.clear()
        self.start(existing=True)
        subs = self.host.subcommands()
        for untouched in ("vol-create-as", "vol-upload", "vol-delete", "vol-resize"):
            self.assertNotIn(untouched, subs)
        self.assertEqual(subs.count("define"), 1)
        self.assertEqual(self.host.domains["yolobox-box"], defined)
        self.assertIn("start", subs)

    def test_a_changed_operator_name_rebuilds_the_cidata_while_stopped(self):
        self.start()
        self.host.states["yolobox-box"] = "shut off"
        self.host.calls.clear()
        with mock.patch.object(self.box.release, "host_user", return_value="bob"):
            self.start(existing=True)
        subs = self.host.subcommands()
        self.assertLess(subs.index("vol-delete"), subs.index("vol-create-as"))
        self.assertIn(
            "LIMA_CIDATA_USER=bob",
            (Path(self.box.instance_dir()) / "cidata" / "lima.env").read_text(),
        )

    def test_a_failed_upload_leaves_no_half_filled_volume(self):
        self.host.fail["vol-upload"] = "no space"
        with self.assertRaises(self.box.YoError) as caught:
            self.start()
        self.assertIn("no space", caught.exception.message)
        self.assertEqual(self.host.volumes, {})
        self.assertNotIn("define", self.host.subcommands())

    def test_an_interrupted_upload_leaves_no_half_filled_volume(self):
        self.host.interrupted["vol-upload"] = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.start()
        self.assertEqual(self.host.volumes, {})

    def test_a_partial_volume_that_cannot_be_removed_is_said_out_loud(self):
        self.host.fail["vol-delete"] = "pool busy"
        self.host.interrupted["vol-upload"] = KeyboardInterrupt()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(KeyboardInterrupt):
            self.start()
        self.assertIn(
            "removing the partial volume failed too: pool busy", stderr.getvalue()
        )
        del self.host.interrupted["vol-upload"]
        self.host.volumes.clear()
        self.host.fail["vol-upload"] = "no space"
        with self.assertRaisesRegex(
            self.box.YoError,
            "no space; removing the partial volume failed too: pool busy",
        ):
            self.start()

    def test_a_failed_cidata_rebuild_keeps_the_old_key_and_is_retried(self):
        self.start()
        self.host.states["yolobox-box"] = "shut off"
        lima_env = Path(self.box.instance_dir()) / "cidata" / "lima.env"
        self.host.fail["vol-upload"] = "no space"
        with mock.patch.object(self.box.release, "host_user", return_value="bob"):
            with self.assertRaisesRegex(self.box.YoError, "no space"):
                self.start(existing=True)
            self.assertIn("LIMA_CIDATA_USER=alice", lima_env.read_text())
            self.assertNotIn("yolobox-box-cidata.iso", self.host.volumes)
            self.host.fail.clear()
            self.host.calls.clear()
            self.start(existing=True)
        self.assertIn("LIMA_CIDATA_USER=bob", lima_env.read_text())
        self.assertIn("yolobox-box-cidata.iso", self.host.volumes)
        self.assertEqual(self.host.subcommands().count("vol-upload"), 1)

    def test_a_failed_iso_build_keeps_the_old_key_and_leaves_no_scraps(self):
        self.start()
        self.host.states["yolobox-box"] = "shut off"
        run = self.host.run

        def refuse_xorriso(argv, **kwargs):
            if argv[0] == "xorriso":
                raise self.box.YoError("xorriso failed")
            return run(argv, **kwargs)

        with (
            mock.patch.object(self.box.proc, "run", refuse_xorriso),
            mock.patch.object(self.box.release, "host_user", return_value="bob"),
        ):
            with self.assertRaisesRegex(self.box.YoError, "xorriso failed"):
                self.start(existing=True)
        self.assertEqual(
            sorted(os.listdir(self.box.instance_dir())),
            ["cidata", "disk-volume", "domain.xml", "operator.pub"],
        )
        self.assertIn(
            "LIMA_CIDATA_USER=alice",
            (Path(self.box.instance_dir()) / "cidata" / "lima.env").read_text(),
        )

    def test_a_staged_file_that_is_not_utf8_is_rebuilt_not_a_crash(self):
        self.start()
        self.host.states["yolobox-box"] = "shut off"
        (Path(self.box.instance_dir()) / "cidata" / "user-data").write_bytes(
            b"\xff\xfe"
        )
        self.host.calls.clear()
        self.start(existing=True)
        self.assertIn("vol-upload", self.host.subcommands())

    def test_an_operator_named_agent_stops_before_anything_is_created(self):
        with mock.patch.object(self.box.release, "host_user", return_value="agent"):
            with self.assertRaisesRegex(self.box.YoError, "box's agent account"):
                self.start()
        self.assertEqual(self.host.volumes, {})
        self.assertEqual(self.host.domains, {})

    def test_an_empty_operator_name_stops_before_anything_is_created(self):
        with mock.patch.object(self.box.release, "host_user", return_value=""):
            with self.assertRaisesRegex(self.box.YoError, "cannot name the operator"):
                self.start()
        self.assertEqual(self.host.volumes, {})


class TestADomainThatDiesWhileYoWaits(LibvirtCase):
    """The host's OOM killer ends qemu while yo waits for the guest to answer."""

    ANSWERED = ["yolobox-unlock: answered id=cryptsetup:/dev/vda2"]

    def setUp(self):
        super().setUp()
        self.host.tagged_domain("box", FIRST_PORT)
        self.host.states["yolobox-box"] = "shut off"
        for patcher in (
            mock.patch.object(self.box.proc, "spawn", self.host.spawn),
            mock.patch.object(
                self.box.shutil, "which", return_value="/usr/bin/getfacl"
            ),
            mock.patch.object(self.driver, "operator_key", return_value=KEY_BLOB),
            # A wait that polls again has missed the domain going away.
            mock.patch.object(
                self.box.unlock.time,
                "sleep",
                side_effect=AssertionError("yo waited on a domain that is off"),
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        for quiet in (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            quiet.__enter__()
            self.addCleanup(quiet.__exit__, None, None, None)

    def killed_on_the_next_probe(self, killed_as="shut off"):
        """The sshd probes find nothing, and the first one finds the domain gone."""

        def probe(argv):
            self.host.states["yolobox-box"] = killed_as
            return NOT_LISTENING

        self.host.sshd = probe

    def up(self):
        with self.assertRaises(self.box.YoError) as caught:
            locked(
                self.box,
                self.box.vm_up,
                run_services=False,
                passphrase=self.derived(b"secret"),
            )
        return caught.exception.message

    def test_a_domain_killed_after_the_unlock_is_an_error_naming_the_oom_killer(self):
        self.host.helper = lambda: (self.ANSWERED + ["yolobox-unlock: unlocked"], 0, "")
        self.killed_on_the_next_probe()
        message = self.up()
        self.assertIn(
            "box is shut off, not Running, while yo waited for its sshd to accept "
            "the operator",
            message,
        )
        self.assertIn(
            "`virsh -c qemu:///system domstate --reason yolobox-box` says why", message
        )
        self.assertIn("possibly killed by the host's OOM killer", message)
        self.assertIn("check `journalctl -k -g oom`", message)
        self.assertIn("then run this command again", message)

    def test_a_domain_killed_while_the_helper_streamed_is_an_error_at_once(self):
        def dropped():
            self.host.states["yolobox-box"] = "shut off"
            return self.ANSWERED, 255, ""

        self.host.helper = dropped
        self.host.sshd = lambda argv: NOT_LISTENING
        message = self.up()
        self.assertIn(
            "box is shut off, not Running, while yo waited for its initrd to take "
            "the disk passphrase",
            message,
        )
        self.assertIn("`journalctl -k -g oom`", message)

    def test_a_domain_paused_under_yo_names_the_way_to_resume_it(self):
        self.host.helper = lambda: (["yolobox-unlock: unlocked"], 0, "")
        self.killed_on_the_next_probe("paused")
        message = self.up()
        self.assertIn("box is paused, not Running", message)
        self.assertIn("domstate --reason yolobox-box", message)

    def test_a_domain_in_a_state_yo_has_no_word_for_says_where_to_look(self):
        self.host.helper = lambda: (["yolobox-unlock: unlocked"], 0, "")
        self.killed_on_the_next_probe("crashed")
        message = self.up()
        self.assertIn("box is crashed, not Running", message)
        self.assertIn(
            "`virsh -c qemu:///system domstate --reason yolobox-box` says why", message
        )

    def test_a_domain_undefined_while_yo_waits_says_yo_up_defines_it_again(self):
        self.host.helper = lambda: (["yolobox-unlock: unlocked"], 0, "")

        def undefined(argv):
            del self.host.domains["yolobox-box"], self.host.states["yolobox-box"]
            return NOT_LISTENING

        self.host.sshd = undefined
        message = self.up()
        self.assertIn("box is not listed, not Running", message)
        self.assertIn("then run this command again", message)

    def test_a_domain_paused_during_a_connected_probe_is_named_not_the_ssh_timeout(
        self,
    ):
        self.host.helper = lambda: (["yolobox-unlock: unlocked"], 0, "")

        def paused(argv):
            self.host.states["yolobox-box"] = "paused"
            return 255, "Timeout, server 127.0.0.1 not responding."

        self.host.sshd = paused
        message = self.up()
        self.assertIn("box is paused, not Running", message)
        self.assertNotIn("as the operator failed", message)

    def test_a_domain_that_stays_up_is_waited_for_as_before(self):
        self.host.helper = lambda: (["yolobox-unlock: unlocked"], 0, "")
        # Nothing listening yet, then the key refused while the operator's key
        # is being written: both are a boot in progress, retried.
        key_refused = (255, "operator@127.0.0.1: Permission denied (publickey).")
        answers = iter([NOT_LISTENING, key_refused, (0, "")])
        self.host.sshd = lambda argv: next(answers)
        with mock.patch.object(self.box.unlock.time, "sleep") as sleep:
            locked(self.box, self.driver.start, lambda: b"secret")
        self.assertEqual(
            sleep.call_args_list,
            [mock.call(self.box.OPERATOR_POLL_SECONDS)] * 2,
        )


class TestDiskVolume(LibvirtCase):
    def setUp(self):
        super().setUp()
        os.makedirs(self.box.instance_dir())
        seed = Path(self.home) / "seed.qcow2"
        seed.write_bytes(b"Q" * 4096)
        patcher = mock.patch.object(
            self.box.libvirt, "ensure_seed", return_value=str(seed)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def ensure(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.driver.ensure_disk_volume(100)

    def test_a_volume_whose_resize_failed_is_deleted_and_rebuilt(self):
        self.host.fail["vol-resize"] = "no space"
        with self.assertRaisesRegex(self.box.YoError, "no space"):
            self.ensure()
        self.host.fail.clear()
        self.host.calls.clear()
        self.ensure()
        self.assertEqual(
            [
                sub
                for sub in self.host.subcommands()
                if sub in ("vol-delete", "vol-create-as", "vol-upload", "vol-resize")
            ],
            ["vol-delete", "vol-create-as", "vol-upload", "vol-resize"],
        )
        self.assertEqual(self.host.capacity["yolobox-box.qcow2"], 100 << 30)

    def test_a_volume_an_interrupt_left_behind_is_rebuilt(self):
        self.kept_volume(4, record="building")
        self.ensure()
        self.assertIn("vol-delete", self.host.subcommands())
        self.assertEqual(self.host.capacity["yolobox-box.qcow2"], 100 << 30)

    def test_a_volume_recorded_built_is_adopted_as_it_is(self):
        self.kept_volume(40)
        self.ensure()
        self.assertEqual(
            [sub for sub in self.host.subcommands() if sub not in READS], []
        )
        self.assertEqual(self.host.volumes["yolobox-box.qcow2"], b"a whole disk")
        self.assertEqual(self.host.capacity["yolobox-box.qcow2"], 40 << 30)

    def test_a_volume_yo_has_no_record_of_is_never_deleted_on_a_guess(self):
        for record in (None, "lizard", ""):
            with self.subTest(record=record):
                Path(self.box.instance_dir(), "disk-volume").unlink(missing_ok=True)
                self.kept_volume(100, record=record)
                with self.assertRaises(self.box.YoError) as caught:
                    self.ensure()
                message = caught.exception.message
                self.assertIn("cannot tell whether it holds a finished disk", message)
                self.assertIn("yo -m box destroy --yes && yo -m box bootstrap", message)
                self.assertEqual(
                    self.host.volumes["yolobox-box.qcow2"], b"a whole disk"
                )
                self.assertNotIn("vol-delete", self.host.subcommands())


class FakeReply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class TestSeed(LibvirtCase):
    def seed(self, payload, pinned=None):
        digest = pinned or hashlib.sha512(payload).hexdigest()
        return (
            mock.patch.object(self.box.libvirt, "SEED_SHA512", digest),
            mock.patch.object(
                self.box.urllib.request, "urlopen", return_value=FakeReply(payload)
            ),
        )

    def cached(self):
        return os.path.join(self.home, ".cache", "yolobox", self.box.SEED_NAME)

    def test_a_verified_download_is_cached(self):
        pin, opener = self.seed(b"image")
        with pin, opener as urlopen, mock.patch.object(self.box.ui, "err"):
            path = self.box.ensure_seed()
        self.assertEqual(path, self.cached())
        self.assertEqual(Path(path).read_bytes(), b"image")
        urlopen.assert_called_once()

    def test_a_cached_seed_that_verifies_is_not_downloaded_again(self):
        pin, opener = self.seed(b"image")
        with pin, opener as urlopen, mock.patch.object(self.box.ui, "err"):
            self.box.ensure_seed()
            self.box.ensure_seed()
        urlopen.assert_called_once()

    def test_a_download_with_the_wrong_hash_is_refused_and_not_kept(self):
        pin, opener = self.seed(b"tampered", pinned="0" * 128)
        with pin, opener, mock.patch.object(self.box.ui, "err"):
            with self.assertRaises(self.box.YoError) as caught:
                self.box.ensure_seed()
        self.assertIn("not the pinned", caught.exception.message)
        self.assertEqual(os.listdir(os.path.dirname(self.cached())), [])

    def test_a_cached_seed_that_no_longer_verifies_is_replaced(self):
        os.makedirs(os.path.dirname(self.cached()))
        Path(self.cached()).write_bytes(b"corrupt")
        pin, opener = self.seed(b"image")
        with pin, opener as urlopen, mock.patch.object(self.box.ui, "err"):
            self.box.ensure_seed()
        self.assertEqual(Path(self.cached()).read_bytes(), b"image")
        urlopen.assert_called_once()

    def test_two_downloads_at_once_do_not_share_a_file(self):
        class Dropped(FakeReply):
            def read(self, size=-1):
                raise OSError("connection reset")

        def second():
            with self.assertRaisesRegex(self.box.YoError, "connection reset"):
                self.box.download_seed(self.cached())

        class Interrupted(FakeReply):
            reads = 0

            def read(self, size=-1):
                self.reads += 1
                if self.reads == 2:  # the first download has all its bytes
                    second()
                return super().read(size)

        pin, _ = self.seed(b"image")
        replies = [Interrupted(b"image"), Dropped()]
        with (
            pin,
            mock.patch.object(self.box.urllib.request, "urlopen", side_effect=replies),
            mock.patch.object(self.box.ui, "err"),
        ):
            self.box.ensure_seed()
        self.assertEqual(Path(self.cached()).read_bytes(), b"image")
        self.assertEqual(
            os.listdir(os.path.dirname(self.cached())), [self.box.SEED_NAME]
        )

    def test_a_network_failure_is_an_error_naming_the_url(self):
        failure = self.box.urllib.error.URLError("no route")
        with (
            mock.patch.object(self.box.urllib.request, "urlopen", side_effect=failure),
            mock.patch.object(self.box.ui, "err"),
            self.assertRaises(self.box.YoError) as caught,
        ):
            self.box.ensure_seed()
        self.assertIn(self.box.SEED_URL, caught.exception.message)


class TestDriverChoice(unittest.TestCase):
    def test_linux_gets_the_libvirt_driver(self):
        box, name = load_libvirt_yo()
        self.addCleanup(unload_yo, name)
        self.assertIsInstance(box.driver_for("linux"), box.LibvirtDriver)
        self.assertIsInstance(box.box.driver, box.LibvirtDriver)

    def test_the_instance_dir_is_under_the_state_dir(self):
        box, name = load_libvirt_yo("e2e")
        self.addCleanup(unload_yo, name)
        self.assertTrue(
            box.instance_dir().endswith(".local/state/yolobox/instances/e2e")
        )
        self.assertTrue(
            box.ssh_cfg().endswith("/.local/state/yolobox/instances/e2e/ssh.config")
        )

    def test_an_unknown_platform_is_refused(self):
        box, name = load_libvirt_yo()
        self.addCleanup(unload_yo, name)
        with self.assertRaisesRegex(box.YoError, "not 'win32'"):
            box.driver_for("win32")


if __name__ == "__main__":
    unittest.main()
