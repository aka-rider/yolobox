"""YOLOBOX_INSTANCE: driving a second lima instance with the same `yo`."""

import contextlib
import json
import os
import sys
import unittest
from unittest import mock

from test_yo import FakeHome, load_yo, yo

INSTANCE_B = "second"


def limactl_json(local_port=None, **config):
    ssh = {} if local_port is None else {"localPort": local_port}
    return json.dumps({"status": "Stopped", "config": {"ssh": ssh, **config}})


def fake_limactl(stdout, returncode=0):
    return mock.patch.object(
        yo, "run", return_value=mock.Mock(returncode=returncode, stdout=stdout)
    )


def as_instance(name):
    return mock.patch.object(yo, "INSTANCE", name)


class TestNamesFollowTheEnvironment(unittest.TestCase):
    def test_unset_is_the_box_itself(self):
        self.assertEqual(yo.INSTANCE, "yolobox")
        self.assertEqual(yo.VM_HOST, "lima-yolobox")
        self.assertEqual(yo.HERDR_SSH_ALIAS, "yolobox")
        self.assertEqual(yo.INCLUDE_LINE, "Include ~/.lima/yolobox/ssh.config")
        self.assertEqual(yo.SSH_LOCAL_PORT, 60022)
        self.assertIn("Match host lima-yolobox user agent", yo.EDITOR_SSH_BLOCK)

    def test_another_instance_gets_its_own_names(self):
        name = "yo_%s" % INSTANCE_B
        try:
            other = load_yo(name, instance=INSTANCE_B)
        finally:
            sys.modules.pop(name)
        self.assertEqual(other.INSTANCE, INSTANCE_B)
        self.assertEqual(other.VM_HOST, "lima-second")
        self.assertEqual(other.HERDR_SSH_ALIAS, INSTANCE_B)
        self.assertEqual(other.INCLUDE_LINE, "Include ~/.lima/second/ssh.config")
        self.assertTrue(other.lima_dir().endswith(".lima/second"))
        self.assertIn("Match host lima-second user agent", other.EDITOR_SSH_BLOCK)
        self.assertIn("~/.lima/second/ssh-agent.sock", other.EDITOR_SSH_BLOCK)

    def test_a_name_lima_would_refuse_is_refused_before_anything_runs(self):
        for bad in (
            "",
            "has space",
            "../escape",
            "-lead",
            "trail-",
            "a--b",
            "_config",
            "lima-second",
        ):
            with self.subTest(name=bad):
                with self.assertRaises(SystemExit) as caught:
                    yo.instance_from_env(bad)
                self.assertIn("YOLOBOX_INSTANCE", str(caught.exception))


class TestSeveralInstancesCoexist(FakeHome):
    """The default instance plus two named ones, side by side on one host."""

    NAMES = (None, "second", "scratch")
    PORTS = {"second": 60023, "scratch": 60024}

    def setUp(self):
        super().setUp()
        self.boxes = []
        for index, instance in enumerate(self.NAMES):
            name = "yo_box%d" % index
            self.boxes.append(load_yo(name, instance=instance))
            self.addCleanup(sys.modules.pop, name)
        for box in self.boxes:
            self.write_lima_ssh_config(box)

    def port_of(self, box):
        return self.PORTS.get(box.INSTANCE, box.SSH_LOCAL_PORT)

    def write_lima_ssh_config(self, box):
        os.makedirs(box.lima_dir())
        with open(box.ssh_cfg(), "w") as handle:
            handle.write(
                "Host %s\n  Hostname 127.0.0.1\n  Port %d\n"
                '  IdentityFile "/k/user"\n' % (box.VM_HOST, self.port_of(box))
            )

    def read_ssh_config(self, box):
        with open(box.ssh_cfg()) as handle:
            return handle.read()

    def test_every_derived_name_is_distinct(self):
        for derive in (
            lambda box: box.INSTANCE,
            lambda box: box.VM_HOST,
            lambda box: box.HERDR_SSH_ALIAS,
            lambda box: box.lima_dir(),
            lambda box: box.known_hosts_path(),
            lambda box: box.agent_control_path(),
            lambda box: box.INCLUDE_LINE,
            lambda box: box.EDITOR_SSH_BLOCK,
        ):
            values = [derive(box) for box in self.boxes]
            self.assertEqual(len(set(values)), len(self.boxes), values)

    def test_every_instance_reaches_its_own_port(self):
        ports = []
        for box in self.boxes:
            with mock.patch.object(
                box,
                "run",
                return_value=mock.Mock(
                    returncode=0, stdout=limactl_json(self.port_of(box))
                ),
            ):
                ports.append(box.ssh_port())
        self.assertEqual(ports, [60022, 60023, 60024])

    def test_each_ssh_config_holds_only_its_own_blocks(self):
        for box in self.boxes:
            box.ensure_agent_ssh_config()
        for box in self.boxes:
            text = self.read_ssh_config(box)
            self.assertIn(box.EDITOR_SSH_BLOCK, text)
            self.assertIn("Host %s\n  User agent" % box.HERDR_SSH_ALIAS, text)
            self.assertIn("Port %d" % self.port_of(box), text)
            for other in self.boxes:
                if other is not box:
                    self.assertNotIn(other.VM_HOST, text)
                    self.assertNotIn(".lima/%s/" % other.INSTANCE, text)

    def test_rerunning_one_changes_no_file_of_any_instance(self):
        for box in self.boxes:
            box.ensure_agent_ssh_config()
        before = [self.read_ssh_config(box) for box in self.boxes]
        for box in reversed(self.boxes):
            box.ensure_agent_ssh_config()
        self.assertEqual([self.read_ssh_config(box) for box in self.boxes], before)

    def test_each_include_line_is_found_only_by_its_own_instance(self):
        ssh_dir = os.path.join(self.home, ".ssh")
        os.mkdir(ssh_dir)
        for box in self.boxes[:2]:
            box.add_ssh_config_include()
        found = [box.ssh_config_include_line() is not None for box in self.boxes]
        self.assertEqual(found, [True, True, False])
        with open(os.path.join(ssh_dir, "config")) as handle:
            self.assertEqual(len(handle.read().splitlines()), 2)


class TestSshPort(unittest.TestCase):
    def test_the_default_instance_is_the_constant_and_asks_nobody(self):
        with fake_limactl("") as fake_run:
            self.assertEqual(yo.ssh_port(), yo.SSH_LOCAL_PORT)
        fake_run.assert_not_called()

    def test_another_instance_reports_the_port_it_was_created_with(self):
        with as_instance(INSTANCE_B), fake_limactl(limactl_json(60023)) as fake_run:
            self.assertEqual(yo.ssh_port(), 60023)
        self.assertEqual(
            fake_run.call_args.args[0], ["limactl", "list", INSTANCE_B, "--json"]
        )

    def test_a_port_that_is_not_fixed_is_an_error_naming_the_remedy(self):
        unfixed = {
            "0, lima picks per start": limactl_json(0),
            "missing": limactl_json(),
            "a string": limactl_json("60023"),
            "a boolean": limactl_json(True),
            "negative": limactl_json(-1),
            "past the last port": limactl_json(65536),
            "null": json.dumps({"config": {"ssh": {"localPort": None}}}),
        }
        for label, stdout in unfixed.items():
            with self.subTest(localPort=label):
                with as_instance(INSTANCE_B), fake_limactl(stdout):
                    with self.assertRaises(yo.YoError) as caught:
                        yo.ssh_port()
                self.assertIn(
                    "limactl create --name second --set '.ssh.localPort=",
                    caught.exception.message,
                )

    def test_an_instance_limactl_cannot_show_is_an_error(self):
        for stdout, returncode in (("", 1), ("not json", 0)):
            with self.subTest(stdout=stdout):
                with as_instance(INSTANCE_B), fake_limactl(stdout, returncode):
                    with self.assertRaises(yo.YoError):
                        yo.ssh_port()


class TestNothingFallsBackToTheDefaultPortForAnotherInstance(FakeHome):
    def test_the_initrd_probe_before_lima_wrote_ssh_config_uses_the_created_port(self):
        with as_instance(INSTANCE_B), fake_limactl(limactl_json(60023)):
            argv = yo.initrd_ssh_argv("true")
        self.assertIn("Port=60023", argv)
        self.assertNotIn("Port=%d" % yo.SSH_LOCAL_PORT, argv)

    def test_the_initrd_probe_refuses_when_the_port_is_unknown(self):
        with as_instance(INSTANCE_B), fake_limactl(limactl_json(0)):
            with self.assertRaises(yo.YoError):
                yo.initrd_ssh_argv("true")

    def test_every_message_that_names_the_port_names_the_instances(self):
        attempt = yo.HelperRun(
            returncode=yo.SSH_UNREACHABLE, stderr="", migrated=False, streamed=False
        )
        migration = yo.HelperRun(
            returncode=yo.HELPER_EXIT_MIGRATION_FAILED,
            stderr="",
            migrated=False,
            streamed=False,
        )
        with as_instance(INSTANCE_B), fake_limactl(limactl_json(60023)):
            messages = [
                yo.helper_failure_message(attempt),
                yo.helper_failure_message(migration),
                yo.initrd_timeout_error(True).message,
                yo.initrd_timeout_error(False).message,
            ]
        for message in messages:
            with self.subTest(message=message[:40]):
                self.assertIn("60023", message)
                self.assertNotIn(str(yo.SSH_LOCAL_PORT), message)


class TestVmUpOnARunningBoxWithoutSshd(unittest.TestCase):
    def test_the_message_names_the_instances_port(self):
        running = yo.LimaInfo(
            host_agent_pid=1,
            port_forwards=[],
            mounts=[],
            forward_agent=False,
            status="Running",
            over_vsock=False,
            propagate_proxy_env=False,
            load_dot_ssh_pub_keys=False,
            ssh_local_port=60023,
        )
        with (
            as_instance(INSTANCE_B),
            mock.patch.object(yo, "lima_config_gaps", return_value=[]),
            mock.patch.object(yo, "lima_instance_info", return_value=running),
            mock.patch.object(yo, "stage2_up", return_value=False),
            mock.patch.object(yo, "initrd_waiting", return_value=False),
        ):
            with self.assertRaises(yo.YoError) as caught:
                yo.vm_up()
        self.assertIn("127.0.0.1:60023", caught.exception.message)


class TestVmUpNeverCreatesAnotherInstance(unittest.TestCase):
    def vm_up(self, instance):
        """Run vm_up on an instance limactl does not know; return error and stubs."""
        stubs = {}
        with contextlib.ExitStack() as stack:
            stack.enter_context(as_instance(instance))
            for name in (
                "luks_passphrase",
                "start_and_unlock",
                "spawn_limactl_start",
                "ensure_agent_ssh_config",
                "ensure_guest_known_hosts",
                "vm_services",
                "run",
            ):
                stubs[name] = stack.enter_context(mock.patch.object(yo, name))
            stack.enter_context(
                mock.patch.object(yo, "lima_config_gaps", return_value=[])
            )
            stack.enter_context(
                mock.patch.object(yo, "lima_instance_info", return_value=None)
            )
            try:
                yo.vm_up()
            except yo.YoError as failure:
                return failure, stubs
        return None, stubs

    def test_a_missing_instance_fails_before_touch_id_and_before_limactl_start(self):
        error, stubs = self.vm_up(INSTANCE_B)
        self.assertIn("limactl create --name second", error.message)
        stubs["luks_passphrase"].assert_not_called()
        stubs["start_and_unlock"].assert_not_called()
        stubs["spawn_limactl_start"].assert_not_called()
        stubs["run"].assert_not_called()

    def test_the_default_instance_is_still_created_when_missing(self):
        error, stubs = self.vm_up(yo.DEFAULT_INSTANCE)
        self.assertIsNone(error)
        stubs["luks_passphrase"].assert_called_once_with()
        stubs["start_and_unlock"].assert_called_once()


if __name__ == "__main__":
    unittest.main()
