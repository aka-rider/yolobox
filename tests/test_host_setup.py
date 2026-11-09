"""linux/host-setup.sh can be run again: its own definitions and then a slice
of its steps run under shell_harness.ShellCase, with fake systemctl, virsh,
nft and restorecon, against a scratch root.

Tools whose absence skips tests: GNU stat and install.
"""

import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from shell_harness import REPO, ShellCase, is_gnu, needs_tool

HOST_SETUP = REPO / "linux" / "host-setup.sh"
HOOK_STEP = "libvirt hook that keeps the passt ACL"
EGRESS_STEP = "host egress table"


@needs_tool(
    ["stat", "install"],
    is_gnu("stat") and is_gnu("install"),
    "needs GNU stat and install",
)
class TestHostSetupCanBeRerun(ShellCase):
    """A second run of linux/host-setup.sh changes nothing on a healthy host and
    finishes what a run that died left half done, and a
    damaged checkout is refused before the host is touched. The script's own
    definitions and then a slice of its steps run here under bash, with fake
    systemctl, virsh, nft and restorecon; they skip without GNU stat and
    install."""

    def run_slice(
        self,
        first,
        last,
        *,
        missing=None,
        hook_mtime=None,
        started="",
        active=False,
        table_installed=False,
        libvirtd=False,
    ):
        """Run host-setup.sh from the text `first` up to the text `last`, after
        its definitions, from a scratch checkout that lacks the file
        `missing`, against a scratch root. Return the finished process and the
        calls to systemctl (other than show and is-active) and virsh.
        `started` is what `systemctl show` prints for virtqemud's start;
        `hook_mtime` pre-installs the hook with that mtime; `table_installed`
        pre-installs the table and its unit, identical to the repo's; `active`
        is whether the table unit is active; `libvirtd` whether the monolithic
        libvirtd is enabled or active. virsh speaks German unless LC_ALL=C, as
        a translated virsh would on a German host."""
        text = HOST_SETUP.read_text()
        definitions = text[: text.index("[[ $EUID -eq 0 ]]")]
        steps = text[text.index(first) : text.index(last)]
        # One root per slice: a test may run several.
        root = Path(tempfile.mkdtemp(dir=self.tmp))
        here = root / "repo"
        here.mkdir()
        for name in (
            "yolobox-libvirt-hook.sh",
            "yolobox-host.nft",
            "yolobox-host.service",
        ):
            if name != missing:
                shutil.copy(HOST_SETUP.parent / name, here / name)
        fakes = {
            "nft": "exit 0",
            "restorecon": "exit 0",
            "systemctl": (
                "case $1 in\n"
                '  show) printf "%s\\n" "$FAKE_STARTED" ;;\n'
                '  is-enabled|is-active) case "$*" in *libvirtd*) exit "${FAKE_LIBVIRTD:-1}" ;; *) exit "$FAKE_IS_ACTIVE" ;; esac ;;\n'
                '  *) echo "$*" >>"$CALLS" ;;\n'
                "esac"
            ),
            "virsh": (
                "shift 2\n"
                "case $1 in\n"
                "  net-list) echo default ;;\n"
                "  net-info)\n"
                '    if [ "${LC_ALL:-}" = C ]; then\n'
                "      printf 'Autostart:      yes\\nActive:         yes\\n'\n"
                "    else\n"
                "      printf 'Automatischer Start: ja\\nAktiv:          ja\\n'\n"
                "    fi ;;\n"
                '  *) echo "$*" >>"$CALLS" ;;\n'
                "esac"
            ),
        }
        for name, body in fakes.items():
            self.fake(name, "#!/bin/sh\n%s\n" % body)
        hook = root / "hook"
        if hook_mtime is not None:
            shutil.copy(here / "yolobox-libvirt-hook.sh", hook)
            hook.chmod(0o755)
            os.utime(hook, (hook_mtime, hook_mtime))
        nft_dest = root / "nft-file"
        unit_dest = root / "unit-file"
        if table_installed:
            shutil.copy(here / "yolobox-host.nft", nft_dest)
            shutil.copy(here / "yolobox-host.service", unit_dest)
            nft_dest.chmod(0o644)
            unit_dest.chmod(0o644)
        # In the checkout, so that a slice that finds its own directory finds
        # the scratch one.
        script = here / HOST_SETUP.name
        script.write_text(
            '%sHERE="%s" PASST_HOOK="%s" NFT_DEST="%s" UNIT_DEST="%s"\n%s'
            % (
                definitions,
                here,
                hook,
                nft_dest,
                unit_dest,
                steps.replace("/usr/sbin/nft", str(self.bin_dir / "nft")),
            )
        )
        calls = root / "calls"
        calls.touch()
        result = self.run_bash(
            script,
            env={
                "LANG": "de_DE.UTF-8",
                "CALLS": str(calls),
                "FAKE_STARTED": started,
                "FAKE_IS_ACTIVE": "0" if active else "3",
                "FAKE_LIBVIRTD": "0" if libvirtd else "1",
            },
        )
        return result, calls.read_text().splitlines()

    def run_steps(self, first, last, **kwargs):
        """The calls of a slice from step `first` to step `last` that succeeds."""
        result, calls = self.run_slice('step "%s' % first, 'step "%s' % last, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return calls

    def test_a_checkout_missing_a_file_it_installs_is_refused_before_the_first_step(
        self,
    ):
        text = HOST_SETUP.read_text()
        read = sorted(set(re.findall(r"\$HERE/([\w.-]+)", text)))
        self.assertTrue(read)
        for name in read:
            with self.subTest(file=name):
                result, _ = self.run_slice('HERE="', 'step "', missing=name)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertRegex(result.stderr, r"missing \S*/%s$" % re.escape(name))
        result, _ = self.run_slice('HERE="', 'step "')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_host_with_the_monolithic_libvirtd_is_refused_before_the_daemons_change(
        self,
    ):
        self.assertEqual(
            self.run_steps("libvirt daemons", "libvirt version gate"),
            ["enable --now virtqemud.socket virtstoraged.socket virtnetworkd.socket"],
        )
        result, calls = self.run_slice(
            'step "libvirt daemons', 'step "libvirt version gate', libvirtd=True
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("monolithic libvirtd.service", result.stderr)
        self.assertEqual(calls, [])

    def test_the_default_network_is_switched_off_on_a_host_that_speaks_german(self):
        self.assertEqual(
            self.run_steps("libvirt's default network off", "polkit rule"),
            ["net-autostart --disable default", "net-destroy default"],
        )

    def test_virtqemud_restarts_when_it_started_before_the_hook_was_written(self):
        self.assertEqual(
            self.run_steps(HOOK_STEP, EGRESS_STEP, started="@1000"),
            ["try-restart virtqemud.service"],
        )

    def test_a_run_that_died_before_the_restart_is_finished_by_the_next(self):
        """The hook is installed and identical, so the restart cannot hang on
        this run having installed it."""
        self.assertEqual(
            self.run_steps(HOOK_STEP, EGRESS_STEP, hook_mtime=2000, started="@1000"),
            ["try-restart virtqemud.service"],
        )

    def test_virtqemud_is_left_alone_when_it_started_after_the_hook(self):
        self.assertEqual(
            self.run_steps(HOOK_STEP, EGRESS_STEP, hook_mtime=2000, started="@3000"), []
        )

    def test_a_virtqemud_that_never_ran_is_left_alone(self):
        self.assertEqual(self.run_steps(HOOK_STEP, EGRESS_STEP, started=""), [])

    def test_a_start_time_it_cannot_read_stops_the_run(self):
        """Skipping the restart would leave the hook unloaded, silently."""
        for started in ("Thu 2026-11-05 10:00:00 UTC", "@", "@12a", "1000", "n/a"):
            with self.subTest(started=started):
                result, calls = self.run_slice(
                    'step "%s' % HOOK_STEP, 'step "%s' % EGRESS_STEP, started=started
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("cannot read virtqemud's start time", result.stderr)
                self.assertIn(started, result.stderr)
                self.assertEqual(calls, [])

    def test_an_active_table_unit_is_reloaded_even_when_no_file_changed(self):
        """The loaded table may have been deleted by hand under an active unit.
        Never restarted: the restart would propagate to virtqemud."""
        self.assertEqual(
            self.run_steps(
                EGRESS_STEP, "storage pool", active=True, table_installed=True
            ),
            [
                "daemon-reload",
                "enable yolobox-host.service",
                "reload yolobox-host.service",
            ],
        )

    def test_an_inactive_table_unit_is_enabled_and_started(self):
        self.assertEqual(
            self.run_steps(
                EGRESS_STEP, "storage pool", active=False, table_installed=True
            ),
            ["daemon-reload", "enable --now yolobox-host.service"],
        )


if __name__ == "__main__":
    unittest.main()
