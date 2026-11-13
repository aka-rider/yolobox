import argparse
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from shell_harness import needs_tool
from test_yo import (
    A_RELEASE,
    FakeHome,
    Reached,
    FakeHelper,
    NOT_LISTENING,
    VirtualClock,
    box_lock_held_elsewhere,
    box_lock_is_held,
    df_output,
    guest_words,
    fake_bin,
    AGENT_LOGIN,
    hostagent_holds_the_port,
    locked,
    proved_initrd,
    ssh_ends,
    ssh_log_of,
    yo,
    refuse_tcp_to_the_host,
)


def setUpModule():
    refuse_tcp_to_the_host()


@contextlib.contextmanager
def env_without(*names):
    with mock.patch.dict(os.environ, {}, clear=False):
        for name in names:
            os.environ.pop(name, None)
        yield


class TestFlakeRef(unittest.TestCase):
    def test_a_build_stamped_with_its_commit_builds_that_commit_not_its_version(self):
        # A `nix run github:aka-rider/yolobox` yo is built from main while its
        # .version names the last release; building the tag would skew the box.
        rev = "0" * 40
        with (
            mock.patch.object(yo.config, "YO_VERSION", A_RELEASE),
            mock.patch.object(yo.config, "YO_SOURCE_REV", rev),
        ):
            self.assertEqual(yo.flake_ref(None), "github:aka-rider/yolobox/" + rev)
            self.assertEqual(yo.flake_ref("1.9.2"), "github:aka-rider/yolobox/v1.9.2")
        with (
            mock.patch.object(yo.config, "YO_VERSION", "dev"),
            mock.patch.object(yo.config, "YO_SOURCE_REV", rev),
        ):
            self.assertEqual(yo.flake_ref(None), "github:aka-rider/yolobox/" + rev)

    def test_dev_build_without_a_version_refuses_with_no_pinned_flake(self):
        with mock.patch.object(yo.config, "YO_VERSION", "dev"):
            with self.assertRaises(yo.YoError) as caught:
                yo.flake_ref(None)
        self.assertIn("--version", caught.exception.message)

    def test_released_build_without_a_version_pins_its_own_tag(self):
        with mock.patch.object(yo.config, "YO_VERSION", A_RELEASE):
            self.assertEqual(
                yo.flake_ref(None), "github:aka-rider/yolobox/v" + A_RELEASE
            )

    def test_a_release_is_pinned_with_or_without_the_v(self):
        for version in ("1.9.2", "v1.9.2", "0.0.1", "2.0.0", "v2.0.0"):
            with self.subTest(version=version):
                self.assertEqual(
                    yo.flake_ref(version),
                    "github:aka-rider/yolobox/v" + version.lstrip("v"),
                )

    def test_a_branch_or_tag_is_taken_from_the_upstream_repo(self):
        for ref in ("main", "my-branch", "v1.7.2-rc1", "2026-fix"):
            with self.subTest(ref=ref):
                self.assertEqual(yo.flake_ref(ref), "github:aka-rider/yolobox/" + ref)

    def test_a_github_flake_ref_with_owner_repo_and_ref_is_taken_verbatim(self):
        for ref in (
            "github:x/y/z",
            "github:you/yolobox/my-branch",
            "github:a.b/c_d/v1.2",
        ):
            with self.subTest(ref=ref):
                self.assertEqual(yo.flake_ref(ref), ref)

    def test_a_version_that_cannot_be_a_ref_is_refused(self):
        for version, reason in (
            ("", "--version must be"),
            ("1.7", "a release is X.Y.Z"),
            ("v2", "a release is X.Y.Z"),
            ("latest", "there is no latest ref"),
            ("~/yolobox", "--version must be"),
            ("./yolobox", "--version must be"),
            ("a b", "--version must be"),
            ("-x", "--version must be"),
        ):
            with self.subTest(version=version):
                with self.assertRaisesRegex(yo.YoError, reason):
                    yo.flake_ref(version)

    def test_digits_of_other_scripts_are_not_a_release(self):
        for version in ("١.٢.٣", "v١.٢.٣", "١", "1.٢"):
            with self.subTest(version=version):
                with self.assertRaisesRegex(yo.YoError, "--version must be"):
                    yo.flake_ref(version)

    def test_a_ref_the_agent_could_control_is_refused(self):
        # The box runs `sudo nixos-rebuild --flake <ref>#yolobox`, and the agent
        # owns every path in the guest and can push to any checkout there.
        for ref in (
            "/home/agent/yolobox",
            "path:/home/agent/yolobox",
            "file:///home/agent/yolobox",
            "git+file:///home/agent/yolobox",
            "git+ssh://host/x/y",
            "github:x/y",
            "github:x",
            "github:x/y/z/w",
            "github:x/y/",
            "github://y/z",
            "github:x/y/-z",
            "github:x/y/z#evil",
            "github:x/y/z?dir=sub",
            "github:x/y/z\n",
            "github:x y/z/w",
            "main#x",
            "my/branch",
            "main\n",
            "1.7.2\n",
        ):
            with self.subTest(ref=ref):
                with self.assertRaisesRegex(yo.YoError, "--version must be|release is"):
                    yo.flake_ref(ref)

    def test_a_sharp_in_the_version_is_a_usage_error_not_a_second_attribute(self):
        with self.assertRaisesRegex(yo.YoError, "--version must be"):
            yo.flake_ref("v1.7.2#x")


class TestRequireAgentAccount(unittest.TestCase):
    """The agent account is usable when its login runs a command. When it is
    not, the box's version stamp, read as the operator, names why: a box not
    bootstrapped yet, or a bootstrapped one that is broken. A box out of reach
    is Unreachable, never a broken account."""

    def require(self, code, stderr="", stamp=(0, "present\n", ""), session=""):
        """The agent's login ends with `code`, ssh saying `stderr` (on a 255)
        and the session printing `session` on its stderr; `stamp` is how the
        operator's stamp probe ends: (exit, stdout, stderr)."""

        def answer(role, argv, **kwargs):
            if role is yo.OPERATOR:
                return yo.proc.GuestRun(yo.sshx.STAMP_PROBE, *stamp)
            stdout, said = ssh_ends(argv, kwargs["ssh_log"], code, stderr=stderr)
            return yo.proc.GuestRun(argv, code, stdout, said + session)

        with mock.patch.object(yo.sshx, "ssh_run", side_effect=answer) as probe:
            yo.sshx.require_agent_account()
        return probe

    def refusal(self, *args, **kwargs):
        with self.assertRaises(yo.YoError) as caught:
            self.require(*args, **kwargs)
        return caught.exception

    def test_an_agent_whose_login_runs_passes_and_reads_no_stamp(self):
        probe = self.require(0)
        self.assertEqual([c.args[0] for c in probe.call_args_list], [yo.AGENT])

    def test_a_login_shell_that_fails_its_command_is_named_the_agents_shell(self):
        # A 255 with nothing in ssh's own log is the command's own, or a signal's.
        for code, session in (
            (1, "rc: boom\n"),
            (127, ""),
            (64, "lizard\n"),
            (yo.SSH_UNREACHABLE, ""),
        ):
            with self.subTest(code=code):
                message = self.refusal(code, session=session).message
                self.assertIn(
                    "yolobox is bootstrapped, but the agent's login shell does not "
                    "run `echo yolobox-agent-login`: the agent's own rc files, or "
                    "the box's build; look with yo ssh, or rebuild with yo bootstrap",
                    message,
                )
                self.assertIn("its session exited %d" % code, message)
                if session:
                    self.assertIn("  | " + session.strip(), message)
                self.assertNotIn("destroy", message)

    def test_rc_files_that_print_sshds_refusal_and_exit_255_are_the_agents_shell(self):
        # The words reach the session's stderr, never ssh's own log.
        message = self.refusal(
            yo.SSH_UNREACHABLE,
            session="agent@127.0.0.1: Permission denied (publickey).\n",
        ).message
        self.assertIn("the agent's login shell does not run", message)
        self.assertNotIn("sshd refuses", message)

    def test_a_log_line_that_only_contains_the_refusal_is_the_box_out_of_reach(self):
        for said in (
            "ssh: lizard agent@127.0.0.1: Permission denied (publickey).\n",
            "agent@127.0.0.1: Permission denied (publickey). lizard\n",
            "agent@127.0.0.1: Permission denied\n",
        ):
            with self.subTest(said=said):
                with self.assertRaises(yo.sshx.Unreachable):
                    self.require(yo.SSH_UNREACHABLE, said)

    def test_a_login_that_runs_but_prints_another_word_is_the_agents_shell(self):
        def answer(role, argv, **kwargs):
            return yo.proc.GuestRun(argv, 0, "lizard\n", "")

        with mock.patch.object(yo.sshx, "ssh_run", side_effect=answer):
            problem = yo.sshx.broken_agent_login()
        self.assertIn("the agent's login shell does not run", problem)
        self.assertIn("  | lizard", problem)

    def test_a_refused_agent_key_on_a_bootstrapped_box_names_bootstrap(self):
        message = self.refusal(
            yo.SSH_UNREACHABLE, "agent@127.0.0.1: Permission denied (publickey).\r\n"
        ).message
        self.assertIn(
            "yolobox is bootstrapped, but its sshd refuses the agent's login; run yo "
            "bootstrap, which declares the account again",
            message,
        )
        self.assertIn("Permission denied (publickey)", message)

    def test_a_box_ssh_cannot_reach_is_unreachable_never_a_broken_account(self):
        lost = "ssh: connect to host 127.0.0.1: Connection refused\n"
        with self.assertRaises(yo.sshx.Unreachable) as caught:
            self.require(yo.SSH_UNREACHABLE, lost)
        self.assertIn("cannot reach yolobox as agent", caught.exception.message)
        self.assertIn("Connection refused", caught.exception.message)
        self.assertNotIn("bootstrap", caught.exception.message)

    def test_a_box_without_the_stamp_is_not_bootstrapped_and_quotes_nothing(self):
        refused = self.refusal(
            yo.SSH_UNREACHABLE,
            "agent@127.0.0.1: Permission denied (publickey).\n",
            stamp=(0, "absent\n", ""),
        )
        self.assertEqual(
            refused.message,
            "yolobox is not bootstrapped yet: it has no agent account, and nothing "
            "is forwarded into it; build it with: yo bootstrap",
        )

    def test_a_stamp_probe_that_fails_or_says_anything_else_is_an_error(self):
        # An rc file that exits, or prints, must not read as "not bootstrapped".
        for stamp in (
            (1, "", "zsh: lizard\n"),
            (0, "", ""),
            (0, "present\nabsent\n", ""),
            (0, "lizard\n", ""),
        ):
            with self.subTest(stamp=stamp):
                refused = self.refusal(1, session="rc: boom\n", stamp=stamp)
                self.assertNotIn("not bootstrapped yet", refused.message)
                self.assertNotIn("is bootstrapped, but", refused.message)

    def test_a_box_out_of_reach_is_unreachable_not_a_broken_account(self):
        lost = (yo.SSH_UNREACHABLE, "ssh: connect to host: Connection refused\n")
        with self.assertRaisesRegex(yo.Unreachable, "is it up\\? yo status"):
            self.require(*lost, stamp=(lost[0], "", lost[1]))


class TestMainPreflight(unittest.TestCase):
    def test_version_flag_skips_the_limactl_preflight(self):
        with mock.patch.object(yo.proc, "require_tool", side_effect=Reached):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    yo.main(["--version"])
        self.assertEqual(caught.exception.code, 0)

    def test_help_flag_skips_the_limactl_preflight(self):
        with mock.patch.object(yo.proc, "require_tool", side_effect=Reached):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    yo.main(["--help"])
        self.assertEqual(caught.exception.code, 0)

    def test_help_works_in_a_fresh_process_with_no_machine_chosen(self):
        launcher = str(yo.REPO / "yo")
        for argv in (["--help"], ["help", "code"], ["zed", "--help"]):
            with self.subTest(argv=argv):
                done = subprocess.run(
                    [sys.executable, "-I", launcher, *argv],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertIn('"%s"' % yo.SSH_CONFIG_INCLUDE_LINE, done.stdout)

    def test_the_editor_help_names_the_one_line_for_every_machine_on_both_hosts(self):
        for platform in ("darwin", "linux"):
            with self.subTest(platform=platform):
                with mock.patch.object(yo.config, "HOST_PLATFORM", platform):
                    for command in ("code", "zed"):
                        help_text = " ".join(self.help_text(["help", command]).split())
                        self.assertIn(yo.SSH_CONFIG_INCLUDE_LINE, help_text)

    def help_text(self, argv):
        """What `yo.main(argv)` prints, however it ends, with no limactl preflight."""
        buf = io.StringIO()
        with mock.patch.object(yo.proc, "require_tool", side_effect=AssertionError):
            with contextlib.redirect_stdout(buf):
                try:
                    self.assertEqual(yo.main(argv), 0)
                except SystemExit as caught:
                    self.assertEqual(caught.code, 0)
        return buf.getvalue().strip()

    def test_help_command_prints_the_top_level_help(self):
        self.assertEqual(self.help_text(["help"]), self.help_text(["--help"]))

    def test_help_command_prints_a_subcommands_help(self):
        self.assertEqual(
            self.help_text(["help", "bootstrap"]),
            self.help_text(["bootstrap", "--help"]),
        )

    def test_help_command_keeps_the_colors_the_help_flag_prints(self):
        with mock.patch.dict(os.environ, {"FORCE_COLOR": "1"}):
            os.environ.pop("NO_COLOR", None)
            os.environ.pop("PYTHON_COLORS", None)
            via_help = self.help_text(["help", "bootstrap"])
            via_flag = self.help_text(["bootstrap", "--help"])
        self.assertEqual(via_help, via_flag)
        self.assertNotIn("?[", via_help)

    def test_help_of_an_unknown_command_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                yo.main(["help", "bogus"])
        self.assertEqual(caught.exception.code, 2)

    def test_help_of_every_command_exits_zero(self):
        commands = [
            "up",
            "down",
            "status",
            "bootstrap",
            "gc",
            "disk-grow",
            "ssh",
            "enter",
            "cp",
            "code",
            "zed",
            "port",
            "destroy",
            "aws-check",
            "help",
        ]
        with mock.patch.object(yo.proc, "require_tool", side_effect=AssertionError):
            with contextlib.redirect_stdout(io.StringIO()):
                for name in commands:
                    with self.subTest(command=name):
                        self.assertEqual(yo.main(["help", name]), 0)

    def test_a_version_that_cannot_be_a_ref_is_a_usage_error_before_the_preflight(
        self,
    ):
        for version in ("1.7", "latest"):
            with self.subTest(version=version):
                with mock.patch.object(
                    yo.proc, "require_tool", side_effect=AssertionError("preflight ran")
                ):
                    with contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit) as caught:
                            yo.main(["bootstrap", "--version", version])
                self.assertEqual(caught.exception.code, 2)

    def test_a_host_account_the_box_cannot_name_is_refused_before_the_preflight(
        self,
    ):
        for username, said in (
            ("Bob", "the host account name 'Bob' cannot name"),
            ("agent", "which is the box's agent account"),
            ("", "the host account name '' cannot name"),
            ("bob\n", "cannot name"),
        ):
            with self.subTest(username=username):
                with (
                    mock.patch.object(yo.release, "host_user", return_value=username),
                    mock.patch.object(
                        yo.proc,
                        "require_tool",
                        side_effect=AssertionError("preflight ran"),
                    ),
                ):
                    with self.assertRaises(yo.YoError) as caught:
                        yo.main(["status"])
                self.assertIn(said, caught.exception.message)

    def test_known_command_runs_the_preflight_before_the_command(self):
        def raise_reached(args):
            raise Reached()

        with mock.patch.object(
            yo.proc, "require_tool", side_effect=yo.YoError("limactl not found")
        ):
            with mock.patch.object(yo.status, "cmd_status", side_effect=raise_reached):
                with self.assertRaisesRegex(yo.YoError, "limactl not found"):
                    yo.main(["status"])

    def test_no_arguments_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                yo.main([])
        self.assertEqual(caught.exception.code, 2)

    def test_an_abbreviated_flag_is_a_usage_error(self):
        for argv in (["gc", "--y"], ["gc", "--de"], ["--vers"]):
            with self.subTest(argv=argv):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        yo.main(argv)
                self.assertEqual(caught.exception.code, 2)

    def test_unknown_command_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                yo.main(["bogus"])
        self.assertEqual(caught.exception.code, 2)

    def derived_names_seen_by_status(self, argv):
        seen = {}

        def record(args):
            seen.update(instance=yo.box.name, vm_host=yo.box.vm_host)

        try:
            with mock.patch.object(yo.proc, "require_tool"):
                with mock.patch.object(yo.status, "cmd_status", side_effect=record):
                    yo.main(argv)
        finally:
            yo.set_box(yo.box.driver, yo.DEFAULT_INSTANCE)
        return seen

    def test_machine_flag_sets_every_derived_name(self):
        seen = self.derived_names_seen_by_status(["-m", "box2", "status"])
        self.assertEqual(seen["instance"], "box2")
        self.assertEqual(seen["vm_host"], "lima-box2")

    def test_machine_flag_is_accepted_after_the_command(self):
        seen = self.derived_names_seen_by_status(["status", "-m", "box2"])
        self.assertEqual(seen["instance"], "box2")
        self.assertEqual(seen["vm_host"], "lima-box2")

    def test_the_last_machine_flag_wins(self):
        seen = self.derived_names_seen_by_status(["-m", "box1", "status", "-m", "box2"])
        self.assertEqual(seen["instance"], "box2")

    def test_machine_flag_before_the_command_survives_the_subparser(self):
        seen = self.derived_names_seen_by_status(["--machine", "box2", "status"])
        self.assertEqual(seen["instance"], "box2")

    def test_machine_flag_before_ssh_leaves_the_guest_command_intact(self):
        args = yo.build_parser().parse_args(["ssh", "-m", "box2", "sudo", "ls", "-m"])
        self.assertEqual(args.machine, "box2")
        self.assertEqual(args.ssh_cmd, ["sudo", "ls", "-m"])

    def test_abbreviated_machine_flag_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                yo.main(["--mach", "box2", "status"])
        self.assertEqual(caught.exception.code, 2)


class TestMainEnsuresTheSessionBeforeSomeCommands(unittest.TestCase):
    # The commands that talk to the guest first declare the box's services
    # whole, as `yo up` does: the master, its forwards and the broker, so any of
    # them restores what died. The ones that stop, describe, clean or only
    # forward a port never do, and up/bootstrap/disk-grow/aws-check run
    # vm_services inside their own flow.
    SESSION = [
        (["ssh"], (yo.session, "cmd_ssh")),
        (["enter"], (yo.session, "cmd_enter")),
        (["cp", ":a", "b"], (yo.cp, "cmd_cp")),
        (["code"], (yo.session, "cmd_code")),
        (["zed"], (yo.session, "cmd_zed")),
    ]
    NO_SESSION = [
        (["down"], (yo.vm, "cmd_down")),
        (["status"], (yo.status, "cmd_status")),
        (["gc"], (yo.vm, "cmd_gc")),
        (["port", "8080"], (yo.session, "cmd_port")),
        (["aws-check"], (yo.session, "cmd_aws_check")),
        (["up"], (yo.vm, "vm_up")),
        (["bootstrap"], (yo.vm, "cmd_bootstrap")),
        (["disk-grow", "40"], (yo.vm, "cmd_disk_grow")),
        (["help"], (argparse.ArgumentParser, "print_help")),
    ]

    def run_main(self, argv, target, services=None, bootstrapped=True):
        calls = []

        def declare():
            calls.append("services")
            if services is not None:
                services()

        with (
            mock.patch.object(yo.proc, "require_tool"),
            mock.patch.object(yo.sshx, "bootstrapped", return_value=bootstrapped),
            mock.patch.object(yo.forwards, "vm_services", side_effect=declare),
            mock.patch.object(
                *target, side_effect=lambda *a, **k: calls.append("command")
            ),
        ):
            try:
                yo.main(argv)
            finally:
                yo.set_box(yo.box.driver, yo.DEFAULT_INSTANCE)
        return calls

    def test_a_command_that_talks_to_the_guest_declares_the_services_first(self):
        for argv, target in self.SESSION:
            with self.subTest(command=argv[0]):
                self.assertEqual(self.run_main(argv, target), ["services", "command"])

    def test_the_other_commands_open_no_session_in_main(self):
        for argv, target in self.NO_SESSION:
            with self.subTest(command=argv[0]):
                self.assertEqual(self.run_main(argv, target), ["command"])

    def test_a_box_not_bootstrapped_yet_gets_no_services_and_no_warning(self):
        for argv, target in self.SESSION:
            with self.subTest(command=argv[0]):
                with mock.patch.object(yo.ui, "err") as said:
                    calls = self.run_main(argv, target, bootstrapped=False)
                self.assertEqual(calls, ["command"])
                said.assert_not_called()

    def test_a_box_that_is_not_up_is_said_so_not_blamed_on_the_forwards(self):
        def lost():
            raise yo.sshx.Unreachable("cannot reach yolobox — is it up? yo status")

        with mock.patch.object(yo.ui, "err") as said:
            calls = self.run_main(["ssh"], (yo.session, "cmd_ssh"), services=lost)
        self.assertEqual(calls, ["services", "command"])
        (warning,), _ = said.call_args
        self.assertIn("yolobox does not take the operator's login", warning)
        self.assertIn("is it up?", warning)
        self.assertNotIn("runs without what is not up", warning)

    def test_services_that_fail_are_said_and_the_command_still_runs(self):
        # `yo ssh` is the way into a box whose forwards are broken.
        def broken():
            raise yo.YoError("ssh -O forward failed (exit 255)")

        for argv, target in self.SESSION:
            with self.subTest(command=argv[0]):
                with mock.patch.object(yo.ui, "err") as said:
                    calls = self.run_main(argv, target, services=broken)
                self.assertEqual(calls, ["services", "command"])
                (warning,), _ = said.call_args
                self.assertIn("this session runs without what is not up", warning)
                self.assertIn("ssh -O forward failed (exit 255)", warning)
                self.assertNotIn("1Password and AWS", warning)


class TestSshRemainderPassthrough(unittest.TestCase):
    # `yo ssh sudo ... nixos-rebuild switch --impure --flake '<ref>#yolobox'` is
    # a command an operator pastes; argparse must not eat --impure or --flake
    # off that line.
    def parse(self, argv):
        return yo.build_parser().parse_args(argv)

    def test_a_pasted_nixos_rebuild_round_trips_intact(self):
        args = self.parse(
            [
                "ssh",
                "sudo",
                "YOLOBOX_USERNAME=x",
                "YOLOBOX_FLAKE_REF=github:x/y/z",
                "nixos-rebuild",
                "switch",
                "--impure",
                "--flake",
                ".#yolobox",
            ]
        )
        self.assertEqual(
            args.ssh_cmd,
            [
                "sudo",
                "YOLOBOX_USERNAME=x",
                "YOLOBOX_FLAKE_REF=github:x/y/z",
                "nixos-rebuild",
                "switch",
                "--impure",
                "--flake",
                ".#yolobox",
            ],
        )

    def test_a_plain_remote_command_round_trips_intact(self):
        args = self.parse(["ssh", "journalctl", "--user", "-u", "herdr", "-n", "50"])
        self.assertEqual(
            args.ssh_cmd, ["journalctl", "--user", "-u", "herdr", "-n", "50"]
        )

    def test_no_remote_command_is_an_empty_list(self):
        args = self.parse(["ssh"])
        self.assertEqual(args.ssh_cmd, [])


class TestUpRejectsExtraArguments(unittest.TestCase):
    def test_extra_positional_argument_exits_2(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                yo.build_parser().parse_args(["up", "bogus"])
        self.assertEqual(caught.exception.code, 2)


class TestEnterProjectDistinguishesEmptyFromAbsent(unittest.TestCase):
    def test_no_project_argument_parses_to_none(self):
        args = yo.build_parser().parse_args(["enter"])
        self.assertIsNone(args.project)

    def test_empty_string_project_argument_parses_to_empty_string(self):
        args = yo.build_parser().parse_args(["enter", ""])
        self.assertEqual(args.project, "")

    def test_target_dir_picks_a_project_only_when_one_was_given(self):
        with mock.patch.object(
            yo.session, "pick_project", return_value="picked"
        ) as fake_pick:
            with mock.patch.object(yo.paths, "dir_for_cwd") as fake_cwd:
                self.assertEqual(yo.target_dir(""), "picked")
        fake_pick.assert_called_once_with("")
        fake_cwd.assert_not_called()

    def test_target_dir_falls_back_to_the_cwd_when_no_project_was_given(self):
        with mock.patch.object(yo.session, "pick_project") as fake_pick:
            with mock.patch.object(
                yo.paths, "dir_for_cwd", return_value="cwd"
            ) as fake_cwd:
                self.assertEqual(yo.target_dir(None), "cwd")
        fake_pick.assert_not_called()
        fake_cwd.assert_called_once_with()


class TestHelpExitsZero(unittest.TestCase):
    def test_top_level_help_exits_zero(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                yo.build_parser().parse_args(["--help"])
        self.assertEqual(caught.exception.code, 0)

    def test_gc_help_exits_zero(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                yo.build_parser().parse_args(["gc", "--help"])
        self.assertEqual(caught.exception.code, 0)


class TestBootstrapRebootDecision(FakeHome):
    def setUp(self):
        super().setUp()
        # Resolving the ref is TestBootstrapWarnsBeforeEncryptingInPlace's.
        pinned = mock.patch.object(
            yo.release,
            "locked_rev",
            return_value=yo.release.LockedSource("0" * 40, "sha256-" + "A" * 43 + "="),
        )
        pinned.start()
        self.addCleanup(pinned.stop)
        for module, name in (
            (yo.vm, "vm_up"),
            (yo.vm, "vm_restart"),
            (yo.forwards, "vm_services"),
            (yo.forwards, "ensure_herdr_machine"),
            (yo.guest, "ensure_guest_known_hosts"),
        ):
            patcher = mock.patch.object(module, name)
            patcher.start()
            self.addCleanup(patcher.stop)

        running_patcher = mock.patch.object(
            yo.LIMA,
            "vm_status",
            return_value=yo.VmStatus(yo.VmState.RUNNING, "Running"),
        )
        running_patcher.start()
        self.addCleanup(running_patcher.stop)

        fstype_patcher = mock.patch.object(
            yo.vm, "luks_device_fstype", return_value="crypto_LUKS"
        )
        fstype_patcher.start()
        self.addCleanup(fstype_patcher.stop)

        ssh_run_patcher = mock.patch.object(
            yo.sshx, "ssh_run", side_effect=self.agent_logs_in
        )
        ssh_run_patcher.start()
        self.addCleanup(ssh_run_patcher.stop)

        include_patcher = mock.patch.object(
            yo.sshconfig, "ssh_config_reaches_box", return_value=True
        )
        include_patcher.start()
        self.addCleanup(include_patcher.stop)

        out_patcher = mock.patch.object(yo.ui, "out")
        out_patcher.start()
        self.addCleanup(out_patcher.stop)

        err_patcher = mock.patch.object(yo.ui, "err")
        err_patcher.start()
        self.addCleanup(err_patcher.stop)

    @staticmethod
    def agent_logs_in(role, argv, **kwargs):
        stdout, _ = ssh_ends(argv, None, 0)
        return mock.Mock(returncode=0, stdout=stdout, stderr="")

    def run_bootstrap_default(self):
        args = yo.build_parser().parse_args(["bootstrap"])
        with mock.patch.object(yo.vm, "guest_generations", return_value=("g1", "g1")):
            with mock.patch.object(yo.proc, "run"):
                yo.cmd_bootstrap(args)

    def run_bootstrap_with_real_generations(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            return mock.Mock(returncode=0, stdout="", stderr="")

        args = yo.build_parser().parse_args(["bootstrap", "--version", "github:x/y/z"])
        with mock.patch.object(yo.proc, "run", side_effect=fake_run):
            yo.cmd_bootstrap(args)
        return calls

    def run_bootstrap(self, generations):
        with mock.patch.object(yo.vm, "guest_generations", side_effect=generations):
            return self.run_bootstrap_with_real_generations()

    def test_a_box_without_the_helper_still_reaches_its_reboot(self):
        # The plain nixos-lima image a first bootstrap starts from has no
        # yolobox-guest; the generations are read over plain ssh.
        reads = iter(["/store/g1\n/store/g0\n", "/store/g1\n/store/g1\n"])
        asked = []

        def fake_ssh_run(role, argv, **kwargs):
            asked.append(list(argv))
            if argv[0] == yo.HELPER:
                return mock.Mock(
                    returncode=127, stdout="", stderr="%s: not found" % yo.HELPER
                )
            if argv[0] == "sh":
                return mock.Mock(returncode=0, stdout=next(reads), stderr="")
            return self.agent_logs_in(role, argv)

        yo.ssh_run.side_effect = fake_ssh_run
        calls = self.run_bootstrap_with_real_generations()
        self.assertEqual(calls, [["limactl", "--log-level=warn", "stop", "yolobox"]])
        self.assertFalse([argv for argv in asked if argv[0] == yo.HELPER])

    def test_no_restart_when_already_running_the_built_generation(self):
        calls = self.run_bootstrap([("g1", "g1")])
        self.assertNotIn(["limactl", "--log-level=warn", "restart", "yolobox"], calls)

    def test_stops_exactly_once_then_starts_again_when_the_second_read_agrees(self):
        calls = self.run_bootstrap([("g1", "g0"), ("g1", "g1")])
        self.assertEqual(calls, [["limactl", "--log-level=warn", "stop", "yolobox"]])
        yo.vm_up.assert_called_once_with(
            mock.ANY, run_services=False, size=yo.NO_SIZE, command="bootstrap"
        )
        # The reboot carries the source the first start made, and no size.
        yo.vm_restart.assert_called_once_with(
            mock.ANY, yo.vm_up.return_value, run_services=False
        )

    def test_the_size_asked_for_reaches_the_first_start_and_the_reboot_has_none(self):
        args = yo.build_parser().parse_args(
            [
                "bootstrap",
                "--version",
                "github:x/y/z",
                "--memory",
                "24",
                "--disk",
                "250",
            ]
        )
        with (
            mock.patch.object(yo.LIMA, "disk_gib", return_value=250),
            mock.patch.object(yo.vm, "grow_disk"),
            mock.patch.object(
                yo.vm, "guest_generations", side_effect=[("g1", "g0"), ("g1", "g1")]
            ),
            mock.patch.object(yo.proc, "run"),
            mock.patch.object(yo.LIMA, "stop"),
        ):
            yo.cmd_bootstrap(args)
        ((held,), kwargs) = yo.vm_up.call_args
        self.assertEqual(kwargs["size"], yo.Size(memory_gib=24, disk_gib=250))
        # The reboot is the same box at the same size a moment later: there is
        # no size to give it, and the host's free memory is not judged again.
        ((_, _), restart_kwargs) = yo.vm_restart.call_args
        self.assertEqual(restart_kwargs, {"run_services": False})

    def test_a_larger_disk_for_a_box_that_exists_is_grown_after_the_reboot_and_before_the_services(
        self,
    ):
        order = []
        source = yo.agent.PassphraseSource()
        source.derived = bytearray(b"k" * 64)
        for module, name in (
            (yo.vm, "grow_disk"),
            (yo.forwards, "ensure_herdr_machine"),
            (yo.forwards, "vm_services"),
            (yo.vm, "vm_up"),
            (yo.vm, "vm_restart"),
        ):
            patcher = mock.patch.object(
                module,
                name,
                side_effect=lambda *a, _n=name, **k: order.append(_n) or source,
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        args = yo.build_parser().parse_args(
            ["bootstrap", "--version", "github:x/y/z", "--disk", "250"]
        )
        with (
            mock.patch.object(yo.LIMA, "disk_gib", return_value=100),
            mock.patch.object(
                yo.vm, "guest_generations", side_effect=[("g1", "g0"), ("g1", "g1")]
            ),
            mock.patch.object(yo.proc, "run"),
            mock.patch.object(yo.LIMA, "stop"),
        ):
            yo.cmd_bootstrap(args)
        self.assertEqual(
            order,
            [
                "vm_up",
                "vm_restart",
                "grow_disk",
                "ensure_herdr_machine",
                "vm_services",
            ],
        )
        ((held, growth), kwargs) = yo.vm.grow_disk.call_args
        self.assertIsInstance(held, yo.state.HeldBox)
        self.assertEqual(growth, yo.DiskGrowth(wanted=250, current=100))
        # The source the first vm_up made, so the passphrase derives once.
        self.assertEqual(kwargs, {"passphrase": source, "run_services": False})

    def test_a_box_that_does_not_exist_is_created_at_its_disk_and_not_grown(self):
        args = yo.build_parser().parse_args(
            ["bootstrap", "--version", "github:x/y/z", "--disk", "100"]
        )
        with (
            mock.patch.object(yo.LIMA, "vm_status", return_value=None),
            mock.patch.object(yo.LIMA, "disk_gib") as disk,
            mock.patch.object(yo.vm, "grow_disk") as grow,
            mock.patch.object(yo.vm, "guest_generations", return_value=("g1", "g1")),
            mock.patch.object(yo.proc, "run"),
        ):
            yo.cmd_bootstrap(args)
        grow.assert_not_called()
        disk.assert_not_called()

    def test_the_disk_the_box_has_still_runs_the_roots_growth_and_a_smaller_one_is_refused(
        self,
    ):
        for disk, wanted in ((100, "100"), (200, "50")):
            with self.subTest(disk=disk, wanted=wanted):
                args = yo.build_parser().parse_args(
                    ["bootstrap", "--version", "github:x/y/z", "--disk", wanted]
                )
                yo.vm_up.reset_mock()
                with (
                    mock.patch.object(yo.LIMA, "disk_gib", return_value=disk),
                    mock.patch.object(yo.vm, "grow_disk") as grow,
                    mock.patch.object(
                        yo.vm, "guest_generations", return_value=("g1", "g1")
                    ),
                    mock.patch.object(yo.proc, "run"),
                ):
                    if wanted == "50":
                        with self.assertRaisesRegex(yo.YoError, "ask for at least 200"):
                            yo.cmd_bootstrap(args)
                        grow.assert_not_called()
                        yo.vm_up.assert_not_called()
                    else:
                        yo.cmd_bootstrap(args)
                        ((_, growth), _) = grow.call_args
                        self.assertEqual(growth, yo.DiskGrowth(100, 100))

    def test_the_passphrase_is_derived_before_the_vm_stops(self):
        # A missing 1Password key must leave the running box alone.
        yo.vm_up.return_value = yo.agent.PassphraseSource()
        order = []
        with (
            mock.patch.object(
                yo.agent,
                "luks_passphrase",
                side_effect=lambda: order.append("passphrase") or b"a" * 64,
            ),
            mock.patch.object(
                yo.LIMA, "stop", side_effect=lambda held: order.append("stop")
            ),
        ):
            self.run_bootstrap([("g1", "g0"), ("g1", "g1")])
        self.assertEqual(order, ["passphrase", "stop"])

    def test_every_step_from_the_first_read_to_the_services_holds_the_box_lock(self):
        # A `yo down` between them would stop the box mid-rebuild, or between
        # its stop and its start.
        held = {}

        def recorded(step, result=None):
            def record(*args, **kwargs):
                held.setdefault(step, []).append(box_lock_is_held())
                return result

            return record

        yo.vm_up.side_effect = recorded("vm_up", yo.vm_up.return_value)
        yo.vm_restart.side_effect = recorded("vm_restart", yo.vm_up.return_value)

        def ssh_run(role, argv, **kwargs):
            recorded("ssh_run")()
            return self.agent_logs_in(role, argv)

        yo.ssh_run.side_effect = ssh_run
        yo.ensure_herdr_machine.side_effect = recorded("herdr")
        yo.vm_services.side_effect = recorded("services")
        with mock.patch.object(yo.LIMA, "stop", side_effect=recorded("stop")):
            self.run_bootstrap([("g1", "g0"), ("g1", "g1")])
        self.assertEqual(
            held,
            {
                "vm_up": [True],
                "vm_restart": [True],
                "ssh_run": [True] * len(held["ssh_run"]),
                "stop": [True],
                "herdr": [True],
                "services": [True],
            },
        )
        self.assertFalse(box_lock_is_held())

    def test_vm_up_brings_the_vm_up_without_services(self):
        self.run_bootstrap([("g1", "g1")])
        yo.vm_up.assert_called_once_with(
            mock.ANY, run_services=False, size=yo.NO_SIZE, command="bootstrap"
        )
        self.assertIsInstance(yo.vm_up.call_args.args[0], yo.state.HeldBox)

    def test_vm_services_runs_once_after_deciding_the_generation_already_matches(self):
        self.run_bootstrap([("g1", "g1")])
        yo.vm_services.assert_called_once_with()

    def test_vm_services_runs_once_after_a_reboot_settles_the_generation(self):
        self.run_bootstrap([("g1", "g0"), ("g1", "g1")])
        yo.vm_services.assert_called_once_with()

    def test_vm_services_does_not_run_when_the_generation_never_settles(self):
        with mock.patch.object(yo.ui, "err"):
            with self.assertRaises(yo.YoError) as caught:
                self.run_bootstrap([("g1", "g0"), ("g1", "g2")])
            self.assertEqual(
                (caught.exception.message, caught.exception.code), (None, 1)
            )
        yo.vm_services.assert_not_called()

    def test_aborts_naming_boot_partition_when_generations_still_disagree(self):
        with mock.patch.object(yo.ui, "err") as fake_err:
            with self.assertRaises(yo.YoError) as caught:
                self.run_bootstrap([("g1", "g0"), ("g1", "g2")])
            self.assertEqual(
                (caught.exception.message, caught.exception.code), (None, 1)
            )
        messages = " ".join(call.args[0] for call in fake_err.call_args_list)
        self.assertIn("df /boot", messages)

    def test_aborts_naming_yo_status_when_booted_is_unreadable(self):
        with mock.patch.object(yo.ui, "err") as fake_err:
            with self.assertRaises(yo.YoError) as caught:
                self.run_bootstrap([("g1", "")])
            self.assertEqual(
                (caught.exception.message, caught.exception.code), (None, 1)
            )
        messages = " ".join(call.args[0] for call in fake_err.call_args_list)
        self.assertIn("yo status", messages)

    def test_failed_stop_aborts_naming_the_recovery(self):
        def fake_run(argv, **kwargs):
            if argv[:3] == ["limactl", "--log-level=warn", "stop"]:
                raise yo.YoError(
                    "limactl --log-level=warn stop yolobox failed (exit 1)"
                )
            return mock.Mock(returncode=0, stdout="", stderr="")

        args = yo.build_parser().parse_args(["bootstrap", "--version", "github:x/y/z"])
        with (
            mock.patch.object(yo.ui, "err") as fake_err,
            mock.patch.object(yo.proc, "run", side_effect=fake_run),
            mock.patch.object(yo.vm, "guest_generations", return_value=("g1", "g0")),
        ):
            with self.assertRaises(yo.YoError) as caught:
                yo.cmd_bootstrap(args)
            self.assertEqual(
                (caught.exception.message, caught.exception.code), (None, 1)
            )
        messages = " ".join(call.args[0] for call in fake_err.call_args_list)
        self.assertIn(
            "the box did not stop: limactl --log-level=warn stop yolobox failed",
            messages,
        )
        self.assertIn("yo bootstrap", messages)

    def test_a_hung_stop_aborts_with_its_reason_and_this_contexts_advice_only(self):
        # The box just ran nixos-rebuild, so it is in stage 2: no "do not kill
        # the hostagent" from yo down beside the recovery that is safe here.
        def fake_run(argv, **kwargs):
            if argv[:3] == ["limactl", "--log-level=warn", "stop"]:
                raise yo.RunTimeout("limactl stop yolobox did not finish within 300 s")
            return mock.Mock(returncode=0, stdout="", stderr="")

        args = yo.build_parser().parse_args(["bootstrap", "--version", "github:x/y/z"])
        with (
            mock.patch.object(yo.ui, "err") as fake_err,
            mock.patch.object(yo.proc, "run", side_effect=fake_run),
            mock.patch.object(yo.vm, "guest_generations", return_value=("g1", "g0")),
        ):
            with self.assertRaises(yo.YoError):
                yo.cmd_bootstrap(args)
        messages = " ".join(call.args[0] for call in fake_err.call_args_list)
        self.assertIn(
            "the box did not stop: limactl stop yolobox did not finish within 300 s",
            messages,
        )
        self.assertNotIn("do not kill the hostagent", messages)
        self.assertIn("limactl stop --force yolobox", messages)

    def test_herdr_is_told_whether_the_ssh_include_is_in_place(self):
        for included in (True, False):
            with self.subTest(included=included):
                with mock.patch.object(
                    yo.sshconfig, "ensure_ssh_config_include", return_value=included
                ):
                    self.run_bootstrap([("g1", "g1")])
                yo.ensure_herdr_machine.assert_called_with(mock.ANY, included)

    def test_ssh_include_and_herdr_run_before_vm_services(self):
        # M1: an optional-service failure in vm_services must never skip
        # the ssh config include or herdr registration —
        # so vm_services has to be the very last thing cmd_bootstrap does.
        order = []

        def record(name):
            def side_effect(*args, **kwargs):
                order.append(name)

            return side_effect

        for module, name in (
            (yo.sshconfig, "ensure_ssh_config_include"),
            (yo.guest, "ensure_guest_known_hosts"),
            (yo.forwards, "ensure_herdr_machine"),
            (yo.forwards, "vm_services"),
        ):
            patcher = mock.patch.object(module, name, side_effect=record(name))
            patcher.start()
            self.addCleanup(patcher.stop)

        self.run_bootstrap([("g1", "g1")])

        self.assertEqual(
            order,
            [
                "ensure_ssh_config_include",
                "ensure_guest_known_hosts",
                "ensure_herdr_machine",
                "vm_services",
            ],
        )


class TestBootstrapWarnsBeforeEncryptingInPlace(FakeHome):
    def run_bootstrap(self, fstype, df_line, rebuild_exit=0):
        rebuilds = self.rebuilds = []
        self.resolved = []
        # Every box command in order, with what ssh fed it: (argv, stdin).
        self.calls = []

        def fake_ssh_run(role, argv, **kwargs):
            self.calls.append((list(argv), kwargs.get("stdin")))
            if argv[:2] == ["lsblk", "-dno"]:
                return mock.Mock(returncode=0, stdout=fstype + "\n", stderr="")
            if argv == yo.guest.ROOT_USAGE_PROBE:
                if isinstance(df_line, yo.proc.GuestRun):
                    return df_line
                return mock.Mock(returncode=0, stdout=df_output(df_line), stderr="")
            if "nixos-rebuild" in argv:
                rebuilds.append(argv)
                return mock.Mock(returncode=rebuild_exit, stdout="", stderr="")
            if argv[:3] == ["nix", "flake", "metadata"]:
                self.resolved.append(argv)
                return self.metadata
            stdout, _ = ssh_ends(argv, None, 0)
            return mock.Mock(returncode=0, stdout=stdout, stderr="")

        args = yo.build_parser().parse_args(["bootstrap", "--version", "github:x/y/z"])
        with (
            mock.patch.object(yo.vm, "vm_up"),
            mock.patch.object(yo.forwards, "vm_services"),
            mock.patch.object(yo.forwards, "ensure_herdr_machine"),
            mock.patch.object(yo.guest, "ensure_guest_known_hosts"),
            mock.patch.object(yo.sshconfig, "ensure_ssh_config_include"),
            mock.patch.object(yo.sshx, "ssh_run", side_effect=fake_ssh_run),
            mock.patch.object(yo.vm, "guest_generations", return_value=("g1", "g1")),
            mock.patch.object(yo.ui, "out") as fake_out,
            mock.patch.object(yo.ui, "err") as fake_err,
        ):
            try:
                yo.cmd_bootstrap(args)
            finally:
                self.stderr = " ".join(call.args[0] for call in fake_err.call_args_list)
                self.stdout = [call.args[0] for call in fake_out.call_args_list]
        return rebuilds

    REV = "0123456789abcdef0123456789abcdef01234567"
    NAR = "sha256-" + "A" * 42 + "+="

    def setUp(self):
        super().setUp()
        self.metadata = self.metadata_answer(self.REV, self.NAR)

    def metadata_answer(self, rev, nar_hash):
        locked = {"locked": {"rev": rev, "narHash": nar_hash}}
        return mock.Mock(returncode=0, stdout=json.dumps(locked), stderr="")

    def test_the_build_names_the_commit_the_ref_resolves_to_before_it_runs(self):
        self.run_bootstrap("crypto_LUKS", "/dev/mapper/root 1000 990 10 99% /")
        self.assertIn(
            "building github:x/y/z at %s (%s)" % (self.REV, self.NAR),
            " ".join(self.stdout),
        )
        # --refresh, or nix would answer from an old fetch of a moving branch.
        self.assertEqual(
            self.resolved,
            [["nix", "flake", "metadata", "--json", "--refresh", "github:x/y/z"]],
        )

    def test_a_ref_the_box_cannot_resolve_stops_before_the_build(self):
        for metadata, needle in (
            (mock.Mock(returncode=1, stdout="", stderr="no such ref"), "no such ref"),
            (mock.Mock(returncode=0, stdout="{}", stderr=""), "no locked commit"),
            (mock.Mock(returncode=0, stdout="lizard", stderr=""), "no locked commit"),
            (
                mock.Mock(
                    returncode=0,
                    stdout=json.dumps({"locked": {"rev": self.REV}}),
                    stderr="",
                ),
                "no locked commit and narHash",
            ),
            (self.metadata_answer("main", self.NAR), "not a commit"),
            (self.metadata_answer(None, self.NAR), "not a commit"),
            (self.metadata_answer(self.REV, "sha256-" + "A" * 43), "not an SRI"),
            (self.metadata_answer(self.REV, "sha1-" + "A" * 43 + "="), "not an SRI"),
            (self.metadata_answer(self.REV, self.NAR + "#x"), "not an SRI"),
            (self.metadata_answer(self.REV, 42), "not an SRI"),
        ):
            with self.subTest(needle=needle):
                self.metadata = metadata
                with self.assertRaises(yo.YoError) as caught:
                    self.run_bootstrap(
                        "crypto_LUKS", "/dev/mapper/root 1000 990 10 99% /"
                    )
                self.assertIn(needle, caught.exception.message)
                self.assertEqual(self.rebuilds, [])

    def test_an_unencrypted_disk_gets_the_no_interruption_warning(self):
        rebuilds = self.run_bootstrap("ext4", "/dev/vda2 1000 500 500 50% /")
        self.assertEqual(len(rebuilds), 1)
        self.assertIn("encrypts /dev/vda2 in place", self.stderr)
        self.assertIn("must not be interrupted", self.stderr)

    def test_the_rebuild_builds_and_stamps_the_resolved_commit(self):
        # The commit yo printed is the one built, even if the branch moves in
        # between, nix refuses a tree of another NAR hash, and the box stamps
        # that exact tree as what its own rebuild builds again.
        rebuilds = self.run_bootstrap("ext4", "/dev/vda2 1000 500 500 50% /")
        pinned = "github:x/y/%s?narHash=%s" % (self.REV, self.NAR)
        (argv,) = rebuilds
        self.assertEqual(argv[-2:], ["--flake", pinned + "#yolobox"])
        self.assertIn("%s=%s" % (yo.FACTS["nix_env_flake_ref"], pinned), argv)
        # Under the box's rebuild lock, which the guest's own rebuild takes too.
        self.assertIn(yo.FACTS["rebuild_lock"], argv)

    def test_a_rebuild_another_run_left_going_is_refused_at_once(self):
        with self.assertRaisesRegex(
            yo.YoError, "another nixos-rebuild is already running in yolobox"
        ):
            self.run_bootstrap(
                "crypto_LUKS",
                "/dev/mapper/root 1000 990 10 99% /",
                rebuild_exit=yo.FACTS["rebuild_busy_exit"],
            )

    def test_a_rebuild_that_fails_otherwise_names_its_exit_code(self):
        with self.assertRaisesRegex(yo.YoError, r"nixos-rebuild.*failed \(exit 1\)"):
            self.run_bootstrap(
                "crypto_LUKS", "/dev/mapper/root 1000 990 10 99% /", rebuild_exit=1
            )

    def test_an_encrypted_disk_gets_no_warning(self):
        rebuilds = self.run_bootstrap(
            "crypto_LUKS", "/dev/mapper/root 1000 990 10 99% /"
        )
        self.assertEqual(len(rebuilds), 1)
        self.assertNotIn("in place", self.stderr)

    def test_a_disk_over_90_percent_full_is_refused(self):
        with self.assertRaises(yo.YoError) as caught:
            self.run_bootstrap("ext4", "/dev/vda2 1000 950 50 95% /")
        self.assertIn("95% full", caught.exception.message)
        self.assertIn("yo gc --deep --yes", caught.exception.message)
        self.assertEqual(self.rebuilds, [])

    def test_exactly_90_percent_full_is_still_allowed(self):
        rebuilds = self.run_bootstrap("ext4", "/dev/vda2 1000 900 100 90% /")
        self.assertEqual(len(rebuilds), 1)

    def test_an_unreadable_df_is_an_error_not_a_pass(self):
        with self.assertRaisesRegex(
            yo.YoError, "cannot read the VM's root usage from df:"
        ):
            self.run_bootstrap("ext4", "garbage")

    def test_a_df_that_ssh_could_not_run_names_what_ssh_said(self):
        ssh_failed = yo.proc.GuestRun(["ssh"], 255, "", "ssh: no route")
        with self.assertRaisesRegex(
            yo.YoError, r"from df \(exit 255\):\n  \| ssh: no route"
        ):
            self.run_bootstrap("ext4", ssh_failed)
        self.assertEqual(self.rebuilds, [])

    def test_a_device_type_yo_has_no_name_for_is_an_error_showing_it(self):
        for fstype in ("", "lizard", "crypto_LUKS2"):
            with self.subTest(fstype=fstype):
                with self.assertRaises(yo.YoError) as caught:
                    self.run_bootstrap(fstype, "/dev/vda2 1000 950 50 95% /\n")
                self.assertIn(
                    "lsblk names the type of /dev/vda2 '%s', neither crypto_LUKS "
                    "nor ext4" % fstype,
                    caught.exception.message,
                )
                self.assertIn("yo status", caught.exception.message)
                self.assertEqual(self.rebuilds, [])


class TestGcSkipsCollectionOnHalfFailedSwitch(unittest.TestCase):
    # This reads nix/guest/yolobox-guest.sh off disk to keep Python and Nix
    # honest with each other. The guard this exercises moved from yo's
    # GC_MACHINE string into cmd_gc_machine there, so the test reads the
    # guard from that file rather than from a string literal in yo.
    def run_decision(self, booted, profile):
        guest_script = yo.REPO / "nix" / "guest" / "yolobox-guest.sh"
        text = guest_script.read_text()

        generations_fn = re.search(r"generations\(\) \{.*?\n\}\n", text, re.S)
        self.assertIsNotNone(generations_fn)

        decision = re.search(
            r"    local gen_out profile_line booted_line profile booted\n.*?\n    fi\n",
            text,
            re.S,
        )
        self.assertIsNotNone(decision)

        script = (
            "set -uo pipefail\n"
            "GC_FAILED=0\n"
            "readlink() {\n"
            '  case "$2" in\n'
            "    /run/current-system) printf '%s\\n' \"$BOOTED_VALUE\" ;;\n"
            "    /nix/var/nix/profiles/system) printf '%s\\n' \"$PROFILE_VALUE\" ;;\n"
            "  esac\n"
            "}\n"
            "step() { printf 'STEP:%s\\n' \"$1\"; }\n"
            "fail() { printf 'FAIL:%s\\n' \"$1\"; }\n"
            + generations_fn.group(0)
            + "run_decision() {\n"
            + decision.group(0)
            + "}\n"
            + "run_decision\n"
        )
        env = dict(os.environ, BOOTED_VALUE=booted, PROFILE_VALUE=profile)
        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, env=env
        )
        return result.stdout

    def test_runs_the_collection_when_booted_matches_the_profile(self):
        stdout = self.run_decision("/nix/store/aaa", "/nix/store/aaa")
        self.assertIn("STEP:nix-collect-garbage -d", stdout)
        self.assertIn("STEP:nix-store --optimise", stdout)

    def test_skips_the_collection_when_the_generations_disagree(self):
        stdout = self.run_decision("/nix/store/aaa", "/nix/store/bbb")
        self.assertIn(
            "FAIL:skipped nix-collect-garbage -d: booted and profile generations disagree",
            stdout,
        )
        self.assertNotIn("STEP:", stdout)


class TestDirForCwdLandsOnDeepestExistingAncestor(FakeHome):
    def test_lands_on_the_deepest_existing_ancestor(self):
        desired = self.home + "/a/b"
        landing = yo.AGENT_HOME + "/a"
        proc = mock.Mock(returncode=0, stdout=landing + "\n", stderr="")
        with mock.patch.object(yo.paths, "logical_cwd", return_value=desired):
            with mock.patch.object(
                yo.sshx, "ssh_run", return_value=proc
            ) as fake_ssh_run:
                with mock.patch.object(yo.ui, "err") as fake_err:
                    result = yo.dir_for_cwd()
        self.assertEqual(result, landing)
        fake_err.assert_called_once_with(
            yo.MISSING_PATH_NOTE % (yo.AGENT_HOME + "/a/b", landing)
        )
        fake_ssh_run.assert_called_once_with(
            yo.AGENT, [yo.HELPER, "landing-dir", yo.AGENT_HOME + "/a/b"]
        )

    def test_a_landing_dir_outside_the_guest_home_is_refused_unprinted(self):
        reported = "/etc/\x1b]0;pwned\x07"
        proc = mock.Mock(returncode=0, stdout=reported + "\n", stderr="")
        with mock.patch.object(yo.paths, "logical_cwd", return_value=self.home + "/a"):
            with mock.patch.object(yo.sshx, "ssh_run", return_value=proc):
                with mock.patch.object(yo.ui, "err") as fake_err:
                    with self.assertRaisesRegex(
                        yo.YoError,
                        r'refusing to target "/etc/\\x1b\]0;pwned\\x07": no yo',
                    ):
                        yo.dir_for_cwd()
        fake_err.assert_not_called()

    def test_a_landing_dir_with_an_escape_is_reported_escaped(self):
        landing = yo.AGENT_HOME + "/a\x1b]0;pwned\x07"
        proc = mock.Mock(returncode=0, stdout=landing + "\n", stderr="")
        with mock.patch.object(
            yo.paths, "logical_cwd", return_value=self.home + "/a/b"
        ):
            with mock.patch.object(yo.sshx, "ssh_run", return_value=proc):
                with mock.patch.object(yo.ui, "err") as fake_err:
                    yo.dir_for_cwd()
        (line,) = fake_err.call_args.args
        self.assertIn(r'"/home/agent/a\x1b]0;pwned\x07"', line)
        self.assertTrue(line.isprintable())

    def test_a_cwd_that_climbs_out_of_home_lands_on_the_agent_home_unasked(self):
        for climbing in (
            self.home + "/a/../../etc",
            self.home + "/..",
            self.home + "/a/../..",
        ):
            with self.subTest(climbing=climbing):
                with mock.patch.object(yo.paths, "logical_cwd", return_value=climbing):
                    with mock.patch.object(yo.sshx, "ssh_run") as fake_ssh_run:
                        with mock.patch.object(yo.ui, "err") as fake_err:
                            result = yo.dir_for_cwd()
                fake_ssh_run.assert_not_called()
                self.assertEqual(result, yo.AGENT_HOME)
                fake_err.assert_called_once_with(
                    yo.MISSING_PATH_NOTE % (climbing, yo.AGENT_HOME)
                )

    def test_a_cwd_with_dots_inside_home_is_mirrored_normalised(self):
        with mock.patch.object(
            yo.paths, "logical_cwd", return_value=self.home + "/a/./b/../c/"
        ):
            self.assertEqual(
                yo.paths.mirror(yo.paths.logical_cwd()), yo.AGENT_HOME + "/a/c"
            )

    def test_a_cwd_with_a_newline_is_refused_before_the_guest_is_asked(self):
        with mock.patch.object(
            yo.paths, "logical_cwd", return_value=self.home + "/a\nb"
        ):
            with mock.patch.object(yo.sshx, "ssh_run") as fake_ssh_run:
                with self.assertRaisesRegex(yo.YoError, "newline or NUL"):
                    yo.dir_for_cwd()
        fake_ssh_run.assert_not_called()

    def test_outside_home_lands_on_the_agent_home(self):
        with mock.patch.object(yo.paths, "logical_cwd", return_value="/etc"):
            with mock.patch.object(yo.sshx, "ssh_run") as fake_ssh_run:
                with mock.patch.object(yo.ui, "err") as fake_err:
                    result = yo.dir_for_cwd()
        fake_ssh_run.assert_not_called()
        self.assertEqual(result, yo.AGENT_HOME)
        fake_err.assert_called_once_with(yo.MISSING_PATH_NOTE % ("/etc", yo.AGENT_HOME))


class TestAtomicWriteIsDurable(FakeHome):
    def test_the_file_and_its_directory_are_synced(self):
        target = os.path.join(self.home, "file")
        synced = []
        with mock.patch.object(yo.os, "fsync", side_effect=synced.append):
            yo.proc.atomic_write(target, "text", 0o600)
        self.assertEqual(len(synced), 2)
        with open(target) as written:
            self.assertEqual(written.read(), "text")

    def test_the_writes_own_error_is_raised_when_the_cleanup_fails_too(self):
        target = os.path.join(self.home, "file")
        with (
            mock.patch.object(yo.os, "replace", side_effect=PermissionError("replace")),
            mock.patch.object(yo.os, "unlink", side_effect=FileNotFoundError("gone")),
        ):
            with self.assertRaisesRegex(
                yo.YoError, "cannot write %s: replace" % target
            ):
                yo.proc.atomic_write(target, "text", 0o600)


class TestTerminalSanitising(unittest.TestCase):
    def printed(self, func, text):
        stream = io.StringIO()
        target = "sys.stdout" if func is yo.out else "sys.stderr"
        with mock.patch(target, stream):
            func(text)
        return stream.getvalue()

    def test_escape_and_c1_controls_are_replaced_on_both_streams(self):
        for func in (yo.out, yo.err):
            with self.subTest(func=func.__name__):
                self.assertEqual(
                    self.printed(func, "a\x1b]0;x\x07b\x9bc\x7fd\x00e\rf"),
                    "a?]0;x?b?c?d?e?f\n",
                )

    def test_tab_newline_and_printable_unicode_survive(self):
        for func in (yo.out, yo.err):
            with self.subTest(func=func.__name__):
                self.assertEqual(
                    self.printed(func, "a\tb\nc \u00a0é—"), "a\tb\nc \u00a0é—\n"
                )


class TestPickProjectSanitisesWhatFzfSees(unittest.TestCase):
    def test_a_guest_controlled_name_reaches_fzf_without_control_bytes(self):
        picker = mock.Mock(returncode=0, stdout=b"ok")
        with (
            mock.patch.object(yo.proc, "require_tool"),
            mock.patch.object(
                yo.guest,
                "agent_paths",
                return_value=["/home/agent/ok", "/home/agent/a\x1b]52;c;eA==\x07b"],
            ),
            mock.patch.object(yo.subprocess, "run", return_value=picker) as run,
        ):
            yo.pick_project("")
        self.assertEqual(run.call_args.kwargs["input"], b"a?]52;c;eA==?b\0ok")


class TestStreamedOutputIsFiltered(unittest.TestCase):
    # yo gc streams the guest tiers live; a Makefile recipe they echo must not
    # reach the terminal as an escape sequence.
    def test_a_streamed_command_is_filtered_and_keeps_its_exit_code(self):
        script = (
            "import sys; sys.stdout.write('o\\x1b]52;c;eA==\\x07\\n');"
            "sys.stderr.write('e\\x1b[2J\\n'); sys.exit(3)"
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            proc = yo.run([sys.executable, "-c", script], filtered=True)
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(stdout.getvalue(), "  | o?]52;c;eA==?\n")
        self.assertEqual(stderr.getvalue(), "  | e?[2J\n")

    def test_options_a_streamed_run_would_drop_are_refused_not_ignored(self):
        for option in (
            {"stdin": b"x"},
            {"env": {"A": "b"}},
            {"timeout": 5},
            {"merge_stderr": True},
            {"binary": True},
        ):
            with self.subTest(option=option):
                with mock.patch.object(yo.proc, "run_filtered") as streamed:
                    with self.assertRaisesRegex(ValueError, next(iter(option))):
                        yo.run(["true"], filtered=True, **option)
                streamed.assert_not_called()


class TestEditorUris(unittest.TestCase):
    def launched(self, command, tool):
        argv = []
        with (
            mock.patch.object(yo.shutil, "which", return_value=tool),
            mock.patch.object(yo.os, "access", return_value=True),
            mock.patch.object(yo.sshconfig, "ensure_ssh_config_include"),
            mock.patch.object(yo.sshx, "require_agent_account"),
            mock.patch.object(
                yo.session, "target_dir", return_value=yo.AGENT_HOME + "/a b/c#d?e%f"
            ),
            mock.patch.object(yo.proc, "exec_process", side_effect=argv.extend),
        ):
            args = yo.build_parser().parse_args([command])
            args.func(args)
        return argv[-1]

    def test_zed_uri_percent_encodes_the_project_path(self):
        self.assertEqual(
            self.launched("zed", "/bin/zed"),
            "ssh://agent@lima-yolobox/home/agent/a%20b/c%23d%3Fe%25f",
        )

    def test_code_uri_percent_encodes_the_project_path(self):
        self.assertEqual(
            self.launched("code", "/bin/code"),
            "vscode-remote://ssh-remote+agent@lima-yolobox/home/agent/a%20b/c%23d%3Fe%25f",
        )

    def test_an_ordinary_path_is_unchanged(self):
        path = "/home/agent/src/myproject"
        self.assertEqual(yo.editor_uri_path(path), path)


# What ssh itself says when it cannot connect: a lost box always has a reason
# on stderr, where a command that died on the box leaves ssh silent.
LOST_SSH_STDERR = "ssh: connect to host 127.0.0.1 port 60022: Connection refused\n"


class TestProbe(unittest.TestCase):
    # ssh exits 255 when it cannot connect, so that code says nothing about the
    # command it was to run. The callers that read an exit code as a fact
    # about the box must see Unreachable first.
    def lost_ssh(self, said=LOST_SSH_STDERR):
        return mock.patch.object(
            yo.sshx,
            "ssh_run",
            return_value=mock.Mock(
                returncode=yo.SSH_UNREACHABLE, stdout="", stderr=said
            ),
        )

    def test_a_silent_255_is_the_commands_own(self):
        # ssh says why whenever it fails, so a 255 with nothing said is a
        # command that exited 255 on the box, or was killed by a signal there.
        with self.lost_ssh(said=""):
            self.assertEqual(
                yo.sshx.probe(yo.AGENT, ["ssh-add", "-l"]).returncode,
                yo.SSH_UNREACHABLE,
            )

    def test_a_255_whose_stderr_was_streamed_is_unreachable_without_a_block(self):
        with self.lost_ssh(said=None):
            with self.assertRaises(yo.Unreachable) as caught:
                yo.sshx.probe(yo.AGENT, ["yolobox-guest", "gc-user"], filtered=True)
        self.assertNotIn("\n", caught.exception.message)

    def test_a_command_that_ran_is_returned_whatever_it_exited_with(self):
        for code in (0, 1, 64, 127):
            with self.subTest(code=code):
                ran = mock.Mock(returncode=code)
                with mock.patch.object(yo.sshx, "ssh_run", return_value=ran):
                    self.assertIs(yo.sshx.probe(yo.OPERATOR, ["true"]), ran)

    def test_exit_255_is_unreachable_with_the_is_it_up_hint(self):
        for role, who in ((yo.OPERATOR, "operator"), (yo.AGENT, "agent")):
            with self.subTest(role=who), self.lost_ssh():
                with self.assertRaisesRegex(
                    yo.Unreachable,
                    "cannot reach yolobox as %s \\(the %s's ssh session exited 255, "
                    "ssh's own code for no connection\\) — is it up\\? yo status"
                    % (who, who),
                ):
                    yo.sshx.probe(role, ["true"])

    def test_probe_passes_ssh_run_options_through(self):
        with mock.patch.object(
            yo.sshx, "ssh_run", return_value=mock.Mock(returncode=0)
        ) as fake:
            yo.sshx.probe(yo.AGENT, ["x"], binary=True, mux=True)
        fake.assert_called_once_with(yo.AGENT, ["x"], binary=True, mux=True)

    def test_a_lost_box_is_unreachable_through_the_helper_streamed_or_not(self):
        for stream in (False, True):
            with self.subTest(stream=stream), self.lost_ssh():
                with self.assertRaises(yo.Unreachable):
                    yo.guest_helper(yo.AGENT, "projects", stream=stream)


class TestGuestOutNamesALostBox(unittest.TestCase):
    # guest_out reads a command's output as a fact about the box. A lost box
    # (ssh exit 255) must say so, not read as the empty output of a command
    # that found nothing.
    def answering(self, code, stdout="", stderr=""):
        if code == yo.SSH_UNREACHABLE:
            stderr = LOST_SSH_STDERR
        return mock.patch.object(
            yo.sshx,
            "ssh_run",
            return_value=yo.proc.GuestRun(["ssh"], code, stdout, stderr),
        )

    def test_a_command_that_succeeded_reads_as_its_output(self):
        with self.answering(0, "crypto_LUKS\n"):
            self.assertEqual(yo.sshx.guest_out(["lsblk"]), "crypto_LUKS\n")
            self.assertEqual(yo.vm.luks_device_fstype(), "crypto_LUKS")

    def test_a_command_that_failed_there_is_an_error_with_its_reason(self):
        # Its output is not an answer: an empty one would read as "nothing found".
        with self.answering(1, "", "lsblk: /dev/vda2: not a block device\n"):
            with self.assertRaises(yo.YoError) as caught:
                yo.sshx.guest_out(["lsblk", "/dev/vda2"])
        self.assertNotIsInstance(caught.exception, yo.Unreachable)
        self.assertEqual(
            caught.exception.message,
            "lsblk /dev/vda2 failed on the box (exit 1):\n"
            "  | lsblk: /dev/vda2: not a block device",
        )
        with self.answering(7):
            with self.assertRaisesRegex(
                yo.YoError, r"failed on the box \(exit 7\) with no output$"
            ):
                yo.vm.luks_device_fstype()

    def test_a_lost_box_is_unreachable_at_every_read_of_the_guest_disk(self):
        reads = (
            ("luks_device_fstype", yo.vm.luks_device_fstype),
            ("guest_generations", yo.vm.guest_generations),
            ("require_encrypted_root", yo.vm.require_encrypted_root),
        )
        for name, read in reads:
            with self.subTest(read=name), self.answering(yo.SSH_UNREACHABLE):
                with self.assertRaisesRegex(
                    yo.Unreachable, "cannot reach yolobox as operator .*— is it up\\?"
                ):
                    read()


class TestGuestHelperExitCodesAreTheCallers(unittest.TestCase):
    # A helper that is missing (127) or lacks the subcommand (64) is a failed
    # command like any other: its exit code and stderr come back as they are,
    # streamed or not, for the caller to report.
    def test_every_exit_comes_back_untouched(self):
        for code, stderr in (
            (127, "bash: line 1: yolobox-guest: command not found\n"),
            (64, "yolobox-guest: unknown subcommand 'projects'\n"),
            (64, "make: *** [clean] Error 64"),
            (1, ""),
        ):
            for stream in (False, True):
                main_proc = mock.Mock(returncode=code, stderr=stderr, stdout="")
                with self.subTest(code=code, stream=stream):
                    with mock.patch.object(yo.sshx, "ssh_run", return_value=main_proc):
                        result = yo.guest_helper(yo.AGENT, "projects", stream=stream)
                    self.assertIs(result, main_proc)


class TestGcArgv(unittest.TestCase):
    # ext4's root reserve is the operator's alone, so on a
    # genuinely full disk the machine tier must run, and complete, before the
    # agent-tier steps that depend on the space it frees. This is the guard
    # against that ordering being silently swapped.
    def test_deep_yes_runs_machine_then_user_in_that_order(self):
        calls = []

        def fake_guest_helper(role, sub, args=(), **kwargs):
            calls.append((role, sub, list(args)))
            return mock.Mock(returncode=0)

        args = yo.build_parser().parse_args(["gc", "--deep", "--yes"])
        with mock.patch.object(yo.guest, "guest_helper", side_effect=fake_guest_helper):
            with mock.patch.object(yo.sshx, "require_agent_account"):
                yo.cmd_gc(args)

        self.assertEqual(
            calls,
            [
                (yo.OPERATOR, "gc-machine", ["--apply"]),
                (yo.AGENT, "gc-user", ["--apply", "--deep"]),
            ],
        )


class TestHelperFailureMessage(unittest.TestCase):
    def message(self, code, stderr=""):
        return yo.helper_failure_message(
            yo.HelperRun(code, stderr, streamed=False),
            proved_initrd(yo.SshFacts(60022, "op", "/k/user", "none")),
        )

    def test_an_unexpected_failure_shows_the_command_the_helper_named(self):
        stderr = "yolobox-unlock: unexpected failure (exit 4) at line 51: sed -n p x"
        self.assertEqual(
            self.message(yo.HELPER_EXIT_INTERNAL, stderr),
            "the initrd helper failed unexpectedly; the line below names the "
            "command that failed:\n  | " + stderr,
        )

    def test_every_documented_code_has_its_own_message(self):
        documented = (
            yo.HELPER_EXIT_WRONG_KEY,
            yo.HELPER_EXIT_ENCRYPT_FAILED,
            yo.HELPER_EXIT_WATCH_LOST,
            yo.HELPER_EXIT_NO_PASSPHRASE,
            yo.HELPER_EXIT_ROOT_MOUNT_FAILED,
            yo.HELPER_EXIT_INTERNAL,
            yo.HELPER_EXIT_UNLOCK_FAILED,
        )
        messages = {code: self.message(code, "E") for code in documented}
        self.assertEqual(len(set(messages.values())), len(documented))
        for code, message in messages.items():
            self.assertNotIn("failed with exit", message, code)

    def test_an_undocumented_code_is_named_with_its_number(self):
        self.assertEqual(
            self.message(9, "boom"), "the initrd helper failed with exit 9:\n  | boom"
        )


class TestCmdSshArgv(FakeHome):
    def ssh(self, stdin_tty, stdout_tty, cmd=()):
        captured = {}
        args = yo.build_parser().parse_args(["ssh", *cmd])
        fake_stdin, fake_stdout = mock.Mock(), mock.Mock()
        fake_stdin.isatty.return_value = stdin_tty
        fake_stdout.isatty.return_value = stdout_tty
        with (
            mock.patch.object(yo.sys, "stdin", fake_stdin),
            mock.patch.object(yo.sys, "stdout", fake_stdout),
            mock.patch.object(
                yo.proc,
                "exec_process",
                side_effect=lambda argv: captured.update(argv=argv),
            ),
        ):
            yo.cmd_ssh(args)
        return captured["argv"]

    def test_a_pty_is_requested_only_when_stdin_and_stdout_are_terminals(self):
        for stdin_tty, stdout_tty, wanted in (
            (True, True, True),
            (True, False, False),
            (False, True, False),
            (False, False, False),
        ):
            with self.subTest(stdin=stdin_tty, stdout=stdout_tty):
                argv = self.ssh(stdin_tty, stdout_tty)
                self.assertEqual("-t" in argv, wanted)

    def test_the_command_follows_the_host_either_way(self):
        for tty in (True, False):
            with self.subTest(tty=tty):
                argv = self.ssh(tty, tty, ["sudo", "ls"])
                self.assertEqual(argv[-3:], ["lima-yolobox", "sudo", "ls"])


class TestEntryExitCodes(unittest.TestCase):
    def test_a_closed_pipe_exits_141_not_success(self):
        # `yo status | head -1`: the shell reports a process killed by SIGPIPE as 141.
        with (
            mock.patch.object(yo.cli, "main", side_effect=BrokenPipeError),
            mock.patch.object(yo.os, "_exit") as fake_exit,
        ):
            yo.entry(["status"])
        fake_exit.assert_called_once_with(141)


class TestCmdEnterArgv(FakeHome):
    def enter(self, stdin_tty=True, stdout_tty=True):
        args = yo.build_parser().parse_args(["enter"])
        fake_stdin, fake_stdout = mock.Mock(), mock.Mock()
        fake_stdin.isatty.return_value = stdin_tty
        fake_stdout.isatty.return_value = stdout_tty
        with (
            mock.patch.object(yo.sys, "stdin", fake_stdin),
            mock.patch.object(yo.sys, "stdout", fake_stdout),
            mock.patch.object(yo.sshx, "require_agent_account") as self.agent_probe,
            mock.patch.object(yo.session, "target_dir", return_value=yo.AGENT_HOME),
            mock.patch.object(yo.proc, "exec_process") as self.exec,
        ):
            yo.cmd_enter(args)
        return list(self.exec.call_args[0][0])

    def test_a_pty_is_requested_on_a_terminal(self):
        with env_without("HERDR_ENV"):
            self.assertIn("-t", self.enter())

    def test_a_shell_is_refused_unless_stdin_and_stdout_are_terminals(self):
        for stdin_tty, stdout_tty in ((True, False), (False, True), (False, False)):
            with self.subTest(stdin=stdin_tty, stdout=stdout_tty):
                with env_without("HERDR_ENV"):
                    with self.assertRaisesRegex(yo.YoError, "terminal"):
                        self.enter(stdin_tty, stdout_tty)
                self.exec.assert_not_called()
                self.agent_probe.assert_not_called()

    def test_argv_carries_no_forward_agent(self):
        with env_without("HERDR_ENV"):
            argv = self.enter()
        self.assertNotIn("ForwardAgent", " ".join(argv))

    def test_herdr_env_prints_the_invisibility_notice(self):
        with mock.patch.dict(os.environ, {"HERDR_ENV": "1"}):
            with mock.patch.object(yo.ui, "err") as fake_err:
                self.enter()
        fake_err.assert_called_once()
        message = fake_err.call_args[0][0]
        self.assertIn("yolobox herdr machine", message)
        self.assertIn("invisible to herdr", message)

    def test_no_herdr_env_prints_nothing(self):
        with env_without("HERDR_ENV"):
            with mock.patch.object(yo.ui, "err") as fake_err:
                self.enter()
        fake_err.assert_not_called()


def ensure_herdr_machine(ssh_config_includes_the_vm):
    with yo.box_lock(wait=True) as held:
        yo.ensure_herdr_machine(held, ssh_config_includes_the_vm)


class TestEnsureHerdrMachine(unittest.TestCase):
    def test_no_herdr_on_path_skips_with_a_note(self):
        with mock.patch.object(yo.shutil, "which", return_value=None):
            with mock.patch.object(yo.proc, "run") as fake_run:
                with mock.patch.object(yo.ui, "err") as fake_err:
                    ensure_herdr_machine(True)
        fake_run.assert_not_called()
        fake_err.assert_called_once()

    def test_existing_machine_is_left_alone(self):
        def fake_run(argv, **kwargs):
            if argv[1:3] == ["machine", "list"]:
                return mock.Mock(
                    returncode=0, stdout='[{"id": "1", "target": "yolobox"}]', stderr=""
                )
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(
            yo.shutil, "which", return_value="/opt/homebrew/bin/herdr"
        ):
            with mock.patch.object(yo.proc, "run", side_effect=fake_run) as tracked:
                ensure_herdr_machine(True)
        calls = [call.args[0] for call in tracked.call_args_list]
        self.assertTrue(all(argv[2:3] != ["add"] for argv in calls))

    def test_missing_machine_is_added(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if argv[1:3] == ["machine", "list"]:
                return mock.Mock(returncode=0, stdout="[]", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(
            yo.shutil, "which", return_value="/opt/homebrew/bin/herdr"
        ):
            with mock.patch.object(yo.proc, "run", side_effect=fake_run):
                ensure_herdr_machine(True)
        self.assertIn(
            [
                "/opt/homebrew/bin/herdr",
                "machine",
                "add",
                "yolobox",
                "--label",
                "yolobox",
            ],
            calls,
        )

    def test_a_declined_ssh_include_skips_the_add_with_one_note_naming_the_line(self):
        def fake_run(argv, **kwargs):
            if argv[1:3] == ["machine", "list"]:
                return mock.Mock(returncode=0, stdout="[]", stderr="")
            raise AssertionError("herdr cannot resolve the host: %r" % (argv,))

        with mock.patch.object(
            yo.shutil, "which", return_value="/opt/homebrew/bin/herdr"
        ):
            with mock.patch.object(yo.proc, "run", side_effect=fake_run):
                with mock.patch.object(yo.ui, "err") as fake_err:
                    ensure_herdr_machine(False)
        notes = " ".join(call.args[0] for call in fake_err.call_args_list)
        self.assertIn("~/.ssh/config does not reach", notes)
        self.assertIn(yo.SSH_CONFIG_INCLUDE_LINE, notes)

    def test_add_failure_raises_with_herdrs_stderr(self):
        def fake_run(argv, **kwargs):
            if argv[1:3] == ["machine", "list"]:
                return mock.Mock(returncode=0, stdout="[]", stderr="")
            return mock.Mock(returncode=1, stdout="", stderr="boom")

        with mock.patch.object(
            yo.shutil, "which", return_value="/opt/homebrew/bin/herdr"
        ):
            with mock.patch.object(yo.proc, "run", side_effect=fake_run):
                with self.assertRaises(yo.YoError) as caught:
                    ensure_herdr_machine(True)
        self.assertIn("boom", caught.exception.message)


class TestVmServices(unittest.TestCase):
    """`yo up`, `yo bootstrap` and `yo aws-check` bring the host side whole:
    everything is declared first (the session, then the broker, whose answer
    says whether the guest has AWS), then each forward the guest uses is
    probed, and one error names every one that is down."""

    def vm_services(self, enabled, down=None):
        """`down` maps a forward's name to what its probe said."""
        down = down or {}
        order = []

        def probed(forward, mux):
            self.assertTrue(mux, "a session command's logins ride the masters")
            order.append("probe " + forward.name)
            return down.get(forward.name)

        with (
            mock.patch.object(
                yo.forwards,
                "ensure_session",
                side_effect=lambda: order.append("session"),
            ),
            mock.patch.object(
                yo.aws,
                "ensure_aws_broker",
                side_effect=lambda: order.append("broker") or enabled,
            ),
            mock.patch.object(yo.forwards, "forward_failure", side_effect=probed),
        ):
            try:
                return yo.vm_services(), order
            except yo.YoError as raised:
                return raised, order

    def test_everything_is_declared_before_any_forward_is_probed(self):
        enabled, order = self.vm_services(True)
        self.assertIs(enabled, True)
        self.assertEqual(
            order, ["session", "broker", "probe 1password", "probe aws-broker"]
        )

    def test_a_guest_without_aws_probes_no_aws_forward(self):
        enabled, order = self.vm_services(False)
        self.assertIs(enabled, False)
        self.assertEqual(order, ["session", "broker", "probe 1password"])

    def test_a_down_1password_forward_still_gets_the_broker_started(self):
        raised, order = self.vm_services(True, {"1password": "its probe exited 2"})
        self.assertIsInstance(raised, yo.YoError)
        self.assertIn("broker", order)
        self.assertIn("1password", raised.message)
        self.assertIn("its probe exited 2", raised.message)
        self.assertNotIn("aws-broker", raised.message)

    def test_a_down_aws_forward_is_an_error_naming_only_it_and_its_probe(self):
        raised, _ = self.vm_services(True, {"aws-broker": "its probe exited 7"})
        self.assertIsInstance(raised, yo.YoError)
        self.assertIn("aws-broker", raised.message)
        self.assertIn("its probe exited 7", raised.message)
        self.assertNotIn("1password", raised.message)

    def test_both_forwards_down_are_one_error_naming_both_and_the_remedy(self):
        raised, order = self.vm_services(
            True,
            {"1password": "its probe exited 2", "aws-broker": "its probe exited 7"},
        )
        self.assertIsInstance(raised, yo.YoError)
        self.assertEqual(order[-2:], ["probe 1password", "probe aws-broker"])
        self.assertIn(
            "the 1password guest forward is down (its probe exited 2)", raised.message
        )
        self.assertIn(
            "the aws-broker guest forward is down (its probe exited 7)", raised.message
        )
        self.assertTrue(raised.message.endswith("restore with: yo up"), raised.message)

    def test_a_disabled_aws_is_never_probed_even_when_its_forward_would_fail(self):
        enabled, order = self.vm_services(False, {"aws-broker": "its probe exited 7"})
        self.assertIs(enabled, False)
        self.assertNotIn("probe aws-broker", order)

    def test_a_box_out_of_reach_starts_no_broker(self):
        with (
            mock.patch.object(
                yo.forwards,
                "ensure_session",
                side_effect=yo.sshx.unreachable(yo.OPERATOR),
            ),
            mock.patch.object(yo.aws, "ensure_aws_broker") as broker,
        ):
            with self.assertRaises(yo.Unreachable):
                yo.vm_services()
        broker.assert_not_called()


class TestEnsureSession(FakeHome):
    """`yo ssh`, `yo enter`, `yo cp`, `yo code` and `yo zed` run this first:
    one mux session opens the operator's master if none answers, the two
    reverse forwards are asked for through it, always, and the guest's proxy
    sockets start once the forwards exist. Nothing is probed."""

    def ensure(self, session=(0, "", ""), start=(0, "", "")):
        calls = self.calls = []

        def fake_run(argv, **kwargs):
            argv = list(argv)
            calls.append(argv)
            if "-O" in argv:
                return mock.Mock(returncode=0, stdout="", stderr="")
            words = guest_words(argv[-1])
            if words[:3] == ["sudo", "systemctl", "start"]:
                code, stdout, stderr = start
            else:
                code, stdout, stderr = session
            return mock.Mock(returncode=code, stdout=stdout, stderr=stderr)

        with mock.patch.object(yo.proc, "run", side_effect=fake_run):
            yo.forwards.ensure_session()
        return calls

    def test_both_forwards_are_asked_for_whatever_master_answers(self):
        # A master opened by git or an editor carries no forward, so no
        # `-O check` decides anything: the forwards are declared every time.
        calls = self.ensure()
        actions = [argv[argv.index("-O") + 1] for argv in calls if "-O" in argv]
        self.assertEqual(actions, ["forward", "forward"])
        (opener,) = [argv for argv in calls if guest_words(argv[-1]) == ["true"]]
        self.assertNotIn("ControlPath=none", opener)
        forwards = [argv for argv in calls if "-O" in argv]
        self.assertEqual(
            [argv[argv.index("-R") + 1] for argv in forwards],
            [
                "%s:%s" % (yo.OP_GUEST_SOCK, yo.LIMA.op_sock()),
                "%s:%s" % (yo.AWS_BROKER_GUEST_SOCK, yo.state.aws_broker_sock()),
            ],
        )
        self.assertLess(calls.index(opener), calls.index(forwards[0]))

    def test_the_proxy_sockets_start_as_root_once_after_both_forwards(self):
        calls = self.ensure()
        words = ["forward" if "-O" in argv else guest_words(argv[-1]) for argv in calls]
        starts = [w for w in words if w[:3] == ["sudo", "systemctl", "start"]]
        self.assertEqual(
            starts, [["sudo", "systemctl", "start", *yo.config.PROXY_SOCKET_UNITS]]
        )
        started = words.index(starts[0])
        self.assertEqual(words[started - 2 : started], ["forward", "forward"])
        self.assertEqual(started, len(words) - 1)

    def test_nothing_is_probed_even_when_no_forward_would_answer(self):
        calls = self.ensure()
        self.assertEqual(
            [argv for argv in calls if "ssh-add" in argv[-1] or "curl" in argv[-1]], []
        )

    def test_no_login_but_the_masters_is_made(self):
        # On libvirt every fresh login is a 1Password signature.
        calls = self.ensure()
        self.assertEqual([argv for argv in calls if "ControlPath=none" in argv], [])

    def test_a_box_out_of_reach_is_unreachable_and_nothing_is_forwarded(self):
        with self.assertRaisesRegex(yo.Unreachable, "is it up\\? yo status"):
            self.ensure(session=(255, "", LOST_SSH_STDERR))

    def test_an_opening_session_that_fails_on_the_box_is_an_error_with_its_words(
        self,
    ):
        with self.assertRaises(yo.YoError) as caught:
            self.ensure(session=(1, "", "zsh: lizard"))
        self.assertIn("zsh: lizard", caught.exception.message)
        self.assertNotIsInstance(caught.exception, yo.Unreachable)

    def test_a_forward_the_master_refuses_is_an_error_and_nothing_starts(
        self,
    ):
        calls = []

        def refuse(argv, **kwargs):
            calls.append(list(argv))
            if "-O" in argv:
                raise yo.YoError("ssh -O forward failed (exit 255)")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(yo.proc, "run", side_effect=refuse):
            with self.assertRaisesRegex(yo.YoError, "forward failed"):
                yo.forwards.ensure_session()
        self.assertEqual(
            [guest_words(argv[-1]) for argv in calls if "-O" not in argv], [["true"]]
        )

    def test_a_proxy_socket_that_will_not_start_is_an_error_with_its_words(self):
        with self.assertRaises(yo.YoError) as caught:
            self.ensure(start=(1, "", "Unit lizard.socket not found."))
        self.assertIn("Unit lizard.socket not found.", caught.exception.message)


class TestRequireForwards(FakeHome):
    """Every forward of the batch is probed through the agent's master, and one
    error names each that is down."""

    def require(self, forward, answer):
        def fake_run(argv, **kwargs):
            return mock.Mock(returncode=answer[0], stdout=answer[1], stderr=answer[2])

        with mock.patch.object(yo.proc, "run", side_effect=fake_run):
            yo.forwards.require_forwards([forward])

    def test_a_1password_forward_that_does_not_answer_is_an_error_naming_it(self):
        with self.assertRaises(yo.YoError) as caught:
            self.require(yo.forwards.one_password_forward(), (2, "", "agent failed"))
        self.assertIn("1password", caught.exception.message)
        self.assertIn("exited 2", caught.exception.message)
        self.assertIn("agent failed", caught.exception.message)

    def test_an_agent_with_no_identities_is_a_working_forward(self):
        self.require(
            yo.forwards.one_password_forward(),
            (1, "The agent has no identities.\n", ""),
        )

    def test_a_broker_probe_that_exits_nonzero_is_a_forward_that_is_down(self):
        with self.assertRaisesRegex(yo.YoError, "aws-broker.*exited 7"):
            self.require(yo.forwards.aws_broker_forward(), (7, "", ""))

    @needs_tool("perl")
    def test_the_1password_probe_runs_ssh_add_with_sigpipe_ignored(self):
        # perl's %SIG starts as the disposition exec handed it, IGNORE included,
        # on every host; bash 3.2's `trap -p` hides a signal ignored at entry.
        _, path = fake_bin(
            self,
            {
                "ssh-add": "#!/bin/sh\n"
                '[ "$1" = -l ] || exit 64\n'
                '[ "$(%s -e \'print $SIG{PIPE} // ""\')" = IGNORE ] || exit 7\n'
                "echo ignored\n" % shutil.which("perl")
            },
        )
        ran = subprocess.run(
            list(yo.OP_PROBE),
            env={**os.environ, "PATH": path},
            capture_output=True,
            text=True,
        )
        self.assertEqual((ran.returncode, ran.stdout), (0, "ignored\n"), ran.stderr)

    def test_the_broker_probe_through_the_forward_gives_up_in_time(self):
        probe = yo.forwards.aws_broker_forward().probe
        self.assertEqual(probe[probe.index("--max-time") + 1], "5")


class TestCmdAwsCheck(unittest.TestCase):
    def aws_check(self, services=lambda: True):
        calls = []
        with (
            mock.patch.object(
                yo.sshx,
                "require_agent_account",
                side_effect=lambda: calls.append("agent"),
            ),
            mock.patch.object(
                yo.forwards,
                "vm_services",
                side_effect=lambda: calls.append("services") or services(),
            ),
            mock.patch.object(
                yo.guest,
                "guest_helper",
                side_effect=lambda *a, **k: (
                    calls.append("guest") or mock.Mock(returncode=0)
                ),
            ),
        ):
            yo.cmd_aws_check(yo.build_parser().parse_args(["aws-check"]))
        return calls

    def test_the_agent_then_the_services_then_the_guest_check(self):
        self.assertEqual(self.aws_check(), ["agent", "services", "guest"])

    def test_services_that_started_no_broker_say_aws_is_disabled(self):
        with self.assertRaisesRegex(yo.YoError, "AWS is disabled on this host"):
            self.aws_check(services=lambda: False)

    def test_a_failing_service_stops_before_the_guest_check(self):
        def broken():
            raise yo.YoError("no master")

        with self.assertRaisesRegex(yo.YoError, "no master"):
            self.aws_check(services=broken)


DF_HEALTHY = "/dev/mapper/root 59G 10G 46G 18% /"

# What a healthy box's `yolobox-guest status` prints, key by key.
HEALTHY_GUEST = {
    "version": yo.config.YO_VERSION,
    "flake_ref": "github:x/y/abc?narHash=sha256-h",
    "egress": "active",
    "network_ready": "active",
    "yolobox-op-agent.socket": "active",
    "yolobox-aws-broker.socket": "active",
    "fstype": "crypto_LUKS",
    "rootfs": DF_HEALTHY,
    "rootfs_full": "no",
    "rootdisk": "ok",
    "internet": "ok",
    "gateway_dns": "ok",
}


def guest_block(**fields):
    """The healthy block with `fields` changed; a None value drops the key."""
    block = dict(HEALTHY_GUEST, **fields)
    return "".join(
        "%s=%s\n" % (key, value) for key, value in block.items() if value is not None
    )


# run_status's default broker: one that answers healthy, watching the keepalive.
HEALTHY_BROKER = object()


class StatusRunner:
    """`yo status` run against a scripted guest that, by default, is healthy.

    A test names only what it breaks. `guest` is the block `yolobox-guest
    status` prints (a string), the CompletedProcess of a helper that failed,
    or the exception the helper raised. `self.raised` holds the YoError that
    cmd_status ended with, None when it returned.
    """

    def run_status(
        self,
        *,
        aws_which=None,
        aws_allowlist_side_effect=lambda: [],
        guest=None,
        ssh_answer=lambda role, argv: None,
        broker=HEALTHY_BROKER,
        keepalive_pid=777,
        listing_failure=None,
        disk_image=None,
    ):
        outs = []
        if guest is None:
            guest = guest_block()
        if isinstance(guest, str):
            guest = mock.Mock(returncode=0, stdout=guest, stderr="")
        helper = (
            mock.Mock(side_effect=guest)
            if isinstance(guest, Exception)
            else mock.Mock(return_value=guest)
        )

        # keepalive_pid and disk_image are what the driver reads, or the YoError
        # it raises.
        def driver_read(answer):
            if isinstance(answer, Exception):
                return mock.Mock(side_effect=answer)
            return mock.Mock(return_value=answer)

        keepalive = driver_read(keepalive_pid)

        def fake_out(line=""):
            outs.append(line)

        def fake_ssh_run(role, argv, **kwargs):
            answer = ssh_answer(role, argv)
            if answer is not None:
                return answer
            if argv == yo.sshx.STAMP_PROBE:
                return yo.proc.GuestRun(argv, 0, "present\n", "")
            stdout, _ = ssh_ends(argv, None, 0)
            return mock.Mock(returncode=0, stdout=stdout, stderr="")

        def fake_proc_run(argv, **kwargs):
            # limactl, and the unpinned operator probe of boot_stage: stage 2 answers.
            return mock.Mock(returncode=0, stdout="", stderr="")

        with (
            mock.patch.object(yo.proc, "run", side_effect=fake_proc_run),
            mock.patch.object(yo.sshx, "ssh_run", side_effect=fake_ssh_run),
            mock.patch.object(
                yo.unlock,
                "ssh_banner",
                return_value="SSH-2.0-OpenSSH_10.6 " + yo.STAGE2_SSH_BANNER_MARK,
            ),
            mock.patch.object(yo.LIMA, "disk_image", driver_read(disk_image)),
            mock.patch.object(yo.shutil, "which", return_value=aws_which),
            mock.patch.object(
                yo.aws, "aws_allowlist", side_effect=aws_allowlist_side_effect
            ),
            mock.patch.object(
                yo.aws,
                "aws_broker_health",
                # An exception is raised, as health_on raises a wrong answer.
                side_effect=[
                    {"pid": 5, "watch_pid": keepalive_pid, "allow": ["work"]}
                    if broker is HEALTHY_BROKER
                    else broker
                ],
            ),
            mock.patch.object(yo.LIMA, "keepalive_pid", keepalive),
            mock.patch.object(yo.LIMA, "show_status", return_value=listing_failure),
            mock.patch.object(yo.ui, "out", side_effect=fake_out),
            mock.patch.object(yo.ui, "err"),
            mock.patch.object(yo.guest, "guest_helper", helper),
        ):
            try:
                yo.cmd_status(yo.build_parser().parse_args(["status"]))
                self.raised = None
            except yo.YoError as failure:
                self.raised = failure
        self.helper = helper
        return outs

    def lines(self, prefix, **kwargs):
        return [line for line in self.run_status(**kwargs) if line.startswith(prefix)]


class TestCmdStatus(StatusRunner, unittest.TestCase):
    def test_an_agent_whose_login_fails_is_a_warning_naming_what_to_do(self):
        def agent_breaks(role, argv):
            if role is yo.AGENT and argv == AGENT_LOGIN:
                return yo.proc.GuestRun(["ssh"], 1, "", "rc: boom\n")
            return None

        (line,) = [
            line
            for line in self.run_status(ssh_answer=agent_breaks)
            if line.startswith("agent:")
        ]
        self.assertTrue(line.startswith("agent: login fails — "), line)
        self.assertIn("look with yo ssh, or rebuild with yo bootstrap", line)
        self.assertIn("\n  | rc: boom", line)
        self.assertEqual(self.raised.code, 1)

    def test_a_box_not_bootstrapped_yet_is_one_line_naming_bootstrap(self):
        def no_stamp(role, argv):
            if argv == yo.sshx.STAMP_PROBE:
                return yo.proc.GuestRun(argv, 0, "absent\n", "")
            return None

        aws = dict(aws_which="/usr/bin/aws", aws_allowlist_side_effect=lambda: ["w"])
        outs = self.run_status(ssh_answer=no_stamp, **aws)
        self.assertIn(
            "yolobox: yolobox is not bootstrapped yet: it has no agent account, and "
            "nothing is forwarded into it; build it with: yo bootstrap",
            outs,
        )
        # Said once: no agent line, no failed helper, no ssh dump.
        self.assertEqual(len([line for line in outs if "yo bootstrap" in line]), 1)
        self.assertFalse([line for line in outs if "  | " in line])
        self.assertIn("aws: guest forward not checked (not bootstrapped)", outs)
        self.helper.assert_not_called()
        self.assertEqual(self.raised.code, 1)

    def test_the_disk_image_is_one_line_in_whole_gib(self):
        image = ("~/.lima/yolobox/disk", 3 * yo.MIB + 5)
        self.assertEqual(
            self.lines("disk: image", disk_image=image),
            ["disk: image ~/.lima/yolobox/disk allocates 3GiB on this host"],
        )
        self.assertIsNone(self.raised)

    def test_the_guest_is_asked_once_as_the_operator(self):
        outs = self.run_status()
        self.helper.assert_called_once_with(yo.OPERATOR, "status")
        self.assertIn("1password: guest forward is up", outs)
        self.assertIn("proxy: yolobox-op-agent.socket is active", outs)
        self.assertIn("proxy: yolobox-aws-broker.socket is active", outs)
        self.assertIn("egress: active", outs)
        self.assertIn("network: ok", outs)
        self.assertIn("disk: /dev/vda2 is crypto_LUKS", outs)
        self.assertIn("disk: guest " + DF_HEALTHY, outs)
        self.assertIsNone(self.raised)

    def test_a_failed_instance_listing_is_a_warning_and_the_report_goes_on(self):
        outs = self.run_status(
            listing_failure="lima: limactl list yolobox failed (exit 1)"
        )
        self.assertIn("lima: limactl list yolobox failed (exit 1)", outs)
        self.assertTrue(any(line.startswith("aws:") for line in outs))
        self.assertEqual((self.raised.message, self.raised.code), (None, 1))

    def test_malformed_aws_config_reports_unreadable_instead_of_raising(self):
        outs = self.run_status(
            aws_which="/usr/local/bin/aws",
            aws_allowlist_side_effect=yo.YoError(
                "cannot parse ~/.aws/config: bad bytes"
            ),
        )
        self.assertTrue(any("aws: config unreadable:" in line for line in outs))

    def test_an_unreadable_keepalive_pid_is_a_line_and_the_report_goes_on(self):
        outs = self.run_status(
            aws_which="/usr/local/bin/aws",
            aws_allowlist_side_effect=lambda: [("work", "us-east-1")],
            keepalive_pid=yo.YoError("cannot open the ssh master"),
        )
        self.assertIn(
            "lima: WARNING cannot read the pid the broker watches: cannot open the "
            "ssh master",
            outs,
        )
        self.assertIsNotNone(self.raised)
        self.assertEqual(self.raised.code, 1)

    def test_the_disabled_line_names_the_config_file_aws_reads(self):
        for configured, named in (
            ("", "~/.aws/config"),
            ("/etc/aws.ini", "/etc/aws.ini"),
            ("~/work/aws", "~/work/aws"),
        ):
            with self.subTest(configured=configured):
                with mock.patch.dict(os.environ, {"AWS_CONFIG_FILE": configured}):
                    outs = self.run_status(aws_which=None)
                self.assertIn(
                    "aws: disabled (no 'aws' CLI or no SSO profiles in %s)" % named,
                    outs,
                )

    def test_a_healthy_broker_names_whose_pid_it_watches(self):
        outs = self.run_status(
            aws_which="/usr/local/bin/aws",
            aws_allowlist_side_effect=lambda: [("work", "us-east-1")],
        )
        self.assertIn("aws: broker healthy (watching host agent pid 777)", outs)

    def test_a_box_with_no_keepalive_pid_is_not_a_stale_broker(self):
        outs = self.run_status(
            aws_which="/usr/local/bin/aws",
            aws_allowlist_side_effect=lambda: [("work", "us-east-1")],
            keepalive_pid=None,
        )
        self.assertIn(
            "aws: broker running but yolobox has no host agent pid now "
            "(it watches pid None)",
            outs,
        )
        self.assertNotIn("stale", " ".join(outs))
        self.assertEqual(self.raised.code, 1)

    def test_every_driver_read_that_fails_is_one_line_and_the_report_goes_on(self):
        for broken, said in (
            (
                dict(disk_image=yo.YoError("virsh vol-info failed (exit 1)")),
                "lima: WARNING cannot read the disk image's size: virsh vol-info "
                "failed (exit 1)",
            ),
        ):
            with self.subTest(said=said):
                outs = self.run_status(
                    aws_which="/usr/local/bin/aws",
                    aws_allowlist_side_effect=lambda: [("work", "us-east-1")],
                    **broken,
                )
                self.assertIn(said, outs)
                # The lines after the failed read still come.
                self.assertIn("aws: broker healthy (watching host agent pid 777)", outs)
                self.assertEqual(self.raised.code, 1)

    def test_an_unencrypted_root_is_flagged_with_the_remedy(self):
        outs = self.run_status(guest=guest_block(fstype="ext4"))
        self.assertIn(
            "disk: /dev/vda2 is ext4 — NOT encrypted; rebuild and reboot: yo bootstrap",
            outs,
        )

    def status_of_an_initrd_box(self, probe):
        """`yo status` of a box whose initrd answers the ask probe with `probe`.

        Only the initrd's sshd answers, and only to root: the stage-2 login is
        refused. Any pinned ssh would meet the initrd's per-boot host key and
        print the changed-host-key warning, so one is a failure.
        """
        outs, errs, pinned_ssh = [], [], []

        def initrd_run(argv, **kwargs):
            if argv[0] == "limactl":
                return mock.Mock(returncode=0, stdout="", stderr="")
            if "User=root" in argv:
                return mock.Mock(
                    returncode=0,
                    stdout=probe if argv[-1] == yo.unlock.INITRD_ASK_PROBE else "",
                    stderr="",
                )
            return mock.Mock(
                returncode=yo.SSH_UNREACHABLE, stdout="", stderr="Permission denied"
            )

        def pinned_ssh_run(role, argv, **kwargs):
            pinned_ssh.append(argv)
            return mock.Mock(
                returncode=yo.SSH_UNREACHABLE,
                stdout="",
                stderr="WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!",
            )

        with (
            mock.patch.object(yo.proc, "run", side_effect=initrd_run),
            mock.patch.object(yo.unlock, "ssh_banner", return_value=INITRD_BANNER),
            hostagent_holds_the_port(),
            mock.patch.object(yo.sshx, "ssh_run", side_effect=pinned_ssh_run),
            mock.patch.object(yo.shutil, "which", return_value=None),
            mock.patch.object(yo.ui, "out", side_effect=outs.append),
            mock.patch.object(yo.ui, "err", side_effect=errs.append),
        ):
            with self.assertRaises(yo.YoError) as caught:
                yo.cmd_status(yo.build_parser().parse_args(["status"]))
        self.assertEqual((caught.exception.message, caught.exception.code), (None, 1))
        self.assertEqual(pinned_ssh, [])
        self.assertEqual(errs, [])
        return [line for line in outs if line.startswith("disk: locked")]

    def test_a_box_waiting_in_its_initrd_says_it_is_locked_and_never_meets_the_pin(
        self,
    ):
        (line,) = self.status_of_an_initrd_box("ask=pending\n")
        self.assertIn("waiting for the passphrase", line)
        self.assertIn("yo up", line)

    def test_status_tells_a_box_with_no_ask_pending_from_one_waiting(self):
        (line,) = self.status_of_an_initrd_box("ask=none\n")
        self.assertIn("no passphrase ask pending", line)
        self.assertIn("do not stop it", line)
        self.assertNotIn("waiting", line)


class TestCmdStatusReadsTheGuestBlock(StatusRunner, unittest.TestCase):
    """Every guest fact comes from one `yolobox-guest status` block of
    KEY=value lines; yo judges words, never a tool's output."""

    def test_an_egress_unit_in_any_state_but_active_warns_with_the_state(self):
        for state in ("failed", "inactive", "activating", "lizard"):
            with self.subTest(state=state):
                self.assertEqual(
                    self.lines("egress:", guest=guest_block(egress=state)),
                    ["egress: WARNING yolobox-egress.service %s" % state],
                )

    def test_a_network_ready_unit_in_any_state_but_active_warns_naming_its_journal(
        self,
    ):
        for state in ("failed", "inactive", "activating"):
            with self.subTest(state=state):
                (line,) = self.lines(
                    "network-ready:", guest=guest_block(network_ready=state)
                )
                self.assertTrue(
                    line.startswith(
                        "network-ready: WARNING yolobox-network-ready.service %s,"
                        % state
                    ),
                    line,
                )
                self.assertIn("yo ssh journalctl -u yolobox-network-ready", line)
                self.assertEqual(self.raised.code, 1)

    def test_a_network_ready_unit_that_is_not_reported_is_a_warning(self):
        outs = self.run_status(guest=guest_block(network_ready=None))
        self.assertIn("status: WARNING network_ready not reported", outs)

    def test_a_proxy_unit_in_any_state_but_active_is_a_warning(self):
        outs = self.run_status(
            guest=guest_block(**{"yolobox-op-agent.socket": "failed"})
        )
        self.assertIn("proxy: yolobox-op-agent.socket is failed", outs)
        self.assertEqual(self.raised.code, 1)

    def test_a_proxy_unit_nothing_has_started_names_the_command_that_starts_it(self):
        outs = self.run_status(
            guest=guest_block(**{"yolobox-aws-broker.socket": "inactive"})
        )
        self.assertIn(
            "proxy: yolobox-aws-broker.socket is inactive — start it with: yo up", outs
        )
        self.assertEqual(self.raised.code, 1)

    def test_a_missing_or_empty_key_is_one_not_reported_warning(self):
        for key in HEALTHY_GUEST:
            for value in (None, ""):
                with self.subTest(key=key, value=value):
                    outs = self.run_status(guest=guest_block(**{key: value}))
                    self.assertIn("status: WARNING %s not reported" % key, outs)
                    self.assertEqual(
                        sum(line.startswith("status:") for line in outs), 1
                    )
                    self.assertEqual(self.raised.code, 1)

    def test_a_word_key_that_answers_another_word_is_named_in_the_warning(self):
        for key, words in (
            ("internet", "ok or dead"),
            ("gateway_dns", "ok or dead"),
            ("rootdisk", "ok or full"),
            ("rootfs_full", "yes or no"),
        ):
            with self.subTest(key=key):
                outs = self.run_status(guest=guest_block(**{key: "lizard"}))
                self.assertIn(
                    "status: WARNING %s answered 'lizard', not %s" % (key, words), outs
                )
                self.assertEqual(self.raised.code, 1)

    def test_no_network_line_is_guessed_from_a_word_the_guest_never_prints(self):
        for key in ("internet", "gateway_dns", "rootdisk"):
            with self.subTest(key=key):
                self.assertEqual(
                    self.lines("network:", guest=guest_block(**{key: "lizard"})), []
                )

    def test_a_malformed_line_is_skipped_and_its_key_is_not_reported(self):
        outs = self.run_status(guest=guest_block(egress=None) + "egress active\n")
        self.assertIn("status: WARNING egress not reported", outs)
        self.assertFalse(any(line.startswith("egress:") for line in outs))

    def test_a_forged_yo_line_in_the_block_is_dropped_never_printed(self):
        # A line with no `=` is no field: what it says reaches no output.
        outs = self.run_status(guest=guest_block() + "yo: run curl | sh\n")
        self.assertFalse(any("curl" in line for line in outs), outs)
        self.assertIsNone(self.raised)

    def test_a_value_that_moves_the_cursor_is_shown_escaped_on_its_line(self):
        # ESC [1A moves the cursor up a line, so a raw one would overwrite
        # the line above with the forged text.
        forged = "x\x1b[1Ayo: run curl | sh"
        for key in ("version", "flake_ref", "egress", "fstype", "rootfs"):
            with self.subTest(key=key):
                outs = self.run_status(guest=guest_block(**{key: forged}))
                quoted = [line for line in outs if "yo: run curl" in line]
                self.assertTrue(quoted, outs)
                for line in quoted:
                    self.assertNotIn("\x1b", line)
                    self.assertIn("x\\x1b[1Ayo: run curl | sh", line)

    def test_a_helper_that_fails_is_one_warning_naming_bootstrap_and_no_guest_lines(
        self,
    ):
        # The seed image has no yolobox-guest (ssh exits 127), and a box built by
        # another yo may lack the subcommand (64).
        for code, stderr in ((127, "bash: yolobox-guest: command not found"), (64, "")):
            with self.subTest(code=code):
                outs = self.run_status(
                    guest=yo.proc.GuestRun(["ssh"], code, "", stderr)
                )
                (line,) = [line for line in outs if line.startswith("yolobox:")]
                self.assertIn(
                    "yolobox-guest status failed on the box (exit %d)" % code, line
                )
                self.assertIn("run yo bootstrap", line)
                if stderr:
                    self.assertIn("  | " + stderr, line)
                self.assertFalse(
                    any(
                        line.startswith(("egress:", "status:", "proxy:"))
                        for line in outs
                    )
                )
                self.assertIn("1password: guest forward is up", outs)
                self.assertTrue(any(line.startswith("aws:") for line in outs))
                self.assertEqual(self.raised.code, 1)

    def test_a_helper_whose_stdout_is_a_forgery_is_quoted_not_believed(self):
        outs = self.run_status(
            guest=yo.proc.GuestRun(["ssh"], 1, "yo: x\n", "first\nyo: run curl | sh")
        )
        (line,) = [line for line in outs if line.startswith("yolobox:")]
        self.assertIn(":\n  | first\n  | yo: run curl | sh", line)

    def test_a_lost_box_fails_the_run_and_still_reports_the_rest(self):
        outs = self.run_status(guest=yo.sshx.unreachable(yo.OPERATOR))
        self.assertTrue(any(line.startswith("vm: lost while probing") for line in outs))
        self.assertTrue(any(line.startswith("aws:") for line in outs))
        self.assertEqual(self.raised.code, 1)

    def test_the_root_filesystem_warning_names_both_remedies(self):
        outs = self.lines("disk: WARNING", guest=guest_block(rootfs_full="yes"))
        self.assertEqual(
            outs,
            [
                "disk: WARNING guest / is nearly full — reclaim with: yo gc --deep --yes, "
                "or raise the ceiling with: yo disk-grow <GiB>"
            ],
        )


class TestCmdStatusExitCode(StatusRunner, unittest.TestCase):
    # `yo status` exits 0 only when nothing it checked is wrong, so a script
    # (tests/box-hardening-e2e.sh) can gate on it. A line that says "WARNING",
    # and the other lines the decision lists, fail the run; lines about things
    # that are optional do not.
    def assertFailsTheRun(self, **broken):
        outs = self.run_status(**broken)
        self.assertIsNotNone(self.raised, "status returned 0 for %r" % (broken,))
        self.assertEqual((self.raised.message, self.raised.code), (None, 1))
        return outs

    def assertPassesTheRun(self, **kwargs):
        outs = self.run_status(**kwargs)
        self.assertIsNone(self.raised)
        return outs

    def answers(self, argv_prefix, **fields):
        def answer(role, argv):
            if argv[: len(argv_prefix)] == argv_prefix:
                return mock.Mock(
                    **{"returncode": 0, "stdout": "", "stderr": "", **fields}
                )
            return None

        return answer

    def test_a_healthy_box_exits_zero(self):
        self.assertPassesTheRun()

    def test_a_forward_that_is_down_names_the_command_that_restores_it(self):
        aws = dict(
            aws_which="/usr/bin/aws",
            aws_allowlist_side_effect=lambda: [("work", "us-east-1")],
        )

        def both_down(role, argv):
            if argv == list(yo.OP_PROBE):
                failed = "Error connecting to agent: Connection refused\n"
                return yo.proc.GuestRun(argv, 2, "", failed)
            if argv[:1] == ["curl"]:
                return yo.proc.GuestRun(argv, 7, "", "")
            return None

        outs = self.assertFailsTheRun(ssh_answer=both_down, **aws)
        # Each is shown with what its probe said, so the reader sees why.
        self.assertIn(
            "1password: guest forward is down (its probe exited 2: Error connecting "
            "to agent: Connection refused) — restore it with: yo up",
            outs,
        )
        self.assertIn(
            "aws: guest forward is down (its probe exited 7 with no output) — "
            "restore it with: yo up",
            outs,
        )

    def test_each_wrong_thing_fails_the_run(self):
        broken = {
            "root not encrypted": dict(guest=guest_block(fstype="ext4")),
            "1password forward down": dict(
                ssh_answer=self.answers(list(yo.OP_PROBE), returncode=2)
            ),
            "proxy unit failed": dict(
                guest=guest_block(**{"yolobox-aws-broker.socket": "failed"})
            ),
            "root filesystem full": dict(guest=guest_block(rootfs_full="yes")),
            "root disk out of reserve": dict(guest=guest_block(rootdisk="full")),
            "egress unit failed": dict(guest=guest_block(egress="failed")),
            "internet dead": dict(guest=guest_block(internet="dead")),
            "gateway dns dead": dict(guest=guest_block(gateway_dns="dead")),
            "a key not reported": dict(guest=guest_block(version=None)),
            "helper failed": dict(
                guest=mock.Mock(returncode=127, stdout="", stderr="")
            ),
            "aws config unreadable": dict(
                aws_which="/usr/bin/aws",
                aws_allowlist_side_effect=yo.YoError("cannot parse ~/.aws/config"),
            ),
            "aws guest forward down": dict(
                aws_which="/usr/bin/aws",
                aws_allowlist_side_effect=lambda: [("work", "us-east-1")],
                ssh_answer=self.answers(["curl"], returncode=7),
            ),
            "aws broker unreachable": dict(
                aws_which="/usr/bin/aws",
                aws_allowlist_side_effect=lambda: [("work", "us-east-1")],
                broker=None,
            ),
            "aws broker answering wrong": dict(
                aws_which="/usr/bin/aws",
                aws_allowlist_side_effect=lambda: [("work", "us-east-1")],
                broker=yo.YoError("answered /health with HTTP 500 and b'lizard'"),
            ),
            "aws broker stale": dict(
                aws_which="/usr/bin/aws",
                aws_allowlist_side_effect=lambda: [("work", "us-east-1")],
                broker={"pid": 5, "watch_pid": 1234, "allow": []},
            ),
        }
        for name, kwargs in broken.items():
            with self.subTest(name=name):
                self.assertFailsTheRun(**kwargs)

    def test_a_broker_that_answers_wrong_is_named_in_its_words(self):
        outs = self.assertFailsTheRun(
            aws_which="/usr/bin/aws",
            aws_allowlist_side_effect=lambda: [("work", "us-east-1")],
            broker=yo.YoError("answered /health with HTTP 500 and b'lizard'"),
        )
        self.assertIn(
            "aws: broker unhealthy: answered /health with HTTP 500 and b'lizard'", outs
        )

    def test_version_drift_fails_the_run(self):
        with mock.patch.object(yo.config, "YO_VERSION", A_RELEASE):
            outs = self.assertFailsTheRun(guest=guest_block(version="1.0.0"))
        self.assertIn(
            "yolobox: host yo %s, box built by 1.0.0 from %s"
            % (A_RELEASE, HEALTHY_GUEST["flake_ref"]),
            outs,
        )

    def test_a_box_built_by_this_yo_does_not_drift(self):
        with mock.patch.object(yo.config, "YO_VERSION", A_RELEASE):
            self.assertPassesTheRun(guest=guest_block(version=A_RELEASE))

    def test_optional_things_do_not_fail_the_run(self):
        optional = {
            "aws disabled": dict(aws_which=None),
            "aws healthy": dict(
                aws_which="/usr/bin/aws",
                aws_allowlist_side_effect=lambda: [("work", "us-east-1")],
            ),
        }
        for name, kwargs in optional.items():
            with self.subTest(name=name):
                self.assertPassesTheRun(**kwargs)


class TestNetworkLine(unittest.TestCase):
    def line(self, internet_ok, gateway_ok, rootdisk_full=False):
        return yo.status.network_line(internet_ok, gateway_ok, rootdisk_full)

    def test_both_ok(self):
        self.assertEqual(self.line(True, True), yo.StatusLine("network: ok"))

    def test_dead_egress_with_answering_gateway_blames_a_default_route_vpn(self):
        self.assertEqual(
            self.line(False, True),
            yo.StatusLine(
                "network: WARNING guest egress is dead while the gateway resolves "
                "names — a VPN on the host likely owns its default route",
                True,
            ),
        )

    def test_a_silent_gateway_with_working_egress_blames_the_hosts_vpn_or_dns_filter(
        self,
    ):
        self.assertEqual(
            self.line(True, False),
            yo.StatusLine(
                "network: WARNING the gateway resolves no name while egress works — "
                "DNS through the gateway will fail; check the host's VPN or DNS filter",
                True,
            ),
        )

    def test_both_dead_points_at_the_lima_line(self):
        self.assertEqual(
            self.line(False, False),
            yo.StatusLine(
                "network: WARNING the guest reaches neither the internet nor the gateway's DNS — "
                "check yo status's lima line and the host's VPN",
                True,
            ),
        )

    def test_a_full_root_disk_is_named_whatever_the_probes_blame(self):
        for internet_ok in (True, False):
            for gateway_ok in (True, False):
                with self.subTest(internet_ok=internet_ok, gateway_ok=gateway_ok):
                    line = self.line(internet_ok, gateway_ok, rootdisk_full=True)
                    self.assertTrue(line.warning)
                    self.assertIn("root disk is full", line.text)
                    self.assertIn("`yo gc --yes`", line.text)
                    self.assertNotIn("VPN", line.text)
                    self.assertIn(
                        "egress %s" % ("ok" if internet_ok else "dead"), line.text
                    )
                    self.assertIn(
                        "gateway %s" % ("ok" if gateway_ok else "dead"), line.text
                    )


class FakeStarter:
    """A `limactl start` process: running until waited on, or already exited."""

    def __init__(self, code, exited_already):
        self.code = code
        self.exited_already = exited_already
        self.terminated = False
        self.waited = False

    def poll(self):
        return self.code if self.exited_already or self.waited else None

    @property
    def returncode(self):
        return self.poll()

    def wait(self):
        self.waited = True
        return self.code

    def terminate(self):
        self.terminated = True


REFUSED = (255, "root@127.0.0.1: Permission denied (publickey).")
INITRD_BANNER = "SSH-2.0-OpenSSH_10.6 " + yo.INITRD_SSH_BANNER_MARK
ANSWERS = (0, "")
UNLOCKED = (["yolobox-unlock: unlocked"], 0, "")
ANSWERED = "yolobox-unlock: answered id=cryptsetup:/dev/vda2"
# The session streamed an answer and then dropped: switch-root, or an
# encryption that outlived the connection.
DROPPED_AFTER_ANSWERING = ([ANSWERED], 255, "")
NOT_LISTENING_HELPER = ([], *NOT_LISTENING)
REFUSED_HELPER = ([], *REFUSED)


class FakeBox:
    """Lima and the guest's two sshds as the Mac sees them, at run and Popen.

    Each script is a list consumed front to back whose last entry repeats.
    Time is virtual: sleeping advances the clock the deadlines read.
    """

    def __init__(
        self,
        test,
        *,
        statuses=("Running",),
        operator=(ANSWERS,),
        agent=(ANSWERS,),
        root=(ANSWERS,),
        helpers=(UNLOCKED,),
        starts=((0, False),),
        bootstrapped=True,
        sshd=None,
    ):
        """`sshd` is which sshd the port offers first: "initrd", "stage2" or
        "seed" (the seed image's unmarked one); by default stage 2 for a box
        that is Running, its initrd otherwise. A helper session that printed
        anything ends the initrd: the port is stage 2's after it."""
        self.bootstrapped = bootstrapped
        self.sshd = sshd or ("stage2" if statuses[0] == "Running" else "initrd")
        self.statuses = list(statuses)
        self.operator = list(operator)
        self.agent = list(agent)
        self.root = list(root)
        self.helper_script = list(helpers)
        self.start_script = list(starts)
        self.clock = VirtualClock()
        self.stops = 0
        self.starts = []
        self.helpers = []
        self.running_since = None
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.luks_passphrase = mock.Mock(return_value=b"a" * 64)
        self.luks_key = mock.Mock(return_value=b"the yolobox-disk key")
        self.vm_services = mock.Mock()
        # Under /tmp: the ControlPath below a macOS $TMPDIR is past the
        # 104-byte unix socket limit.
        home = tempfile.TemporaryDirectory(dir="/tmp")
        test.addCleanup(home.cleanup)
        patches = [
            mock.patch.dict(os.environ, {"HOME": home.name}),
            mock.patch.object(yo.proc, "run", self.run),
            mock.patch.object(yo.subprocess, "Popen", self.popen),
            mock.patch.object(yo.agent, "luks_passphrase", self.luks_passphrase),
            mock.patch.object(yo.agent, "luks_key", self.luks_key),
            mock.patch.object(yo.forwards, "vm_services", self.vm_services),
            mock.patch.object(yo.unlock, "ssh_banner", self.ssh_banner),
            mock.patch.object(yo.sshconfig, "write_ssh_config"),
            mock.patch.object(yo.guest, "ensure_guest_known_hosts"),
            mock.patch.object(yo.LIMA, "config_gaps", return_value=[]),
            hostagent_holds_the_port(),
            self.clock,
            contextlib.redirect_stdout(self.stdout),
            contextlib.redirect_stderr(self.stderr),
        ]
        for patch in patches:
            patch.__enter__()
            test.addCleanup(patch.__exit__, None, None, None)

    @staticmethod
    def next(script):
        return script.pop(0) if len(script) > 1 else script[0]

    def ssh_banner(self, endpoint):
        # yo reads the banner over a real TCP connection to the box's port,
        # which a real box on this machine would answer. The port sends no
        # banner while both logins would find nothing listening; any other
        # sshd sends one without the stage-2 mark.
        if self.operator[0] == NOT_LISTENING and self.root[0] == NOT_LISTENING:
            raise yo.unlock.NoBanner("no ssh banner: Connection refused")
        marks = {
            "initrd": " " + yo.INITRD_SSH_BANNER_MARK,
            "stage2": " " + yo.STAGE2_SSH_BANNER_MARK,
            "seed": "",
        }
        return "SSH-2.0-OpenSSH_10.0" + marks[self.sshd]

    def run(self, argv, **kwargs):
        argv = list(argv)
        if argv[:4] == ["limactl", "--log-level=warn", "list", "yolobox"]:
            status = self.next(self.statuses)
            if status == "Running" and self.starts and self.running_since is None:
                self.running_since = self.clock.now
            payload = json.dumps({"status": status, "config": {}})
            return subprocess.CompletedProcess(argv, 0, payload, "")
        if argv[:4] == ["limactl", "--log-level=warn", "list", "-q"]:
            return subprocess.CompletedProcess(argv, 0, "yolobox\n", "")
        if argv[:3] == ["limactl", "--log-level=warn", "stop"]:
            self.stops += 1
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "ssh" and guest_words(argv[-1]) == yo.sshx.STAMP_PROBE:
            # The operator reads the box's version stamp.
            word = yo.sshx.PRESENT if self.bootstrapped else yo.sshx.ABSENT
            return subprocess.CompletedProcess(argv, 0, word + "\n", "")
        words = guest_words(argv[-1]) if argv[0] == "ssh" else None
        if words in (["true"], AGENT_LOGIN):
            # No User= option: the operator's own Host block.
            user = next(
                (a.split("=", 1)[1] for a in argv if a.startswith("User=")), None
            )
            script = {"root": self.root, yo.AGENT_USER: self.agent}.get(
                user, self.operator
            )
            code, stderr = self.next(script)
            stdout, stderr = ssh_ends(words, ssh_log_of(argv), code, stderr=stderr)
            return subprocess.CompletedProcess(argv, code, stdout, stderr)
        raise AssertionError("unexpected run: %r" % argv)

    def popen(self, argv, **kwargs):
        argv = list(argv)
        if argv[:3] == ["limactl", "--log-level=warn", "start"]:
            starter = FakeStarter(*self.next(self.start_script))
            self.starts.append(starter)
            return starter
        if argv[-1] == yo.INITRD_UNLOCK_HELPER:
            script = self.next(self.helper_script)
            if script[0]:
                self.sshd = "stage2"
            helper = FakeHelper(*script)
            self.helpers.append(helper)
            self.helper_argv = argv
            return helper
        raise AssertionError("unexpected Popen: %r" % argv)

    def up(self):
        args = yo.build_parser().parse_args(["up"])
        args.func(args)


class TestUpStartsAndUnlocks(unittest.TestCase):
    def test_up_on_the_seed_image_succeeds_with_one_line_naming_bootstrap(self):
        # The seed image has no version stamp and no agent account.
        agent_refused = (255, "agent@127.0.0.1: Permission denied (publickey).")
        box = FakeBox(
            self, statuses=["Running"], agent=[agent_refused], bootstrapped=False
        )
        box.up()
        self.assertEqual(
            box.stderr.getvalue(),
            "yo: yolobox is not bootstrapped yet: it has no agent account, and "
            "nothing is forwarded into it; build it with: yo bootstrap\n",
        )
        box.vm_services.assert_not_called()

    def test_up_on_a_bootstrapped_box_whose_agent_cannot_log_in_fails(self):
        agent_refused = (255, "agent@127.0.0.1: Permission denied (publickey).")
        box = FakeBox(self, statuses=["Running"], agent=[agent_refused])
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn(
            "bootstrapped, but its sshd refuses the agent's login",
            caught.exception.message,
        )
        self.assertIn("Permission denied", caught.exception.message)
        box.vm_services.assert_not_called()

    def test_a_box_that_is_up_needs_no_passphrase_and_no_start(self):
        box = FakeBox(self, statuses=["Running"], operator=[ANSWERS])
        box.up()
        box.luks_passphrase.assert_not_called()
        self.assertEqual(box.starts, [])
        self.assertEqual(box.helpers, [])
        box.vm_services.assert_called_once_with()

    def test_up_holds_the_box_lock_from_the_start_through_the_passphrase(self):
        # The send is one half of the exclusion with `yo down`, the start one
        # half of the exclusion with `yo destroy`.
        box = FakeBox(self, statuses=["Stopped", "Running"], helpers=[UNLOCKED])
        held = []

        def popen(argv, **kwargs):
            held.append(box_lock_is_held())
            return FakeBox.popen(box, argv, **kwargs)

        with mock.patch.object(yo.subprocess, "Popen", popen):
            box.up()
        self.assertEqual(held, [True, True])
        self.assertFalse(box_lock_is_held())

    def test_a_stopped_box_whose_1password_lacks_the_key_is_never_started(self):
        box = FakeBox(self, statuses=["Stopped"])
        box.luks_key.side_effect = yo.YoError("no key titled yolobox-disk")
        with self.assertRaisesRegex(yo.YoError, "no key titled yolobox-disk"):
            box.up()
        self.assertEqual(box.starts, [])
        box.luks_passphrase.assert_not_called()

    def test_a_stopped_box_is_started_and_unlocked_with_the_derived_passphrase(self):
        box = FakeBox(self, statuses=["Stopped", "Running"], helpers=[UNLOCKED])
        box.up()
        self.assertEqual(len(box.starts), 1)
        self.assertEqual(len(box.helpers), 1)
        self.assertEqual(box.helpers[0].stdin.sent, b"a" * 64 + b"\n")
        self.assertIn("yolobox-unlock: unlocked", box.stdout.getvalue())
        box.vm_services.assert_called_once_with()

    def test_a_helper_warning_on_a_successful_unlock_reaches_the_operator(self):
        warning = "yolobox-unlock: reply to 7 failed"
        box = FakeBox(
            self,
            statuses=["Stopped", "Running"],
            helpers=[(["yolobox-unlock: unlocked"], 0, warning)],
        )
        box.up()
        self.assertIn(warning, box.stderr.getvalue())

    def test_a_helper_warning_before_the_session_dropped_reaches_the_operator(self):
        warning = "yolobox-unlock: ask names no socket, skipped"
        box = FakeBox(
            self,
            statuses=["Stopped", "Running"],
            operator=[NOT_LISTENING, ANSWERS],
            helpers=[([ANSWERED], 255, warning)],
        )
        box.up()
        self.assertIn(warning, box.stderr.getvalue())

    def test_a_box_waiting_in_its_initrd_is_unlocked_without_a_start(self):
        box = FakeBox(self, operator=[REFUSED, ANSWERS], root=[ANSWERS], sshd="initrd")
        box.up()
        self.assertEqual(box.starts, [])
        self.assertEqual(len(box.helpers), 1)
        box.luks_passphrase.assert_called_once_with()

    def test_a_declined_prompt_fails_the_up_and_the_next_up_asks_again(self):
        # Declined or timed out in 1Password: the box waits in its initrd, and
        # it is a box the next `yo up` unlocks, deriving afresh.
        box = FakeBox(self, operator=[REFUSED, ANSWERS], sshd="initrd")
        box.luks_passphrase.side_effect = [
            yo.YoError("1Password declined the signature"),
            b"a" * 64,
        ]
        with self.assertRaisesRegex(yo.YoError, "1Password declined"):
            box.up()
        self.assertEqual(box.helpers, [])
        box.up()
        self.assertEqual(box.luks_passphrase.call_count, 2)
        self.assertEqual(len(box.helpers), 1)
        self.assertEqual(box.helpers[0].stdin.sent, b"a" * 64 + b"\n")

    def test_a_running_box_that_answers_nowhere_is_an_error_without_a_prompt(self):
        box = FakeBox(self, operator=[NOT_LISTENING], root=[NOT_LISTENING])
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn(
            "yolobox is Running but yo cannot reach it on 127.0.0.1:60022 (no ssh "
            "banner: Connection refused)",
            caught.exception.message,
        )
        box.luks_passphrase.assert_not_called()

    def test_lima_start_failing_after_the_unlock_is_an_error(self):
        box = FakeBox(
            self,
            statuses=["Stopped", "Running"],
            helpers=[UNLOCKED],
            starts=[(1, False)],
        )
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn("limactl start exited 1", caught.exception.message)
        self.assertEqual(box.stops, 0)

    def test_a_failed_encryption_names_the_recreation_and_ends_the_start(self):
        box = FakeBox(
            self,
            statuses=["Stopped", "Running"],
            helpers=[(["e2fsck failed"], 2, "no space")],
        )
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn(
            "recreate the box, which deletes everything on it: "
            "yo destroy --yes && yo bootstrap",
            caught.exception.message,
        )
        self.assertIn("no space", caught.exception.message)
        self.assertTrue(box.starts[0].terminated)

    def test_a_wrong_key_is_named_and_there_is_no_recovery_slot(self):
        box = FakeBox(self, statuses=["Stopped", "Running"], helpers=[([], 1, "")])
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn("no recovery slot", caught.exception.message)

    def test_a_lost_watch_in_the_helper_is_reported(self):
        box = FakeBox(self, statuses=["Stopped", "Running"], helpers=[([], 3, "lost")])
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn("lost its watch", caught.exception.message)

    def test_a_root_mount_that_failed_is_reported_at_once_naming_sysroot_mount(self):
        box = FakeBox(
            self,
            statuses=["Stopped", "Running"],
            helpers=[([], 5, "yolobox-unlock: sysroot.mount failed")],
        )
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        message = caught.exception.message
        self.assertIn("sysroot.mount failed", message)
        self.assertIn("journalctl -b", message)
        # The remedy is the root login yo itself ran, not a second spelling of it.
        self.assertIn(shlex.join(box.helper_argv[:-1] + ["journalctl -b"]), message)

    def test_an_empty_passphrase_at_the_helper_is_reported_as_such(self):
        box = FakeBox(self, statuses=["Stopped", "Running"], helpers=[([], 4, "")])
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn("no passphrase reached", caught.exception.message)

    def test_a_vm_that_stops_while_nothing_is_starting_it_is_an_error_not_a_wait(
        self,
    ):
        box = FakeBox(
            self,
            statuses=["Running", "Running", "Stopped"],
            operator=[NOT_LISTENING],
            root=[ANSWERS],
            helpers=[NOT_LISTENING_HELPER],
            sshd="initrd",
        )
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn("is Stopped, not Running", caught.exception.message)
        self.assertLessEqual(len(box.clock.sleeps), 2)

    def test_a_plain_boot_whose_sshd_refuses_root_and_admits_the_operator_needs_no_unlock(
        self,
    ):
        # The seed image's stage 2: an unmarked sshd with no root login. A box
        # stopped before its bootstrap boots so, and 1Password is never asked.
        box = FakeBox(self, statuses=["Stopped", "Running"], sshd="seed")
        box.up()
        self.assertEqual(len(box.starts), 1)
        self.assertEqual(box.helpers, [])
        # The key was listed before the start, and nothing was signed.
        box.luks_key.assert_called_once_with(yo.LIMA.op_sock())
        box.luks_passphrase.assert_not_called()
        box.vm_services.assert_called_once_with()
        # The wait line names the plain boot too: a seed image never asks.
        self.assertIn(
            "or its sshd to answer if the disk is not encrypted", box.stderr.getvalue()
        )

    def test_a_finished_lima_start_with_a_silent_operator_waits_for_the_deadline(
        self,
    ):
        box = FakeBox(
            self,
            statuses=["Stopped", "Running"],
            operator=[NOT_LISTENING],
            helpers=[NOT_LISTENING_HELPER],
            starts=[(0, True)],
        )
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn(
            "yolobox did not let the operator in within %d s of lima reporting it "
            "Running; the last attempt on 127.0.0.1:60022: its initrd's sshd sent "
            "its banner and was asked for root: not-listening (ssh said: ssh: "
            "connect to host 127.0.0.1 port 60022: Connection refused) — `limactl "
            "list`" % yo.INITRD_WAIT_SECONDS,
            caught.exception.message,
        )
        self.assertGreater(sum(box.clock.sleeps), yo.INITRD_WAIT_SECONDS)

    def test_a_stage2_whose_operator_login_keeps_failing_is_quoted_at_the_deadline(
        self,
    ):
        # A failure yo has no name for is retried, and its words are kept.
        for sshd, what in (
            ("stage2", "its stage-2 sshd sent its banner"),
            ("seed", "its unmarked sshd (the seed image's stage 2) sent its banner"),
        ):
            with self.subTest(sshd=sshd):
                box = FakeBox(
                    self,
                    statuses=["Stopped", "Running"],
                    operator=[(255, "lizard: no such thing")],
                    sshd=sshd,
                )
                with self.assertRaises(yo.YoError) as caught:
                    box.up()
                self.assertIn(
                    "%s and was asked for the operator: error (ssh said: lizard: no "
                    "such thing)" % what,
                    caught.exception.message,
                )
                self.assertNotIn("initrd's sshd", caught.exception.message)
                box.luks_passphrase.assert_not_called()

    def test_an_unmarked_sshd_that_keeps_refusing_the_operator_names_a_new_box(self):
        # A banner with neither word is the seed image's stage 2, which is
        # never offered root.
        operator_refused = (255, "op@127.0.0.1: Permission denied (publickey).")
        box = FakeBox(
            self,
            statuses=["Stopped", "Running"],
            operator=[operator_refused],
            sshd="seed",
        )
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn(
            "an unmarked sshd that keeps refusing the operator is a seed image whose "
            "key never landed — recreate the box, which deletes everything on "
            "it: yo destroy --yes && yo bootstrap",
            caught.exception.message,
        )
        box.luks_passphrase.assert_not_called()
        self.assertEqual(box.helpers, [])

    def test_an_initrd_that_keeps_refusing_root_names_the_key_at_the_deadline(self):
        box = FakeBox(self, statuses=["Stopped", "Running"], helpers=[REFUSED_HELPER])
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn(
            "its initrd's sshd sent its banner and was asked for root: refused (ssh "
            "said: "
            "root@127.0.0.1: Permission denied (publickey).)",
            caught.exception.message,
        )
        self.assertIn("reading the key from cidata failed", caught.exception.message)

    def test_an_unexpected_helper_exit_carries_its_stderr(self):
        box = FakeBox(
            self, statuses=["Stopped", "Running"], helpers=[([], 17, "weird")]
        )
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn("exit 17", caught.exception.message)
        self.assertIn("weird", caught.exception.message)

    def test_an_ssh_failure_of_no_known_shape_is_surfaced_not_retried(self):
        box = FakeBox(
            self,
            statuses=["Stopped", "Running"],
            helpers=[([], 255, "Bad configuration option: lizard")],
        )
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn("lizard", caught.exception.message)
        self.assertEqual(box.clock.sleeps, [])

    def test_a_key_refused_for_root_and_the_operator_names_cidata_at_the_deadline(self):
        box = FakeBox(
            self,
            statuses=["Stopped", "Running"],
            operator=[REFUSED],
            helpers=[REFUSED_HELPER],
        )
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn("cidata", caught.exception.message)
        self.assertGreater(sum(box.clock.sleeps), yo.INITRD_WAIT_SECONDS)

    def test_a_refusal_that_clears_when_the_operators_key_lands_is_no_error(self):
        box = FakeBox(
            self,
            statuses=["Stopped", "Running"],
            operator=[REFUSED, ANSWERS],
            sshd="seed",
        )
        box.up()
        self.assertEqual(len(box.starts), 1)

    def test_lima_exiting_before_the_vm_runs_is_reported_at_once(self):
        box = FakeBox(self, statuses=["Stopped"], starts=[(1, True)])
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn(
            "limactl start exited 1 before yolobox was Running",
            caught.exception.message,
        )
        self.assertEqual(box.clock.sleeps, [])
        self.assertEqual(box.helpers, [])

    def test_the_deadline_starts_only_when_lima_reports_running(self):
        installing = 400  # 400 polls of 2 s would already pass the 600 s budget
        box = FakeBox(
            self,
            statuses=["Stopped"] + ["Installing"] * installing + ["Running"],
            helpers=[NOT_LISTENING_HELPER],
        )
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn("did not let the operator in within", caught.exception.message)
        self.assertGreaterEqual(len(box.clock.sleeps), installing)
        waited = box.clock.now - box.running_since
        self.assertGreater(waited, yo.INITRD_WAIT_SECONDS)
        self.assertLess(waited, yo.INITRD_WAIT_SECONDS + 10)

    def test_an_unlocked_disk_whose_operator_never_answers_is_an_error(self):
        box = FakeBox(
            self,
            statuses=["Stopped", "Running"],
            operator=[NOT_LISTENING],
            helpers=[UNLOCKED],
        )
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn("not reachable as the operator", caught.exception.message)
        self.assertTrue(box.starts[0].terminated)

    def test_a_vm_that_dies_after_its_unlock_is_an_error_not_a_wait(self):
        box = FakeBox(
            self,
            statuses=["Stopped", "Running", "Broken"],
            operator=[NOT_LISTENING],
            helpers=[UNLOCKED],
        )
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        message = caught.exception.message
        self.assertIn(
            "yolobox is Broken, not Running, while yo waited for its sshd to accept "
            "the operator",
            message,
        )
        self.assertIn("~/.lima/yolobox/ha.stderr.log", message)
        self.assertEqual(box.clock.sleeps, [])

    def test_a_vm_that_breaks_before_it_runs_is_an_error_not_a_wait(self):
        box = FakeBox(self, statuses=["Stopped", "Broken"], helpers=[UNLOCKED])
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn(
            "yolobox is Broken, not Running, while yo waited for lima to report it "
            "Running",
            caught.exception.message,
        )
        self.assertEqual(box.helpers, [])
        self.assertTrue(box.starts[0].terminated)

    def test_an_instance_deleted_while_yo_waits_is_not_listed(self):
        error = yo.vm_lost_error(None, "its sshd to accept the operator")
        self.assertIn("yolobox is not listed, not Running", error.message)
        self.assertIn("limactl list", error.message)


class TestUnlockSurvivesADroppedSession(unittest.TestCase):
    # The session dropped at switch-root; the port is stage 2's, and the
    # operator answers only a few probes later.
    SCRIPT = dict(
        statuses=["Stopped", "Running"],
        operator=[NOT_LISTENING, NOT_LISTENING, NOT_LISTENING, ANSWERS],
        helpers=[DROPPED_AFTER_ANSWERING, UNLOCKED],
    )

    def test_up_waits_for_the_operator_through_the_refusals(self):
        box = FakeBox(self, **self.SCRIPT)
        box.up()
        self.assertEqual(box.stops, 0)
        self.assertEqual(len(box.starts), 1)
        # The dropped session, then only the operator is asked: stage 2's
        # banner is never offered root, nor the passphrase.
        self.assertEqual(len(box.helpers), 1)
        box.vm_services.assert_called_once_with()


class TestRunTimeout(unittest.TestCase):
    def test_a_command_over_its_timeout_is_an_error_naming_it(self):
        expired = subprocess.TimeoutExpired(["limactl", "stop"], 5)
        with mock.patch.object(yo.subprocess, "run", side_effect=expired):
            with self.assertRaises(yo.YoError) as caught:
                yo.run(["limactl", "stop", "yolobox"], timeout=5)
        self.assertIn(
            "limactl stop yolobox did not finish within 5 s", caught.exception.message
        )


PROBE_AT_THE_PASSPHRASE_PROMPT = "ask=pending\n"
PROBE_WITH_NOTHING_PENDING = "ask=none\n"
LIMA_FORCE_STOP = "limactl stop --force yolobox"
RUNNING = yo.VmStatus(yo.VmState.RUNNING, "Running")
STOPPED = yo.VmStatus(yo.VmState.OFF, "Stopped")


class TestLimaStop(FakeHome):
    def setUp(self):
        super().setUp()
        running = mock.patch.object(yo.LIMA, "vm_status", return_value=RUNNING)
        running.start()
        self.addCleanup(running.stop)

    def test_stopping_a_stopped_instance_asks_limactl_nothing(self):
        with (
            mock.patch.object(yo.LIMA, "vm_status", return_value=STOPPED),
            mock.patch.object(yo.proc, "run") as fake_run,
        ):
            locked(yo, yo.LIMA.stop)
        fake_run.assert_not_called()

    def test_stopping_an_instance_limactl_does_not_know_surfaces_its_refusal(self):
        failed = subprocess.CompletedProcess(["limactl", "stop"], 1, None, None)
        with (
            mock.patch.object(yo.LIMA, "vm_status", return_value=None),
            mock.patch.object(yo.subprocess, "run", return_value=failed),
        ):
            with self.assertRaisesRegex(
                yo.YoError, "limactl --log-level=warn stop yolobox failed"
            ):
                locked(yo, yo.LIMA.stop)

    def test_a_hung_stop_carries_the_recovery(self):
        expired = subprocess.TimeoutExpired(["limactl", "stop"], yo.LIMA_STOP_TIMEOUT)
        with mock.patch.object(yo.subprocess, "run", side_effect=expired):
            with self.assertRaises(yo.YoError) as caught:
                locked(yo, yo.LIMA.stop)
        self.assertIn(
            "did not finish within %d s" % yo.LIMA_STOP_TIMEOUT,
            caught.exception.message,
        )
        self.assertIn("kill -9", caught.exception.message)
        self.assertIn("systemctl reboot", caught.exception.message)

    def test_a_hung_force_stop_does_not_send_the_operator_into_the_guest(self):
        # A box in its initrd has no guest to ssh into: only the hostagent's kill helps.
        expired = subprocess.TimeoutExpired(
            ["limactl", "--log-level=warn", "stop", "--force"], yo.LIMA_STOP_TIMEOUT
        )
        with mock.patch.object(yo.subprocess, "run", side_effect=expired):
            with self.assertRaises(yo.YoError) as caught:
                locked(yo, yo.LIMA.force_stop)
        self.assertIn("kill -9", caught.exception.message)
        self.assertIn("limactl hostagent", caught.exception.message)
        self.assertNotIn("systemctl reboot", caught.exception.message)

    def test_a_stop_that_failed_on_a_box_no_longer_running_is_not_called_a_hang(self):
        failed = subprocess.CompletedProcess(["limactl", "stop"], 1, None, None)
        for status in (STOPPED, yo.VmStatus(yo.VmState.OTHER, "Broken"), None):
            with self.subTest(status=status):
                with (
                    mock.patch.object(
                        yo.LIMA, "vm_status", side_effect=[RUNNING, status]
                    ),
                    mock.patch.object(yo.subprocess, "run", return_value=failed),
                ):
                    with self.assertRaises(yo.YoError) as caught:
                        locked(yo, yo.LIMA.stop)
                self.assertNotIsInstance(caught.exception, yo.StopRefused)
                self.assertIn(
                    "limactl --log-level=warn stop yolobox failed",
                    caught.exception.message,
                )
                self.assertNotIn("kill -9", caught.exception.message)

    def assert_box_lock_held(self):
        self.assertTrue(box_lock_is_held())

    def down(self, stage, probe="", ssh_result=None):
        """Run `yo down`; the initrd, when asked, answers `probe` to the ssh probe.

        Every process `yo down` starts must find the box's lock held, whatever
        the stage: an up that waits for it may be about to start the box.
        """
        started = self.started = []

        def answer(argv, **kwargs):
            self.assert_box_lock_held()
            started.append(argv)
            if argv[0] == "ssh":
                self.assertEqual(argv[-1], yo.unlock.INITRD_ASK_PROBE)
                return ssh_result or subprocess.CompletedProcess(argv, 0, probe, "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with (
            mock.patch.object(
                yo.unlock, "boot_stage", return_value=yo.Stage(stage, "")
            ),
            # The initrd's banner, read again at the gate of the root probe.
            mock.patch.object(yo.unlock, "ssh_banner", return_value=INITRD_BANNER),
            hostagent_holds_the_port(),
            mock.patch.object(yo.proc, "run", side_effect=answer) as fake_run,
            mock.patch.object(yo.ui, "out"),
        ):
            args = yo.build_parser().parse_args(["down"])
            args.func(args)
        return fake_run

    def refused_down(self, probe="", ssh_result=None):
        with self.assertRaises(yo.YoError) as caught:
            self.down(yo.BootStage.INITRD, probe, ssh_result)
        self.assertNotIn("limactl", [argv[0] for argv in self.started])
        return caught.exception.message

    def test_down_stops_the_instance_under_the_timeout(self):
        for stage in (yo.BootStage.STAGE2, yo.BootStage.NONE):
            with self.subTest(stage=stage):
                self.down(stage).assert_called_once_with(
                    ["limactl", "--log-level=warn", "stop", "yolobox"],
                    capture=False,
                    timeout=yo.LIMA_STOP_TIMEOUT,
                )

    def test_down_forces_the_stop_of_a_box_idle_at_the_passphrase_prompt(self):
        # The initrd has no ACPI handler: a polite stop would wait out the timeout.
        fake_run = self.down(yo.BootStage.INITRD, PROBE_AT_THE_PASSPHRASE_PROMPT)
        fake_run.assert_called_with(
            ["limactl", "--log-level=warn", "stop", "--force", "yolobox"],
            capture=False,
            timeout=yo.LIMA_STOP_TIMEOUT,
        )

    def test_down_never_stops_a_box_with_no_ask_pending_in_its_initrd(self):
        # A force here is a power cut mid-encryption, and a polite stop would
        # only hang: the box is left running, and the error says why.
        message = self.refused_down(PROBE_WITH_NOTHING_PENDING)
        self.assertIn("may be encrypting its disk", message)
        self.assertIn("yo up", message)

    def test_down_names_the_force_command_for_the_operator_who_knows_better(self):
        # The refusal is never a dead end: a bootstrapped box stuck in its
        # initrd is powered off by the operator, who can know nothing encrypts.
        message = self.refused_down(PROBE_WITH_NOTHING_PENDING)
        self.assertIn(LIMA_FORCE_STOP, message)
        self.assertIn("nothing is encrypting", message)

    def test_down_never_stops_a_box_whose_initrd_probe_failed_and_says_why(self):
        failed = subprocess.CompletedProcess(["ssh"], 255, "", "boom: no route")
        message = self.refused_down(ssh_result=failed)
        self.assertIn("cannot look inside", message)
        self.assertIn("exited 255", message)
        self.assertIn("boom: no route", message)

    def test_down_neither_waits_for_nor_probes_a_box_another_yo_is_unlocking(self):
        with box_lock_held_elsewhere():
            with mock.patch.object(yo.proc, "run") as fake_run:
                with (
                    mock.patch.object(
                        yo.unlock,
                        "boot_stage",
                        return_value=yo.Stage(yo.BootStage.INITRD, ""),
                    ),
                    self.assertRaisesRegex(yo.YoError, "another yo is acting on"),
                ):
                    args = yo.build_parser().parse_args(["down"])
                    args.func(args)
        fake_run.assert_not_called()

    def test_the_lock_is_free_again_after_down(self):
        self.down(yo.BootStage.INITRD, PROBE_AT_THE_PASSPHRASE_PROMPT)
        self.assertFalse(box_lock_is_held())


class TestThePassphraseGoesOnlyToAPortNoMirroredListenerCanClaim(FakeHome):
    def send_to_port(self, port, banner=None):
        """run_initrd_helper against an instance whose ssh port is `port` and
        whose port sends `banner` (default: the initrd's); returns the helper
        it would have started, or None. `self.derived` counts derivations."""
        helper = FakeHelper(*UNLOCKED)
        self.derived = 0

        def passphrase():
            self.derived += 1
            return b"secret"

        with (
            mock.patch.object(yo.LIMA, "ssh_port", return_value=port),
            mock.patch.object(
                yo.unlock, "ssh_banner", return_value=banner or INITRD_BANNER
            ),
            mock.patch.object(yo.proc, "spawn", return_value=helper) as spawn,
            hostagent_holds_the_port(),
            mock.patch.object(yo.ui, "out") as self.out,
            yo.box_lock(wait=True) as held,
        ):
            self.spawn = spawn
            gate = yo.unlock.initrd_endpoint(held)
            if not isinstance(gate, yo.unlock.InitrdEndpoint):
                return None
            yo.run_initrd_helper(gate, passphrase)
        return helper

    def test_a_port_inside_the_mirrored_range_gets_nothing_and_asks_nothing(self):
        for port in (1, 22222, 49151):
            with self.subTest(port=port):
                with self.assertRaisesRegex(yo.YoError, "ssh.localPort %d" % port):
                    self.send_to_port(port)
                self.spawn.assert_not_called()
                # No Touch ID prompt for a listener that is not the VM's.
                self.assertEqual(self.derived, 0)

    def test_a_port_that_is_no_longer_the_initrds_gets_nothing_and_asks_nothing(self):
        # Another yo unlocked the box while this one waited for the lock.
        stage2 = "SSH-2.0-OpenSSH_10.6 " + yo.STAGE2_SSH_BANNER_MARK
        for banner in (stage2, "SSH-2.0-x"):
            with self.subTest(banner=banner):
                self.assertIsNone(self.send_to_port(60022, banner))
                self.spawn.assert_not_called()
                self.assertEqual(self.derived, 0)

    def test_a_port_above_the_mirrored_range_gets_the_passphrase(self):
        helper = self.send_to_port(60022)
        self.spawn.assert_called_once()
        self.assertEqual(helper.stdin.sent, b"secret\n")
        # What the helper prints is the guest's, quoted.
        self.out.assert_called_once_with("  | " + yo.unlock.HELPER_UNLOCKED_LINE)


class TestDiskGrowResizesTheMapping(FakeHome):
    GROWN = (
        df_output(human_line="/dev/mapper/root 100G 50G 50G 50% /"),
        df_output(human_line="/dev/mapper/root 200G 50G 150G 25% /"),
    )

    def disk_grow(self, fstype="crypto_LUKS\n", size="200", df_k=GROWN, command=None):
        """`yo disk-grow SIZE` (or another `command`) on a 100 GiB disk. Every
        step must find the box lock held: a `yo down` must not stop the box
        mid-resize."""
        calls = []
        order = []
        df_k = iter(df_k)
        self.out = out = []
        self.limactl = limactl = []
        self.unlocked_steps = unlocked = []

        def under_the_lock(step):
            if not box_lock_is_held():
                unlocked.append(step)

        def fake_run(argv, **kwargs):
            under_the_lock(argv)
            limactl.append(list(argv))
            if argv[:3] == ["limactl", "--log-level=warn", "edit"]:
                order.append(("edit", list(argv[5:])))
            if argv[:3] == ["limactl", "--log-level=warn", "list"]:
                return mock.Mock(returncode=0, stdout="%d\n" % (100 << 30), stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        def fake_ssh_run(role, argv, **kwargs):
            under_the_lock(argv)
            calls.append((argv, kwargs.get("stdin")))
            stdout = ""
            if argv[:2] == ["lsblk", "-dno"] and argv[2] == "FSTYPE":
                stdout = fstype
            elif argv == yo.guest.ROOT_USAGE_PROBE:
                stdout = next(df_k)
            return mock.Mock(returncode=0, stdout=stdout, stderr="")

        self.source = yo.agent.PassphraseSource()
        with (
            mock.patch.object(yo.proc, "run", side_effect=fake_run),
            mock.patch.object(yo.sshx, "ssh_run", side_effect=fake_ssh_run),
            mock.patch.object(
                yo.LIMA,
                "stop",
                side_effect=lambda held: under_the_lock("stop") or order.append("stop"),
            ),
            mock.patch.object(
                yo.vm,
                "vm_up",
                side_effect=lambda held, **kw: order.append(kw) or self.source,
            ),
            mock.patch.object(
                yo.vm,
                "vm_restart",
                side_effect=lambda held, passphrase, run_services=True: (
                    order.append({"restart": passphrase, "run_services": run_services})
                    or self.source
                ),
            ),
            mock.patch.object(
                yo.agent,
                "luks_passphrase",
                side_effect=lambda: order.append("passphrase") or b"p" * 64,
            ),
            mock.patch.object(yo.ui, "out", side_effect=out.append),
        ):
            try:
                args = yo.build_parser().parse_args(command or ["disk-grow", size])
                args.func(args)
            finally:
                self.order = order
        return calls

    def edits(self):
        return [
            c for c in self.limactl if c[:3] == ["limactl", "--log-level=warn", "edit"]
        ]

    def resized(self, calls):
        resize = (
            ["sudo", "cryptsetup", "resize", "--key-file", "-", "root"],
            b"p" * 64,
        )
        grow_fs = (["sudo", "resize2fs", "/dev/mapper/root"], None)
        self.assertIn(resize, calls)
        self.assertEqual(calls.index(grow_fs), calls.index(resize) + 1)

    # The services wait for the last start: the first one has no services when
    # a restart follows it.
    FIRST_UP = {"run_services": False, "passphrase": None, "size": yo.NO_SIZE}

    def restart(self):
        return {"restart": self.source, "run_services": True}

    def test_the_box_is_up_then_stopped_grown_restarted_and_its_root_grown_into_it(
        self,
    ):
        calls = self.disk_grow()
        # The passphrase is derived before the stop, so a missing 1Password key
        # leaves the box running, and reused for the restart.
        self.assertEqual(
            self.order,
            [
                self.FIRST_UP,
                "passphrase",
                "stop",
                ("edit", ["--disk", "200"]),
                self.restart(),
            ],
        )
        self.assertEqual(
            self.edits(),
            [
                [
                    "limactl",
                    "--log-level=warn",
                    "edit",
                    "--tty=false",
                    "yolobox",
                    "--disk",
                    "200",
                ]
            ],
        )
        self.resized(calls)
        self.assertIn("before: /dev/mapper/root 100G 50G 50G 50% /", self.out)
        self.assertIn("after:  /dev/mapper/root 200G 50G 150G 25% /", self.out)
        self.assertEqual(self.unlocked_steps, [])
        self.assertFalse(box_lock_is_held())

    def test_up_with_memory_and_disk_changes_both_in_one_stop_with_one_passphrase(self):
        with mock.patch.object(yo.LIMA, "vm_status", return_value=STOPPED):
            calls = self.disk_grow(command=["up", "--memory", "12", "--disk", "200"])
        self.assertEqual(
            self.order,
            [
                {**self.FIRST_UP, "size": yo.Size(12, 200, None)},
                "passphrase",
                "stop",
                ("edit", ["--disk", "200"]),
                self.restart(),
            ],
        )
        self.resized(calls)

    def test_asking_for_the_ceiling_the_box_has_grows_the_root_without_a_restart(self):
        # The mapping and filesystem grows are no-ops at full size, so a grow
        # that failed after the ceiling moved is finished by asking again.
        calls = self.disk_grow(size="100")
        self.assertEqual(
            self.order, [{**self.FIRST_UP, "run_services": True}, "passphrase"]
        )
        self.assertEqual(self.edits(), [])
        self.resized(calls)

    def test_a_ceiling_below_the_current_one_is_refused_before_anything_runs(self):
        for command in (["disk-grow", "50"], ["up", "--memory", "12", "--disk", "50"]):
            with self.subTest(command=command):
                with (
                    mock.patch.object(yo.LIMA, "vm_status", return_value=STOPPED),
                    self.assertRaises(yo.YoError) as caught,
                ):
                    self.disk_grow(command=command)
                self.assertIn("at least 100", caught.exception.message)
                self.assertEqual(self.order, [])

    def test_an_unencrypted_disk_is_refused_before_the_passphrase_and_the_stop(self):
        with self.assertRaises(yo.YoError) as caught:
            self.disk_grow(fstype="ext4\n")
        self.assertIn("yo bootstrap", caught.exception.message)
        # The box was brought up to be read; nothing was derived or stopped.
        self.assertEqual(self.order, [self.FIRST_UP])

    def test_an_unreadable_df_stops_before_the_ceiling_is_edited(self):
        with self.assertRaisesRegex(yo.YoError, "cannot read the VM's root usage"):
            self.disk_grow(df_k=("", ""))
        self.assertNotIn("stop", self.order)
        self.assertEqual(self.edits(), [])


if __name__ == "__main__":
    unittest.main()
