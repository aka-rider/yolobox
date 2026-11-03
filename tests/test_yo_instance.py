"""-m/--machine: driving a second lima instance with the same `yo`."""

import argparse
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from test_yo import FakeHome, fake_limactl, load_yo, unload_yo, yo

INSTANCE_B = "second"


def limactl_json(local_port=None, **config):
    ssh = {} if local_port is None else {"localPort": local_port}
    return json.dumps({"status": "Stopped", "config": {"ssh": ssh, **config}})


@contextlib.contextmanager
def as_instance(name):
    yo.set_box(yo.box.driver, name)
    try:
        yield
    finally:
        yo.set_box(yo.box.driver, yo.DEFAULT_INSTANCE)


class TestNamesFollowTheMachine(unittest.TestCase):
    def test_the_default_machine_is_the_box_itself(self):
        self.assertEqual(yo.box.name, "yolobox")
        self.assertEqual(yo.box.vm_host, "lima-yolobox")
        self.assertEqual(yo.box.herdr_alias, "yolobox")
        self.assertEqual(yo.box.include_line, "Include ~/.lima/yolobox/ssh.config")
        self.assertEqual(yo.SSH_LOCAL_PORT, 60022)
        self.assertIn("Match host lima-yolobox user agent", yo.box.editor_ssh_block)

    def test_another_instance_gets_its_own_names(self):
        name = "yo_%s" % INSTANCE_B
        try:
            other = load_yo(name)
        finally:
            unload_yo(name)
        other.set_box(other.box.driver, INSTANCE_B)
        self.assertEqual(other.box.name, INSTANCE_B)
        self.assertEqual(other.box.vm_host, "lima-second")
        self.assertEqual(other.box.herdr_alias, INSTANCE_B)
        self.assertEqual(other.box.include_line, "Include ~/.lima/second/ssh.config")
        self.assertTrue(other.instance_dir().endswith(".lima/second"))
        self.assertIn("Match host lima-second user agent", other.box.editor_ssh_block)
        self.assertIn("~/.lima/second/ssh-agent.sock", other.box.editor_ssh_block)

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
            # Valid for lima, but the box's hostname yolobox-<name> is a DNS label.
            "has.dot",
            "has_underscore",
            "x" * 56,
            "box\n",
            "box\n\n",
        ):
            with self.subTest(name=bad):
                with self.assertRaises(argparse.ArgumentTypeError) as caught:
                    yo.instance_name(bad)
                self.assertIn("not a usable machine name", str(caught.exception))

    def test_the_longest_name_whose_hostname_is_a_label_is_accepted(self):
        longest = "x" * 55
        self.assertEqual(yo.instance_name(longest), longest)
        self.assertEqual(yo.instance_name("Mixed-Case"), "Mixed-Case")

    def test_a_refused_name_exits_2_and_leaves_the_default_machine(self):
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as caught:
                yo.main(["-m", "lima-second", "status"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("not a usable machine name", stderr.getvalue())
        self.assertEqual(yo.box.name, yo.DEFAULT_INSTANCE)


class TestHostnameFollowsTheMachine(unittest.TestCase):
    """Each machine's box gets its own hostname, so its own tailnet node."""

    def hostname_env(self):
        (entry,) = [e for e in yo.nix_env("alice") if e.startswith("YOLOBOX_HOSTNAME=")]
        return entry

    def test_the_default_machine_keeps_the_name_every_box_had(self):
        self.assertEqual(self.hostname_env(), "YOLOBOX_HOSTNAME=yolobox")

    def test_another_machine_is_yolobox_dash_its_name(self):
        with as_instance(INSTANCE_B):
            self.assertEqual(self.hostname_env(), "YOLOBOX_HOSTNAME=yolobox-second")

    def test_the_name_is_lowercased_because_dns_labels_are(self):
        with as_instance("Second-BOX"):
            self.assertEqual(self.hostname_env(), "YOLOBOX_HOSTNAME=yolobox-second-box")

    def test_the_libvirt_domain_keeps_its_own_spelling(self):
        with as_instance("Second"):
            self.assertEqual(yo.box.vm_name, "yolobox-Second")


class TestSeveralInstancesCoexist(FakeHome):
    """The default instance plus two named ones, side by side on one host."""

    NAMES = (None, "second", "scratch")
    PORTS = {"second": 60023, "scratch": 60024}

    def setUp(self):
        super().setUp()
        self.boxes = []
        for index, instance in enumerate(self.NAMES):
            name = "yo_box%d" % index
            box = load_yo(name)
            if instance is not None:
                box.set_box(box.box.driver, instance)
            self.boxes.append(box)
            self.addCleanup(unload_yo, name)
        for box in self.boxes:
            self.write_lima_ssh_config(box)

    def port_of(self, box):
        return self.PORTS.get(box.box.name, box.SSH_LOCAL_PORT)

    def write_lima_ssh_config(self, box):
        os.makedirs(box.instance_dir())
        with open(box.ssh_cfg(), "w") as handle:
            handle.write(
                "Host %s\n  Hostname 127.0.0.1\n  Port %d\n"
                '  IdentityFile "/k/user"\n' % (box.box.vm_host, self.port_of(box))
            )

    def read_ssh_config(self, box):
        with open(box.ssh_cfg()) as handle:
            return handle.read()

    def test_every_derived_name_is_distinct(self):
        for derive in (
            lambda box: box.box.name,
            lambda box: box.box.vm_host,
            lambda box: box.box.herdr_alias,
            lambda box: box.instance_dir(),
            lambda box: box.known_hosts_path(),
            lambda box: box.agent_control_path(),
            lambda box: box.box.include_line,
            lambda box: box.box.editor_ssh_block,
        ):
            values = [derive(box) for box in self.boxes]
            self.assertEqual(len(set(values)), len(self.boxes), values)

    def test_every_instance_reaches_its_own_port(self):
        ports = []
        for box in self.boxes:
            with fake_limactl(limactl_json(self.port_of(box)), module=box):
                ports.append(box.box.driver.ssh_port())
        self.assertEqual(ports, [60022, 60023, 60024])

    def test_each_ssh_config_holds_only_its_own_blocks(self):
        for box in self.boxes:
            box.ensure_agent_ssh_config()
        for box in self.boxes:
            text = self.read_ssh_config(box)
            self.assertIn(box.box.editor_ssh_block, text)
            self.assertIn("Host %s\n  User agent" % box.box.herdr_alias, text)
            self.assertIn("Port %d" % self.port_of(box), text)
            for other in self.boxes:
                if other is not box:
                    self.assertNotIn(other.box.vm_host, text)
                    self.assertNotIn(".lima/%s/" % other.box.name, text)

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
            self.assertEqual(yo.box.driver.ssh_port(), yo.SSH_LOCAL_PORT)
        fake_run.assert_not_called()

    def test_another_instance_reports_the_port_it_was_created_with(self):
        with as_instance(INSTANCE_B), fake_limactl(limactl_json(60023)) as fake_run:
            self.assertEqual(yo.box.driver.ssh_port(), 60023)
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
                        yo.box.driver.ssh_port()
                self.assertIn(
                    "limactl create --name second --set '.ssh.localPort=",
                    caught.exception.message,
                )

    def test_an_instance_limactl_does_not_know_is_an_error_naming_the_remedy(self):
        with as_instance(INSTANCE_B), fake_limactl("", listed=[]):
            with self.assertRaisesRegex(
                yo.YoError, "instance second does not exist, so yo cannot learn"
            ):
                yo.box.driver.ssh_port()

    def test_a_limactl_that_fails_or_garbles_its_answer_is_not_a_missing_instance(
        self,
    ):
        for stdout, returncode, stderr in (
            ("", 1, "limactl: permission denied"),
            ("not json", 0, ""),
            ("[1, 2]", 0, ""),
        ):
            with self.subTest(stdout=stdout, stderr=stderr):
                with as_instance(INSTANCE_B), fake_limactl(stdout, returncode, stderr):
                    with self.assertRaises(yo.YoError) as caught:
                        yo.box.driver.ssh_port()
                self.assertIn("limactl list second --json", caught.exception.message)
                self.assertIn(stdout or stderr, caught.exception.message)
                self.assertNotIn("does not exist", caught.exception.message)


class TestNothingFallsBackToTheDefaultPortForAnotherInstance(FakeHome):
    def test_the_initrd_probe_before_lima_wrote_ssh_config_uses_the_created_port(self):
        with as_instance(INSTANCE_B), fake_limactl(limactl_json(60023)):
            argv = yo.initrd_ssh_argv("true")
        self.assertIn("Port=60023", argv)
        self.assertNotIn("Port=%d" % yo.SSH_LOCAL_PORT, argv)

    def test_the_initrd_probe_refuses_when_the_port_is_unknown(self):
        with as_instance(INSTANCE_B), fake_limactl(limactl_json(0)):
            with self.assertRaisesRegex(
                yo.YoError, "second has ssh\\.localPort 0, which is not a"
            ):
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
            mock.patch.object(yo.LIMA, "config_gaps", return_value=[]),
            mock.patch.object(yo.lima, "lima_instance_info", return_value=running),
            mock.patch.object(yo.unlock, "stage2_up", return_value=False),
            mock.patch.object(
                yo.unlock, "sshd_answers", return_value=yo.SshOutcome.NOT_LISTENING
            ),
        ):
            with self.assertRaises(yo.YoError) as caught:
                yo.vm_up()
        self.assertIn("127.0.0.1:60023", caught.exception.message)


class TestVmUpNeverCreatesAnotherInstance(unittest.TestCase):
    def vm_up(self, instance, size=yo.NO_SIZE, info=None):
        """Run vm_up on an instance limactl reports as `info`; return error and stubs."""
        stubs = {}
        with contextlib.ExitStack() as stack:
            stack.enter_context(as_instance(instance))
            for target, name in (
                (yo.agent, "luks_passphrase"),
                (yo.LIMA, "start"),
                (yo.proc, "spawn"),
                (yo.sshconfig, "ensure_agent_ssh_config"),
                (yo.guest, "ensure_guest_known_hosts"),
                (yo.forwards, "vm_services"),
                (yo.proc, "run"),
            ):
                stubs[name] = stack.enter_context(mock.patch.object(target, name))
            stack.enter_context(
                mock.patch.object(yo.LIMA, "config_gaps", return_value=[])
            )
            stack.enter_context(
                mock.patch.object(yo.lima, "lima_instance_info", return_value=info)
            )
            try:
                yo.vm_up(size=size)
            except yo.YoError as failure:
                return failure, stubs
        return None, stubs

    def test_a_missing_instance_fails_before_touch_id_and_before_limactl_start(self):
        error, stubs = self.vm_up(INSTANCE_B)
        self.assertIn("limactl create --name second", error.message)
        self.assert_refused_before_anything(stubs)

    def test_the_default_instance_is_still_created_when_missing(self):
        error, stubs = self.vm_up(yo.DEFAULT_INSTANCE)
        self.assertIsNone(error)
        stubs["luks_passphrase"].assert_called_once_with()
        stubs["start"].assert_called_once()

    def assert_refused_before_anything(self, stubs):
        stubs["luks_passphrase"].assert_not_called()
        stubs["start"].assert_not_called()
        stubs["spawn"].assert_not_called()
        stubs["run"].assert_not_called()

    def test_a_sized_default_instance_is_left_to_limactl_create(self):
        error, stubs = self.vm_up(yo.DEFAULT_INSTANCE, yo.Size(24, 250))
        self.assertIn(
            "limactl create --name yolobox --memory 24 --disk 250 %s\n"
            % (yo.SCRIPT_DIR / "lima" / "yolobox.yaml"),
            error.message,
        )
        self.assertNotIn("--set", error.message)
        self.assertTrue(
            error.message.endswith(
                "then run this command again without --memory 24 --disk 250"
            )
        )
        self.assert_refused_before_anything(stubs)

    def test_a_sized_named_instance_also_needs_its_own_port(self):
        error, stubs = self.vm_up(INSTANCE_B, yo.Size(None, 250))
        self.assertIn(
            "limactl create --name second --set '.ssh.localPort=<free port>' "
            "--disk 250 %s" % (yo.SCRIPT_DIR / "lima" / "yolobox.yaml"),
            error.message,
        )
        self.assertNotIn("--memory", error.message)
        self.assertTrue(
            error.message.endswith("then run this command again without --disk 250")
        )
        self.assert_refused_before_anything(stubs)

    STOPPED = yo.LimaInfo(
        host_agent_pid=None,
        port_forwards=[],
        mounts=[],
        forward_agent=False,
        status="Stopped",
        over_vsock=False,
        propagate_proxy_env=False,
        load_dot_ssh_pub_keys=False,
        ssh_local_port=60023,
    )

    def test_memory_for_an_instance_that_exists_names_limactl_edit(self):
        error, stubs = self.vm_up(INSTANCE_B, yo.Size(24, None), info=self.STOPPED)
        self.assertIn("second already exists", error.message)
        self.assertIn("drop --memory 24", error.message)
        self.assertIn(
            "stop it (yo -m second down), then: limactl edit --yes second --memory 24",
            error.message,
        )
        self.assertNotIn("limactl delete", error.message)
        self.assert_refused_before_anything(stubs)

    def test_a_disk_for_an_instance_that_exists_names_disk_grow_and_the_loss(self):
        error, stubs = self.vm_up(INSTANCE_B, yo.Size(None, 250), info=self.STOPPED)
        self.assertIn("drop --disk 250", error.message)
        self.assertIn(
            "keeping what is on it: yo -m second disk-grow 250", error.message
        )
        self.assertIn("everything on it is lost: limactl delete second", error.message)
        self.assertNotIn("limactl edit", error.message)
        self.assert_refused_before_anything(stubs)


class TestLimaStartNeverDropsASize(unittest.TestCase):
    """start() is reached with a size only when vm_up's own refusal was skipped."""

    def start(self, exists):
        with (
            as_instance(INSTANCE_B),
            mock.patch.object(yo.lima, "lima_instance_exists", return_value=exists),
            mock.patch.object(yo.proc, "spawn") as spawn,
            self.assertRaises(yo.YoError) as caught,
        ):
            yo.LIMA.start(b"pass", yo.Size(24, None))
        spawn.assert_not_called()
        return caught.exception.message

    def test_an_instance_that_exists_is_refused_as_vm_up_would(self):
        self.assertIn("limactl edit --yes second --memory 24", self.start(True))

    def test_a_missing_instance_is_left_to_limactl_create(self):
        self.assertIn("limactl create --name second", self.start(False))


class TestVmUpOnALimaConfigGap(unittest.TestCase):
    def test_a_gap_is_a_note_and_the_box_still_starts(self):
        notes = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(as_instance(yo.DEFAULT_INSTANCE))
            for module, name in (
                (yo.agent, "luks_passphrase"),
                (yo.sshconfig, "ensure_agent_ssh_config"),
                (yo.guest, "ensure_guest_known_hosts"),
                (yo.forwards, "vm_services"),
            ):
                stack.enter_context(mock.patch.object(module, name))
            stack.enter_context(
                mock.patch.object(yo.LIMA, "config_gaps", return_value=[yo.GAP_MOUNTS])
            )
            stack.enter_context(
                mock.patch.object(yo.lima, "lima_instance_info", return_value=None)
            )
            stack.enter_context(
                mock.patch.object(yo.ui, "err", side_effect=notes.append)
            )
            start = stack.enter_context(mock.patch.object(yo.LIMA, "start"))
            yo.vm_up()
        start.assert_called_once()
        self.assertEqual(len(notes), 1)
        self.assertIn(yo.GAP_MOUNTS, notes[0])


class TestRemediesNameTheMachine(FakeHome):
    def test_the_default_machine_prints_bare_yo(self):
        self.assertEqual(yo.yo_cmd("up"), "yo up")
        self.assertEqual(yo.yo_cmd("gc", "--deep", "--yes"), "yo gc --deep --yes")

    def test_the_not_up_hint_asks_about_the_machine_it_names(self):
        self.assertEqual(
            yo.not_up_hint("cannot reach the VM"),
            "cannot reach the VM — is it up? yo status",
        )
        with as_instance("box2"):
            self.assertEqual(
                yo.not_up_hint("cannot read x", parenthesized=True),
                "cannot read x — is it up? (yo -m box2 status)",
            )

    def test_another_machine_is_named_in_the_remedies(self):
        with as_instance("box2"), mock.patch.object(yo.config, "YO_VERSION", "1.0.0"):
            self.assertEqual(yo.yo_cmd("up"), "yo -m box2 up")
            self.assertIn("yo -m box2 bootstrap", yo.version_drift("1.0.0", "0.9.0"))
            self.assertTrue(yo.rebuild_hint().startswith("yo -m box2 ssh "))

    def test_the_enter_refusal_names_the_machine(self):
        with as_instance("box2"), mock.patch.object(yo.sys, "stdin") as stdin:
            stdin.isatty.return_value = False
            with self.assertRaises(yo.YoError) as caught:
                yo.cmd_enter(yo.build_parser().parse_args(["enter"]))
        self.assertTrue(caught.exception.message.startswith("yo -m box2 enter "))


if __name__ == "__main__":
    unittest.main()
