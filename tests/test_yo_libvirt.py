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
import shutil
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from test_yo import ACCEPT_TIMEOUT_SECONDS, FakeHome, load_yo, unload_yo

REPO = Path(__file__).resolve().parent.parent
# LibvirtCase.setUp patches `which` on the stdlib module itself, which yo shares,
# so a test that looks for a real tool needs the function as it was at import.
REAL_WHICH = shutil.which
INITRD_KEYS_SCRIPT = REPO / "nix" / "luks" / "yolobox-initrd-authorized-keys.sh"

VM_UID, VM_GID = 990, 989
OPERATOR = "alice"
KEY_BLOB = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(32))
OTHER_BLOB = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(1, 33))
PUBLIC_KEY = "ssh-ed25519 %s yolobox-operator" % base64.b64encode(KEY_BLOB).decode()
EPHEMERAL_RANGE = "32768\t60999\n"
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
        "domcapabilities",
        "vol-list",
        "vol-info",
    }
)
# What `virsh domcapabilities` says of the host's q35 kvm machine, cut to the
# two things the domain policy reads from it.
EMULATOR = "/usr/bin/qemu-system-x86_64"
LOADER = "/usr/share/edk2/ovmf/OVMF_CODE_4M.qcow2"
DOMCAPABILITIES_XML = """\
<domainCapabilities>
  <path>%s</path>
  <domain>kvm</domain>
  <machine>pc-q35-11.1</machine>
  <arch>x86_64</arch>
  <os supported='yes'>
    <loader supported='yes'>
      <value>/usr/share/edk2/ovmf/OVMF_CODE_4M.secboot.qcow2</value>
      <value>%s</value>
      <value>/usr/share/edk2/ovmf/OVMF_CODE.fd</value>
      <enum name='type'>
        <value>rom</value>
        <value>pflash</value>
      </enum>
    </loader>
  </os>
</domainCapabilities>
""" % (EMULATOR, LOADER)
# The virsh subcommands that edit a domain's definition.
WRITES = frozenset({"setmaxmem", "setmem", "setvcpus"})
# What ssh prints when nothing listens behind the box's port: a domain that is off.
SSH_REFUSED = (255, "ssh: connect to host 127.0.0.1 port 61000: Connection refused")


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
        self.max_cpus = {}  # domain -> its maximum vCPUs, default 8
        self.memory_kib = {}  # domain -> its current memory (`setmem`), default max
        self.dominfo = {}  # domain -> what `virsh dominfo` prints instead
        # Runs as `virsh dominfo NAME` starts, given NAME: another tool acting.
        self.before_dominfo = lambda name: None
        # The same as `virsh dumpxml NAME` starts.
        self.before_dumpxml = lambda name: None
        self.virsh_envs = []  # the environment of each `virsh` run
        # The guest's sshds, for the `ssh ... true` probes: argv -> (code, stderr).
        self.sshd = lambda argv: (0, "")
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
            # yo parses what virsh prints, which virsh translates.
            assert kwargs["env"]["LC_ALL"] == "C", "virsh run in the user's locale"
            self.virsh_envs.append(kwargs["env"])
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
                returncode=0 if running else 255, stderr=self.master_output
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
        elif argv[0] == "ssh" and argv[-1] == "true":
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
            result = completed(returncode=code, stderr=stderr)
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
        assert kwargs["env"]["LC_ALL"] == "C", "virsh run in the user's locale"
        assert not self.watchers, "virsh event was armed a second time"
        events, ending = self.event_script
        watcher = FakeEventStream(self, argv[4], events, ending, self.off_at_eof)
        self.watchers.append(watcher)
        return watcher

    def set_definition(self, sub, args):
        """setmaxmem, setmem and setvcpus on the definition of a domain, as libvirt
        refuses them: only with --config on a shut-off domain, and a current
        value never above its maximum."""
        name, value = args[0], args[1]
        assert "--config" in args, "a live change to a domain that is shut off"
        if self.states[name] != "shut off":
            return completed(returncode=1, stderr="error: domain is running")
        if sub == "setvcpus":
            if "--maximum" in args:
                self.max_cpus[name] = int(value)
                self.cpus[name] = min(self.cpus.get(name, DOMAIN_CPUS), int(value))
            elif int(value) > self.max_cpus.get(name, DOMAIN_CPUS):
                return completed(returncode=1, stderr="error: too many vCPUs")
            else:
                self.cpus[name] = int(value)
            return completed()
        kib = int(re.fullmatch(r"(\d+)G", value).group(1)) << 20
        if sub == "setmaxmem":
            current = self.memory_kib.get(name, self.max_memory_kib[name])
            self.max_memory_kib[name] = kib
            self.memory_kib[name] = min(current, kib)
        elif kib > self.max_memory_kib[name]:
            return completed(returncode=1, stderr="error: memory above the maximum")
        else:
            self.memory_kib[name] = kib
        return completed()

    def virsh(self, sub, args):
        if sub in self.interrupted:
            raise self.interrupted[sub]
        if sub in self.fail:
            return completed(returncode=1, stderr=self.fail[sub])
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
        if sub in ("setmaxmem", "setmem", "setvcpus"):
            return self.set_definition(sub, args)
        if sub == "dumpxml":
            self.before_dumpxml(args[0])
            if args[0] not in self.domains:
                return completed(
                    returncode=1,
                    stderr="error: failed to get domain '%s'\n" % args[0],
                )
            return completed(self.domains[args[0]])
        if sub == "domcapabilities":
            return completed(DOMCAPABILITIES_XML)
        if sub == "domstate":
            return completed(self.states[args[0]] + "\n")
        if sub == "define":
            xml = Path(args[0]).read_text()
            name = ET.fromstring(xml).findtext("name")
            self.domains[name] = xml
            self.states[name] = "shut off"
            return completed()
        if sub == "start":
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

    def tagged_domain(self, name, port, address="127.0.0.1"):
        xml = self.box.domain_xml(
            name,
            port,
            VM_UID,
            VM_GID,
            self.box.LIBVIRT_MEMORY_GIB,
            DOMAIN_CPUS,
        )
        self.domains["yolobox-" + name] = xml.replace(
            "address='127.0.0.1'", "address='%s'" % address
        )
        self.states["yolobox-" + name] = "running"
        self.max_memory_kib["yolobox-" + name] = self.box.LIBVIRT_MEMORY_GIB << 20

    def foreign_domain(self, name, memory_kib, domain_state="running"):
        """A domain of another tool, which yo only lists and asks for its memory."""
        self.domains[name] = "<domain type='kvm'><name>%s</name></domain>" % name
        self.states[name] = domain_state
        self.max_memory_kib[name] = memory_kib


class FakeHelper:
    """The initrd helper's ssh session: scripted stdout lines, exit code, stderr."""

    def __init__(self, lines, code, stderr):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO("".join(line + "\n" for line in lines).encode())
        self.stderr = io.BytesIO(stderr.encode())
        self.code = code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def wait(self):
        return self.code


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
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
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
            return_value=[(KEY_BLOB, "yolobox-operator")],
        )
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def host_memory(self, kib):
        """The host's MemTotal, as /proc/meminfo prints it."""
        self.meminfo.write_text(
            "MemTotal:       %d kB\nMemFree:         1048576 kB\n" % kib
        )

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


# `virsh dumpxml` of a running domain, as libvirt 12.8 prints one: what the
# template produces plus what libvirt adds when it defines it (uuid, the
# resolved firmware, emulator, controllers, ps2 input, audio, watchdog,
# addresses) and when it runs it (the id, pty paths, aliases, labels). Its DAC
# label is the vm account's.
LIBVIRT_STORED_DOMAIN = """\
<domain type='kvm' id='10'>
  <name>yolobox-yolobox</name>
  <uuid>49e37216-8dc2-432f-9e39-7aca92c26024</uuid>
  <metadata>
    <yolobox:instance xmlns:yolobox="https://github.com/aka-rider/yolobox">yolobox</yolobox:instance>
  </metadata>
  <memory unit='KiB'>MEMORY_KIB</memory>
  <currentMemory unit='KiB'>MEMORY_KIB</currentMemory>
  <vcpu placement='static'>8</vcpu>
  <resource>
    <partition>/machine</partition>
  </resource>
  <os firmware='efi'>
    <type arch='x86_64' machine='pc-q35-11.1'>hvm</type>
    <firmware>
      <feature enabled='no' name='enrolled-keys'/>
      <feature enabled='no' name='secure-boot'/>
    </firmware>
    <loader readonly='yes' type='pflash' format='qcow2'>/usr/share/edk2/ovmf/OVMF_CODE_4M.qcow2</loader>
    <nvram template='/usr/share/edk2/ovmf/OVMF_VARS_4M.qcow2' templateFormat='qcow2' format='qcow2'>/var/lib/libvirt/qemu/nvram/yolobox-yolobox_VARS.qcow2</nvram>
    <boot dev='hd'/>
  </os>
  <features>
    <acpi/>
    <apic/>
  </features>
  <cpu mode='host-passthrough' check='none' migratable='on'/>
  <clock offset='utc'/>
  <on_poweroff>destroy</on_poweroff>
  <on_reboot>restart</on_reboot>
  <on_crash>destroy</on_crash>
  <devices>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>
    <disk type='volume' device='disk'>
      <driver name='qemu' type='qcow2' discard='unmap' detect_zeroes='unmap'/>
      <source pool='default' volume='yolobox-yolobox.qcow2' index='2'/>
      <backingStore/>
      <target dev='vda' bus='virtio'/>
      <alias name='virtio-disk0'/>
      <address type='pci' domain='0x0000' bus='0x01' slot='0x00' function='0x0'/>
    </disk>
    <disk type='volume' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source pool='default' volume='yolobox-yolobox-cidata.iso' index='1'/>
      <backingStore/>
      <target dev='sda' bus='sata'/>
      <readonly/>
      <alias name='sata0-0-0'/>
      <address type='drive' controller='0' bus='0' target='0' unit='0'/>
    </disk>
    <controller type='usb' index='0' model='none'/>
    <controller type='sata' index='0'>
      <alias name='ide'/>
      <address type='pci' domain='0x0000' bus='0x00' slot='0x1f' function='0x2'/>
    </controller>
    <controller type='pci' index='0' model='pcie-root'>
      <alias name='pcie.0'/>
    </controller>
    <controller type='pci' index='1' model='pcie-root-port'>
      <model name='pcie-root-port'/>
      <target chassis='1' port='0x10'/>
      <alias name='pci.1'/>
      <address type='pci' domain='0x0000' bus='0x00' slot='0x02' function='0x0' multifunction='on'/>
    </controller>
    <controller type='pci' index='2' model='pcie-root-port'>
      <model name='pcie-root-port'/>
      <target chassis='2' port='0x11'/>
      <alias name='pci.2'/>
      <address type='pci' domain='0x0000' bus='0x00' slot='0x02' function='0x1'/>
    </controller>
    <controller type='pci' index='3' model='pcie-root-port'>
      <model name='pcie-root-port'/>
      <target chassis='3' port='0x12'/>
      <alias name='pci.3'/>
      <address type='pci' domain='0x0000' bus='0x00' slot='0x02' function='0x2'/>
    </controller>
    <interface type='user'>
      <mac address='52:54:00:fe:e5:f7'/>
      <ip address='192.168.5.15' family='ipv4' prefix='24'/>
      <route family='ipv4' gateway='192.168.5.2'/>
      <portForward proto='tcp' address='127.0.0.1'>
        <range start='61000' to='22'/>
      </portForward>
      <model type='virtio'/>
      <backend type='passt'/>
      <mtu size='65520'/>
      <alias name='net0'/>
      <address type='pci' domain='0x0000' bus='0x00' slot='0x01' function='0x0'/>
    </interface>
    <serial type='pty'>
      <source path='/dev/pts/3'/>
      <target type='isa-serial' port='0'>
        <model name='isa-serial'/>
      </target>
      <alias name='serial0'/>
    </serial>
    <console type='pty' tty='/dev/pts/3'>
      <source path='/dev/pts/3'/>
      <target type='serial' port='0'/>
      <alias name='serial0'/>
    </console>
    <input type='mouse' bus='ps2'>
      <alias name='input0'/>
    </input>
    <input type='keyboard' bus='ps2'>
      <alias name='input1'/>
    </input>
    <audio id='1' type='none'/>
    <video>
      <model type='none'/>
      <alias name='video0'/>
    </video>
    <watchdog model='itco' action='reset'>
      <alias name='watchdog0'/>
    </watchdog>
    <memballoon model='none'/>
    <rng model='virtio'>
      <backend model='random'>/dev/urandom</backend>
      <alias name='rng0'/>
      <address type='pci' domain='0x0000' bus='0x02' slot='0x00' function='0x0'/>
    </rng>
  </devices>
  <seclabel type='dynamic' model='selinux' relabel='yes'>
    <label>system_u:system_r:svirt_t:s0:c865,c946</label>
    <imagelabel>system_u:object_r:svirt_image_t:s0:c865,c946</imagelabel>
  </seclabel>
  <seclabel type='static' model='dac' relabel='yes'>
    <label>+955:+953</label>
    <imagelabel>+955:+953</imagelabel>
  </seclabel>
</domain>
"""


class TestDomainTemplate(LibvirtCase):
    def render(self, name="box", port=FIRST_PORT):
        return self.box.domain_xml(
            name,
            port,
            VM_UID,
            VM_GID,
            self.box.LIBVIRT_MEMORY_GIB,
            DOMAIN_CPUS,
        )

    def devices(self, xml):
        return [child.tag for child in ET.fromstring(xml).find("devices")]

    def gaps(self, info, instance="box"):
        return self.box.domain_gaps(info, instance, VM_UID, VM_GID, self.firmware())

    def firmware(self):
        return self.box.HostFirmware(EMULATOR, frozenset({LOADER}))

    def with_device(self, element):
        return self.render().replace("<video>", element + "<video>")

    def with_rng(self, element):
        return re.sub(r"<rng .*?</rng>", element, self.render(), flags=re.S)

    def assert_gaps(self, xml, *gaps):
        found = self.gaps(self.box.parse_domain(xml))
        self.assertEqual(found, list(gaps))
        for gap in gaps:
            self.assertIn(gap, self.driver.config_gap_message(found))

    def test_every_device_tag_the_template_lacks_is_a_gap_named_in_the_message(self):
        broken = {
            "graphics": "<graphics type='vnc'/>",
            "vsock": "<vsock model='virtio'/>",
            "filesystem": "<filesystem type='mount'/>",
            "channel": "<channel type='pty'/>",
            "hostdev": "<hostdev/>",
            "tpm": "<tpm model='tpm-crb'/>",
            "shmem": "<shmem name='x'/>",
            "redirdev": "<redirdev bus='usb' type='spicevmc'/>",
            "smartcard": "<smartcard mode='passthrough' type='tcp'/>",
            "parallel": "<parallel type='pty'/>",
            "iommu": "<iommu model='intel'/>",
            "panic": "<panic model='isa'/>",
        }
        for tag, element in broken.items():
            with self.subTest(tag=tag):
                self.assert_gaps(self.with_device(element), "element:devices/" + tag)

    def test_a_device_of_the_templates_tag_with_another_type_is_a_gap(self):
        broken = {
            "value:devices/input@type=passthrough": "<input type='passthrough' bus='ps2'/>",
            "value:devices/input@type=evdev": "<input type='evdev' bus='ps2'/>",
            "value:devices/controller@type=virtio-serial": (
                "<controller type='virtio-serial'/>"
            ),
            "value:devices/audio@type=pulseaudio": "<audio id='1' type='pulseaudio'/>",
        }
        for gap, element in broken.items():
            with self.subTest(gap=gap):
                self.assert_gaps(self.with_device(element), gap)
        base = self.render()
        changed = {
            "value:devices/serial@type=tcp": base.replace(
                "<serial type='pty'>", "<serial type='tcp'>"
            ),
            "value:devices/console@type=unix": base.replace(
                "<console type='pty'>", "<console type='unix'>"
            ),
            "value:devices/memballoon@model=virtio": base.replace(
                "<memballoon model='none'/>", "<memballoon model='virtio'/>"
            ),
        }
        for gap, xml in changed.items():
            with self.subTest(gap=gap):
                self.assert_gaps(xml, gap)

    def test_a_second_serial_or_console_is_a_gap(self):
        for tag in ("serial", "console"):
            with self.subTest(tag=tag):
                self.assert_gaps(
                    self.with_device("<%s type='pty'/>" % tag),
                    "repeated:devices/" + tag,
                )

    def test_a_disk_that_is_not_a_pool_volume_of_this_box_is_a_gap(self):
        base = self.render()
        broken = {
            "value:devices/disk@type=file": base.replace(
                "<disk type='volume' device='disk'>", "<disk type='file' device='disk'>"
            ),
            "value:devices/disk@device=lun": base.replace(
                "<disk type='volume' device='disk'>",
                "<disk type='volume' device='lun'>",
            ),
            "value:devices/disk/source@pool=homes": base.replace(
                "pool='default' volume='yolobox-box.qcow2'",
                "pool='homes' volume='yolobox-box.qcow2'",
            ),
            "value:devices/disk/source@volume=other.qcow2": base.replace(
                "volume='yolobox-box.qcow2'", "volume='other.qcow2'"
            ),
            "attribute:devices/disk/source@file": base.replace(
                "volume='yolobox-box.qcow2'",
                "volume='yolobox-box.qcow2' file='/etc/shadow'",
            ),
            "element:devices/disk/source/seclabel": base.replace(
                "volume='yolobox-box.qcow2'/>",
                "volume='yolobox-box.qcow2'><seclabel model='dac' relabel='no'/></source>",
            ),
            "element:devices/disk/seclabel": base.replace(
                "<target dev='vda' bus='virtio'/>",
                "<target dev='vda' bus='virtio'/><seclabel model='selinux' relabel='no'/>",
            ),
            "element:devices/disk/shareable": base.replace(
                "<target dev='vda' bus='virtio'/>",
                "<target dev='vda' bus='virtio'/><shareable/>",
            ),
            "value:devices/disk/driver@discard=ignore": base.replace(
                "discard='unmap'", "discard='ignore'"
            ),
            "attribute:devices/disk/driver@cache": base.replace(
                "<driver name='qemu' type='raw'/>",
                "<driver name='qemu' type='raw' cache='unsafe'/>",
            ),
        }
        for gap, xml in broken.items():
            with self.subTest(gap=gap):
                self.assert_gaps(xml, gap)

    def test_a_backing_store_is_a_gap_in_each_of_its_parts(self):
        # libvirt stores an empty <backingStore/>; one with a file is a host path.
        xml = self.render().replace(
            "<target dev='vda' bus='virtio'/>",
            "<backingStore type='file'><source file='/home/op/base.qcow2'/>"
            "</backingStore><target dev='vda' bus='virtio'/>",
        )
        self.assert_gaps(
            xml,
            "attribute:devices/disk/backingStore@type",
            "element:devices/disk/backingStore/source",
        )

    def test_a_boot_from_a_host_file_or_another_firmware_is_a_gap(self):
        base = self.render()
        broken = {
            "element:os/kernel": "<kernel>/home/op/vmlinuz</kernel>",
            "element:os/initrd": "<initrd>/home/op/initrd.img</initrd>",
            "element:os/cmdline": "<cmdline>init=/bin/sh</cmdline>",
            "element:os/dtb": "<dtb>/home/op/x.dtb</dtb>",
            "element:os/smbios": "<smbios mode='host'/>",
            "element:os/acpi": "<acpi><table type='slic'>/home/op/slic.dat</table></acpi>",
            "text:os/loader=/home/op/evil.fd": (
                "<loader readonly='yes' type='pflash'>/home/op/evil.fd</loader>"
            ),
            "value:os/loader@readonly=no": (
                "<loader readonly='no' type='pflash'>%s</loader>" % LOADER
            ),
            "text:os/nvram=/home/op/vars.fd": (
                "<nvram template='/usr/share/edk2/ovmf/OVMF_VARS_4M.qcow2'>"
                "/home/op/vars.fd</nvram>"
            ),
            "value:os/nvram@template=/home/op/vars.fd": (
                "<nvram template='/home/op/vars.fd'>"
                "/var/lib/libvirt/qemu/nvram/yolobox-box_VARS.qcow2</nvram>"
            ),
            "text:os/nvram=/var/lib/libvirt/qemu/nvram/other_VARS.qcow2": (
                "<nvram template='/usr/share/edk2/ovmf/OVMF_VARS_4M.qcow2'>"
                "/var/lib/libvirt/qemu/nvram/other_VARS.qcow2</nvram>"
            ),
        }
        for gap, element in broken.items():
            with self.subTest(gap=gap):
                self.assert_gaps(
                    base.replace("<boot dev='hd'/>", element + "<boot dev='hd'/>"), gap
                )
        self.assert_gaps(
            base.replace(
                "enabled='no' name='secure-boot'", "enabled='yes' name='secure-boot'"
            ),
            "value:os/firmware/feature@enabled=yes",
        )

    def test_an_emulator_other_than_the_hosts_is_a_gap(self):
        xml = self.render().replace(
            "<devices>", "<devices><emulator>/home/op/qemu-wrapper</emulator>"
        )
        self.assert_gaps(xml, "text:devices/emulator=/home/op/qemu-wrapper")

    def test_an_interface_that_reaches_a_host_file_or_a_second_port_is_a_gap(self):
        base = self.render()
        broken = {
            "element:devices/interface/rom": base.replace(
                "<mtu size='65520'/>",
                "<mtu size='65520'/><rom file='/home/op/rom.bin'/>",
            ),
            "attribute:devices/interface/backend@logFile": base.replace(
                "<backend type='passt'/>",
                "<backend type='passt' logFile='/etc/shadow'/>",
            ),
            "attribute:devices/interface/backend@upstream": base.replace(
                "<backend type='passt'/>", "<backend type='passt' upstream='eth0'/>"
            ),
            "element:devices/interface/source": base.replace(
                "<backend type='passt'/>",
                "<backend type='passt'/><source dev='/dev/net/tun'/>",
            ),
            "repeated:devices/interface/portForward/range": base.replace(
                "<range start='61000' to='22'/>",
                "<range start='61000' to='22'/><range start='61001' to='22'/>",
            ),
            "attribute:devices/interface/portForward/range@end": base.replace(
                "<range start='61000' to='22'/>",
                "<range start='61000' end='61100' to='22'/>",
            ),
            "value:devices/interface/portForward/range@to=80": base.replace(
                "<range start='61000' to='22'/>", "<range start='61000' to='80'/>"
            ),
            "repeated:devices/interface/portForward": base.replace(
                "</portForward>",
                "</portForward><portForward proto='tcp' address='127.0.0.1'>"
                "<range start='61001' to='22'/></portForward>",
            ),
            "value:devices/interface/ip@address=192.168.5.16": base.replace(
                "address='192.168.5.15'", "address='192.168.5.16'"
            ),
            "value:devices/interface/route@gateway=192.168.5.1": base.replace(
                "gateway='192.168.5.2'", "gateway='192.168.5.1'"
            ),
        }
        for gap, xml in broken.items():
            with self.subTest(gap=gap):
                self.assert_gaps(xml, gap)

    def test_a_serial_log_a_base_label_or_a_qemu_argument_is_a_gap(self):
        base = self.render()
        qemu = "http://libvirt.org/schemas/domain/qemu/1.0"
        broken = {
            "element:devices/serial/log": base.replace(
                "<serial type='pty'>",
                "<serial type='pty'><log file='/home/op/serial.log'/>",
            ),
            "element:seclabel/baselabel": base.replace(
                "<seclabel type='dynamic' model='selinux' relabel='yes'/>",
                "<seclabel type='dynamic' model='selinux' relabel='yes'>"
                "<baselabel>system_u:system_r:unconfined_t:s0</baselabel></seclabel>",
            ),
            "text:seclabel/label=system_u:system_r:unconfined_t:s0:c1,c2": base.replace(
                "<seclabel type='dynamic' model='selinux' relabel='yes'/>",
                "<seclabel type='dynamic' model='selinux' relabel='yes'>"
                "<label>system_u:system_r:unconfined_t:s0:c1,c2</label></seclabel>",
            ),
            "element:qemu:commandline": base.replace(
                "<domain type='kvm'>", "<domain type='kvm' xmlns:qemu='%s'>" % qemu
            ).replace("</domain>", "<qemu:commandline/></domain>"),
            "element:sysinfo": base.replace(
                "</domain>", "<sysinfo type='smbios'/></domain>"
            ),
            "element:launchSecurity": base.replace(
                "</domain>", "<launchSecurity type='sev'/></domain>"
            ),
            "element:{urn:other}extra": base.replace(
                "</domain>", "<x:extra xmlns:x='urn:other'/></domain>"
            ),
            "element:metadata/{urn:other}note": base.replace(
                "</metadata>", "<x:note xmlns:x='urn:other'/></metadata>"
            ),
        }
        for gap, xml in broken.items():
            with self.subTest(gap=gap):
                self.assert_gaps(xml, gap)

    def test_an_rng_that_reads_anything_but_the_hosts_entropy_devices_is_a_gap(self):
        broken = {
            "value:devices/rng/backend@model=egd": (
                "<rng model='virtio'><backend model='egd'>/dev/urandom</backend></rng>"
            ),
            "missing:devices/rng/backend": "<rng model='virtio'/>",
            "text:devices/rng/backend=/home/op/secret": (
                "<rng model='virtio'>"
                "<backend model='random'>/home/op/secret</backend></rng>"
            ),
        }
        for gap, element in broken.items():
            with self.subTest(gap=gap):
                self.assert_gaps(self.with_rng(element), gap)
        self.assert_gaps(
            self.with_rng(
                "<rng model='virtio'><backend model='egd' type='unix'>"
                "<source mode='connect' path='/run/host.sock'/></backend></rng>"
            ),
            "value:devices/rng/backend@model=egd",
            "attribute:devices/rng/backend@type",
            "text:devices/rng/backend=",
            "element:devices/rng/backend/source",
        )

    def test_a_device_that_is_there_twice_is_one_gap(self):
        xml = self.with_device("<shmem name='a'/><shmem name='b'/>")
        self.assert_gaps(xml, "element:devices/shmem")

    def test_a_domain_as_libvirt_runs_it_has_no_gap(self):
        xml = LIBVIRT_STORED_DOMAIN.replace(
            "+955:+953", "+%d:+%d" % (VM_UID, VM_GID)
        ).replace("MEMORY_KIB", str(self.box.LIBVIRT_MEMORY_GIB << 20))
        self.assertEqual(self.gaps(self.box.parse_domain(xml), "yolobox"), [])

    def test_both_host_entropy_devices_are_no_gap(self):
        for path in ("/dev/urandom", "/dev/random"):
            with self.subTest(path=path):
                xml = self.with_rng(
                    "<rng model='virtio'><backend model='random'>%s</backend></rng>"
                    % path
                )
                self.assertEqual(self.gaps(self.box.parse_domain(xml)), [])

    def test_the_hosts_firmware_is_read_from_domcapabilities(self):
        firmware = self.box.parse_domcapabilities(DOMCAPABILITIES_XML)
        self.assertEqual(firmware.emulator, EMULATOR)
        self.assertIn(LOADER, firmware.loaders)
        for text in (
            "",
            "<domainCapabilities/>",
            "<domainCapabilities><path>/usr/bin/qemu</path></domainCapabilities>",
            "<x",
        ):
            with self.subTest(text=text):
                with self.assertRaises(self.box.YoError):
                    self.box.parse_domcapabilities(text)

    def test_a_rendered_domain_has_no_config_gap(self):
        info = self.box.parse_domain(self.render())
        self.assertEqual(self.gaps(info), [])

    def test_both_seclabels_are_there_and_the_dac_one_relabels_to_the_vm_account(self):
        root = ET.fromstring(self.render())
        labels = {
            (label.get("type"), label.get("model")): label
            for label in root.findall("seclabel")
        }
        self.assertEqual(set(labels), {("dynamic", "selinux"), ("static", "dac")})
        dac = labels[("static", "dac")]
        self.assertEqual(dac.get("relabel"), "yes")
        self.assertEqual(dac.findtext("label"), "+%d:+%d" % (VM_UID, VM_GID))

    def test_the_ssh_forward_binds_loopback_only(self):
        root = ET.fromstring(self.render(port=61234))
        forward = root.find("devices/interface/portForward")
        self.assertEqual(forward.get("address"), "127.0.0.1")
        self.assertEqual(forward.get("proto"), "tcp")
        self.assertEqual(forward.find("range").get("start"), "61234")
        self.assertEqual(forward.find("range").get("to"), "22")

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

    def test_every_guarantee_the_template_gives_is_noticed_when_it_goes_missing(self):
        base = self.render()
        broken = {
            "element:devices/graphics": base.replace(
                "<video>", "<graphics type='vnc'/><video>"
            ),
            "element:devices/channel": base.replace(
                "<video>", "<channel type='pty'/><video>"
            ),
            "element:devices/hostdev": base.replace("<video>", "<hostdev/><video>"),
            "seclabel.selinux": base.replace(
                "<seclabel type='dynamic' model='selinux' relabel='yes'/>", ""
            ),
            "seclabel.dac": base.replace(
                "type='static' model='dac'", "type='dynamic' model='dac'"
            ),
            "metadata": re.sub(r"<yolobox:instance.*?</yolobox:instance>", "", base),
            "portForward.address": base.replace(" address='127.0.0.1'>", ">"),
        }
        for gap, xml in broken.items():
            with self.subTest(gap=gap):
                self.assertEqual(self.gaps(self.box.parse_domain(xml)), [gap])

    def test_a_forward_off_loopback_is_the_address_gap_and_a_value_gap(self):
        xml = self.render().replace("address='127.0.0.1'", "address='0.0.0.0'")
        self.assert_gaps(
            xml,
            "value:devices/interface/portForward@address=0.0.0.0",
            "portForward.address",
        )

    def test_a_dac_label_that_is_not_the_vm_account_is_a_gap(self):
        base = self.render()
        for label in ("+0:+0", "+990:+0", "+0:+989", "990:989", ""):
            with self.subTest(label=label):
                xml = base.replace(
                    "<label>+%d:+%d</label>" % (VM_UID, VM_GID),
                    "<label>%s</label>" % label,
                )
                self.assertIn("seclabel.dac", self.gaps(self.box.parse_domain(xml)))

    def test_a_seclabel_that_does_not_relabel_is_a_gap(self):
        base = self.render()
        for model, gap in (("dac", "seclabel.dac"), ("selinux", "seclabel.selinux")):
            with self.subTest(model=model):
                xml = base.replace(
                    "model='%s' relabel='yes'" % model,
                    "model='%s' relabel='no'" % model,
                )
                self.assertEqual(self.gaps(self.box.parse_domain(xml)), [gap])

    def test_a_second_dac_seclabel_is_a_gap(self):
        xml = self.render().replace(
            "</domain>", "<seclabel type='static' model='dac' relabel='yes'/></domain>"
        )
        self.assertEqual(self.gaps(self.box.parse_domain(xml)), ["seclabel.dac"])

    def test_the_dac_label_is_judged_against_the_account_asked_for(self):
        info = self.box.parse_domain(self.render())
        self.assertEqual(
            self.box.domain_gaps(info, "box", VM_UID + 1, VM_GID, self.firmware()),
            ["seclabel.dac"],
        )

    def test_a_network_that_is_not_one_passt_interface_is_a_gap(self):
        base = self.render()
        extra = "<interface type='user'><backend type='passt'/></interface><serial"
        broken = {
            "bridge": base.replace(
                "<interface type='user'>", "<interface type='bridge'>"
            ),
            "slirp": base.replace(
                "<backend type='passt'/>", "<backend type='default'/>"
            ),
            "no backend": base.replace("<backend type='passt'/>", ""),
            "a second interface": base.replace("<serial", extra, 1),
            "no interface": re.sub(r"<interface.*</interface>", "", base, flags=re.S),
        }
        for name, xml in broken.items():
            with self.subTest(name=name):
                gaps = self.gaps(self.box.parse_domain(xml))
                self.assertIn("interface", gaps)

    def test_a_domain_of_another_instance_is_a_metadata_gap(self):
        xml = self.render().replace(
            ">box</yolobox:instance>", ">other</yolobox:instance>"
        )
        self.assert_gaps(xml, "metadata")

    def test_unparseable_xml_is_an_error_not_an_empty_domain(self):
        with self.assertRaisesRegex(self.box.YoError, "unparseable XML"):
            self.box.parse_domain("<domain")


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
            "x",
            FIRST_PORT + 2,
            VM_UID,
            VM_GID,
            self.box.LIBVIRT_MEMORY_GIB,
            DOMAIN_CPUS,
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
            self.box.libvirt, "loopback_port_bound", return_value=False
        ):
            self.assertEqual(self.driver.allocate_host_port(), FIRST_PORT + 6)

    def test_a_domain_undefined_while_yo_reads_the_others_is_skipped(self):
        self.host.tagged_domain("first", FIRST_PORT)
        self.host.tagged_domain("second", FIRST_PORT + 1)
        self.host.before_dumpxml = lambda name: self.host.domains.pop(
            "yolobox-first", None
        )
        with mock.patch.object(
            self.box.libvirt, "loopback_port_bound", return_value=False
        ):
            self.assertEqual(self.driver.allocate_host_port(), FIRST_PORT)

    def test_a_dumpxml_that_fails_for_a_domain_still_there_is_an_error(self):
        self.host.tagged_domain("first", FIRST_PORT)
        self.host.fail["dumpxml"] = "error: internal error: lizard"
        with self.assertRaisesRegex(
            self.box.YoError, r"virsh dumpxml yolobox-first failed \(exit 1\): .*lizard"
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
            self.box.libvirt, "loopback_port_bound", return_value=False
        ):
            self.assertEqual(self.driver.allocate_host_port(), FIRST_PORT + 1)

    def test_the_same_xml_in_this_machines_domain_is_an_error_not_a_skip(self):
        self.host.domains["yolobox-box"] = self.FOREIGN.replace(
            "<devices>",
            "<metadata><yolobox:instance xmlns:yolobox='%s'>box</yolobox:instance>"
            "</metadata><devices>" % self.box.DOMAIN_NS,
        )
        self.host.states["yolobox-box"] = "running"
        with self.assertRaises(self.box.YoError) as caught:
            self.driver.ssh_port()
        self.assertIn("no numeric start", caught.exception.message)

    def test_the_driver_skips_a_port_bound_on_the_host(self):
        with mock.patch.object(
            self.box.libvirt,
            "loopback_port_bound",
            side_effect=lambda port: port == FIRST_PORT,
        ):
            self.assertEqual(self.driver.allocate_host_port(), FIRST_PORT + 1)

    def test_the_ssh_port_is_read_back_from_the_domain(self):
        self.host.tagged_domain("box", 61234)
        self.assertEqual(self.driver.ssh_port(), 61234)

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
                self.box.libvirt, "loopback_port_bound", return_value=False
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
        forward = [call for call in self.host.calls if "forward" in call][0]
        self.assertEqual(forward[forward.index("-O") + 1], "forward")
        self.assertEqual(
            forward[forward.index("-L") + 1], "127.0.0.1:%d:127.0.0.1:5173" % FIRST_PORT
        )
        self.assertEqual(forward[-1], "lima-box")

    def test_the_port_is_chosen_and_taken_under_the_lock(self):
        held = []
        run = self.host.run

        def spy(argv, **kwargs):
            if "forward" in argv:
                held.append(self.lock_held())
            return run(argv, **kwargs)

        with (
            mock.patch.object(self.box.proc, "run", spy),
            mock.patch.object(
                self.box.libvirt,
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
    raise unittest.SkipTest("the initrd's key program needs gawk, or nix to fetch it")


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
    def config(self):
        return self.box.libvirt_ssh_config(
            "lima-box", 61234, OPERATOR, "/h/.1password/agent.sock", "/h/state/box"
        )

    def test_the_block_is_the_shape_yos_parser_reads(self):
        values = self.box.parse_ssh_host_block(self.config(), "lima-box")
        self.assertEqual(values.hostname, "127.0.0.1")
        self.assertEqual(values.port, "61234")
        self.assertEqual(values.identity_files, ("/h/state/box/operator.pub",))
        self.assertEqual(values.identity_agent, "/h/.1password/agent.sock")

    def test_it_uses_the_agent_alone_and_never_forwards_it(self):
        text = self.config()
        for line in (
            "IdentitiesOnly yes",
            "ForwardAgent no",
            "ControlMaster auto",
            'ControlPath "/h/state/box/ssh.sock"',
            "ControlPersist yes",
            "BatchMode yes",
        ):
            self.assertIn("  " + line + "\n", text)

    def test_the_herdr_alias_carries_the_agent_socket(self):
        values = self.box.parse_ssh_host_block(self.config(), "lima-box")
        alias = self.box.herdr_alias_block(values)
        self.assertIn('IdentityAgent "/h/.1password/agent.sock"', alias)

    def test_a_block_without_an_agent_gives_an_alias_without_one(self):
        text = 'Host lima-box\n  Hostname 127.0.0.1\n  Port 1\n  IdentityFile "/k"\n'
        values = self.box.parse_ssh_host_block(text, "lima-box")
        self.assertNotIn("IdentityAgent", self.box.herdr_alias_block(values))

    def test_the_file_is_rewritten_only_when_the_block_changed(self):
        os.makedirs(self.box.instance_dir())
        self.driver.write_ssh_config(61234)
        owned = self.box.AGENT_SSH_CONFIG_MARKER + "\nowned block\n\n"
        with open(self.box.ssh_cfg()) as handle:
            written = handle.read()
        with open(self.box.ssh_cfg(), "w") as handle:
            handle.write(owned + written)
        self.driver.write_ssh_config(61234)
        with open(self.box.ssh_cfg()) as handle:
            self.assertEqual(handle.read(), owned + written)
        self.driver.write_ssh_config(61235)
        with open(self.box.ssh_cfg()) as handle:
            self.assertIn("Port 61235", handle.read())

    def test_the_initrd_probe_uses_the_agent_and_the_public_key(self):
        os.makedirs(self.box.instance_dir())
        self.driver.write_ssh_config(61234)
        argv = self.box.initrd_ssh_argv("true")
        self.assertIn("IdentityAgent=%s" % self.driver.op_sock(), argv)
        self.assertEqual(
            argv[argv.index("-i") + 1],
            os.path.join(self.box.instance_dir(), "operator.pub"),
        )
        self.assertIn("IdentitiesOnly=yes", argv)

    def test_the_initrd_probe_before_the_config_exists_names_the_public_key(self):
        with mock.patch.object(self.driver, "ssh_port", return_value=61234):
            argv = self.box.initrd_ssh_argv("true")
        self.assertTrue(argv[argv.index("-i") + 1].endswith("/box/operator.pub"))

    def test_lima_keeps_its_private_key_and_no_agent(self):
        mac = load_yo("yo_mac_initrd")
        self.addCleanup(unload_yo, "yo_mac_initrd")
        with mock.patch.object(mac.box.driver, "ssh_port", return_value=60022):
            argv = mac.initrd_ssh_argv("true")
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

    def assertRefused(self, fragment, **kwargs):
        with self.assertRaises(self.box.YoError) as caught:
            self.check(**kwargs)
        message = caught.exception.message
        self.assertIn(fragment, message)
        self.assertIn("host-setup.sh", message)
        return message

    def test_a_ready_host_passes(self):
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

    def host_gaps(self, acl):
        with (
            mock.patch.object(
                self.box.shutil, "which", return_value="/usr/bin/getfacl"
            ),
            mock.patch.object(
                self.box.proc, "run", lambda argv, **kwargs: completed(acl)
            ),
        ):
            return self.box.LIBVIRT.host_gaps()

    def test_the_driver_reports_a_narrowed_mask_as_its_one_host_gap(self):
        self.assertEqual(self.host_gaps(getfacl_listing()), [])
        gaps = self.host_gaps(getfacl_listing(mask="r-x"))
        self.assertEqual(len(gaps), 1)
        self.assertIn("sudo setfacl -m u:yolobox-vm:rwx", gaps[0])

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


class TestStatusAndGaps(LibvirtCase):
    def test_a_domain_that_is_not_defined_has_no_status_and_no_gaps(self):
        self.assertIsNone(self.driver.vm_status())
        self.assertEqual(self.driver.config_gaps(), [])

    def test_running_and_blocked_are_running_and_other_states_keep_their_words(self):
        # "blocked" is a running guest idling on I/O; the same word set keeps
        # a stop waiting through it (require_stoppable).
        self.host.tagged_domain("box", FIRST_PORT)
        self.assertEqual(self.driver.vm_status(), "Running")
        self.host.states["yolobox-box"] = "blocked"
        self.assertEqual(self.driver.vm_status(), "Running")
        self.driver.require_stoppable("blocked")
        self.host.states["yolobox-box"] = "shut off"
        self.assertEqual(self.driver.vm_status(), "shut off")

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

    def test_a_live_domain_with_a_device_the_box_never_has_is_a_gap(self):
        self.host.tagged_domain("box", FIRST_PORT)
        self.host.domains["yolobox-box"] = self.host.domains["yolobox-box"].replace(
            "<video>", "<tpm model='tpm-crb'/><video>"
        )
        self.assertEqual(self.driver.config_gaps(), ["element:devices/tpm"])
        self.assertIn(
            "undefine --nvram yolobox-box",
            self.driver.config_gap_message(["element:devices/tpm"]),
        )

    def refusal(self, memory_gib=None, disk_gib=None):
        return self.driver.creation_refusal(self.box.Size(memory_gib, disk_gib))

    def finished_volume(self, gib):
        self.host.volumes["yolobox-box.qcow2"] = b"a whole disk"
        self.host.capacity["yolobox-box.qcow2"] = gib << 30

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
        self.assertIn("MemTotal 34.0 GiB", refusal)
        self.assertIn("keeps 4 GiB for itself", refusal)
        self.assertIn("may take 6.0 GiB (win 6.0 GiB)", refusal)
        self.assertIn("leaves 24.0 GiB for this box", refusal)
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
        self.host_memory((self.box.HOST_MEMORY_RESERVE_GIB + 7) << 20)
        refusal = self.refusal(memory_gib=8)
        self.assertIn("other active domains may take 0.0 GiB (none)", refusal)
        self.assertIn(
            "leaves 7.0 GiB for this box — that is under the 8 GiB a box needs",
            refusal,
        )
        self.assertNotIn("at most", refusal)
        self.host.foreign_domain("win", 8 << 20)
        refusal = self.refusal(memory_gib=8)
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
        self.finished_volume(100)
        refusal = self.refusal(disk_gib=250)
        self.assertIn("100 GiB", refusal)
        self.assertIn("--disk 250", refusal)
        self.assertIn("drop --disk", refusal)
        self.assertIn("keeping what is on it: yo -m box disk-grow 250", refusal)
        self.assertNotIn("vol-delete", refusal)

    def test_a_smaller_disk_than_the_kept_volume_is_refused_since_a_disk_cannot_shrink(
        self,
    ):
        self.finished_volume(250)
        refusal = self.refusal(disk_gib=100)
        self.assertIn("a disk cannot shrink", refusal)
        self.assertIn(
            "virsh -c qemu:///system vol-delete --pool default yolobox-box.qcow2",
            refusal,
        )

    def test_the_kept_volumes_own_size_or_memory_alone_is_created(self):
        self.finished_volume(100)
        self.assertIsNone(self.refusal(disk_gib=100))
        self.assertIsNone(self.refusal(memory_gib=24))

    def test_an_unfinished_volume_is_rebuilt_at_any_disk_size(self):
        self.host.volumes["yolobox-box.qcow2"] = b""
        self.host.capacity["yolobox-box.qcow2"] = 4096
        self.assertIsNone(self.refusal(disk_gib=250))

    def test_the_disk_image_is_the_pool_volumes_allocation(self):
        self.assertIsNone(self.driver.disk_image())
        self.host.volumes["yolobox-box.qcow2"] = b""
        label, kib = self.driver.disk_image()
        self.assertIn("yolobox-box.qcow2", label)
        self.assertEqual(kib, 2 * 1024 * 1024)

    def test_the_size_is_what_dominfo_and_the_volume_report(self):
        self.host.tagged_domain("box", FIRST_PORT)
        self.host.states["yolobox-box"] = "shut off"
        self.host.volumes["yolobox-box.qcow2"] = b"a whole disk"
        self.host.capacity["yolobox-box.qcow2"] = 120 << 30
        self.host.cpus["yolobox-box"] = 6
        self.assertEqual(self.driver.current_size(), self.box.BoxSize(16, 120, 6))

    def test_memory_that_is_not_whole_gib_is_an_error(self):
        self.host.tagged_domain("box", FIRST_PORT)
        self.host.max_memory_kib["yolobox-box"] = 12 * (1 << 20) + 1
        with self.assertRaisesRegex(self.box.YoError, "not a whole number of GiB"):
            self.driver.current_size()

    def test_a_disk_that_is_not_whole_gib_is_an_error_not_a_rounded_one(self):
        self.host.tagged_domain("box", FIRST_PORT)
        self.host.volumes["yolobox-box.qcow2"] = b"a whole disk"
        self.host.capacity["yolobox-box.qcow2"] = (100 << 30) + 512
        with self.assertRaisesRegex(self.box.YoError, "disk is .* not a whole number"):
            self.driver.current_size()

    def test_a_dominfo_without_cpus_is_an_error(self):
        self.host.tagged_domain("box", FIRST_PORT)
        self.host.dominfo["yolobox-box"] = "Max memory:     16777216 KiB\n"
        with self.assertRaisesRegex(self.box.YoError, "printed no Max memory"):
            self.driver.current_size()

    def test_a_shut_off_domain_gets_its_volume_resized_and_a_running_one_does_not(self):
        self.host.tagged_domain("box", FIRST_PORT)
        self.host.volumes["yolobox-box.qcow2"] = b"a whole disk"
        with (
            mock.patch.object(self.box.ui, "out"),
            self.assertRaisesRegex(
                self.box.YoError, "only while the domain is shut off"
            ),
        ):
            self.driver.grow_disk(100, 200)
        self.assertEqual(self.host.calls_of("vol-resize"), [])
        self.host.states["yolobox-box"] = "shut off"
        with mock.patch.object(self.box.ui, "out"):
            self.driver.grow_disk(100, 200)
        self.assertEqual(
            self.host.calls_of("vol-resize")[0][3:],
            ["vol-resize", "--pool", "default", "yolobox-box.qcow2", "200G"],
        )
        self.assertEqual(self.host.capacity["yolobox-box.qcow2"], 200 << 30)

    def test_the_nix_commands_name_the_machine_and_never_the_host_os(self):
        env = self.box.nix_env("alice", "github:x/y/z")
        self.assertEqual(
            env,
            [
                "YOLOBOX_USERNAME=alice",
                "YOLOBOX_HOSTNAME=yolobox-box",
                "YOLOBOX_FLAKE_REF=github:x/y/z",
            ],
        )


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
                    self.driver.stop,
                    self.driver.removals,
                    self.driver.config_gaps,
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
        self.assertIsNone(self.pid("Control socket connect(/x): No such file\n"))

    def test_a_pid_that_would_hit_init_is_not_a_pid(self):
        self.assertIsNone(self.pid("Master running (pid=1)\n"))

    def test_reading_the_pid_for_status_opens_no_master(self):
        self.host.master_running = False
        self.assertIsNone(self.driver.running_keepalive_pid())
        self.assertFalse(self.host.master_running)
        self.assertEqual([call for call in self.host.calls if call[-1] == "true"], [])

    def test_reading_the_pid_for_status_reads_a_running_master(self):
        self.host.master_running = True
        self.host.master_output = "Master running (pid=4242)\r\n"
        self.assertEqual(self.driver.running_keepalive_pid(), 4242)


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
        self.driver.stop()

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
        self.assertEqual(self.driver.vm_status(), "shut off")

    def test_events_that_are_not_the_stop_are_waited_past_by_the_same_watcher(self):
        self.stop(["in shutdown", "running", "shut off"])
        self.assertEqual(len(self.host.watchers), 1)
        self.assertEqual(self.driver.vm_status(), "shut off")

    def test_a_stop_that_lands_before_any_event_is_seen_does_not_wait_for_one(self):
        self.host.shutdown_state = "shut off"
        self.stop()
        self.assertEqual(self.driver.vm_status(), "shut off")

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
        self.assertEqual(self.driver.vm_status(), "in shutdown")

    def test_a_domain_that_went_off_as_the_event_stream_ended_is_stopped(self):
        self.host.off_at_eof = True
        self.stop(["in shutdown"], "timeout")
        self.assertEqual(self.driver.vm_status(), "shut off")

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
            self.driver.stop()
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
            mock.patch.object(self.box.release, "locked_rev", return_value="0" * 40),
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
            "the domain did not stop: domain yolobox-box is 'paused', so it will "
            "not shut down by itself. A domain pauses when its disk image cannot "
            "grow because the host's disk is full",
            text,
        )
        self.assertIn("domblkerror yolobox-box", text)
        self.assertIn(self.driver.force_stop_command(), text)
        self.assertIn("It runs stage 2", text)
        self.assertNotIn("Do not force it off", text)

    def test_no_stop_message_down_can_reach_names_a_power_cut(self):
        for state in ("paused", "pmsuspended", "crashed", "lizard"):
            with self.subTest(state=state):
                self.host.states["yolobox-box"] = state
                with self.assertRaises(self.box.YoError) as caught:
                    self.driver.stop()
                self.assertNotIn("destroy", caught.exception.message)
                self.assertNotIn(
                    self.driver.force_stop_command(), caught.exception.message
                )

    def test_a_state_yo_cannot_leave_does_not_send_the_user_back_to_down(self):
        # `yo down` would meet the same refusal, so the message names what
        # shows the cause instead.
        for state in ("crashed", "lizard"):
            with self.subTest(state=state):
                self.host.states["yolobox-box"] = state
                with self.assertRaises(self.box.YoError) as caught:
                    self.driver.stop()
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
            self.driver.stop()


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
        self.host.volumes["yolobox-box.qcow2"] = b"a whole disk"
        self.host.capacity["yolobox-box.qcow2"] = 100 << 30
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

    def down(self, stage, initrd=None):
        initrd = initrd or self.box.unlock.InitrdState.WAITING_FOR_PASSPHRASE
        reading = self.box.unlock.InitrdReading(initrd, "")
        self.host.event_script = (["shut off"], "hang")
        with (
            mock.patch.object(self.box.unlock, "boot_stage", return_value=stage),
            mock.patch.object(self.box.unlock, "initrd_state", return_value=reading),
        ):
            self.box.cmd_down(None)

    def test_a_box_waiting_in_its_initrd_is_powered_off_without_a_shutdown_request(
        self,
    ):
        self.down(self.box.BootStage.INITRD)
        self.assertEqual(self.host.calls_of("destroy")[0][4:], ["yolobox-box"])
        self.assertEqual(self.host.watchers, [])
        self.assertNotIn("shutdown", self.host.subcommands())
        self.assertEqual(self.driver.vm_status(), "shut off")

    def test_a_box_that_is_up_or_unanswering_is_asked_to_shut_down(self):
        for stage in (self.box.BootStage.STAGE2, self.box.BootStage.NONE):
            with self.subTest(stage=stage):
                self.host.watchers.clear()
                self.host.states["yolobox-box"] = "running"
                self.down(stage)
                self.assertIn("shutdown", self.host.subcommands())
                self.assertEqual(self.host.calls_of("destroy"), [])

    def test_a_box_not_provably_idle_in_its_initrd_is_neither_destroyed_nor_asked_to_shut_down(
        self,
    ):
        states = self.box.unlock.InitrdState
        for initrd in set(states) - {states.WAITING_FOR_PASSPHRASE}:
            with self.subTest(initrd=initrd):
                with self.assertRaisesRegex(self.box.YoError, "Not stopping it"):
                    self.down(self.box.BootStage.INITRD, initrd)
                self.assertEqual(self.host.calls_of("destroy"), [])
                self.assertNotIn("shutdown", self.host.subcommands())

    def test_the_refusal_prints_the_destroy_command_unless_the_box_is_encrypting(self):
        states = self.box.unlock.InitrdState
        destroy = "virsh -c qemu:///system destroy yolobox-box"
        for initrd in set(states) - {states.WAITING_FOR_PASSPHRASE}:
            with self.subTest(initrd=initrd):
                with self.assertRaises(self.box.YoError) as caught:
                    self.down(self.box.BootStage.INITRD, initrd)
                self.assertEqual(
                    destroy in caught.exception.message,
                    initrd is not states.MIGRATING,
                )

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
        self.instance_dir.mkdir(parents=True)
        (self.instance_dir / "operator.pub").write_text(PUBLIC_KEY)
        self.flake_ref = Path(self.box.state.flake_ref_file())
        self.flake_ref.parent.mkdir(parents=True, exist_ok=True)
        self.flake_ref.write_text("main\n")
        self.lock_file = Path(self.box.state.state_dir()) / "box.unlock.lock"
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
        self.flake_ref.unlink()
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
                with self.assertRaises(self.box.LockBusy):
                    with self.box.state.unlock_lock(wait=False):
                        pass
            return self.host.run(argv, **kwargs)

        with mock.patch.object(
            self.box.proc, "run", side_effect=assert_lock_held_while_it_mutates
        ):
            self.destroy(yes=True)
        self.assertNotIn("yolobox-box", self.host.domains)
        self.assertFalse(self.instance_dir.exists())
        self.assertFalse(self.flake_ref.exists())
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
        take_the_lock = self.box.state.unlock_lock

        @contextlib.contextmanager
        def lock_after_a_yo_up_started_the_box(wait):
            self.host.states["yolobox-box"] = "running"
            with take_the_lock(wait) as held:
                yield held

        with mock.patch.object(
            self.box.state, "unlock_lock", lock_after_a_yo_up_started_the_box
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
        undefine.remove()
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
        self.assertTrue(self.flake_ref.exists())
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
            "delete file ~/.local/state/yolobox/box.flake-ref",
        ):
            with self.subTest(piece=piece):
                self.assertIn(piece, listing)
        self.assertNotIn("yolobox-other", listing)
        self.assertNotIn("unlock.lock", listing)
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
        with self.box.state.unlock_lock(wait=True):
            pass
        waiting_yo = os.open(self.lock_file, os.O_RDWR)
        self.addCleanup(os.close, waiting_yo)
        self.destroy(yes=True)
        self.assertTrue(self.lock_file.exists())
        self.assertEqual(os.fstat(waiting_yo).st_nlink, 1)

    def test_pieces_that_are_gone_are_skipped_and_the_rest_removed(self):
        del self.host.volumes["yolobox-box-cidata.iso"]
        self.host.herdr_machines.pop(0)
        self.flake_ref.unlink()
        self.destroy(yes=True)
        self.assertEqual(self.mutations().count("vol-delete"), 1)
        self.assertNotIn("yolobox-box", self.host.domains)
        self.assertFalse(self.instance_dir.exists())

    def test_the_operators_master_quits_under_the_lock_that_orders_ensure_master(self):
        master_lock = os.path.join(self.instance_dir, self.box.MASTER_LOCK_NAME)
        held_while_quitting = []

        def watch_the_exit(argv, **kwargs):
            if argv[0] == "ssh" and "exit" in argv and "User=agent" not in argv:
                held_while_quitting.append(self.lock_held(master_lock))
            return self.host.run(argv, **kwargs)

        with mock.patch.object(self.box.proc, "run", side_effect=watch_the_exit):
            self.destroy(yes=True)
        self.assertEqual(held_while_quitting, [True])

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
        self.assertTrue(self.flake_ref.exists())

    def test_a_failing_herdr_removal_is_raised(self):
        self.host.herdr_remove_fails = True
        with self.assertRaisesRegex(
            self.box.YoError, "machine remove m-7 failed.*no such machine"
        ):
            self.destroy(yes=True)
        self.assertTrue(self.flake_ref.exists())

    def test_a_box_another_yo_is_unlocking_is_not_destroyed(self):
        with self.box.state.unlock_lock(wait=True):
            with self.assertRaisesRegex(
                self.box.YoError, "another yo is unlocking box"
            ):
                self.destroy(yes=True)
        self.assertLessEqual(set(self.host.subcommands()), READS)
        self.assertIn("yolobox-box", self.host.domains)
        self.assertTrue(self.instance_dir.exists())

    def test_the_listing_does_not_wait_for_an_unlocking_yo(self):
        with self.box.state.unlock_lock(wait=True):
            self.destroy(yes=False)
        self.assertIn("yo -m box destroy --yes", self.lines[-1])

    def test_the_hint_that_deletes_and_rebuilds_names_the_remembered_ref(self):
        self.flake_ref.write_text("github:aka-rider/yolobox/v1.2.3\n")
        self.assertEqual(
            self.box.state.destroy_then_bootstrap_command(),
            "yo -m box destroy --yes && "
            "yo -m box bootstrap --version github:aka-rider/yolobox/v1.2.3",
        )
        self.flake_ref.unlink()
        self.assertEqual(
            self.box.state.destroy_then_bootstrap_command(),
            "yo -m box destroy --yes && yo -m box bootstrap",
        )


class TestADeadAgent(LibvirtCase):
    """1Password's agent is gone. What needs no login still works, through
    main() and the real boot_stage; what must log in says so before any ssh."""

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
                self.box.libvirt, "loopback_port_bound", return_value=False
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
        self.assertEqual(self.driver.vm_status(), "shut off")
        self.assertFalse(
            [argv for argv in self.ssh_calls() if argv[-1] != "true"],
            "down ran more than the boot-stage probes",
        )

    def test_destroy_deletes_the_box_it_cannot_log_in_to(self):
        instance_dir = Path(self.box.state.instance_dir())
        instance_dir.mkdir(parents=True)
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

    def test_down_refuses_an_sshd_it_cannot_identify_and_names_the_agent(self):
        # An unmarked banner and both logins refused: an initrd, maybe encrypting
        # the disk, or an older stage 2. Neither stop is safe on an initrd.
        self.host.states["yolobox-box"] = "running"
        with mock.patch.object(
            self.box.unlock, "ssh_banner", return_value="SSH-2.0-OpenSSH_10.6"
        ):
            with self.assertRaises(self.box.YoError) as caught:
                self.box.main(["-m", "box", "down"])
        message = caught.exception.message
        self.assertTrue(message.startswith("not stopping it: "), message)
        self.assertIn("Found: " + self.dead(), message)
        # The passphrase may have gone in earlier this boot: no force offered.
        self.assertNotIn(self.driver.force_stop_command(), message)
        self.assertNotIn("shutdown", self.host.subcommands())
        self.assertNotIn("destroy", self.host.subcommands())
        self.assertEqual(self.driver.vm_status(), "Running")

    def test_status_on_an_unidentified_sshd_offers_no_force_without_the_key(self):
        # The passphrase may have gone in before 1Password died: fix it first.
        with mock.patch.object(
            self.box.unlock, "ssh_banner", return_value="SSH-2.0-OpenSSH_10.6"
        ):
            with self.assertRaises(self.box.YoError):
                self.box.main(["-m", "box", "status"])
        (vm_line,) = [line for line in self.lines if line.startswith("vm: ")]
        self.assertIn("Found: " + self.dead(), vm_line)
        self.assertNotIn(self.driver.force_stop_command(), vm_line)

    def test_a_reboot_with_a_cached_passphrase_still_names_the_agent(self):
        # bootstrap's second vm_up: the box stopped, the passphrase in hand.
        for domain_state in ("shut off", "running"):
            with self.subTest(domain_state=domain_state):
                self.host.calls.clear()
                self.host.states["yolobox-box"] = domain_state
                with self.assertRaises(self.box.YoError) as caught:
                    self.box.vm_up(run_services=False, passphrase=b"cached")
                self.assertTrue(
                    caught.exception.message.startswith(self.dead()),
                    caught.exception.message,
                )
                self.assertNotIn("start", self.host.subcommands())
                self.assertEqual(self.ssh_calls(), [])

    def test_status_names_the_agent_then_reports_the_rest(self):
        with self.assertRaises(self.box.YoError) as caught:
            self.box.main(["-m", "box", "status"])
        self.assertEqual(caught.exception.code, 1)
        self.assertTrue(
            self.lines[0].startswith("login: " + self.dead()), self.lines[0]
        )
        self.assertEqual(
            self.lines[1:3],
            [
                "vm: unreachable — is it up? start it with: yo -m box up",
                "libvirt: reverse forwards ok",
            ],
        )
        self.assertTrue(self.lines[3].startswith("aws: "), self.lines)

    def test_a_command_that_logs_in_names_the_agent_before_any_ssh(self):
        for argv in (["ssh"], ["enter"], ["gc"], ["aws-check"], ["code"]):
            with self.subTest(argv=argv):
                self.host.calls.clear()
                with self.assertRaises(self.box.YoError) as caught:
                    self.box.main(["-m", "box"] + argv)
                self.assertTrue(
                    caught.exception.message.startswith(self.dead()),
                    caught.exception.message,
                )
                self.assertEqual(self.ssh_calls(), [])

    def test_port_rides_a_live_master_without_the_agent(self):
        self.box.main(["-m", "box", "port", "5173"])
        self.assertEqual(len(self.lines), 1)
        self.assertRegex(self.lines[0], r"^127\.0\.0\.1:\d+$")
        self.assertEqual(len(self.host.forwarded), 1)
        self.assertIn(
            "127.0.0.1:%s:127.0.0.1:5173" % self.lines[0].split(":")[1],
            self.host.forwarded[0],
        )

    def test_a_master_whose_connection_died_is_dropped_and_names_the_agent(self):
        self.host.master_session = (255, "Connection to lima-box closed")
        with self.assertRaises(self.box.YoError) as caught:
            self.box.main(["-m", "box", "port", "5173"])
        self.assertTrue(
            caught.exception.message.startswith(self.dead()), caught.exception.message
        )
        self.assertEqual(self.host.forwarded, [])

    def test_port_without_a_master_names_the_agent_and_logs_in_nowhere(self):
        self.host.master_running = False
        with self.assertRaises(self.box.YoError) as caught:
            self.box.main(["-m", "box", "port", "5173"])
        self.assertTrue(
            caught.exception.message.startswith(self.dead()), caught.exception.message
        )
        self.assertEqual([argv for argv in self.ssh_calls() if "-O" not in argv], [])


class TestAnInitrdThatNeverAnswersPointsAtWhatTheHostShows(LibvirtCase):
    def test_the_timeout_names_the_domain_state_and_status_not_a_console(self):
        # virsh console keeps no history and the initrd writes to tty0, so
        # it would show nothing; the domain state and the cidata disk do.
        self.host.tagged_domain("box", FIRST_PORT)
        text = self.box.unlock.initrd_timeout_error(False).message
        self.assertIn("never offered its initrd sshd", text)
        self.assertIn("virsh -c qemu:///system domstate yolobox-box", text)
        self.assertIn("yo -m box status", text)
        self.assertIn("yolobox-box-cidata.iso", text)
        self.assertNotIn("console", text)


class TestAnSshdYoCannotIdentify(LibvirtCase):
    """The key is on offer, yet an unmarked sshd refused the operator and root:
    yo cannot tell an initrd from a stage 2, so it neither starts nor stops."""

    def setUp(self):
        super().setUp()
        self.host.tagged_domain("box", FIRST_PORT)
        self.host.sshd = lambda argv: (255, "Permission denied (publickey).")
        for patcher in (
            mock.patch.object(self.box.proc, "spawn", self.host.spawn),
            mock.patch.object(
                self.box.unlock, "ssh_banner", return_value="SSH-2.0-OpenSSH_10.6"
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def assert_names_the_way_out(self, text):
        # Fix the login and run again; never a force (the initrd may be
        # encrypting), never "see yo status", which would only refuse again.
        self.assertIn("Fix the login", text)
        # No console can be followed here (virsh console has no history and
        # the initrd writes to tty0), so the text says yo cannot see inside.
        self.assertIn("Without a working login yo cannot see inside it", text)
        self.assertIn("does not force it off", text)
        self.assertNotIn("console", text)
        self.assertNotIn(self.driver.force_stop_command(), text)
        self.assertNotIn("destroy", text)
        self.assertNotIn("yo -m box status", text)
        self.assertIn(self.driver.login_fix(), text)

    def test_no_unknown_text_in_any_login_state_names_a_force(self):
        # A listed key may not sign, and root's probe may only have timed out:
        # the initrd may be encrypting whatever the login state says.
        for login in (None, "", "lizard", self.box.vm.login_problem()):
            with self.subTest(login=login):
                self.assert_names_the_way_out(
                    self.box.unknown_stage_message(login, self.box.vm.RERUN)
                )

    def test_down_refuses_names_the_way_out_and_stops_nothing(self):
        with self.assertRaises(self.box.YoError) as caught:
            self.box.cmd_down(None)
        self.assertIn("not stopping it: an sshd answers", caught.exception.message)
        self.assert_names_the_way_out(caught.exception.message)
        self.assertIn("then %s:" % self.box.vm.RERUN, caught.exception.message)
        self.assertNotIn("shutdown", self.host.subcommands())
        self.assertNotIn("destroy", self.host.subcommands())

    def test_up_refuses_instead_of_taking_it_for_stage2(self):
        with self.assertRaises(self.box.YoError) as caught:
            self.box.vm_up(passphrase=b"cached")
        self.assertIn(
            "refused both the operator's key and root's", caught.exception.message
        )
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
        self.assertIn("an sshd answers", vm_lines[0])
        self.assert_names_the_way_out(vm_lines[0])
        self.assertNotIn("start it with", vm_lines[0])
        # status stops nothing, so it names the commands that do.
        self.assertIn("then run yo -m box down or yo -m box up:", vm_lines[0])
        self.assertNotIn(self.box.vm.RERUN, vm_lines[0])
        # Only the unpinned boot-stage probes ran; account_status's pinned ones,
        # which read the ssh.config and its known_hosts, did not.
        pinned = [
            argv
            for argv in self.host.calls
            if argv[0] == "ssh" and "-F" in argv and "/dev/null" not in argv
        ]
        self.assertEqual(pinned, [])


class TestEnsureMaster(LibvirtCase):
    """With the key on offer, a master that answers -O check but whose
    connection died is replaced; a healthy one is ridden as it is."""

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
                self.box.libvirt, "loopback_port_bound", return_value=False
            ),
        ):
            self.box.main(["-m", "box", "port", "5173"])
        (line,) = lines
        return line

    def assert_forwarded(self, printed):
        host_port = printed.split(":")[1]
        self.assertEqual(len(self.host.forwarded), 1)
        self.assertIn("127.0.0.1:%s:127.0.0.1:5173" % host_port, self.host.forwarded[0])

    def test_a_healthy_master_carries_the_forward(self):
        self.assert_forwarded(self.port())

    def test_a_master_whose_connection_died_is_replaced_and_carries_the_forward(self):
        self.host.master_session = (255, "Connection to lima-box closed")
        self.assert_forwarded(self.port())

    def test_with_no_master_a_login_opens_one_that_carries_the_forward(self):
        self.host.master_running = False
        self.assert_forwarded(self.port())

    def test_the_master_is_dropped_and_reopened_under_its_lock(self):
        # Another yo taking the lock in between could otherwise lose its master.
        self.host.master_session = (255, "Connection to lima-box closed")
        lock = os.path.join(self.box.instance_dir(), self.box.MASTER_LOCK_NAME)
        held = []
        run = self.host.run

        def spy(argv, **kwargs):
            if argv[:1] == ["ssh"] and ("exit" in argv or argv[-1] == "true"):
                held.append(self.lock_held(lock))
            return run(argv, **kwargs)

        with mock.patch.object(self.box.proc, "run", spy):
            self.driver.ensure_master()
        self.assertEqual(len(held), 3, "round trip, exit and reopen")
        self.assertTrue(all(held), held)
        self.assertFalse(self.lock_held(lock))


class TestVmUpWithAGap(LibvirtCase):
    def test_a_domain_that_is_not_what_the_template_made_is_never_started(self):
        self.host.tagged_domain("box", FIRST_PORT)
        self.host.states["yolobox-box"] = "shut off"
        self.host.domains["yolobox-box"] = self.host.domains["yolobox-box"].replace(
            "<video>", "<tpm model='tpm-crb'/><video>"
        )
        with mock.patch.object(self.box.agent, "luks_passphrase") as passphrase:
            with self.assertRaises(self.box.YoError) as caught:
                self.box.vm_up()
        self.assertIn("element:devices/tpm", caught.exception.message)
        self.assertIn("undefine --nvram yolobox-box", caught.exception.message)
        passphrase.assert_not_called()
        self.assertNotIn("start", self.host.subcommands())

    def test_a_domain_with_a_foreign_dac_label_is_refused_too(self):
        self.host.tagged_domain("box", FIRST_PORT)
        self.host.domains["yolobox-box"] = self.host.domains["yolobox-box"].replace(
            "+%d:+%d" % (VM_UID, VM_GID), "+0:+0"
        )
        with self.assertRaises(self.box.YoError) as caught:
            self.box.vm_up()
        self.assertIn("seclabel.dac", caught.exception.message)


class TestVmUpResizes(LibvirtCase):
    """`yo up --memory/--disk/--cpus` on a box whose domain exists."""

    def setUp(self):
        super().setUp()
        self.host.tagged_domain("box", FIRST_PORT)
        self.host.states["yolobox-box"] = "shut off"
        self.host.volumes["yolobox-box.qcow2"] = b"a whole disk"
        self.host.capacity["yolobox-box.qcow2"] = 100 << 30
        self.order = []

    def vm_up(self, size):
        """Run vm_up; return the error it raised (or None) and its stubs."""
        stubs = {}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(self.box.ui, "out"))
            for target, name in (
                (self.box.agent, "luks_passphrase"),
                (self.driver, "start"),
                (self.box.sshconfig, "ensure_agent_ssh_config"),
                (self.box.guest, "ensure_guest_known_hosts"),
                (self.box.forwards, "vm_services"),
            ):
                stubs[name] = stack.enter_context(mock.patch.object(target, name))
            for name in ("luks_passphrase", "start"):
                stubs[name].side_effect = lambda *a, _n=name, **k: (
                    self.order.append((_n, len(self.host.calls))) or mock.DEFAULT
                )
            try:
                self.box.vm_up(size=size)
            except self.box.YoError as failure:
                return failure, stubs
        return None, stubs

    def writes(self):
        return [sub for sub in self.host.subcommands() if sub not in READS]

    def assert_nothing_asked_or_changed(self, stubs):
        stubs["luks_passphrase"].assert_not_called()
        stubs["start"].assert_not_called()
        self.assertEqual(self.writes(), [])

    def test_memory_and_cpus_are_set_in_the_definition_after_the_passphrase(self):
        error, stubs = self.vm_up(self.box.Size(24, None, 2))
        self.assertIsNone(error)
        self.assertEqual(
            [call[3:] for call in self.host.calls if call[3:4] and call[3] in WRITES],
            [
                ["setmaxmem", "yolobox-box", "24G", "--config"],
                ["setmem", "yolobox-box", "24G", "--config"],
                ["setvcpus", "yolobox-box", "2", "--config", "--maximum"],
                ["setvcpus", "yolobox-box", "2", "--config"],
            ],
        )
        self.assertEqual(self.driver.current_size(), self.box.BoxSize(24, 100, 2))
        self.assertEqual(self.host.memory_kib["yolobox-box"], 24 << 20)
        stubs["start"].assert_called_once_with(
            stubs["luks_passphrase"].return_value, self.box.NO_SIZE
        )
        passphrase_at = dict(self.order)["luks_passphrase"]
        self.assertTrue(
            all(
                call[3] not in WRITES
                for call in self.host.calls[:passphrase_at]
                if call[3:4]
            ),
            "the box was changed before the passphrase was derived",
        )

    def test_less_memory_and_fewer_cpus_are_set_too(self):
        error, _ = self.vm_up(self.box.Size(8, None, 2))
        self.assertIsNone(error)
        self.assertEqual(self.driver.current_size(), self.box.BoxSize(8, 100, 2))
        self.assertEqual(self.host.memory_kib["yolobox-box"], 8 << 20)

    def test_a_bigger_disk_is_left_to_the_grow_after_the_start(self):
        # The volume is resized by grow_to_asked_disk, once the booted guest is
        # known to be able to grow (TestUpGrowsTheDiskOfAStoppedBox).
        error, stubs = self.vm_up(self.box.Size(None, 250, None))
        self.assertIsNone(error)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.host.capacity["yolobox-box.qcow2"], 100 << 30)
        stubs["start"].assert_called_once_with(
            stubs["luks_passphrase"].return_value, self.box.NO_SIZE
        )

    def test_the_size_the_box_already_has_changes_nothing(self):
        error, stubs = self.vm_up(self.box.Size(16, 100, DOMAIN_CPUS))
        self.assertIsNone(error)
        self.assertEqual(self.writes(), [])
        stubs["start"].assert_called_once_with(
            stubs["luks_passphrase"].return_value, self.box.NO_SIZE
        )

    def test_a_start_without_a_size_never_reads_the_size(self):
        error, _ = self.vm_up(self.box.NO_SIZE)
        self.assertIsNone(error)
        self.assertEqual(self.host.calls_of("dominfo"), [])
        self.assertEqual(self.writes(), [])

    def test_a_change_to_a_box_that_is_not_shut_off_is_refused_before_anything(self):
        for domain_state in ("running", "paused", "in shutdown"):
            with self.subTest(domain_state=domain_state):
                self.host.states["yolobox-box"] = domain_state
                error, stubs = self.vm_up(self.box.Size(24, None, None))
                shown = "Running" if domain_state == "running" else domain_state
                self.assertIn(
                    "box is %s with 16 GiB of memory, %d CPUs and a 100 GiB disk"
                    % (shown, DOMAIN_CPUS),
                    error.message,
                )
                self.assertIn("--memory 24 would change that", error.message)
                self.assertIn("yo -m box down, then run this command", error.message)
                self.assert_nothing_asked_or_changed(stubs)

    def test_a_smaller_disk_is_refused_before_anything_and_not_by_deleting_the_box(
        self,
    ):
        error, stubs = self.vm_up(self.box.Size(24, 50, None))
        self.assertIn("box's disk is 100 GiB", error.message)
        self.assertIn("neither lima nor libvirt can shrink a disk", error.message)
        self.assertNotIn("destroy", error.message)
        self.assert_nothing_asked_or_changed(stubs)

    def test_more_memory_than_the_host_has_room_for_is_refused_before_anything(self):
        self.host.foreign_domain("win", 6 << 20)
        error, stubs = self.vm_up(self.box.Size(HOST_MEMORY_GIB - 4 - 6 + 1, None, 2))
        self.assertIn("does not fit on this host", error.message)
        self.assert_nothing_asked_or_changed(stubs)

    def test_cpus_alone_are_not_judged_by_the_hosts_memory(self):
        self.host_memory(8 << 20)
        error, _ = self.vm_up(self.box.Size(None, None, 2))
        self.assertIsNone(error)
        self.assertEqual(self.driver.current_size().cpus, 2)

    def test_less_memory_is_never_refused_by_a_full_host(self):
        # A plain `yo up` would start the box at its 16 GiB unjudged, so a
        # shrink to 8 cannot fit worse than that.
        self.host_memory(8 << 20)
        error, _ = self.vm_up(self.box.Size(8, None, None))
        self.assertIsNone(error)
        self.assertEqual(self.driver.current_size().memory_gib, 8)
        error, _ = self.vm_up(self.box.Size(9, None, None))
        self.assertIn("does not fit on this host", error.message)

    def test_a_virsh_that_refuses_a_setting_stops_before_the_start(self):
        self.host.fail["setvcpus"] = "error: too many vCPUs"
        error, stubs = self.vm_up(self.box.Size(None, None, 2))
        self.assertIn("too many vCPUs", error.message)
        stubs["start"].assert_not_called()


class TestVmUpCreatesAtTheAskedSize(LibvirtCase):
    def up(self, host_cpus=16, *flags):
        with (
            mock.patch.object(self.driver, "require_tool"),
            mock.patch.object(self.box.os, "cpu_count", return_value=host_cpus),
            mock.patch.object(self.box.agent, "luks_passphrase") as passphrase,
            mock.patch.object(self.driver, "start") as start,
        ):
            try:
                self.box.main(["-m", "box", "up", *flags])
            except self.box.YoError as failure:
                return failure, passphrase, start
        return None, passphrase, start

    def test_all_three_flags_reach_the_creation(self):
        with (
            mock.patch.object(self.box.sshconfig, "ensure_agent_ssh_config"),
            mock.patch.object(self.box.guest, "ensure_guest_known_hosts"),
            mock.patch.object(self.box.forwards, "vm_services"),
            # A box just created runs the stock image, whose root is not encrypted.
            mock.patch.object(self.box.vm, "luks_device_fstype", return_value="ext4"),
            # The box start() would have created, at the size it was asked.
            mock.patch.object(
                self.driver, "current_size", return_value=self.box.BoxSize(24, 250, 3)
            ),
        ):
            error, passphrase, start = self.up(
                16, "--memory", "24", "--disk", "250", "--cpus", "3"
            )
        self.assertIsNone(error)
        self.assertEqual(self.host.calls_of("vol-resize"), [])
        start.assert_called_once_with(
            passphrase.return_value, self.box.Size(24, 250, 3)
        )

    def test_a_host_that_cannot_give_a_default_creates_nothing(self):
        for host in (1, None):
            with self.subTest(host=host):
                error, passphrase, start = self.up(host)
                self.assertIn("CPU", error.message)
                passphrase.assert_not_called()
                start.assert_not_called()


SS_LINE = (
    "LISTEN 0      4096       127.0.0.1:61000      0.0.0.0:*    uid:%d ino:98765 "
    "sk:2f cgroup:/system.slice/virtqemud.service <->"
)


class TestInitrdListener(LibvirtCase):
    def verify(self, *listeners, port=61000):
        self.host.listeners = "".join(line + "\n" for line in listeners)
        self.driver.verify_initrd_listener(port)

    def test_the_owner_is_read_from_every_listing_line(self):
        self.assertEqual(
            self.box.listener_uids(SS_LINE % 990 + "\n\n" + SS_LINE % 1000 + "\n"),
            [990, 1000],
        )
        self.assertEqual(self.box.listener_uids(""), [])

    def test_a_line_without_an_owner_is_a_root_listener_and_so_a_stranger(self):
        line = "LISTEN 0 4096 127.0.0.1:61000 0.0.0.0:*"
        self.assertEqual(self.box.listener_uids(line + "\n"), [0])
        with self.assertRaisesRegex(self.box.YoError, "uid 0"):
            self.verify(line)

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

    def test_the_passphrase_is_not_sent_when_the_listener_is_a_strangers(self):
        self.host.tagged_domain("box", 61000)
        self.host.listeners = SS_LINE % 1000 + "\n"
        with mock.patch.object(
            self.box.subprocess, "Popen", side_effect=FileNotFoundError
        ) as popen:
            with self.assertRaises(self.box.YoError) as caught:
                self.box.run_initrd_helper(b"secret")
        popen.assert_not_called()
        self.assertIn("not sent there", caught.exception.message)

    def test_the_vm_accounts_listener_lets_the_helper_run(self):
        self.host.tagged_domain("box", 61000)
        self.host.listeners = SS_LINE % VM_UID + "\n"
        with mock.patch.object(
            self.box.subprocess, "Popen", side_effect=FileNotFoundError
        ) as popen:
            with self.assertRaises(self.box.YoError) as caught:
                self.box.run_initrd_helper(b"secret")
        popen.assert_called_once()
        self.assertIn("ssh not found", caught.exception.message)

    def test_lima_asks_no_process_and_refuses_only_a_port_lima_mirrors(self):
        mac = load_yo("yo_mac_listener")
        self.addCleanup(unload_yo, "yo_mac_listener")
        with mock.patch.object(mac.proc, "run") as run:
            mac.box.driver.verify_initrd_listener(60022)
            with self.assertRaisesRegex(mac.YoError, "ssh.localPort 22222"):
                mac.box.driver.verify_initrd_listener(22222)
        run.assert_not_called()


class TestStart(LibvirtCase):
    def start(self, memory_gib=None, disk_gib=None, cpus=None, size=None):
        """driver.start() of the flags as vm_up decides a creation size from them,
        or of `size` as it is."""
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
                self.box.libvirt, "loopback_port_bound", return_value=False
            ),
        ):
            if size is None:
                size = self.box.Size(memory_gib, disk_gib, cpus).for_creation()
            self.driver.start(b"pass", size)
        return unlock

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
        unlock.assert_called_once_with(b"pass")

    def memory(self):
        """The <memory> of the defined domain, as (unit, value)."""
        element = ET.fromstring(self.host.domains["yolobox-box"]).find("memory")
        return element.get("unit"), element.text

    def test_a_box_given_no_size_gets_the_default_memory_and_disk(self):
        self.start()
        self.assertEqual(self.memory(), ("GiB", str(self.box.LIBVIRT_MEMORY_GIB)))
        self.assertEqual(
            self.host.capacity["yolobox-box.qcow2"], self.box.LIBVIRT_DISK_GIB << 30
        )

    def test_a_box_is_created_at_the_size_asked_for(self):
        self.start(memory_gib=24, disk_gib=250)
        self.assertEqual(self.memory(), ("GiB", "24"))
        self.assertEqual(self.host.capacity["yolobox-box.qcow2"], 250 << 30)

    def test_memory_alone_leaves_the_disk_at_its_default(self):
        self.start(memory_gib=24)
        self.assertEqual(self.memory(), ("GiB", "24"))
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
                    unlock = self.start(size=self.box.NO_SIZE)
                unlock.assert_called_once_with(b"pass")
                self.assertEqual(self.host.calls_of("define"), [])
                self.assertNotIn("yolobox-box.qcow2", self.host.volumes)

    def test_creating_without_the_cpus_vm_up_decides_is_an_error(self):
        with self.assertRaisesRegex(self.box.YoError, "CPU count vm_up decides"):
            self.start(size=self.box.NO_SIZE)

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

    def test_a_passt_directory_the_vm_user_cannot_write_in_refuses_the_start(self):
        self.host.acl = getfacl_listing(mask="r-x")
        with self.assertRaisesRegex(self.box.YoError, "sudo setfacl -m u:yolobox-vm"):
            self.start()
        self.assertNotIn("start", self.host.subcommands())

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
        self.assertEqual(held, [True, True])
        self.assertFalse(self.lock_held())

    def test_every_virsh_call_names_the_system_connection(self):
        self.start()
        for call in self.host.calls:
            if call[0] == "virsh":
                self.assertEqual(call[1:3], ["-c", "qemu:///system"])

    def test_the_domain_forwards_the_port_ssh_config_names(self):
        self.start()
        self.assertEqual(self.driver.ssh_port(), FIRST_PORT)
        with open(self.box.ssh_cfg()) as handle:
            self.assertIn("  Port %d\n" % FIRST_PORT, handle.read())

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
            ["cidata", "domain.xml", "operator.pub", "ssh.config"],
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

    def test_a_second_start_changes_nothing_that_did_not_change(self):
        self.start()
        self.host.states["yolobox-box"] = "shut off"
        self.host.calls.clear()
        self.start()
        subs = self.host.subcommands()
        for untouched in (
            "define",
            "vol-create-as",
            "vol-upload",
            "vol-delete",
            "vol-resize",
        ):
            self.assertNotIn(untouched, subs)
        self.assertIn("start", subs)

    def test_a_changed_operator_name_rebuilds_the_cidata_while_stopped(self):
        self.start()
        self.host.states["yolobox-box"] = "shut off"
        self.host.calls.clear()
        with mock.patch.object(self.box.release, "host_user", return_value="bob"):
            self.start()
        subs = self.host.subcommands()
        self.assertLess(subs.index("vol-delete"), subs.index("vol-create-as"))
        self.assertNotIn("define", subs)
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
                self.start()
            self.assertIn("LIMA_CIDATA_USER=alice", lima_env.read_text())
            self.assertNotIn("yolobox-box-cidata.iso", self.host.volumes)
            self.host.fail.clear()
            self.host.calls.clear()
            self.start()
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
                self.start()
        self.assertEqual(
            sorted(os.listdir(self.box.instance_dir())),
            ["cidata", "domain.xml", "operator.pub", "ssh.config"],
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
        self.start()
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

    MIGRATING = ["yolobox-unlock: answered id=yolobox:luks-migrate"]

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
            return SSH_REFUSED

        self.host.sshd = probe

    def up(self):
        with self.assertRaises(self.box.YoError) as caught:
            self.box.vm_up(run_services=False, passphrase=b"secret")
        return caught.exception.message

    def test_a_domain_killed_after_the_unlock_is_an_error_naming_the_oom_killer(self):
        self.host.helper = lambda: (self.MIGRATING + ["unlocked"], 0, "")
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
        self.assertIn(
            "keeping its disk: yo -m box down && virsh -c qemu:///system undefine "
            "--nvram yolobox-box, then yo -m box up --memory N; then run this "
            "command again",
            message,
        )

    def test_a_domain_killed_while_the_migration_streamed_is_an_error_at_once(self):
        def dropped():
            self.host.states["yolobox-box"] = "shut off"
            return self.MIGRATING, 255, ""

        self.host.helper = dropped
        self.host.sshd = lambda argv: SSH_REFUSED
        message = self.up()
        self.assertIn(
            "box is shut off, not Running, while yo waited for its initrd to take "
            "the disk passphrase",
            message,
        )
        self.assertIn("`journalctl -k -g oom`", message)

    def test_a_domain_paused_under_yo_names_the_way_to_resume_it(self):
        self.host.helper = lambda: (["unlocked"], 0, "")
        self.killed_on_the_next_probe("paused")
        message = self.up()
        self.assertIn("box is paused, not Running", message)
        self.assertIn(
            "resume it with `virsh -c qemu:///system resume yolobox-box`, then run "
            "this command",
            message,
        )
        self.assertNotIn("oom", message)

    def test_a_domain_in_a_state_yo_has_no_word_for_says_where_to_look(self):
        self.host.helper = lambda: (["unlocked"], 0, "")
        self.killed_on_the_next_probe("crashed")
        message = self.up()
        self.assertIn("box is crashed, not Running", message)
        self.assertIn(
            "check why with `virsh -c qemu:///system domstate --reason yolobox-box`",
            message,
        )
        self.assertNotIn("oom", message)

    def test_a_domain_undefined_while_yo_waits_says_yo_up_defines_it_again(self):
        self.host.helper = lambda: (["unlocked"], 0, "")

        def undefined(argv):
            del self.host.domains["yolobox-box"], self.host.states["yolobox-box"]
            return SSH_REFUSED

        self.host.sshd = undefined
        message = self.up()
        self.assertIn("box is not listed, not Running", message)
        self.assertIn(
            "domain yolobox-box no longer exists: yo -m box up defines it again",
            message,
        )
        self.assertNotIn("domstate", message)

    def test_a_domain_paused_during_a_connected_probe_is_named_not_the_ssh_timeout(
        self,
    ):
        self.host.helper = lambda: (["unlocked"], 0, "")

        def paused(argv):
            self.host.states["yolobox-box"] = "paused"
            return 255, "Timeout, server 127.0.0.1 not responding."

        self.host.sshd = paused
        message = self.up()
        self.assertIn("box is paused, not Running", message)
        self.assertIn(
            "resume it with `virsh -c qemu:///system resume yolobox-box`", message
        )
        self.assertNotIn("as the operator failed", message)

    def test_an_ssh_failure_of_no_known_shape_after_the_unlock_is_surfaced(self):
        self.host.helper = lambda: (["unlocked"], 0, "")
        self.host.sshd = lambda argv: (255, "Bad configuration option: lizard")
        message = self.up()
        self.assertIn("ssh to box as the operator failed", message)
        self.assertIn("lizard", message)

    def test_a_login_whose_command_fails_after_the_unlock_is_surfaced(self):
        self.host.helper = lambda: (["unlocked"], 0, "")
        self.host.sshd = lambda argv: (1, "/run/current-system/sw/bin/true: lizard")
        message = self.up()
        self.assertIn("ssh to box as the operator failed", message)
        self.assertIn("true: lizard", message)

    def test_virsh_speaks_the_c_locale_whatever_the_users_locale(self):
        with mock.patch.dict(
            os.environ, {"LANG": "de_DE.UTF-8", "LC_ALL": "de_DE.UTF-8"}
        ):
            self.driver.vm_status()
        envs = self.host.virsh_envs
        self.assertTrue(envs)
        for env in envs:
            self.assertEqual(env["LC_ALL"], "C")
            self.assertEqual(env["LANG"], "de_DE.UTF-8")

    def test_a_domain_that_stays_up_is_waited_for_as_before(self):
        self.host.helper = lambda: (["unlocked"], 0, "")
        # Nothing listening yet, then the key refused while the operator's key
        # is being written: both are a boot in progress, retried.
        key_refused = (255, "operator@127.0.0.1: Permission denied (publickey).")
        answers = iter([SSH_REFUSED, key_refused, (0, "")])
        self.host.sshd = lambda argv: next(answers)
        with mock.patch.object(self.box.unlock.time, "sleep") as sleep:
            self.driver.start(b"secret", self.box.NO_SIZE)
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
        self.host.volumes["yolobox-box.qcow2"] = b""
        self.host.capacity["yolobox-box.qcow2"] = 4096
        self.ensure()
        self.assertIn("vol-delete", self.host.subcommands())
        self.assertEqual(self.host.capacity["yolobox-box.qcow2"], 100 << 30)

    def test_a_volume_of_the_smallest_box_size_is_adopted_as_it_is(self):
        self.host.volumes["yolobox-box.qcow2"] = b"a whole disk"
        self.host.capacity["yolobox-box.qcow2"] = 40 << 30
        self.ensure()
        self.assertEqual(
            [sub for sub in self.host.subcommands() if sub not in READS], []
        )
        self.assertEqual(self.host.volumes["yolobox-box.qcow2"], b"a whole disk")
        self.assertEqual(self.host.capacity["yolobox-box.qcow2"], 40 << 30)

    def test_a_volume_below_the_smallest_box_size_is_rebuilt(self):
        self.host.volumes["yolobox-box.qcow2"] = b"half a disk"
        self.host.capacity["yolobox-box.qcow2"] = 39 << 30
        self.ensure()
        self.assertIn("vol-delete", self.host.subcommands())
        self.assertEqual(self.host.capacity["yolobox-box.qcow2"], 100 << 30)


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
        self.assertEqual(
            box.box.driver.initrd_identity,
            ".local/state/yolobox/instances/e2e/operator.pub",
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
