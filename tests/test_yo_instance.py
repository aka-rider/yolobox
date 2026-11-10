"""-m/--machine: driving a second lima instance with the same `yo`."""

import argparse
import contextlib
import io
import json
import os
import subprocess
import unittest
from unittest import mock

from shell_harness import needs_tool
from test_yo import (
    A_RELEASE,
    FakeHome,
    fake_bin,
    fake_limactl,
    hostagent_holds_the_port,
    load_yo,
    locked,
    proved_initrd,
    unload_yo,
    yo,
    refuse_tcp_to_the_host,
)


def setUpModule():
    refuse_tcp_to_the_host()


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
        # Under a home short enough for its sockets on a Mac.
        longest = "x" * 55
        with mock.patch.dict(os.environ, {"HOME": "/h"}):
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
            os.makedirs(box.instance_dir())

    def port_of(self, box):
        return self.PORTS.get(box.box.name, box.SSH_LOCAL_PORT)

    def write_ssh_config(self, box):
        with fake_limactl(limactl_json(self.port_of(box)), module=box):
            box.sshconfig.write_ssh_config()

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
            self.write_ssh_config(box)
        for box in self.boxes:
            text = self.read_ssh_config(box)
            self.assertIn("Match host %s user agent" % box.box.vm_host, text)
            self.assertIn('ControlPath "%s"' % box.agent_control_path(), text)
            self.assertIn("Host %s\n" % box.box.herdr_alias, text)
            self.assertIn("Port %d" % self.port_of(box), text)
            for other in self.boxes:
                if other is not box:
                    self.assertNotIn(other.box.vm_host, text)
                    self.assertNotIn(".lima/%s/" % other.box.name, text)

    def test_rerunning_one_changes_no_file_of_any_instance(self):
        for box in self.boxes:
            self.write_ssh_config(box)
        before = [self.read_ssh_config(box) for box in self.boxes]
        for box in reversed(self.boxes):
            self.write_ssh_config(box)
        self.assertEqual([self.read_ssh_config(box) for box in self.boxes], before)

    @needs_tool("ssh")
    def test_one_glob_line_reaches_every_machine_through_real_ssh(self):
        for box in self.boxes:
            self.write_ssh_config(box)
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
            fake_run.call_args.args[0],
            ["limactl", "--log-level=warn", "list", INSTANCE_B, "--json"],
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
                    "recreate the box, which deletes everything on it: "
                    "yo -m second destroy --yes && yo -m second bootstrap",
                    caught.exception.message,
                )

    def test_a_port_another_boxs_listener_can_claim_is_refused_with_the_remedy(self):
        # Lima mirrors guest TCP ports 1024-49151 onto the host's loopback, and
        # root, the passphrase and the first host-key fetch go to this port
        # with no host key checked.
        for port in (1, 1023, 1024, 22222, 49151):
            with self.subTest(localPort=port):
                with as_instance(INSTANCE_B), fake_limactl(limactl_json(port)):
                    with self.assertRaises(yo.YoError) as caught:
                        yo.box.driver.ssh_port()
                self.assertIn("ssh.localPort %d" % port, caught.exception.message)
                self.assertIn(
                    "yo -m second down && limactl --log-level=warn edit --tty=false second --set "
                    "'.ssh.localPort=<PORT>' && yo -m second up",
                    caught.exception.message,
                )
                self.assertIn(
                    "yo -m second destroy --yes && yo -m second bootstrap",
                    caught.exception.message,
                )

    def test_the_listener_check_refuses_a_port_past_the_last_one_with_the_remedy(
        self,
    ):
        # ssh.config is lima's own file, so the port read from it gets the
        # bounds ssh_port() gives the one read from limactl.
        with as_instance(INSTANCE_B), hostagent_holds_the_port():
            yo.LIMA.verify_vm_listener(65535)
            for port in (65536, 70000):
                with self.subTest(port=port):
                    with self.assertRaises(yo.YoError) as caught:
                        yo.LIMA.verify_vm_listener(port)
                    self.assertIn(
                        "second has ssh.localPort %d, which is past the last TCP "
                        "port, 65535" % port,
                        caught.exception.message,
                    )
                    self.assertIn(
                        "yo -m second down && limactl --log-level=warn edit",
                        caught.exception.message,
                    )

    def test_the_lowest_and_highest_safe_ports_are_accepted(self):
        for port in (49152, 65535):
            with as_instance(INSTANCE_B), fake_limactl(limactl_json(port)):
                self.assertEqual(yo.box.driver.ssh_port(), port)

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
                self.assertIn(
                    "limactl --log-level=warn list second --json",
                    caught.exception.message,
                )
                self.assertIn(stdout or stderr, caught.exception.message)
                self.assertNotIn("does not exist", caught.exception.message)


class TestNothingFallsBackToTheDefaultPortForAnotherInstance(FakeHome):
    def test_the_initrd_probe_uses_the_created_port(self):
        with as_instance(INSTANCE_B), fake_limactl(limactl_json(60023)):
            argv = yo.unlock.operator_argv("true", yo.LIMA.ssh_facts())
        self.assertIn("Port=60023", argv)
        self.assertNotIn("Port=%d" % yo.SSH_LOCAL_PORT, argv)

    def test_the_initrd_probe_refuses_when_the_port_is_unknown(self):
        with as_instance(INSTANCE_B), fake_limactl(limactl_json(0)):
            with self.assertRaisesRegex(
                yo.YoError, "second has ssh\\.localPort 0, which is not a"
            ):
                yo.unlock.operator_argv("true", yo.LIMA.ssh_facts())

    def test_every_message_that_names_the_port_names_the_instances(self):
        attempt = yo.HelperRun(returncode=yo.SSH_UNREACHABLE, stderr="", streamed=False)
        with as_instance(INSTANCE_B), fake_limactl(limactl_json(60023)):
            gate = proved_initrd(yo.LIMA.ssh_facts())
            messages = [
                yo.helper_failure_message(attempt, gate),
                yo.initrd_timeout_error(yo.unlock.Round("no banner", "")).message,
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
            user_uid=yo.OPERATOR_GUEST_UID,
            ssh_local_port=60023,
            vm_type="vz",
        )
        with (
            as_instance(INSTANCE_B),
            mock.patch.object(yo.lima, "lima_instance_info", return_value=running),
            mock.patch.object(yo.unlock, "ssh_banner", return_value=""),
            yo.box_lock(wait=True) as held,
        ):
            with self.assertRaises(yo.YoError) as caught:
                yo.vm_up(held)
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
        user_uid=yo.OPERATOR_GUEST_UID,
        ssh_local_port=port,
        vm_type="vz",
    )


# The calls whose order the start tests check.
ORDERED = ("luks_passphrase", "start", "create")


class VmUpOnLima(unittest.TestCase):
    """vm_up on an instance limactl reports as `info`, with every side effect stubbed."""

    def vm_up(self, instance, size=yo.NO_SIZE, info=None, cpus=16):
        """Return the error vm_up raised (or None) and the stubs, which also
        record the order of the calls that change the box in `self.order`."""
        stubs = {}
        self.order = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(as_instance(instance))
            stack.enter_context(
                mock.patch.object(yo.os, "cpu_count", return_value=cpus)
            )
            for target, name in (
                (yo.agent, "luks_passphrase"),
                (yo.agent.PassphraseSource, "require_key"),
                (yo.LIMA, "start"),
                (yo.LIMA, "create"),
                (yo.proc, "spawn"),
                (yo.sshconfig, "write_ssh_config"),
                (yo.guest, "ensure_guest_known_hosts"),
                (yo.forwards, "vm_services"),
                (yo.sshx, "bootstrapped"),
                (yo.sshx, "broken_agent_login"),
                (yo.ui, "err"),
                (yo.proc, "run"),
            ):
                stubs[name] = stack.enter_context(mock.patch.object(target, name))
                if name in ORDERED:
                    stubs[name].side_effect = lambda *a, _n=name, **k: (
                        self.order.append(_n) or mock.DEFAULT
                    )
            stubs["broken_agent_login"].return_value = None
            stack.enter_context(
                mock.patch.object(yo.lima, "lima_instance_info", return_value=info)
            )
            try:
                with yo.box_lock(wait=True) as held:
                    yo.vm_up(held, size=size)
            except yo.YoError as failure:
                return failure, stubs
        return None, stubs

    def assert_refused_before_anything(self, stubs):
        stubs["luks_passphrase"].assert_not_called()
        for name in ("start", "create", "spawn", "run"):
            stubs[name].assert_not_called()


class TestVmUpCreatesAMissingInstance(VmUpOnLima):
    def test_a_missing_instance_is_created_without_a_passphrase(self):
        # A new box is the seed image, whose root is plaintext: no Touch ID.
        for instance in (yo.DEFAULT_INSTANCE, INSTANCE_B):
            with self.subTest(instance=instance):
                error, stubs = self.vm_up(instance)
                self.assertIsNone(error)
                self.assertEqual(self.order, ["create", "start"])
                # The start gets a source that derives only if an initrd asks.
                stubs["luks_passphrase"].assert_not_called()


class TestVmUpCreatesAtTheAskedSize(VmUpOnLima):
    def test_a_missing_box_is_created_at_all_three_sizes_asked(self):
        error, stubs = self.vm_up(INSTANCE_B, yo.Size(24, 250, 3))
        self.assertIsNone(error)
        stubs["create"].assert_called_once_with(mock.ANY, yo.NewBox(24, 250, 3))

    def test_a_box_created_without_cpus_gets_one_less_than_the_host_up_to_four(self):
        for host, expected in ((2, 1), (3, 2), (5, 4), (64, 4)):
            with self.subTest(host=host):
                error, stubs = self.vm_up(yo.DEFAULT_INSTANCE, cpus=host)
                self.assertIsNone(error)
                stubs["create"].assert_called_once_with(
                    mock.ANY, yo.NewBox(None, None, expected)
                )

    def test_a_host_with_one_cpu_or_an_unknown_count_creates_nothing(self):
        for host in (1, None):
            with self.subTest(host=host):
                error, stubs = self.vm_up(yo.DEFAULT_INSTANCE, cpus=host)
                self.assertIsInstance(error, yo.YoError)
                self.assertIn("CPU", error.message)
                self.assert_refused_before_anything(stubs)

    def create(self, instance, listing):
        """LIMA.create of a box asked 24 GiB, 250 GiB and 3 CPUs, with `listing`
        what `limactl list --json` prints and 49152 held on the host. Returns
        the `limactl create` argv; nothing is started."""
        ran = []

        def run(argv, **kwargs):
            ran.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, listing, "")

        with (
            as_instance(instance),
            mock.patch.object(yo.lima, "lima_instance_exists", return_value=False),
            mock.patch.object(yo.proc, "run", side_effect=run),
            mock.patch.object(
                yo.driver, "loopback_port_bound", side_effect=lambda p: p == 49152
            ),
            mock.patch.object(yo.proc, "spawn") as spawn,
        ):
            locked(yo, yo.LIMA.create, yo.NewBox(24, 250, 3))
        spawn.assert_not_called()
        creates = ["limactl", "--log-level=warn", "create"]
        (create,) = [argv for argv in ran if argv[:3] == creates]
        return create

    def test_a_named_instance_is_created_on_the_lowest_port_no_one_holds(self):
        # 49152 is bound on the host and 49153 is another instance's.
        argv = self.create(INSTANCE_B, limactl_json(49153) + "\n")
        self.assertEqual(
            argv,
            [
                "limactl",
                "--log-level=warn",
                "create",
                "--tty=false",
                "--name",
                "second",
                "--set",
                ".ssh.localPort=49154",
                "--memory",
                "24",
                "--disk",
                "250",
                "--cpus",
                "3",
                str(yo.SCRIPT_DIR / "lima" / "yolobox.yaml"),
            ],
        )

    def test_the_default_instance_keeps_the_port_its_template_fixes(self):
        argv = self.create(yo.DEFAULT_INSTANCE, "")
        self.assertNotIn("--set", argv)

    def test_a_listing_yo_cannot_read_creates_nothing(self):
        for listing in (
            "lizard\n",
            "[]\n",
            json.dumps({"config": None}) + "\n",
            limactl_json("49153") + "\n",
            limactl_json(True) + "\n",
            limactl_json() + "\n",
        ):
            with self.subTest(listing=listing):
                with self.assertRaises(yo.YoError):
                    self.create(INSTANCE_B, listing)


class TestVmUpRefusesSizeFlagsOnAnExistingBox(VmUpOnLima):
    """A box is sized when it is created; afterwards only its disk grows."""

    def up(self, size, status="Stopped"):
        return self.vm_up(INSTANCE_B, size, info=lima_info(status))

    def test_a_size_flag_on_a_box_that_exists_is_refused_before_anything(self):
        for size, flags in (
            (yo.Size(24, None, None), "--memory 24"),
            (yo.Size(None, 250, None), "--disk 250"),
            (yo.Size(16, 100, 4), "--memory 16 --disk 100 --cpus 4"),
        ):
            for status in ("Stopped", "Running"):
                with self.subTest(size=size, status=status):
                    error, stubs = self.up(size, status)
                    self.assertIn("second exists", error.message)
                    self.assertIn(
                        "%s applies only when a box is created" % flags, error.message
                    )
                    self.assertIn(
                        "yo -m second destroy --yes && yo -m second bootstrap %s."
                        % flags,
                        error.message,
                    )
                    self.assertIn("yo -m second disk-grow <GiB>", error.message)
                    self.assert_refused_before_anything(stubs)

    def test_a_stopped_box_asked_for_nothing_is_started_as_it_is(self):
        error, stubs = self.up(yo.NO_SIZE)
        self.assertIsNone(error)
        self.assertEqual(self.order, ["start"])
        ((held, source), _) = stubs["start"].call_args
        self.assertIsInstance(held, yo.state.HeldBox)
        self.assertIsInstance(source, yo.agent.PassphraseSource)
        # A box that is off may be encrypted: the key is listed before it starts.
        stubs["require_key"].assert_called_once_with()

    def test_a_running_box_asked_for_nothing_goes_on_to_the_boot_stage(self):
        with mock.patch.object(
            yo.unlock, "boot_stage", return_value=yo.Stage(yo.BootStage.UNKNOWN, "")
        ) as stage:
            error, stubs = self.up(yo.NO_SIZE, "Running")
        ((held,), _) = stage.call_args
        self.assertIsInstance(held, yo.state.HeldBox)
        self.assertIn("refused root's key", error.message)
        stubs["start"].assert_not_called()


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
            locked(yo, yo.LIMA.grow_disk, 200)
        self.assertEqual(
            run.call_args.args[0],
            [
                "limactl",
                "--log-level=warn",
                "edit",
                "--tty=false",
                "second",
                "--disk",
                "200",
            ],
        )
        self.assertEqual(
            caught.exception.message, "limactl edit refused the disk resize (exit 3)"
        )


class TestLimaShowStatus(unittest.TestCase):
    def show_status(self, returncode):
        with (
            as_instance(INSTANCE_B),
            mock.patch.object(
                yo.proc, "run", return_value=mock.Mock(returncode=returncode)
            ) as run,
        ):
            return yo.LIMA.show_status(), run

    def test_a_listing_that_worked_is_left_to_the_terminal(self):
        line, run = self.show_status(0)
        self.assertIsNone(line)
        self.assertEqual(
            run.call_args.args[0], ["limactl", "--log-level=warn", "list", "second"]
        )

    def test_a_listing_that_failed_is_a_line_with_its_exit_code(self):
        line, _ = self.show_status(3)
        self.assertIn("limactl --log-level=warn list second failed (exit 3)", line)


class TestEveryLimactlCommandIsQuiet(FakeHome):
    """limactl narrates every step at info level; yo narrates its own, so every
    limactl command it builds, start and stop and delete among them, asks for
    warnings only."""

    def test_every_command_the_driver_runs_carries_log_level_warn(self):
        argvs = []

        def run(argv, **kwargs):
            argvs.append(list(argv))
            stdout = {
                "-q": "second\n",
                "{{.Disk}}": "107374182400\n",
                "--json": limactl_json(49153) + "\n",
            }.get(argv[-1], "")
            return subprocess.CompletedProcess(argv, 0, stdout, "")

        def spawn(argv, **kwargs):
            argvs.append(list(argv))
            return mock.Mock(wait=mock.Mock(return_value=0))

        running = yo.VmStatus(yo.VmState.RUNNING, "Running")
        with (
            as_instance(INSTANCE_B),
            mock.patch.object(yo.ui, "out"),
            mock.patch.object(yo.proc, "run", side_effect=run),
            mock.patch.object(yo.proc, "spawn", side_effect=spawn),
            mock.patch.object(yo.driver, "loopback_port_bound", return_value=False),
            mock.patch.object(yo.LIMA, "refuse_fatal_gaps"),
            mock.patch.object(yo.LIMA, "wait_until_running"),
            mock.patch.object(yo.LIMA, "vm_status", return_value=running),
            mock.patch.object(yo.unlock, "unlock_disk"),
            mock.patch.object(yo.unlock, "wait_for_operator"),
        ):
            locked(yo, yo.LIMA.start, lambda: b"")
            locked(yo, yo.LIMA.stop)
            locked(yo, yo.LIMA.force_stop)
            locked(yo, yo.LIMA.delete_instance)
            locked(yo, yo.LIMA.grow_disk, 200)
            yo.LIMA.show_status()
            yo.LIMA.disk_gib()
            yo.LIMA.config_gaps()
            with mock.patch.object(yo.lima, "lima_instance_exists", return_value=False):
                locked(yo, yo.LIMA.create, yo.NewBox(24, 250, 3))
        self.assertEqual(
            {argv[2] for argv in argvs},
            {"start", "stop", "delete", "edit", "list", "create"},
        )
        for argv in argvs:
            self.assertEqual(argv[:2], ["limactl", "--log-level=warn"], argv)

    def test_the_printed_edit_chain_carries_it_too(self):
        with as_instance(INSTANCE_B):
            chain = yo.lima.lima_edit_command([".ssh.overVsock = false"])
        self.assertIn("limactl --log-level=warn edit --tty=false second --set", chain)


class TestLimaDiskGib(unittest.TestCase):
    GIB = 1024**3

    def disk_gib(self, stdout, returncode=0, stderr=""):
        with (
            as_instance(INSTANCE_B),
            mock.patch.object(
                yo.proc,
                "run",
                return_value=mock.Mock(
                    returncode=returncode, stdout=stdout, stderr=stderr
                ),
            ) as run,
        ):
            try:
                return yo.LIMA.disk_gib(), run
            except yo.YoError as failure:
                return failure, run

    def test_the_disk_is_read_in_whole_gib_from_limactls_bytes(self):
        size, run = self.disk_gib("%d\n" % (100 * self.GIB))
        self.assertEqual(size, 100)
        self.assertEqual(
            run.call_args.args[0][:5],
            ["limactl", "--log-level=warn", "list", "second", "--format"],
        )

    def test_a_disk_that_is_not_whole_gib_is_an_error_not_a_rounded_one(self):
        error, _ = self.disk_gib("%d" % (100 * self.GIB + 512))
        self.assertIsInstance(error, yo.YoError)
        self.assertIn("not a whole number of GiB", error.message)

    def test_an_answer_that_is_not_a_number_is_an_error(self):
        for stdout in ("", "1 2", "x", "-1"):
            with self.subTest(stdout=stdout):
                error, _ = self.disk_gib(stdout)
                self.assertIn("reports no disk size", error.message)

    def test_a_limactl_that_fails_says_why_instead_of_naming_a_missing_instance(self):
        error, _ = self.disk_gib("", returncode=1, stderr="no such instance: x")
        self.assertIn("limactl --log-level=warn list second --format", error.message)
        self.assertIn("failed (exit 1): no such instance: x", error.message)
        self.assertNotIn("reports no disk", error.message)


FAKE_LIMACTL = """#!/bin/sh
# A limactl whose stop fails on a stopped instance, as lima's does, and whose
# edit refuses a running one. $STATE holds the status, $LOG what was applied.
[ "$1" = --log-level=warn ] || { echo "limactl without --log-level=warn: $*" >&2; exit 1; }
shift
case "$1" in
  stop)
    [ "$(cat "$STATE")" = Running ] || { echo "instance is already stopped" >&2; exit 1; }
    [ -z "$STOP_FAILS" ] || { echo "stop: timed out" >&2; exit 1; }
    echo Stopped > "$STATE" ;;
  edit)
    [ "$2" = --tty=false ] || { echo "edit would open an editor" >&2; exit 1; }
    [ "$(cat "$STATE")" = Running ] && { echo "cannot edit a running instance" >&2; exit 1; }
    printf '%s\\n' "$5" >> "$LOG" ;;
  *) echo "unexpected limactl $*" >&2; exit 1 ;;
esac
"""

FAKE_YO = """#!/bin/sh
# What yo down and yo up do to limactl, for the machine named or the default.
[ "$1" = -m ] && shift 2
case "$1" in
  down) [ "$(cat "$STATE")" = Running ] && exec limactl --log-level=warn stop "$NAME"; exit 0 ;;
  up) echo up >> "$LOG" ;;
  *) echo "unexpected yo $*" >&2; exit 1 ;;
esac
"""


class TestTheEditRemedyRunsFromEitherState(unittest.TestCase):
    """The printed `yo down && limactl edit ... && yo up` chain, run by a shell
    against a limactl that fails to stop a stopped instance."""

    def run_remedy(self, name, status, stop_fails=False):
        bin_dir, path = fake_bin(self, {"limactl": FAKE_LIMACTL, "yo": FAKE_YO})
        state_file = os.path.join(bin_dir, "state")
        log = os.path.join(bin_dir, "log")
        with open(state_file, "w") as handle:
            handle.write(status + "\n")
        with as_instance(name):
            remedy = yo.lima.lima_edit_command([".ssh.overVsock = false"])
        env = {
            "PATH": path,
            "STATE": state_file,
            "LOG": log,
            "NAME": name,
            "STOP_FAILS": "1" if stop_fails else "",
        }
        completed = subprocess.run(
            ["sh", "-c", remedy], env=env, capture_output=True, text=True
        )
        if not os.path.exists(log):
            return completed, []
        with open(log) as applied:
            return completed, applied.read().splitlines()

    def test_a_stopped_or_running_box_is_edited_and_started_again(self):
        for name in (yo.DEFAULT_INSTANCE, INSTANCE_B):
            for status in ("Stopped", "Running"):
                with self.subTest(name=name, status=status):
                    completed, applied = self.run_remedy(name, status)
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertEqual(applied, [".ssh.overVsock = false", "up"])

    def test_a_stop_that_fails_ends_the_chain_before_the_edit(self):
        completed, applied = self.run_remedy(INSTANCE_B, "Running", stop_fails=True)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(applied, [])


class TestAConfigGapRecreatesAnInstanceByBootstrap(FakeHome):
    def test_destroy_then_bootstrap_which_creates_it(self):
        with as_instance(INSTANCE_B):
            message = yo.LIMA.config_gap_message([yo.GAP_OPERATOR_UID])
        self.assertIn("yo -m second destroy --yes && yo -m second bootstrap", message)

    def test_the_default_instance_is_recreated_by_bootstrap_alone(self):
        self.assertEqual(
            yo.LIMA.recreate_command(yo.NO_SIZE),
            "recreate the box, which deletes everything on it: "
            "yo destroy --yes && yo bootstrap",
        )


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
        with as_instance("box2"):
            self.assertEqual(yo.yo_cmd("up"), "yo -m box2 up")
            self.assertIn("yo -m box2 bootstrap", yo.version_drift(A_RELEASE, "0.9.0"))


if __name__ == "__main__":
    unittest.main()
