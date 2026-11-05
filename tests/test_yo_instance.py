"""-m/--machine: driving a second lima instance with the same `yo`."""

import argparse
import contextlib
import io
import json
import os
import shutil
import unittest
from unittest import mock

from test_yo import (
    A_RELEASE,
    FakeHome,
    fake_limactl,
    host_cpus,
    load_yo,
    unload_yo,
    yo,
)

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


class TestALostInstance(unittest.TestCase):
    def test_a_deleted_named_instance_is_created_with_limactl_not_yo_up(self):
        with as_instance(INSTANCE_B):
            hint = yo.LIMA.vm_lost_hint(None)
            self.assertIn("instance second no longer exists — create it with:", hint)
            self.assertIn("limactl create --name second", hint)
            self.assertNotIn(".log", hint)


class TestNamesFollowTheMachine(unittest.TestCase):
    def test_the_default_machine_is_the_box_itself(self):
        self.assertEqual(yo.box.name, "yolobox")
        self.assertEqual(yo.box.vm_host, "lima-yolobox")
        self.assertEqual(yo.box.herdr_alias, "yolobox")
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
        (entry,) = [
            e
            for e in yo.nix_env("alice", "github:x/y/z")
            if e.startswith("YOLOBOX_HOSTNAME=")
        ]
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

    @unittest.skipIf(shutil.which("ssh") is None, "no ssh")
    def test_one_glob_line_reaches_every_machine_through_real_ssh(self):
        for box in self.boxes:
            box.ensure_agent_ssh_config()
        ssh_dir = os.path.join(self.home, ".ssh")
        os.mkdir(ssh_dir)
        with open(os.path.join(ssh_dir, "config"), "w") as handle:
            # Absolute, so the test holds whichever home ssh expands ~ against.
            handle.write(yo.SSH_CONFIG_INCLUDE_LINE.replace("~", self.home) + "\n")
        self.assertEqual(
            [box.ssh_config_reaches_box() for box in self.boxes],
            [True] * len(self.boxes),
        )


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
            mock.patch.object(yo.unlock, "ssh_banner", return_value=""),
        ):
            with self.assertRaises(yo.YoError) as caught:
                yo.vm_up()
        self.assertIn("127.0.0.1:60023", caught.exception.message)


def lima_info(status, port=60023):
    return yo.LimaInfo(
        host_agent_pid=None,
        port_forwards=[],
        mounts=[],
        forward_agent=False,
        status=status,
        over_vsock=False,
        propagate_proxy_env=False,
        load_dot_ssh_pub_keys=False,
        ssh_local_port=port,
    )


# What the box has now in the resize tests: 16 GiB of memory, a 100 GiB disk, 4 CPUs.
CURRENT = yo.BoxSize(16, 100, 4)


# The calls whose order the resize tests check.
ORDERED = ("luks_passphrase", "resize", "grow_disk", "start")


class VmUpOnLima(unittest.TestCase):
    """vm_up on an instance limactl reports as `info`, with every side effect stubbed."""

    def vm_up(self, instance, size=yo.NO_SIZE, info=None, remembered_ref=None, cpus=16):
        """Return the error vm_up raised (or None) and the stubs, which also
        record the order of the calls that change the box in `self.order`."""
        stubs = {}
        self.order = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(as_instance(instance))
            stack.enter_context(
                mock.patch.object(yo.os, "cpu_count", return_value=cpus)
            )
            stack.enter_context(
                mock.patch.object(
                    yo.state, "remembered_flake_ref", return_value=remembered_ref
                )
            )
            for target, name in (
                (yo.agent, "luks_passphrase"),
                (yo.LIMA, "start"),
                (yo.LIMA, "resize"),
                (yo.LIMA, "grow_disk"),
                (yo.LIMA, "current_size"),
                (yo.proc, "spawn"),
                (yo.sshconfig, "ensure_agent_ssh_config"),
                (yo.guest, "ensure_guest_known_hosts"),
                (yo.forwards, "vm_services"),
                (yo.proc, "run"),
            ):
                stubs[name] = stack.enter_context(mock.patch.object(target, name))
                if name in ORDERED:
                    stubs[name].side_effect = lambda *a, _n=name, **k: (
                        self.order.append(_n) or mock.DEFAULT
                    )
            stubs["current_size"].return_value = CURRENT
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

    def assert_refused_before_anything(self, stubs):
        stubs["luks_passphrase"].assert_not_called()
        for name in (
            "start",
            "resize",
            "grow_disk",
            "spawn",
            "run",
        ):
            stubs[name].assert_not_called()


class TestVmUpNeverCreatesAnotherInstance(VmUpOnLima):
    def test_a_missing_instance_fails_before_touch_id_and_before_limactl_start(self):
        error, stubs = self.vm_up(INSTANCE_B)
        self.assertIn("limactl create --name second", error.message)
        self.assert_refused_before_anything(stubs)

    def test_the_default_instance_is_still_created_when_missing(self):
        error, stubs = self.vm_up(yo.DEFAULT_INSTANCE)
        self.assertIsNone(error)
        stubs["luks_passphrase"].assert_called_once_with()
        stubs["start"].assert_called_once()

    def test_a_missing_named_instance_asked_for_no_size_gets_the_default_cpus_in_the_hint(
        self,
    ):
        # Lima's own default CPU count is not yo's, and the user passed no flag
        # to drop, so the command has --cpus and the message does not say "without".
        for host, expected in ((16, "4"), (3, "2")):
            with self.subTest(host=host):
                error, _ = self.vm_up(INSTANCE_B, cpus=host)
                self.assertIn(
                    "--set '.ssh.localPort=<free port>' --cpus %s " % expected,
                    error.message,
                )
                self.assertNotIn("without", error.message)

    def test_a_sized_named_instance_also_needs_its_own_port(self):
        error, stubs = self.vm_up(INSTANCE_B, yo.Size(None, 250, 3))
        self.assertIn(
            "limactl create --name second --set '.ssh.localPort=<free port>' "
            "--disk 250 --cpus 3 %s" % (yo.SCRIPT_DIR / "lima" / "yolobox.yaml"),
            error.message,
        )
        self.assertNotIn("--memory", error.message)
        self.assertTrue(
            error.message.endswith(
                "then run this command again without --disk 250 --cpus 3"
            )
        )
        self.assert_refused_before_anything(stubs)


class TestVmUpCreatesAtTheAskedSize(VmUpOnLima):
    def test_a_missing_box_is_created_at_all_three_sizes_asked(self):
        error, stubs = self.vm_up(yo.DEFAULT_INSTANCE, yo.Size(24, 250, 3))
        self.assertIsNone(error)
        stubs["start"].assert_called_once_with(
            stubs["luks_passphrase"].return_value, yo.Size(24, 250, 3)
        )

    def test_a_box_created_without_cpus_gets_one_less_than_the_host_up_to_four(self):
        for host, expected in ((2, 1), (3, 2), (5, 4), (64, 4)):
            with self.subTest(host=host):
                error, stubs = self.vm_up(yo.DEFAULT_INSTANCE, cpus=host)
                self.assertIsNone(error)
                stubs["start"].assert_called_once_with(
                    stubs["luks_passphrase"].return_value,
                    yo.Size(None, None, expected),
                )

    def test_a_host_with_one_cpu_or_an_unknown_count_creates_nothing(self):
        for host in (1, None):
            with self.subTest(host=host):
                error, stubs = self.vm_up(yo.DEFAULT_INSTANCE, cpus=host)
                self.assertIsInstance(error, yo.YoError)
                self.assertIn("CPU", error.message)
                self.assert_refused_before_anything(stubs)

    def test_the_actual_start_passes_every_flag_to_limactl(self):
        with (
            as_instance(INSTANCE_B),
            mock.patch.object(yo.lima, "lima_instance_exists", return_value=False),
            mock.patch.object(yo.proc, "spawn") as spawn,
            mock.patch.object(yo.unlock, "unlock_disk"),
            mock.patch.object(yo.unlock, "wait_for_operator"),
        ):
            spawn.return_value.wait.return_value = 0
            yo.LIMA.start(b"pass", yo.Size(24, 250, 3))
        spawn.assert_called_once_with(
            [
                "limactl",
                "start",
                "--name",
                "second",
                "--yes",
                "--memory",
                "24",
                "--disk",
                "250",
                "--cpus",
                "3",
                str(yo.SCRIPT_DIR / "lima" / "yolobox.yaml"),
            ]
        )


class TestVmUpResizesAStoppedBox(VmUpOnLima):
    STOPPED = lima_info("Stopped")

    def resize(self, size, status="Stopped"):
        return self.vm_up(INSTANCE_B, size, info=lima_info(status))

    def test_memory_and_cpus_are_set_before_the_start_and_the_start_asks_for_no_size(
        self,
    ):
        error, stubs = self.resize(yo.Size(24, None, 2))
        self.assertIsNone(error)
        stubs["resize"].assert_called_once_with(24, 2)
        self.assertEqual(self.order, ["luks_passphrase", "resize", "start"])
        stubs["start"].assert_called_once_with(
            stubs["luks_passphrase"].return_value, yo.NO_SIZE
        )
        stubs["grow_disk"].assert_not_called()

    def test_only_what_differs_is_a_change(self):
        # The same memory and disk as now, a new CPU count.
        error, stubs = self.resize(yo.Size(16, 100, 2))
        self.assertIsNone(error)
        stubs["resize"].assert_called_once_with(16, 2)
        stubs["grow_disk"].assert_not_called()

    def test_a_bigger_disk_is_left_to_the_grow_after_the_start(self):
        # The ceiling is raised by grow_to_asked_disk, once the booted guest
        # is known to be able to grow (TestUpGrowsTheDiskOfAStoppedBox).
        error, stubs = self.resize(yo.Size(None, 250, None))
        self.assertIsNone(error)
        stubs["resize"].assert_not_called()
        stubs["grow_disk"].assert_not_called()
        self.assertEqual(self.order, ["luks_passphrase", "start"])
        stubs["start"].assert_called_once_with(
            stubs["luks_passphrase"].return_value, yo.NO_SIZE
        )

    def test_a_size_equal_to_the_current_one_changes_nothing_and_asks_nothing_extra(
        self,
    ):
        error, stubs = self.resize(yo.Size(16, 100, 4))
        self.assertIsNone(error)
        for name in ("resize", "grow_disk"):
            stubs[name].assert_not_called()
        stubs["start"].assert_called_once_with(
            stubs["luks_passphrase"].return_value, yo.NO_SIZE
        )

    def test_a_box_started_without_a_size_keeps_its_cpus_and_is_not_even_measured(self):
        error, stubs = self.resize(yo.NO_SIZE)
        self.assertIsNone(error)
        stubs["current_size"].assert_not_called()
        stubs["resize"].assert_not_called()

    def test_a_change_to_a_box_that_is_not_stopped_is_refused_before_anything(self):
        for status in ("Running", "Starting", "Broken"):
            with self.subTest(status=status):
                error, stubs = self.resize(yo.Size(24, None, None), status)
                self.assertIn(
                    "second is %s with 16 GiB of memory" % status, error.message
                )
                self.assertIn("4 CPUs and a 100 GiB disk", error.message)
                self.assertIn("--memory 24 would change that", error.message)
                self.assertIn(
                    "yo -m second down, then run this command again", error.message
                )
                self.assert_refused_before_anything(stubs)

    def test_a_running_box_asked_only_for_a_bigger_disk_is_pointed_at_disk_grow(self):
        error, _ = self.resize(yo.Size(None, 250, None), "Running")
        self.assertIn("yo -m second down, then run this command again", error.message)
        self.assertIn(
            "to only grow the disk of a running box, yo -m second disk-grow 250",
            error.message,
        )

    def test_disk_grow_is_not_offered_when_memory_or_cpus_change_too_or_it_is_not_running(
        self,
    ):
        for size, status in (
            (yo.Size(24, 250, None), "Running"),
            (yo.Size(None, 250, 2), "Running"),
            (yo.Size(None, 250, None), "Starting"),
        ):
            with self.subTest(size=size, status=status):
                error, _ = self.resize(size, status)
                self.assertNotIn("disk-grow", error.message)

    def test_a_running_box_asked_for_its_own_size_goes_on_to_the_boot_stage(self):
        with mock.patch.object(
            yo.unlock, "boot_stage", return_value=yo.BootStage.UNKNOWN
        ) as stage:
            error, stubs = self.vm_up(
                INSTANCE_B, yo.Size(16, 100, 4), info=lima_info("Running")
            )
        stage.assert_called_once_with()
        self.assertNotIn("would change that", error.message)
        stubs["resize"].assert_not_called()

    def test_a_smaller_disk_is_refused_before_anything_and_destroy_is_no_way_out(self):
        for status in ("Stopped", "Running"):
            with self.subTest(status=status):
                error, stubs = self.resize(yo.Size(24, 50, None), status)
                self.assertIn("second's disk is 100 GiB", error.message)
                self.assertIn(
                    "neither lima nor libvirt can shrink a disk", error.message
                )
                self.assertNotIn("destroy", error.message)
                self.assert_refused_before_anything(stubs)

    def test_the_passphrase_is_derived_before_the_box_is_changed(self):
        error, _ = self.resize(yo.Size(24, 250, None))
        self.assertIsNone(error)
        self.assertEqual(self.order[0], "luks_passphrase")


class TestLimaGrowDisk(unittest.TestCase):
    def test_a_refused_edit_says_what_happened_and_leaves_the_next_step_to_the_caller(
        self,
    ):
        with (
            as_instance(INSTANCE_B),
            mock.patch.object(yo.ui, "out"),
            mock.patch.object(
                yo.proc, "run", return_value=mock.Mock(returncode=3)
            ) as run,
            self.assertRaises(yo.YoError) as caught,
        ):
            yo.LIMA.grow_disk(100, 200)
        self.assertEqual(
            run.call_args.args[0],
            ["limactl", "edit", "--yes", "second", "--disk", "200"],
        )
        self.assertEqual(
            caught.exception.message, "limactl edit refused the disk resize (exit 3)"
        )


class TestLimaSizeCommands(unittest.TestCase):
    GIB = 1024**3

    def current_size(self, stdout, returncode=0):
        with (
            as_instance(INSTANCE_B),
            mock.patch.object(
                yo.proc,
                "run",
                return_value=mock.Mock(returncode=returncode, stdout=stdout, stderr=""),
            ) as run,
        ):
            try:
                return yo.LIMA.current_size(), run
            except yo.YoError as failure:
                return failure, run

    def test_the_size_is_read_in_whole_gib_from_limactls_bytes(self):
        size, run = self.current_size("%d %d 4\n" % (16 * self.GIB, 100 * self.GIB))
        self.assertEqual(size, yo.BoxSize(16, 100, 4))
        self.assertEqual(
            run.call_args.args[0][:4], ["limactl", "list", "second", "--format"]
        )

    def test_memory_that_is_not_whole_gib_is_an_error_not_a_rounded_comparison(self):
        error, _ = self.current_size("%d %d 4" % (int(12.5 * self.GIB), 100 * self.GIB))
        self.assertIsInstance(error, yo.YoError)
        self.assertIn("not a whole number of GiB", error.message)

    def test_a_disk_that_is_not_whole_gib_is_an_error_not_a_rounded_one(self):
        error, _ = self.current_size("%d %d 4" % (16 * self.GIB, 100 * self.GIB + 512))
        self.assertIsInstance(error, yo.YoError)
        self.assertIn("disk is", error.message)
        self.assertIn("not a whole number of GiB", error.message)

    def test_an_answer_that_is_not_three_numbers_is_an_error(self):
        for stdout, returncode in (("", 0), ("1 2", 0), ("1 2 x", 0), ("1 2 3", 1)):
            with self.subTest(stdout=stdout, returncode=returncode):
                error, _ = self.current_size(stdout, returncode)
                self.assertIn("reports no memory, disk and CPUs", error.message)

    def edit(self, returncode):
        with (
            as_instance(INSTANCE_B),
            mock.patch.object(
                yo.proc, "run", return_value=mock.Mock(returncode=returncode)
            ) as run,
        ):
            try:
                yo.LIMA.resize(24, 2)
            except yo.YoError as failure:
                return failure, run
        return None, run

    def test_resize_edits_the_stopped_instance(self):
        error, run = self.edit(0)
        self.assertIsNone(error)
        self.assertEqual(
            run.call_args.args[0],
            ["limactl", "edit", "--yes", "second", "--memory", "24", "--cpus", "2"],
        )

    def test_a_refused_edit_is_an_error_saying_the_box_is_unchanged(self):
        error, _ = self.edit(1)
        self.assertIn("left stopped at its old size", error.message)


class TestVmUpOnALimaConfigGap(unittest.TestCase):
    def test_a_gap_is_a_note_and_the_box_still_starts(self):
        notes = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(as_instance(yo.DEFAULT_INSTANCE))
            stack.enter_context(host_cpus())
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
        with as_instance("box2"), mock.patch.object(yo.config, "YO_VERSION", A_RELEASE):
            self.assertEqual(yo.yo_cmd("up"), "yo -m box2 up")
            self.assertIn("yo -m box2 bootstrap", yo.version_drift(A_RELEASE, "0.9.0"))
            self.assertTrue(yo.rebuild_hint().startswith("yo -m box2 ssh "))

    def test_the_enter_refusal_names_the_machine(self):
        with as_instance("box2"), mock.patch.object(yo.sys, "stdin") as stdin:
            stdin.isatty.return_value = False
            with self.assertRaises(yo.YoError) as caught:
                yo.cmd_enter(yo.build_parser().parse_args(["enter"]))
        self.assertTrue(caught.exception.message.startswith("yo -m box2 enter "))


if __name__ == "__main__":
    unittest.main()
