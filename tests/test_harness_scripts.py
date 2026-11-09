"""Behaviour of the claude launcher scripts in nix/harnesses/, run for real
under bash.

`yolobox-claude-versions` is the one definition of an installed version; the
launcher, the keeper and the installer reach it through CLAUDE_VERSIONS_LIST,
which the build sets. Here that is a fake `versions` that runs the real script
over a scratch versions directory. Tools whose absence skips the module: GNU
find (`-regextype`, `-printf`) and GNU sort (`-V`).
"""

import getpass
import os
import time
import unittest

from shell_harness import BASH, REPO, ShellCase, is_gnu, needs_tool

HARNESSES = REPO / "nix" / "harnesses"
VERSIONS = HARNESSES / "yolobox-claude-versions.sh"
LAUNCHER = HARNESSES / "claude.sh"
KEEPER = HARNESSES / "yolobox-claude-launcher-keep.sh"
INSTALLER = HARNESSES / "yolobox-harness-install.sh"
NETWORK_READY = HARNESSES / "yolobox-network-ready.sh"

VERSION_RE = r"[0-9]+(\.[0-9]+)*(-[A-Za-z0-9.]+)?"
HOUR = 3600


@needs_tool(
    ["find", "sort"], is_gnu("find") and is_gnu("sort"), "needs GNU find and sort"
)
class HarnessCase(ShellCase):
    def setUp(self):
        super().setUp()
        self.versions_dir = self.tmp / "versions"
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.versions_list = self.fake(
            "versions",
            '#!/bin/sh\nexec %s %s "$@"\n' % (" ".join(BASH), VERSIONS),
        )
        self.env.update(
            HOME=str(self.home),
            CLAUDE_VERSIONS_DIR=str(self.versions_dir),
            CLAUDE_VERSION_RE=VERSION_RE,
            CLAUDE_VERSIONS_LIST=str(self.versions_list),
        )

    def install(self, name, age=HOUR, text='#!/bin/sh\necho "$0 $*"\n', mode=0o755):
        """A file `name` in the versions directory, last written `age` seconds ago."""
        self.versions_dir.mkdir(exist_ok=True)
        path = self.versions_dir / name
        path.write_text(text)
        path.chmod(mode)
        then = time.time() - age
        os.utime(path, (then, then))
        return path

    def broken_find(self):
        self.fake("find", "#!/bin/sh\necho 'find: disk on fire' >&2\nexit 1\n")


class TestInstalledVersions(HarnessCase):
    def test_versions_come_oldest_first_in_version_order_not_text_order(self):
        for name in ("2.1.10", "2.1.9", "2.2.0-beta.1", "10.0.0"):
            self.install(name)
        result = self.run_bash(VERSIONS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.split(), ["2.1.9", "2.1.10", "2.2.0-beta.1", "10.0.0"]
        )

    def test_only_executable_files_named_as_a_version_count(self):
        self.install("2.1.1")
        self.install("2.1.2", mode=0o644)
        self.install("2.1.3.tmp0f3a")
        self.install("latest")
        (self.versions_dir / "2.1.4").mkdir()
        (self.versions_dir / "nested").mkdir()
        self.install("nested/2.1.5")
        result = self.run_bash(VERSIONS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["2.1.1"])

    def test_no_versions_directory_is_no_install_and_no_error(self):
        result = self.run_bash(VERSIONS)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_extra_find_tests_narrow_the_list(self):
        self.install("2.1.1")
        self.install("2.1.2", age=0)
        result = self.run_bash(VERSIONS, "!", "-mmin", "-1")
        self.assertEqual(result.stdout.split(), ["2.1.1"])

    def test_a_find_that_fails_is_shown_and_fails_the_script(self):
        self.install("2.1.1")
        self.broken_find()
        result = self.run_bash(VERSIONS)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("disk on fire", result.stderr)


class TestLauncher(HarnessCase):
    def setUp(self):
        super().setUp()
        self.env["YOLOBOX_AGENT_USER"] = getpass.getuser()

    def test_the_newest_settled_version_runs_with_the_arguments_and_herdr_agent(self):
        self.install("2.1.9", text='#!/bin/sh\necho "old $*"\n')
        self.install(
            "2.1.10",
            text='#!/bin/sh\necho "new $* $HERDR_AGENT [$CLAUDE_VERSIONS_DIR]"\n',
        )
        result = self.run_bash(LAUNCHER, "--resume", "x")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "new --resume x claude []\n")

    def test_a_download_in_flight_is_skipped_for_an_older_settled_version(self):
        self.install("2.1.1", text="#!/bin/sh\necho settled\n")
        self.install("2.1.2", age=0, text="#!/bin/sh\necho fresh\n")
        self.assertEqual(self.run_bash(LAUNCHER).stdout, "settled\n")

    def test_a_download_in_flight_is_run_when_it_is_all_there_is(self):
        self.install("2.1.2", age=0, text="#!/bin/sh\necho fresh\n")
        self.assertEqual(self.run_bash(LAUNCHER).stdout, "fresh\n")

    def test_no_install_exits_127_and_says_where_to_look(self):
        for versions in (False, True):
            with self.subTest(directory_exists=versions):
                if versions:
                    self.versions_dir.mkdir()
                result = self.run_bash(LAUNCHER)
                self.assertEqual(result.returncode, 127, result.stderr)
                self.assertIn("no installed version", result.stderr)
                self.assertIn("yolobox-harness-install", result.stderr)

    def test_another_account_is_refused(self):
        self.install("2.1.1")
        result = self.run_bash(
            LAUNCHER, env={"YOLOBOX_AGENT_USER": "not-" + getpass.getuser()}
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("run it as not-", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_a_version_list_that_fails_is_not_read_as_no_install(self):
        self.install("2.1.1")
        self.broken_find()
        result = self.run_bash(LAUNCHER)
        self.assertNotIn(result.returncode, (0, 127))
        self.assertIn("disk on fire", result.stderr)


class TestKeeper(HarnessCase):
    def setUp(self):
        super().setUp()
        self.launcher = self.tmp / "launcher"
        self.launcher.write_text("#!/bin/sh\n")
        self.link = self.home / ".local" / "bin" / "claude"
        self.env.update(
            CLAUDE_LAUNCHER=str(self.launcher), CLAUDE_LAUNCHER_LINK=str(self.link)
        )

    def kept(self):
        if not self.versions_dir.exists():
            return []
        return sorted(path.name for path in self.versions_dir.iterdir())

    def test_the_launcher_link_is_made_and_a_correct_one_left_alone(self):
        result = self.run_bash(KEEPER)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.link.resolve(), self.launcher)
        self.assertIn("relinked", result.stdout)
        self.assertEqual(self.run_bash(KEEPER).stdout, "")

    def test_a_link_that_points_elsewhere_is_replaced(self):
        self.link.parent.mkdir(parents=True)
        self.link.symlink_to("/nonexistent")
        self.assertEqual(self.run_bash(KEEPER).returncode, 0)
        self.assertEqual(os.readlink(self.link), str(self.launcher))

    def test_all_but_the_newest_two_settled_versions_are_pruned(self):
        for name in ("2.1.1", "2.1.2", "2.1.3", "2.1.10"):
            self.install(name)
        result = self.run_bash(KEEPER)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.kept(), ["2.1.10", "2.1.3"])

    def test_a_fresh_download_counts_as_kept_and_an_unsettled_old_one_stays(self):
        self.install("2.1.1")
        self.install("2.1.2", age=0)
        self.install("2.1.3", age=0)
        self.install("2.1.4", age=0)
        self.assertEqual(self.run_bash(KEEPER).returncode, 0)
        self.assertEqual(self.kept(), ["2.1.2", "2.1.3", "2.1.4"])

    def test_fewer_versions_than_the_keep_count_prunes_nothing(self):
        self.install("2.1.1")
        self.assertEqual(self.run_bash(KEEPER).returncode, 0)
        self.assertEqual(self.kept(), ["2.1.1"])

    def test_a_version_list_that_fails_prunes_nothing_and_says_so(self):
        for name in ("2.1.1", "2.1.2", "2.1.3"):
            self.install(name)
        self.broken_find()
        result = self.run_bash(KEEPER)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("disk on fire", result.stderr)
        self.assertEqual(self.kept(), ["2.1.1", "2.1.2", "2.1.3"])


class TestInstaller(HarnessCase):
    """Phases: claude, pi, herdr's pi integration, then each tool's --version."""

    def setUp(self):
        super().setUp()
        self.calls = self.tmp / "calls"
        self.calls.touch()
        self.env["FAKE_CALLS"] = str(self.calls)
        for name in ("claude", "pi", "node", "yolobox-claude-launcher-keep"):
            self.fake(name, '#!/bin/sh\necho "%s $*" >> "$FAKE_CALLS"\n' % name)
        self.fake("curl", '#!/bin/sh\necho "curl $*" >> "$FAKE_CALLS"\necho true\n')
        self.herdr(0, "pi: current")
        (self.home / ".pi" / "agent" / "bin").mkdir(parents=True)
        (self.home / ".pi" / "agent" / "bin" / "pi").write_text("#!/bin/sh\n")
        (self.home / ".pi" / "agent" / "bin" / "pi").chmod(0o755)
        self.install("2.1.1")

    def herdr(self, status_rc, status_out, status_err=""):
        self.fake(
            "herdr",
            "#!/bin/sh\n"
            'echo "herdr $*" >> "$FAKE_CALLS"\n'
            'case "$1 $2" in\n'
            "  'integration status') echo '%s'; echo '%s' >&2; exit %d ;;\n"
            "esac\n" % (status_out, status_err, status_rc),
        )

    def called(self, prefix):
        return [c for c in self.calls.read_text().splitlines() if c.startswith(prefix)]

    def test_a_box_with_everything_installed_downloads_nothing(self):
        result = self.run_bash(INSTALLER)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called("curl"), [])
        self.assertEqual(self.called("herdr integration install"), [])
        self.assertEqual(len(self.called("yolobox-claude-launcher-keep")), 1)

    def test_a_box_without_claude_downloads_it_once_and_keeps_the_launcher(self):
        for path in self.versions_dir.iterdir():
            path.unlink()
        result = self.run_bash(INSTALLER)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.called("curl")), 1)
        self.assertIn("claude.ai/install.sh", self.called("curl")[0])

    def test_a_herdr_integration_that_is_not_current_is_installed(self):
        self.herdr(0, "pi: missing")
        self.assertEqual(self.run_bash(INSTALLER).returncode, 0)
        self.assertEqual(
            self.called("herdr integration install"), ["herdr integration install pi"]
        )

    def test_what_a_failing_integration_probe_says_reaches_the_journal(self):
        self.herdr(1, "", "herdr: config is broken")
        result = self.run_bash(INSTALLER)
        self.assertIn("herdr: config is broken", result.stderr)
        self.assertEqual(
            self.called("herdr integration install"), ["herdr integration install pi"]
        )

    def test_a_failing_phase_does_not_stop_the_others_and_fails_the_unit(self):
        self.fake("claude", "#!/bin/sh\nexit 3\n")
        result = self.run_bash(INSTALLER)
        self.assertEqual(result.returncode, 1)
        self.assertIn("failed phase: claude --version", result.stderr)
        self.assertEqual(self.called("pi"), ["pi --version"])

    def test_pi_is_linked_into_the_bin_directory(self):
        self.assertEqual(self.run_bash(INSTALLER).returncode, 0)
        self.assertEqual(
            os.readlink(self.home / ".local" / "bin" / "pi"),
            str(self.home / ".pi" / "agent" / "bin" / "pi"),
        )


class TestNetworkReady(ShellCase):
    """The flag that the network works is made only when the guest's NIC holds
    an IPv4 address and the lease brought a nameserver, read from
    `dhcpcd --dumplease`."""

    ADDRESS = (
        "2: enp0s1    inet 192.168.5.15/24 brd 192.168.5.255 scope global "
        "dynamic enp0s1\\       valid_lft 86399sec preferred_lft 86399sec\n"
    )

    def setUp(self):
        super().setUp()
        self.flag = self.tmp / "network-ready"
        self.asked = self.tmp / "dhcpcd-args"
        self.listed = self.tmp / "ip-args"
        self.env.update(NETWORK_READY_FLAG=str(self.flag), YOLOBOX_GUEST_NIC="enp0s1")
        self.addresses(self.ADDRESS)

    def addresses(self, text, code=0):
        """What `ip` prints for the NIC, and the exit code it ends with."""
        listing = self.tmp / "addresses"
        listing.write_text(text)
        self.fake(
            "ip",
            '#!/bin/sh\necho "$@" >%s\ncat %s\nexit %d\n'
            % (self.listed, listing, code),
        )

    def lease(self, text, code=0):
        lease = self.tmp / "lease"
        lease.write_text(text)
        self.fake(
            "dhcpcd",
            '#!/bin/sh\necho "$@" >%s\ncat %s\nexit %d\n' % (self.asked, lease, code),
        )
        return self.run_bash(NETWORK_READY)

    def assertFlagged(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.flag.exists())

    def assertRefused(self, result, said):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.flag.exists())
        self.assertIn(said, result.stderr)

    def test_a_lease_with_nameservers_makes_the_flag_from_the_nics_lease(self):
        result = self.lease(
            "ip_address=192.168.5.15\ndomain_name_servers=192.168.5.2 1.1.1.1\n"
        )
        self.assertFlagged(result)
        self.assertEqual(self.asked.read_text(), "--dumplease enp0s1\n")
        self.assertEqual(self.listed.read_text(), "-4 -o addr show dev enp0s1\n")

    def test_a_lease_with_an_empty_nameserver_value_is_refused_with_what_it_said(self):
        self.assertRefused(
            self.lease("ip_address=192.168.5.15\ndomain_name_servers=\n"),
            "domain_name_servers=\n",
        )

    def test_a_lease_with_a_blank_nameserver_value_is_refused(self):
        self.assertRefused(
            self.lease("domain_name_servers= \n"), "no domain_name_servers"
        )

    def test_a_lease_without_the_line_is_refused_with_what_it_said(self):
        self.assertRefused(
            self.lease("ip_address=192.168.5.15\nrouters=192.168.5.2\n"),
            "routers=192.168.5.2",
        )

    def test_a_key_that_merely_contains_the_name_is_no_nameserver(self):
        for line in (
            "new_domain_name_servers=1.1.1.1",
            "domain_name_servers_extra=1.1.1.1",
            " domain_name_servers=1.1.1.1",
        ):
            with self.subTest(line=line):
                self.assertRefused(self.lease(line + "\n"), "no domain_name_servers")

    def test_an_empty_lease_is_refused(self):
        self.assertRefused(self.lease(""), "no domain_name_servers")

    def test_a_dhcpcd_that_fails_is_refused_even_when_it_printed_nameservers(self):
        self.assertRefused(
            self.lease("domain_name_servers=1.1.1.1\n", code=1), "could not dump"
        )

    def test_a_nic_without_an_address_is_refused_before_the_lease_is_read(self):
        # dhcpcd's lease file outlives a boot, so a lease that names servers
        # proves nothing about this one.
        for listing in (
            "",
            "\n",
            "2: enp0s1    inet6 fe80::1/64 scope link\n",
            "2: enp0s1    foo inet\n",
            "2: inet    enp0s1 192.168.5.15/24\n",
            "inet\n",
        ):
            with self.subTest(listing=listing):
                self.addresses(listing)
                self.assertRefused(
                    self.lease("domain_name_servers=1.1.1.1\n"),
                    "enp0s1 has no IPv4 address",
                )
                self.assertFalse(self.asked.exists())

    def test_the_address_may_be_any_line_of_the_listing(self):
        self.addresses("2: enp0s1    inet6 fe80::1/64 scope link\n" + self.ADDRESS)
        self.assertFlagged(self.lease("domain_name_servers=1.1.1.1\n"))

    def test_an_ip_that_fails_is_refused_even_when_it_printed_an_address(self):
        self.addresses(self.ADDRESS, code=1)
        self.assertRefused(
            self.lease("domain_name_servers=1.1.1.1\n"), "ip could not list"
        )


if __name__ == "__main__":
    unittest.main()
