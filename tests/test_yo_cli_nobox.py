"""`cli.entry` with no machine chosen: an argparse `type=` callable runs while
the command line is parsed, before main() has called state.set_box, so none of
them may read the box. `load_yo` presets a box, which hid the read; here it is
deleted before every run. Nothing here skips."""

import contextlib
import io
import unittest
from unittest import mock

from test_yo import host_cpus, load_yo, unload_yo

yo = load_yo("yo_nobox")


def tearDownModule():
    unload_yo("yo_nobox")


class Ran(Exception):
    """The command ran: main() got past every check with these args."""

    def __init__(self, namespace):
        super().__init__()
        self.namespace = namespace


def run_command(args):
    raise Ran(args)


class TestTypeCallablesReadNoBox(unittest.TestCase):
    """One command per `type=` callable: a good value reaches the command, a bad
    one is a usage error (exit 2), and neither needs a box while parsing."""

    def setUp(self):
        patches = [
            mock.patch.object(yo.lima.LIMA, "require_tool"),
            mock.patch.object(yo.lima.LIMA, "require_login_key"),
            mock.patch.object(yo.cli, "heal_before_command"),
            host_cpus(),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def entry(self, argv, command_module, command):
        """entry(argv) with no box chosen: the command's args, or the exit code."""
        if hasattr(yo.state, "box"):
            del yo.state.box
        stderr = io.StringIO()
        with mock.patch.object(command_module, command, side_effect=run_command):
            with contextlib.redirect_stderr(stderr):
                try:
                    return yo.entry(argv), stderr.getvalue()
                except Ran as ran:
                    return ran.namespace, stderr.getvalue()
                except SystemExit as refused:
                    return refused.code, stderr.getvalue()

    def test_a_good_value_reaches_the_command_with_the_machine_chosen(self):
        for argv, module, command, field, value in (
            (
                ["bootstrap", "--version", "main"],
                yo.vm,
                "cmd_bootstrap",
                "version",
                "github:aka-rider/yolobox/main",
            ),
            (
                ["bootstrap", "--version", "github:you/yolobox/x"],
                yo.vm,
                "cmd_bootstrap",
                "version",
                "github:you/yolobox/x",
            ),
            (["-m", "box2", "status"], yo.status, "cmd_status", "machine", "box2"),
            (["up", "--cpus", "2"], yo.vm, "cmd_up", "cpus", 2),
            (["up", "--memory", "8"], yo.vm, "cmd_up", "memory", 8),
            (["bootstrap", "--disk", "40"], yo.vm, "cmd_bootstrap", "disk", 40),
            (["disk-grow", "50"], yo.vm, "cmd_disk_grow", "size", 50),
            (["port", "8080"], yo.session, "cmd_port", "guest_port", 8080),
        ):
            with self.subTest(argv=argv):
                args, stderr = self.entry(argv, module, command)
                self.assertEqual(stderr, "")
                self.assertEqual(getattr(args, field), value)
                self.assertEqual(yo.state.box.name, args.machine)

    def test_a_bad_value_is_a_usage_error_with_no_box(self):
        for argv, module, command, said in (
            (
                ["bootstrap", "--version", "1.7"],
                yo.vm,
                "cmd_bootstrap",
                "a release is X.Y.Z",
            ),
            (
                ["bootstrap", "--version", "latest"],
                yo.vm,
                "cmd_bootstrap",
                "no latest ref",
            ),
            (
                ["bootstrap", "--version", "./here"],
                yo.vm,
                "cmd_bootstrap",
                "--version must be",
            ),
            (
                ["-m", "lima-x", "status"],
                yo.status,
                "cmd_status",
                "not a usable machine name",
            ),
            (
                ["-m", "Box_2", "status"],
                yo.status,
                "cmd_status",
                "not a usable machine name",
            ),
            (["up", "--cpus", "16"], yo.vm, "cmd_up", "--cpus must be"),
            (["up", "--cpus", "lizard"], yo.vm, "cmd_up", "--cpus must be"),
            (["up", "--memory", "1"], yo.vm, "cmd_up", "--memory must be at least"),
            (["up", "--disk", "0"], yo.vm, "cmd_up", "--disk must be a whole number"),
            (["disk-grow", "-1"], yo.vm, "cmd_disk_grow", "disk-grow size must be"),
            (["disk-grow", "١"], yo.vm, "cmd_disk_grow", "disk-grow size must be"),
            (["port", "0"], yo.session, "cmd_port", "guest port must be"),
            (["port", "70000"], yo.session, "cmd_port", "guest port must be"),
        ):
            with self.subTest(argv=argv):
                code, stderr = self.entry(argv, module, command)
                self.assertEqual(code, 2)
                self.assertIn(said, stderr)
                self.assertFalse(hasattr(yo.state, "box"))

    def test_an_old_release_is_refused_once_the_machine_is_chosen_naming_it(self):
        code, stderr = self.entry(
            ["-m", "box2", "bootstrap", "--version", "1.9.1"], yo.vm, "cmd_bootstrap"
        )
        self.assertEqual(code, 2)
        self.assertIn("cannot build release 1.9.1", stderr)
        self.assertIn(
            "nix run github:aka-rider/yolobox/v1.9.1 -- -m box2 bootstrap", stderr
        )
        yo.lima.LIMA.require_tool.assert_not_called()


if __name__ == "__main__":
    unittest.main()
