"""The libvirt driver: what yo does on a Linux host, with virsh faked.

The tests drive `yo` through its public functions and `main()`, on a copy of
the module that holds a LibvirtDriver. `FakeHost` stands in for virsh and the
other host tools: it keeps domains, volumes and states, answers the way virsh
prints, and records every argv so a test can check the order of the calls.
"""

import base64
import contextlib
import fcntl
import hashlib
import io
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from test_yo import FakeHome, load_yo

REPO = Path(__file__).resolve().parent.parent
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


def load_libvirt_yo(instance="box"):
    """A copy of yo driving libvirt, as main() sets it up on a Linux host."""
    name = "yo_libvirt_%s" % instance.replace("-", "_")
    module = load_yo(name)
    module.HOST_PLATFORM = "linux"
    module.set_driver(module.driver_for("linux"))
    module.set_instance(instance)
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
        self.master_output = ""
        self.listeners = ""  # what `ss -ltnHe` prints

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
            Path(argv[argv.index("-o") + 1]).write_bytes(b"ISO" * 100)
            result = completed()
        elif argv[0] == "ssh" and "check" in argv:
            result = completed(stderr=self.master_output)
        elif argv[0] == "ss":
            result = completed(self.listeners)
        else:
            result = completed()
        if kwargs.get("check") and result.returncode != 0:
            raise self.box.YoError(
                "%s failed (exit %d): %s"
                % (" ".join(argv), result.returncode, result.stderr)
            )
        return result

    def virsh(self, sub, args):
        if sub in self.fail:
            return completed(returncode=1, stderr=self.fail[sub])
        if sub == "list":
            return completed("\n".join(self.domains) + "\n\n")
        if sub == "dumpxml":
            return completed(self.domains[args[0]])
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
                "Capacity:       107374182400 bytes\n"
                "Allocation:     2147483648 bytes\n"
            )
        return completed()

    def tagged_domain(self, name, port, address="127.0.0.1"):
        xml = self.box.domain_xml(name, port, VM_UID, VM_GID)
        self.domains["yolobox-" + name] = xml.replace(
            "address='127.0.0.1'", "address='%s'" % address
        )
        self.states["yolobox-" + name] = "running"


class LibvirtCase(FakeHome):
    instance = "box"

    def setUp(self):
        super().setUp()
        self.box, name = load_libvirt_yo(self.instance)
        self.addCleanup(sys.modules.pop, name)
        self.host = FakeHost(self.box)
        for patcher in (
            mock.patch.object(self.box, "run", self.host.run),
            mock.patch.object(
                self.box.pwd,
                "getpwnam",
                return_value=mock.Mock(pw_uid=VM_UID, pw_gid=VM_GID),
            ),
            mock.patch.object(self.box, "host_user", return_value=OPERATOR),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.range_file = Path(self.home) / "port_range"
        self.range_file.write_text(EPHEMERAL_RANGE)
        patcher = mock.patch.object(self.box, "PORT_RANGE_FILE", str(self.range_file))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.driver = self.box.DRIVER

    def lock_held(self):
        """Whether another process would have to wait for the port lock."""
        path = os.path.join(
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
    def render(self, name="box", port=FIRST_PORT):
        return self.box.domain_xml(name, port, VM_UID, VM_GID)

    def devices(self, xml):
        return [child.tag for child in ET.fromstring(xml).find("devices")]

    def gaps(self, info, instance="box"):
        return self.box.domain_gaps(info, instance, VM_UID, VM_GID)

    def test_no_device_that_links_the_guest_to_the_host(self):
        devices = self.devices(self.render())
        for forbidden in self.box.LIBVIRT_FORBIDDEN_DEVICES:
            self.assertNotIn(forbidden, devices)

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
            "device:graphics": base.replace("<video>", "<graphics type='vnc'/><video>"),
            "device:channel": base.replace("<video>", "<channel type='pty'/><video>"),
            "device:hostdev": base.replace("<video>", "<hostdev/><video>"),
            "seclabel.selinux": base.replace(
                "<seclabel type='dynamic' model='selinux' relabel='yes'/>", ""
            ),
            "seclabel.dac": base.replace(
                "type='static' model='dac'", "type='dynamic' model='dac'"
            ),
            "metadata": base.replace("<yolobox:instance", "<yolobox:other").replace(
                "</yolobox:instance>", "</yolobox:other>"
            ),
            "portForward.address": base.replace(" address='127.0.0.1'>", ">"),
        }
        for gap, xml in broken.items():
            with self.subTest(gap=gap):
                self.assertEqual(self.gaps(self.box.parse_domain(xml)), [gap])

    def test_a_dac_label_that_is_not_the_vm_account_is_a_gap(self):
        base = self.render()
        for label in ("+0:+0", "+990:+0", "+0:+989", "990:989", ""):
            with self.subTest(label=label):
                xml = base.replace(
                    "<label>+%d:+%d</label>" % (VM_UID, VM_GID),
                    "<label>%s</label>" % label,
                )
                self.assertEqual(
                    self.gaps(self.box.parse_domain(xml)), ["seclabel.dac"]
                )

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
            self.box.domain_gaps(info, "box", VM_UID + 1, VM_GID), ["seclabel.dac"]
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
        info = self.box.parse_domain(self.render(name="other"))
        self.assertEqual(self.gaps(info), ["metadata"])

    def test_unparseable_xml_is_an_error_not_an_empty_domain(self):
        with self.assertRaises(self.box.YoError):
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
                with self.assertRaises(self.box.YoError):
                    self.box.host_port_window(text)

    def test_the_window_is_read_from_the_kernels_file(self):
        self.range_file.write_text("1024 49151\n")
        self.assertEqual(self.box.read_port_window().start, 49152)

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
            with self.assertRaises(self.box.YoError):
                self.box.loopback_port_bound(80)

    def test_the_driver_skips_ports_other_boxes_forward_but_not_other_domains(self):
        self.host.tagged_domain("first", FIRST_PORT)
        self.host.tagged_domain("second", FIRST_PORT + 1)
        untagged = self.box.domain_xml("x", FIRST_PORT + 2, VM_UID, VM_GID)
        self.host.domains["plain"] = re.sub(
            r"<metadata>.*</metadata>", "", untagged, flags=re.S
        )
        with mock.patch.object(self.box, "loopback_port_bound", return_value=False):
            self.assertEqual(self.driver.allocate_host_port(), FIRST_PORT + 2)

    FOREIGN = (
        "<domain><name>foreign</name><devices><interface>"
        "<portForward><range start='not-a-port' to='22'/></portForward>"
        "</interface></devices></domain>"
    )

    def test_a_foreign_domain_is_never_read_strictly(self):
        self.host.domains["foreign"] = self.FOREIGN
        self.host.tagged_domain("first", FIRST_PORT)
        with mock.patch.object(self.box, "loopback_port_bound", return_value=False):
            self.assertEqual(self.driver.allocate_host_port(), FIRST_PORT + 1)

    def test_the_same_xml_in_a_yolobox_domain_is_an_error_not_a_skip(self):
        self.host.domains["ours"] = self.FOREIGN.replace(
            "<devices>",
            "<metadata><yolobox:instance xmlns:yolobox='%s'>x</yolobox:instance>"
            "</metadata><devices>" % self.box.DOMAIN_NS,
        )
        with self.assertRaises(self.box.YoError) as caught:
            self.driver.allocate_host_port()
        self.assertIn("no numeric start", caught.exception.message)

    def test_the_driver_skips_a_port_bound_on_the_host(self):
        with mock.patch.object(
            self.box, "loopback_port_bound", side_effect=lambda port: port == FIRST_PORT
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
            mock.patch.object(self.box.DRIVER, "require_tool"),
            mock.patch.object(self.box, "ssh_run"),
            mock.patch.object(self.box, "loopback_port_bound", return_value=False),
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
            mock.patch.object(self.box, "run", spy),
            mock.patch.object(
                self.box,
                "loopback_port_bound",
                side_effect=lambda port: held.append(self.lock_held()) or False,
            ),
            mock.patch.object(self.box, "ssh_run"),
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
        with self.assertRaises(self.box.YoError):
            self.run_main("port", "80")

    def test_on_a_mac_it_exits_64_saying_lima_mirrors_ports(self):
        box = load_yo("yo_mac_port")
        self.addCleanup(sys.modules.pop, "yo_mac_port")
        with (
            mock.patch.object(box.DRIVER, "require_tool"),
            self.assertRaises(box.YoError) as caught,
        ):
            box.main(["port", "5173"])
        self.assertEqual(caught.exception.code, 64)
        self.assertIn(
            "mirrors the guest's TCP ports 1024-49151", caught.exception.message
        )


class TestOperatorKey(LibvirtCase):
    def agent(self, identities):
        sock = Path(self.driver.op_sock())
        sock.parent.mkdir(parents=True)
        sock.touch()
        return mock.patch.object(
            self.box, "ssh_agent_identities", return_value=identities
        )

    def test_the_one_key_with_the_title_is_chosen(self):
        with self.agent([(OTHER_BLOB, "yolobox-disk"), (KEY_BLOB, "yolobox-operator")]):
            self.assertEqual(self.driver.operator_key(), KEY_BLOB)

    def test_no_key_with_the_title_is_an_error_naming_it(self):
        with self.agent([(OTHER_BLOB, "yolobox-disk")]):
            with self.assertRaises(self.box.YoError) as caught:
                self.driver.operator_key()
        self.assertIn("no key titled yolobox-operator", caught.exception.message)

    def test_two_keys_with_the_title_are_an_error_not_a_guess(self):
        with self.agent(
            [(KEY_BLOB, "yolobox-operator"), (OTHER_BLOB, "yolobox-operator")]
        ):
            with self.assertRaises(self.box.YoError) as caught:
                self.driver.operator_key()
        self.assertIn("offers 2 keys titled yolobox-operator", caught.exception.message)

    def test_a_missing_agent_socket_names_it(self):
        with self.assertRaises(self.box.YoError) as caught:
            self.driver.operator_key()
        self.assertIn(".1password/agent.sock", caught.exception.message)

    def test_the_public_key_line_carries_the_key_and_the_title_only(self):
        self.assertEqual(self.box.public_key_line(KEY_BLOB), PUBLIC_KEY)


def gawk_argv():
    if shutil.which("gawk"):
        return ["gawk"]
    if shutil.which("nix"):
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
        with self.assertRaises(self.box.YoError):
            self.files("")

    def test_the_agent_account_name_is_refused_for_the_operator(self):
        with self.assertRaises(self.box.YoError) as caught:
            self.files("agent")
        self.assertIn("agent account", caught.exception.message)

    def test_names_the_guest_cannot_create_are_refused(self):
        for name in ("Alice", "a b", "a\nb", "1alice", "a;b", "-a"):
            with self.subTest(name=name):
                with self.assertRaises(self.box.YoError):
                    self.files(name)


class TestSshConfig(LibvirtCase):
    def config(self):
        return self.box.libvirt_ssh_config(
            "lima-box", 61234, OPERATOR, "/h/.1password/agent.sock", "/h/state/box"
        )

    def test_the_block_is_the_shape_yos_parser_reads(self):
        values = self.box.parse_lima_ssh_block(self.config(), "lima-box")
        self.assertEqual(values["Hostname"], "127.0.0.1")
        self.assertEqual(values["Port"], "61234")
        self.assertEqual(values["IdentityFile"], "/h/state/box/operator.pub")
        self.assertEqual(values["IdentityAgent"], "/h/.1password/agent.sock")

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
        values = self.box.parse_lima_ssh_block(self.config(), "lima-box")
        alias = self.box.herdr_alias_block(values)
        self.assertIn('IdentityAgent "/h/.1password/agent.sock"', alias)

    def test_a_block_without_an_agent_gives_an_alias_without_one(self):
        text = 'Host lima-box\n  Hostname 127.0.0.1\n  Port 1\n  IdentityFile "/k"\n'
        values = self.box.parse_lima_ssh_block(text, "lima-box")
        self.assertNotIn("IdentityAgent", self.box.herdr_alias_block(values))

    def test_the_file_is_rewritten_only_when_the_block_changed(self):
        os.makedirs(self.box.lima_dir())
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
        os.makedirs(self.box.lima_dir())
        self.driver.write_ssh_config(61234)
        argv = self.box.initrd_ssh_argv("true")
        self.assertIn("IdentityAgent=%s" % self.driver.op_sock(), argv)
        self.assertEqual(
            argv[argv.index("-i") + 1],
            os.path.join(self.box.lima_dir(), "operator.pub"),
        )
        self.assertIn("IdentitiesOnly=yes", argv)

    def test_the_initrd_probe_before_the_config_exists_names_the_public_key(self):
        with mock.patch.object(self.driver, "ssh_port", return_value=61234):
            argv = self.box.initrd_ssh_argv("true")
        self.assertTrue(argv[argv.index("-i") + 1].endswith("/box/operator.pub"))

    def test_lima_keeps_its_private_key_and_no_agent(self):
        mac = load_yo("yo_mac_initrd")
        self.addCleanup(sys.modules.pop, "yo_mac_initrd")
        with mock.patch.object(mac.DRIVER, "ssh_port", return_value=60022):
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
            if argv[3:] == ["version", "--daemon"]:
                return completed(DAEMON_VERSION_TEXT % version)
            if argv[0] == "systemctl":
                return completed(unit, returncode=0 if unit == "active\n" else 3)
            return completed("qemu:///system\n", returncode=uri, stderr="boom")

        account = mock.Mock(pw_uid=VM_UID, pw_gid=VM_GID) if user else None
        with (
            mock.patch.object(self.box.shutil, "which", return_value=which),
            mock.patch.object(self.box, "run", run),
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
        with self.assertRaises(self.box.YoError):
            self.check(version="11.99.99")

    def test_a_version_that_is_not_a_version_is_an_error(self):
        with self.assertRaises(self.box.YoError):
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
                with self.assertRaises(self.box.YoError):
                    self.box.parse_daemon_version(text)

    def test_the_installed_script_path_is_the_one_next_to_yo(self):
        message = self.assertRefused("virsh not found", which=None)
        self.assertIn(str(self.box.SCRIPT_DIR / "linux" / "host-setup.sh"), message)


class TestStatusAndGaps(LibvirtCase):
    def test_a_domain_that_is_not_defined_has_no_status_and_no_gaps(self):
        self.assertIsNone(self.driver.vm_status())
        self.assertEqual(self.driver.config_gaps(), [])

    def test_running_is_running_and_other_states_keep_their_words(self):
        self.host.tagged_domain("box", FIRST_PORT)
        self.assertEqual(self.driver.vm_status(), "Running")
        self.host.states["yolobox-box"] = "shut off"
        self.assertEqual(self.driver.vm_status(), "shut off")

    def test_another_instances_domain_is_not_this_one(self):
        self.host.tagged_domain("other", FIRST_PORT)
        self.assertIsNone(self.driver.vm_status())

    def test_a_live_domain_with_a_device_the_box_never_has_is_a_gap(self):
        self.host.tagged_domain("box", FIRST_PORT)
        self.host.domains["yolobox-box"] = self.host.domains["yolobox-box"].replace(
            "<video>", "<tpm model='tpm-crb'/><video>"
        )
        self.assertEqual(self.driver.config_gaps(), ["device:tpm"])
        self.assertIn(
            "undefine --nvram yolobox-box",
            self.driver.config_gap_message(["device:tpm"]),
        )

    def test_any_valid_name_is_created(self):
        self.assertIsNone(self.driver.creation_refusal())

    def test_the_disk_image_is_the_pool_volumes_allocation(self):
        self.assertIsNone(self.driver.disk_image())
        self.host.volumes["yolobox-box.qcow2"] = b""
        label, kib = self.driver.disk_image()
        self.assertIn("yolobox-box.qcow2", label)
        self.assertEqual(kib, 2 * 1024 * 1024)

    def test_growing_the_disk_is_refused_naming_the_todo(self):
        for call in (
            self.driver.disk_ceiling_gib,
            lambda: self.driver.grow_disk(100, 200),
        ):
            with self.assertRaises(self.box.YoError) as caught:
                call()
            self.assertIn("TODO.md", caught.exception.message)

    def test_the_nix_commands_name_the_linux_host(self):
        self.assertIn("YOLOBOX_HOST_OS=linux", self.box.nix_env("alice"))


class TestKeepalive(LibvirtCase):
    def pid(self, output):
        self.host.master_output = output
        with mock.patch.object(self.box, "ssh_run"):
            return self.driver.keepalive_pid()

    def test_the_pid_is_the_masters(self):
        self.assertEqual(self.pid("Master running (pid=4242)\r\n"), 4242)

    def test_no_master_means_no_pid(self):
        self.assertIsNone(self.pid("Control socket connect(/x): No such file\n"))

    def test_a_pid_that_would_hit_init_is_not_a_pid(self):
        self.assertIsNone(self.pid("Master running (pid=1)\n"))


class FakeWatcher:
    """A `virsh event` process: it ends when its test says an event arrived."""

    def __init__(self, host, events, returncode=0, hangs=False):
        self.host = host
        self.returncode = returncode
        self.stderr = "lost connection" if returncode else ""
        self.hangs = hangs
        self.events = events
        self.terminated = False
        self.ended = False

    def communicate(self, timeout=None):
        if self.hangs and not self.terminated:
            raise subprocess.TimeoutExpired("virsh event", timeout)
        assert not self.ended, "waited on a watcher that had already ended"
        self.ended = True
        self.host.on_event(self.events)
        return "", self.stderr

    def poll(self):
        return None if self.hangs and not self.terminated else self.returncode

    def terminate(self):
        self.terminated = True


class TestStop(LibvirtCase):
    def arm(self, watchers):
        """Spawn the given watchers in turn; `shutdown` ends the guest when told."""
        self.order = []
        queue = list(watchers)

        def spawn(*args):
            self.order.append("watch")
            return queue.pop(0)

        original = self.host.virsh

        def virsh(sub, args):
            if sub == "shutdown":
                self.order.append("shutdown")
                return completed()
            return original(sub, args)

        self.host.virsh = virsh
        self.host.on_event = lambda events: self.host.states.__setitem__(
            "yolobox-box", events
        )
        return mock.patch.object(self.box, "spawn_virsh", spawn)

    def test_the_watcher_is_armed_before_the_shutdown_is_asked_for(self):
        self.host.tagged_domain("box", FIRST_PORT)
        with self.arm([FakeWatcher(self.host, "shut off")]):
            self.driver.stop()
        self.assertEqual(self.order, ["watch", "shutdown"])
        self.assertEqual(self.driver.vm_status(), "shut off")

    def test_an_event_that_is_not_the_stop_is_waited_past(self):
        self.host.tagged_domain("box", FIRST_PORT)
        watchers = [
            FakeWatcher(self.host, "running"),
            FakeWatcher(self.host, "shut off"),
        ]
        with self.arm(watchers):
            self.driver.stop()
        self.assertEqual(self.order, ["watch", "shutdown", "watch"])

    def test_a_guest_that_never_stops_is_an_error_with_the_hard_way_out(self):
        self.host.tagged_domain("box", FIRST_PORT)
        with self.arm([FakeWatcher(self.host, "", hangs=True)]):
            with self.assertRaises(self.box.YoError) as caught:
                self.driver.stop()
        self.assertIn("destroy yolobox-box", caught.exception.message)

    def test_a_failing_watcher_is_surfaced(self):
        self.host.tagged_domain("box", FIRST_PORT)
        with self.arm([FakeWatcher(self.host, "running", returncode=1)]):
            with self.assertRaises(self.box.YoError) as caught:
                self.driver.stop()
        self.assertIn("lost connection", caught.exception.message)

    def test_a_guest_still_in_shutdown_is_waited_for_not_taken_for_stopped(self):
        self.host.tagged_domain("box", FIRST_PORT)
        watchers = [
            FakeWatcher(self.host, "in shutdown"),
            FakeWatcher(self.host, "shut off"),
        ]
        with self.arm(watchers):
            self.driver.stop()
        self.assertEqual(self.order, ["watch", "shutdown", "watch"])
        self.assertEqual(self.driver.vm_status(), "shut off")

    def test_a_guest_already_in_shutdown_is_waited_for_without_a_second_request(self):
        self.host.tagged_domain("box", FIRST_PORT)
        self.host.states["yolobox-box"] = "in shutdown"
        with self.arm([FakeWatcher(self.host, "shut off")]):
            self.driver.stop()
        self.assertEqual(self.order, ["watch"])

    def test_a_guest_that_pauses_while_stopping_is_an_error_not_a_stop(self):
        for state in ("paused", "pmsuspended"):
            with self.subTest(state=state):
                self.host.tagged_domain("box", FIRST_PORT)
                with self.arm([FakeWatcher(self.host, state)]):
                    with self.assertRaises(self.box.YoError) as caught:
                        self.driver.stop()
                self.assertIn(state, caught.exception.message)
                self.assertIn("destroy yolobox-box", caught.exception.message)

    def test_a_guest_that_is_paused_or_in_a_state_yo_does_not_know_is_not_asked_to_stop(
        self,
    ):
        for state in ("paused", "pmsuspended", "crashed", "lizard"):
            with self.subTest(state=state):
                self.host.tagged_domain("box", FIRST_PORT)
                self.host.states["yolobox-box"] = state
                with self.arm([]):
                    with self.assertRaises(self.box.YoError):
                        self.driver.stop()
                self.assertEqual(self.order, [])

    def test_a_stopped_domain_needs_nothing(self):
        self.host.tagged_domain("box", FIRST_PORT)
        self.host.states["yolobox-box"] = "shut off"
        with self.arm([]):
            self.driver.stop()
        self.assertEqual(self.order, [])

    def test_a_domain_that_does_not_exist_is_an_error(self):
        with self.assertRaises(self.box.YoError):
            self.driver.stop()


class TestVmUpWithAGap(LibvirtCase):
    def test_a_domain_that_is_not_what_the_template_made_is_never_started(self):
        self.host.tagged_domain("box", FIRST_PORT)
        self.host.states["yolobox-box"] = "shut off"
        self.host.domains["yolobox-box"] = self.host.domains["yolobox-box"].replace(
            "<video>", "<tpm model='tpm-crb'/><video>"
        )
        with mock.patch.object(self.box, "luks_passphrase") as passphrase:
            with self.assertRaises(self.box.YoError) as caught:
                self.box.vm_up()
        self.assertIn("device:tpm", caught.exception.message)
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

    def test_only_the_libvirt_driver_stops_on_a_gap(self):
        self.assertTrue(self.driver.gaps_are_fatal)
        self.assertFalse(load_yo("yo_mac_gap").LIMA.gaps_are_fatal)
        self.addCleanup(sys.modules.pop, "yo_mac_gap")


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

    def test_a_line_without_an_owner_is_an_error_not_a_pass(self):
        with self.assertRaises(self.box.YoError):
            self.box.listener_uids("LISTEN 0 4096 127.0.0.1:61000 0.0.0.0:*\n")

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
        with self.assertRaises(self.box.YoError):
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

    def test_lima_checks_nothing(self):
        mac = load_yo("yo_mac_listener")
        self.addCleanup(sys.modules.pop, "yo_mac_listener")
        with mock.patch.object(mac, "run") as run:
            mac.DRIVER.verify_initrd_listener(60022)
        run.assert_not_called()


class TestStart(LibvirtCase):
    def start(self):
        seed = Path(self.home) / "seed.qcow2"
        seed.write_bytes(b"Q" * 4096)
        with (
            mock.patch.object(self.box, "ensure_seed", return_value=str(seed)),
            mock.patch.object(self.driver, "operator_key", return_value=KEY_BLOB),
            mock.patch.object(self.box, "unlock_disk") as unlock,
            mock.patch.object(self.box, "wait_for_operator"),
            mock.patch.object(self.box, "loopback_port_bound", return_value=False),
        ):
            self.driver.start(b"pass")
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

    def test_the_iso_is_built_from_the_staged_files_under_the_cidata_label(self):
        self.start()
        argv = [call for call in self.host.calls if call[0] == "xorriso"]
        self.assertEqual(
            argv[0][:7], ["xorriso", "-as", "mkisofs", "-V", "cidata", "-J", "-R"]
        )
        self.assertEqual(argv[0][-1], os.path.join(self.box.lima_dir(), "cidata"))
        self.assertFalse(
            os.path.exists(os.path.join(self.box.lima_dir(), "cidata.iso"))
        )
        staged = Path(self.box.lima_dir()) / "cidata"
        self.assertEqual(
            sorted(path.name for path in staged.iterdir()),
            sorted(self.box.CIDATA_FILES),
        )

    def test_only_the_public_key_is_written(self):
        self.start()
        self.assertEqual(
            (Path(self.box.lima_dir()) / "operator.pub").read_text(), PUBLIC_KEY + "\n"
        )
        for path in Path(self.box.lima_dir()).rglob("*"):
            if path.is_file():
                self.assertNotIn(
                    base64.b64encode(OTHER_BLOB).decode(), path.read_text()
                )

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
        with mock.patch.object(self.box, "host_user", return_value="bob"):
            self.start()
        subs = self.host.subcommands()
        self.assertLess(subs.index("vol-delete"), subs.index("vol-create-as"))
        self.assertNotIn("define", subs)
        self.assertIn(
            "LIMA_CIDATA_USER=bob",
            (Path(self.box.lima_dir()) / "cidata" / "lima.env").read_text(),
        )

    def test_a_failed_upload_leaves_no_half_filled_volume(self):
        self.host.fail["vol-upload"] = "no space"
        with self.assertRaises(self.box.YoError) as caught:
            self.start()
        self.assertIn("no space", caught.exception.message)
        self.assertEqual(self.host.volumes, {})
        self.assertNotIn("define", self.host.subcommands())

    def test_an_operator_named_agent_stops_before_anything_is_created(self):
        with mock.patch.object(self.box, "host_user", return_value="agent"):
            with self.assertRaises(self.box.YoError):
                self.start()
        self.assertEqual(self.host.volumes, {})
        self.assertEqual(self.host.domains, {})

    def test_an_empty_operator_name_stops_before_anything_is_created(self):
        with mock.patch.object(self.box, "host_user", return_value=""):
            with self.assertRaises(self.box.YoError):
                self.start()
        self.assertEqual(self.host.volumes, {})


class FakeReply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class TestSeed(LibvirtCase):
    def seed(self, payload, pinned=None):
        digest = pinned or hashlib.sha512(payload).hexdigest()
        return (
            mock.patch.object(self.box, "SEED_SHA512", digest),
            mock.patch.object(
                self.box.urllib.request, "urlopen", return_value=FakeReply(payload)
            ),
        )

    def cached(self):
        return os.path.join(self.home, ".cache", "yolobox", self.box.SEED_NAME)

    def test_a_verified_download_is_cached(self):
        pin, opener = self.seed(b"image")
        with pin, opener as urlopen, mock.patch.object(self.box, "err"):
            path = self.box.ensure_seed()
        self.assertEqual(path, self.cached())
        self.assertEqual(Path(path).read_bytes(), b"image")
        urlopen.assert_called_once()

    def test_a_cached_seed_that_verifies_is_not_downloaded_again(self):
        pin, opener = self.seed(b"image")
        with pin, opener as urlopen, mock.patch.object(self.box, "err"):
            self.box.ensure_seed()
            self.box.ensure_seed()
        urlopen.assert_called_once()

    def test_a_download_with_the_wrong_hash_is_refused_and_not_kept(self):
        pin, opener = self.seed(b"tampered", pinned="0" * 128)
        with pin, opener, mock.patch.object(self.box, "err"):
            with self.assertRaises(self.box.YoError) as caught:
                self.box.ensure_seed()
        self.assertIn("not the pinned", caught.exception.message)
        self.assertEqual(os.listdir(os.path.dirname(self.cached())), [])

    def test_a_cached_seed_that_no_longer_verifies_is_replaced(self):
        os.makedirs(os.path.dirname(self.cached()))
        Path(self.cached()).write_bytes(b"corrupt")
        pin, opener = self.seed(b"image")
        with pin, opener as urlopen, mock.patch.object(self.box, "err"):
            self.box.ensure_seed()
        self.assertEqual(Path(self.cached()).read_bytes(), b"image")
        urlopen.assert_called_once()

    def test_a_network_failure_is_an_error_naming_the_url(self):
        failure = self.box.urllib.error.URLError("no route")
        with (
            mock.patch.object(self.box.urllib.request, "urlopen", side_effect=failure),
            mock.patch.object(self.box, "err"),
            self.assertRaises(self.box.YoError) as caught,
        ):
            self.box.ensure_seed()
        self.assertIn(self.box.SEED_URL, caught.exception.message)


class TestDriverChoice(unittest.TestCase):
    def test_linux_gets_the_libvirt_driver_and_names_itself_in_nix_commands(self):
        box, name = load_libvirt_yo()
        self.addCleanup(sys.modules.pop, name)
        self.assertEqual(box.driver_for("linux").host_os, "linux")
        self.assertIsInstance(box.DRIVER, box.LibvirtDriver)

    def test_the_instance_dir_is_under_the_state_dir(self):
        box, name = load_libvirt_yo("e2e")
        self.addCleanup(sys.modules.pop, name)
        self.assertTrue(box.lima_dir().endswith(".local/state/yolobox/instances/e2e"))
        self.assertEqual(
            box.DRIVER.initrd_identity,
            ".local/state/yolobox/instances/e2e/operator.pub",
        )
        self.assertIn(
            "~/.local/state/yolobox/instances/e2e/ssh.config", box.INCLUDE_LINE
        )

    def test_an_unknown_platform_is_refused(self):
        box, name = load_libvirt_yo()
        self.addCleanup(sys.modules.pop, name)
        with self.assertRaises(box.YoError):
            box.driver_for("win32")


if __name__ == "__main__":
    unittest.main()
