"""Behaviour of nix/guest/yolobox-guest.sh, run for real under bash.

The script runs under shell_harness.ShellCase with a fake `curl` ahead of
the system tools (and, per class, fake `dig`, `stat`, `systemctl`,
`tailscale`, `lsblk`, `df`, `sudo` and `id`), and sources a facts file
rendered from facts.json as the box renders its /etc/yolobox/facts.sh. Only
subcommands that need nothing but what macOS and Linux share are exercised,
except `gc-user --deep`, which runs only where GNU find's `-printf` exists
(CI, the guest): the other subcommands (`projects`, `gc-machine`) use it or
sudo too.

Tools whose absence skips tests: GNU find (the `gc-user` classes), flock (the
rebuild lock).
"""

import fcntl
import ipaddress
import shlex
import shutil
import subprocess
import unittest
import urllib.parse

from shell_harness import (
    FACTS,
    REPO,
    ShellCase,
    facts_sh,
    is_gnu,
    needs_tool,
    render_fact,
)

GUEST_SCRIPT = REPO / "nix" / "guest" / "yolobox-guest.sh"

FAKE_CURL = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_CURL_ARGV"
printf '%s' "${FAKE_CURL_ERR:-}" >&2
printf '%s' "$FAKE_CURL_BODY"
exit "$FAKE_CURL_RC"
"""

BROKER_SOCK = FACTS["agent_aws_broker_sock"]


class GuestScript(ShellCase):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.fake("curl", FAKE_CURL)
        self.curl_argv = self.tmp / "curl-argv"
        self.facts = self.tmp / "facts.sh"
        self.fact_overrides = {}
        self.write_facts()
        self.env.update(
            HOME=str(self.home),
            YOLOBOX_FACTS=str(self.facts),
            FAKE_CURL_ARGV=str(self.curl_argv),
            FAKE_CURL_BODY="",
            FAKE_CURL_RC="0",
        )

    def write_facts(self, **overrides):
        """The box's facts file, with every override so far over facts.json."""
        self.fact_overrides.update(overrides)
        self.facts.write_text(facts_sh(**self.fact_overrides))

    def run_script(self, *args, curl_rc=0, curl_body="", extra_env=None):
        env = dict(FAKE_CURL_RC=str(curl_rc), FAKE_CURL_BODY=curl_body)
        env.update(extra_env or {})
        # A subcommand that loops forever (landing-dir on a relative path
        # once did) must fail the test, not hang the suite: the timeout.
        return self.run_bash(GUEST_SCRIPT, *args, env=env)

    def curl_was_called(self):
        return self.curl_argv.exists()


class TestAwsCredsProfileName(GuestScript):
    def test_a_profile_name_that_is_not_a_plain_name_is_refused_before_curl_runs(self):
        for profile in ("-x", "../x", "a b", ".hidden", "a/b", "a?b", "dev\n", "a\nb"):
            with self.subTest(profile=profile):
                result = self.run_script("aws-creds", profile)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn("unknown subcommand", result.stderr)
                self.assertNotEqual(result.stderr.strip(), "")
                self.assertFalse(self.curl_was_called())

    def test_an_empty_profile_name_fails_without_calling_curl(self):
        result = self.run_script("aws-creds", "")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("unknown subcommand", result.stderr)
        self.assertFalse(self.curl_was_called())

    def test_a_missing_profile_argument_fails_without_calling_curl(self):
        result = self.run_script("aws-creds")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("unknown subcommand", result.stderr)
        self.assertFalse(self.curl_was_called())


class TestAwsCredsBrokerAnswer(GuestScript):
    def test_success_prints_only_the_body_and_asks_the_broker_socket_for_the_profile(
        self,
    ):
        body = '{"Version": 1, "AccessKeyId": "AKIA"}'
        result = self.run_script("aws-creds", "my-prof.1_x", curl_body=body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, body + "\n")
        self.assertEqual(result.stderr, "")
        argv = self.curl_argv.read_text().split("\n")
        self.assertIn("http://broker/creds/my-prof.1_x", argv)
        self.assertEqual(argv[argv.index("--unix-socket") + 1], BROKER_SOCK)

    def test_curl_exit_7_52_and_56_say_to_run_yo_up(self):
        # 7: no socket unit; 52 and 56: systemd accepted and nothing answered.
        for rc in (7, 52, 56):
            with self.subTest(curl_exit=rc):
                result = self.run_script("aws-creds", "dev", curl_rc=rc)
                self.assertEqual(result.returncode, 1)
                self.assertIn("run `yo up`", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_any_other_curl_failure_passes_the_broker_error_to_stderr(self):
        result = self.run_script(
            "aws-creds", "dev", curl_rc=22, curl_body="SSO session expired"
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("SSO session expired", result.stderr)
        self.assertNotIn("run `yo up`", result.stderr)


class TestLandingDir(GuestScript):
    def test_a_relative_path_is_refused(self):
        result = self.run_script("landing-dir", "foo")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("unknown subcommand", result.stderr)

    def test_a_missing_tail_lands_at_the_deepest_existing_ancestor(self):
        existing = self.tmp / "a"
        existing.mkdir()
        result = self.run_script("landing-dir", "%s/nonexistent/deep" % existing)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "%s\n" % existing)

    def test_an_existing_path_lands_on_itself(self):
        result = self.run_script("landing-dir", str(self.tmp))
        self.assertEqual(result.stdout, "%s\n" % self.tmp)

    def test_a_path_with_no_existing_ancestor_lands_at_the_root(self):
        """yo refuses a landing place outside the home; the home itself would
        be landed in silently."""
        result = self.run_script("landing-dir", "/yolobox-test-no-such-root/deep")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "/\n")


class TestEverySubcommandRefusesExtraArguments(GuestScript):
    """An argument a subcommand does not take is a usage error, exit 2, never
    dropped; and the marker yo reads as a version skew stays out of it."""

    def test_an_extra_argument_is_a_usage_error(self):
        for argv in (
            ("projects", "extra"),
            ("landing-dir",),
            ("landing-dir", "/", "extra"),
            ("aws-creds",),
            ("aws-creds", "p", "extra"),
            ("aws-check", "extra"),
            ("status", "extra"),
            ("rebuild",),
            ("rebuild", "switch", "extra"),
        ):
            with self.subTest(argv=argv):
                result = self.run_script(*argv)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn("usage: %s" % argv[0], result.stderr)
                self.assertNotIn("unknown subcommand", result.stderr)
                self.assertFalse(self.curl_was_called())


FAKE_AWS = """#!/bin/sh
cat >/dev/null
echo "arn:aws:sts::1:assumed-role/r/$AWS_PROFILE"
"""


class TestAwsCheck(GuestScript):
    def check(self, config):
        self.fake("aws", FAKE_AWS)
        path = self.tmp / "aws-config"
        path.write_text(config)
        return self.run_script("aws-check", extra_env={"AWS_CONFIG_FILE": str(path)})

    def test_every_profile_is_checked_even_when_aws_reads_stdin(self):
        result = self.check("[profile a]\nregion = x\n[default]\n[profile b]\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [line.split()[1] for line in result.stdout.splitlines()],
            ["a", "default", "b"],
        )

    def test_a_config_with_no_profile_fails_instead_of_passing_vacuously(self):
        result = self.check("[sso-session s]\nsso_region = x\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no profile sections", result.stderr)


class TestFactsRendering(unittest.TestCase):
    def test_lists_and_maps_become_words_and_a_shape_nix_cannot_render_is_refused(self):
        self.assertEqual(render_fact("k", ["a", "b"]), "a b")
        self.assertEqual(
            render_fact("k", {"target": "Cargo.toml", ".next": "p"}),
            ".next:p target:Cargo.toml",
        )
        self.assertEqual(render_fact("k", 75), "75")
        for value in (None, True, 1.5, [1], {"a": 1}, [["a"]]):
            with self.subTest(value=value):
                with self.assertRaisesRegex(TypeError, "no YOLOBOX_K rendering"):
                    render_fact("k", value)


class TestUnknownSubcommand(GuestScript):
    def test_an_unknown_subcommand_exits_64_with_the_marker_yo_looks_for(self):
        result = self.run_script("bogus")
        self.assertEqual(result.returncode, 64)
        self.assertIn("unknown subcommand 'bogus'", result.stderr)


# Every fake appends "<name> <cwd> <argv>" to $FAKE_LOG, so a test can tell
# which tool ran where. They stand in for the tools a project's environment
# provides: cargo, go and make are on no box-wide PATH.
FAKE_NOOP = """#!/bin/sh
echo "$(basename "$0") $(pwd) $*" >> "$FAKE_LOG"
"""

# Like real devbox: everything after `--` is joined, each argument in double
# quotes, into one script string that bash runs, so a `$1` after `sh -c` is
# expanded by that outer bash (to nothing) and never reaches sh.
# FAKE_DEVBOX_HIDE=<tool> runs it with a PATH of exactly two directories: the
# fakes without that tool, and $FAKE_SH_DIR holding `sh`, the only external
# the `command -v` probe needs. No ambient directory can supply the tool.
FAKE_DEVBOX = """#!/bin/bash
echo "devbox $(pwd) $*" >> "$FAKE_LOG"
while [ "$1" != -- ]; do shift; done
shift
script=""
for arg in "$@"; do script="$script \\"$arg\\""; done
if [ -n "${FAKE_DEVBOX_HIDE:-}" ]; then
    hidden="$(mktemp -d)"
    for tool in "$FAKE_BIN"/*; do
        [ "${tool##*/}" = "$FAKE_DEVBOX_HIDE" ] || ln -s "$tool" "$hidden/${tool##*/}"
    done
    PATH="$hidden:$FAKE_SH_DIR"
fi
exec "$BASH" -c "$script"
"""

FAKE_CARGO = """#!/bin/sh
echo "cargo $(pwd) $*" >> "$FAKE_LOG"
if [ "$1" = clean ]; then rm -rf ./target; fi
"""

# Reads ./Makefile like make does: no `clean:` line, or a prerequisite of it
# that is not a file, exits 2 with make's own words; -n prints the recipe
# instead of running it. FAKE_STDIN_EATER makes it read stdin to the end,
# FAKE_MAKE_DRYRUN_RC makes `-n` fail with that exit code, and
# FAKE_MAKE_QUOTE=backtick spells the quotes the way older makes do.
FAKE_MAKE = """#!/bin/sh
echo "make $(pwd) $*" >> "$FAKE_LOG"
if [ -n "${FAKE_STDIN_EATER:-}" ]; then cat > /dev/null; fi
open="'"
if [ "${FAKE_MAKE_QUOTE:-}" = backtick ]; then open='`'; fi
if ! grep -q '^clean:' Makefile; then
    echo "make: *** No rule to make target ${open}clean'.  Stop." >&2
    exit 2
fi
for prereq in $(sed -n 's/^clean:[[:space:]]*//p' Makefile); do
    if [ ! -e "$prereq" ]; then
        echo "make: *** No rule to make target ${open}$prereq', needed by ${open}clean'.  Stop." >&2
        exit 2
    fi
done
recipe="$(sed -n '/^clean:/,$ { /^\\t/ s/^\\t//p; }' Makefile)"
if [ "$1" = -n ]; then
    if [ -n "${FAKE_MAKE_DRYRUN_RC:-}" ]; then
        echo "make: boom" >&2
        exit "$FAKE_MAKE_DRYRUN_RC"
    fi
    printf '%s\\n' "$recipe"
else
    printf '%s\\n' "$recipe" | sh
fi
"""

CLEAN_MAKEFILE = "all:\n\ttrue\nclean:\n\trm -rf out\n"


@needs_tool("find", is_gnu("find"), "gc-user needs GNU find's -printf")
class TestGcUserGoCaches(GuestScript):
    def setUp(self):
        super().setUp()
        for name in ("podman", "npm"):
            self.fake(name, "#!/bin/sh\n")
        self.log = self.tmp / "log"

    def module_cache(self, gopath):
        """A module cache the way go writes it: read-only all the way down."""
        download = self.home / gopath / "pkg" / "mod" / "cache" / "download"
        download.mkdir(parents=True)
        (download / "sum.db").write_text("x")
        (download / "sum.db").chmod(0o444)
        for directory in (download, download.parent, download.parent.parent):
            directory.chmod(0o555)
        self.addCleanup(self.make_writable, self.home / gopath)
        return self.home / gopath / "pkg" / "mod"

    @staticmethod
    def make_writable(root):
        subprocess.run(["chmod", "-R", "u+w", str(root)], check=False)

    def test_report_mode_lists_the_module_cache_at_gos_default_gopath(self):
        cache = self.module_cache("go")
        result = self.run_script("gc-user")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(cache), result.stdout)

    def test_apply_removes_the_read_only_module_cache_at_gos_default_gopath(self):
        cache = self.module_cache("go")
        result = self.run_script("gc-user", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(cache.exists())

    def test_a_pkg_mod_without_a_download_cache_is_not_a_go_cache(self):
        stray = self.home / "go" / "pkg" / "mod"
        stray.mkdir(parents=True)
        result = self.run_script("gc-user", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(stray.exists())

    def test_a_module_cache_git_sees_inside_a_repo_is_never_a_cache(self):
        """A vendored pkg/mod is the project's: deleting it dirties the
        checkout and every later push is refused."""
        subprocess.run(["git", "init", "-q", str(self.home / "repo")], check=True)
        cache = self.module_cache("repo")
        result = self.run_script("gc-user", "--apply")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL: kept %s: git does not ignore it" % cache, result.stderr)
        self.assertTrue((cache / "cache" / "download" / "sum.db").exists())

    def test_a_module_cache_git_ignores_inside_a_repo_is_removed(self):
        subprocess.run(["git", "init", "-q", str(self.home / "repo")], check=True)
        (self.home / "repo" / ".gitignore").write_text("pkg/\n")
        cache = self.module_cache("repo")
        result = self.run_script("gc-user", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(cache.exists())


@needs_tool("find", is_gnu("find"), "gc-user --deep needs GNU find's -printf")
class TestGcUserDeep(GuestScript):
    def setUp(self):
        super().setUp()
        for name, body in (
            ("podman", FAKE_NOOP),
            ("npm", FAKE_NOOP),
            ("devbox", FAKE_DEVBOX),
            ("cargo", FAKE_CARGO),
            ("make", FAKE_MAKE),
            ("go", FAKE_NOOP),
        ):
            self.fake(name, body)
        self.log = self.tmp / "log"
        sh_dir = self.tmp / "sh-only"
        sh_dir.mkdir()
        (sh_dir / "sh").symlink_to(shutil.which("sh"))
        self.env.update(
            FAKE_LOG=str(self.log), FAKE_BIN=str(self.bin_dir), FAKE_SH_DIR=str(sh_dir)
        )

    def project(self, name, files, ignore=""):
        """A git repo under $HOME holding `files` (path -> content)."""
        root = self.home / name
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        if ignore:
            files = dict(files, **{".gitignore": ignore})
        for rel, content in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return root

    def log_lines(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def apply(self, **kwargs):
        return self.run_script("gc-user", "--apply", "--deep", **kwargs)

    def test_a_cargo_target_is_cleaned_by_cargo_through_devbox_from_the_project_root(
        self,
    ):
        root = self.project(
            "p",
            {"devbox.json": "{}", "Cargo.toml": "", "target/debug/x": "bin"},
            ignore="target\n",
        )
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("devbox %s run -q -- cargo clean" % root, self.log_lines())
        self.assertIn("cargo %s clean" % root, self.log_lines())
        self.assertFalse((root / "target").exists())

    def test_a_target_without_cargo_toml_is_kept_and_named(self):
        root = self.project("p", {"target/data": "mine"}, ignore="target\n")
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "kept %s/target: no Cargo.toml beside it, so it is not cargo's build"
            " output" % root,
            result.stdout,
        )
        self.assertTrue((root / "target" / "data").exists())
        self.assertFalse(any(line.startswith("cargo ") for line in self.log_lines()))

    def test_a_cargo_target_git_does_not_ignore_is_kept_and_cargo_never_runs(self):
        root = self.project("p", {"Cargo.toml": "", "target/x": "tracked"})
        result = self.apply()
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "FAIL: kept %s/target: git does not ignore it" % root, result.stderr
        )
        self.assertTrue((root / "target" / "x").exists())
        self.assertFalse(any(line.startswith("cargo ") for line in self.log_lines()))

    def test_a_node_modules_git_tracks_is_kept_with_the_remedy_for_tracked_files(self):
        """A .gitignore line does nothing for files git already tracks, so the
        remedy names `git rm --cached`."""
        root = self.project("p", {"node_modules/a": "x"}, ignore="node_modules\n")
        subprocess.run(
            ["git", "-C", str(root), "add", "-f", "node_modules"], check=True
        )
        result = self.apply()
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "FAIL: kept %s/node_modules: git tracks files in it" % root, result.stderr
        )
        self.assertIn("git rm -r --cached", result.stderr)
        self.assertTrue((root / "node_modules" / "a").exists())

    def test_ignored_node_modules_and_venv_are_removed(self):
        root = self.project(
            "p",
            {"node_modules/a/b": "x", ".venv/bin/python": "x", "src/app": "keep"},
            ignore="node_modules\n.venv\n",
        )
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((root / "node_modules").exists())
        self.assertFalse((root / ".venv").exists())
        self.assertTrue((root / "src" / "app").exists())

    def test_a_venv_that_ignores_itself_is_removed(self):
        # uv writes `*` into .venv/.gitignore and nothing into the project's.
        root = self.project("p", {".venv/.gitignore": "*\n", ".venv/bin/py": "x"})
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((root / ".venv").exists())

    def test_a_directory_outside_every_repo_is_kept_without_failing(self):
        loose = self.home / "loose" / "node_modules"
        loose.mkdir(parents=True)
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("kept %s: not inside a git repository" % loose, result.stdout)
        self.assertTrue(loose.exists())

    def test_a_next_beside_package_json_is_removed_and_one_without_is_kept(self):
        web = self.project(
            "web", {"package.json": "{}", ".next/cache": "x"}, ignore=".next\n"
        )
        bare = self.project("bare", {".next/cache": "x"}, ignore=".next\n")
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((web / ".next").exists())
        self.assertTrue((bare / ".next").exists())
        self.assertIn("kept %s/.next: no package.json beside it" % bare, result.stdout)

    def test_the_manifests_a_name_needs_are_the_facts(self):
        self.write_facts(build_output_manifests={"target": "Cargo.toml"})
        bare = self.project("bare", {".next/cache": "x"}, ignore=".next\n")
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((bare / ".next").exists())

    def test_a_makefile_with_a_clean_rule_gets_make_clean_in_its_root(self):
        root = self.project("p", {"Makefile": CLEAN_MAKEFILE})
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("make %s clean" % root, self.log_lines())

    def test_a_makefile_without_a_clean_rule_gets_no_make_clean_and_no_message(self):
        root = self.project("p", {"Makefile": "all:\n\ttrue\n"})
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("make %s clean" % root, self.log_lines())
        self.assertNotIn(str(root), result.stdout)
        self.assertNotIn("FAIL", result.stderr)

    def test_an_envrc_beside_a_makefile_is_ignored_and_make_clean_runs_directly(self):
        root = self.project("p", {"Makefile": CLEAN_MAKEFILE, ".envrc": ""})
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("make %s clean" % root, self.log_lines())
        self.assertFalse(
            any(line.startswith(("devbox ", "direnv ")) for line in self.log_lines())
        )

    def test_a_go_mod_root_gets_go_clean(self):
        root = self.project("p", {"go.mod": "module p\n"})
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("go %s clean" % root, self.log_lines())

    def test_a_tool_missing_from_the_project_environment_keeps_the_target(self):
        root = self.project(
            "p",
            {"devbox.json": "{}", "Cargo.toml": "", "target/x": "bin"},
            ignore="target\n",
        )
        result = self.apply(extra_env={"FAKE_DEVBOX_HIDE": "cargo"})
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "FAIL: kept %s/target: cargo is not in %s's environment" % (root, root),
            result.stderr,
        )
        self.assertTrue((root / "target" / "x").exists())
        self.assertFalse(any(line.startswith("cargo ") for line in self.log_lines()))

    def test_report_mode_sizes_the_cargo_target_shows_the_dry_run_and_changes_nothing(
        self,
    ):
        cargo = self.project(
            "c", {"Cargo.toml": "", "target/x": "bin"}, ignore="target\n"
        )
        make = self.project("m", {"Makefile": CLEAN_MAKEFILE})
        result = self.run_script("gc-user", "--deep")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertTrue(
            any(line.endswith("%s/target  (cargo clean)" % cargo) for line in lines),
            result.stdout,
        )
        header = "%s: make clean would run:" % make
        self.assertIn(header, lines)
        self.assertEqual(lines[lines.index(header) + 1], "    rm -rf out")
        self.assertTrue((cargo / "target" / "x").exists())
        self.assertNotIn("make %s clean" % make, self.log_lines())
        self.assertNotIn("cargo %s clean" % cargo, self.log_lines())

    def test_a_directory_an_earlier_clean_removed_is_reported_gone_not_failed(self):
        # `git check-ignore` on a missing path exits 1 for a dir-only pattern.
        root = self.project(
            "p",
            {
                "Cargo.toml": "",
                "Makefile": "clean:\n\trm -rf target\n",
                "target/x": "bin",
            },
            ignore="target/\n",
        )
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("FAIL", result.stderr)
        self.assertIn("ok:   already gone: %s/target" % root, result.stdout)
        self.assertFalse(any(line.startswith("cargo ") for line in self.log_lines()))

    def test_a_tool_that_reads_stdin_does_not_eat_the_list_of_projects(self):
        first = self.project("a", {"Makefile": CLEAN_MAKEFILE})
        second = self.project("b", {"Makefile": CLEAN_MAKEFILE})
        result = self.run_script(
            "gc-user", "--deep", extra_env={"FAKE_STDIN_EATER": "1"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for root in (first, second):
            self.assertIn("%s: make clean would run:" % root, result.stdout)

    def test_a_clean_rule_with_a_missing_prerequisite_is_a_failure_with_makes_words(
        self,
    ):
        root = self.project("p", {"Makefile": "clean: missing-file\n\ttrue\n"})
        result = self.apply()
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL: make -n clean failed in %s (exit 2)" % root, result.stderr)
        self.assertIn("No rule to make target 'missing-file'", result.stderr)
        self.assertNotIn("make %s clean" % root, self.log_lines())

    def test_a_make_that_quotes_with_a_backtick_still_means_no_clean_rule(self):
        self.project("p", {"Makefile": "all:\n\ttrue\n"})
        result = self.apply(extra_env={"FAKE_MAKE_QUOTE": "backtick"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("FAIL", result.stderr)

    def test_a_dry_run_that_exits_other_than_2_is_a_failure_with_makes_output(self):
        root = self.project("p", {"Makefile": CLEAN_MAKEFILE})
        result = self.apply(extra_env={"FAKE_MAKE_DRYRUN_RC": "3"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL: make -n clean failed in %s (exit 3)" % root, result.stderr)
        self.assertIn("make: boom", result.stderr)
        self.assertNotIn("make %s clean" % root, self.log_lines())

    def test_a_project_root_with_a_space_in_its_path_is_cleaned(self):
        root = self.project(
            "my proj",
            {
                "Cargo.toml": "",
                "Makefile": CLEAN_MAKEFILE,
                "target/x": "bin",
                "node_modules/a": "x",
            },
            ignore="target\nnode_modules\n",
        )
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("make %s clean" % root, self.log_lines())
        self.assertIn("cargo %s clean" % root, self.log_lines())
        self.assertFalse((root / "target").exists())
        self.assertFalse((root / "node_modules").exists())

    def test_a_git_worktree_root_whose_dot_git_is_a_file_is_cleaned(self):
        main = self.project("main", {"Cargo.toml": "", ".gitignore": "target\n"})
        author = [
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "-c",
            "commit.gpgsign=false",
        ]
        subprocess.run(["git", "-C", str(main), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(main), *author, "commit", "-qm", "x"], check=True
        )
        tree = self.home / "tree"
        subprocess.run(
            ["git", "-C", str(main), "worktree", "add", "-q", str(tree), "-b", "wt"],
            check=True,
        )
        (tree / "target").mkdir()
        (tree / "target" / "x").write_text("bin")
        self.assertTrue((tree / ".git").is_file())
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cargo %s clean" % tree, self.log_lines())
        self.assertFalse((tree / "target").exists())

    def test_crates_sharing_one_workspace_target_get_one_cargo_clean(self):
        root = self.project(
            "ws",
            {
                "Cargo.toml": "[workspace]\n",
                "crates/a/Cargo.toml": "",
                "crates/b/Cargo.toml": "",
                "target/x": "bin",
            },
            ignore="target\n",
        )
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        cleans = [line for line in self.log_lines() if line.startswith("cargo ")]
        self.assertEqual(cleans, ["cargo %s clean" % root])


FAKE_DIG = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_DIG_ARGV"
printf '%s' "$FAKE_DIG_OUT"
exit "$FAKE_DIG_RC"
"""

# `stat -f -c '%f %S' /` answers "<free blocks> <block size>".
FAKE_STAT = """#!/bin/sh
printf '%s\\n' "$FAKE_STAT_OUT"
exit "$FAKE_STAT_RC"
"""

# `dig +short NAME A` resolved: a CNAME on the way, then the address.
DNS_REPLY = "lb.example.net.\n140.82.121.4\n"


# `systemctl show -p ActiveState --value UNIT` answers the file FAKE_UNITS/UNIT.
FAKE_SYSTEMCTL = """#!/bin/sh
for unit; do :; done
cat "$FAKE_UNITS/$unit" 2>/dev/null || true
"""

# Each of these answers from one environment variable: its stdout, and
# FAKE_<TOOL>_RC its exit code.

FAKE_TAILSCALE = """#!/bin/sh
printf '%s' "$FAKE_TAILSCALE_OUT"
exit "$FAKE_TAILSCALE_RC"
"""

FAKE_LSBLK = """#!/bin/sh
printf '%s' "$FAKE_LSBLK_OUT"
exit "$FAKE_LSBLK_RC"
"""

# `df -h /` and `df -P /` both print a header and one data line.
FAKE_DF = """#!/bin/sh
case "$1" in
-h) printf 'Filesystem Size Used Avail Use%% Mounted on\n%s\n' "$FAKE_DF_H" ;;
-P) printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n%s\n' "$FAKE_DF_P" ;;
esac
exit "$FAKE_DF_RC"
"""

TAILSCALE_JSON = '{"Version": "1.90.0", "BackendState": "%s", "Peer": {}}'
DF_H = "/dev/mapper/root 59G 10G 46G 18% /"
DF_P = "/dev/mapper/root 61000000 10000000 51000000 18% /"


class TestStatusBlock(GuestScript):
    """`yolobox-guest status` prints one KEY=value line per fact yo shows, in one
    round trip, and a probe that fails leaves its key empty instead of failing
    the block."""

    def setUp(self):
        super().setUp()
        for name, body in (
            ("dig", FAKE_DIG),
            ("stat", FAKE_STAT),
            ("systemctl", FAKE_SYSTEMCTL),
            ("tailscale", FAKE_TAILSCALE),
            ("lsblk", FAKE_LSBLK),
            ("df", FAKE_DF),
        ):
            self.fake(name, body)
        self.dig_argv = self.tmp / "dig-argv"
        self.stamps = self.tmp / "stamps"
        self.stamps.mkdir()
        (self.stamps / "version").write_text("2.0.0\n")
        (self.stamps / "flake-ref").write_text("github:x/y/abc?narHash=sha256-h\n")
        self.units = self.tmp / "units"
        self.units.mkdir()
        for unit in (
            "yolobox-egress",
            "yolobox-network-ready",
            "yolobox-op-agent.socket",
            "yolobox-aws-broker.socket",
        ):
            self.set_unit(unit, "active")
        self.write_facts(
            version_stamp=str(self.stamps / "version"),
            flake_ref_stamp=str(self.stamps / "flake-ref"),
        )
        self.env.update(FAKE_DIG_ARGV=str(self.dig_argv), FAKE_UNITS=str(self.units))

    def set_unit(self, unit, state):
        (self.units / unit).write_text(state + "\n")

    def status(
        self,
        *args,
        curl_rc=0,
        curl_out="200",
        curl_err="",
        dig_out=DNS_REPLY,
        dig_rc=0,
        stat_out="1000000 4096",
        stat_rc=0,
        tailscale_out=TAILSCALE_JSON % "Running",
        tailscale_rc=0,
        lsblk_out="crypto_LUKS\n",
        lsblk_rc=0,
        df_h=DF_H,
        df_p=DF_P,
        df_rc=0,
    ):
        env = {
            "FAKE_DIG_OUT": dig_out,
            "FAKE_DIG_RC": str(dig_rc),
            "FAKE_STAT_OUT": stat_out,
            "FAKE_STAT_RC": str(stat_rc),
            "FAKE_TAILSCALE_OUT": tailscale_out,
            "FAKE_TAILSCALE_RC": str(tailscale_rc),
            "FAKE_LSBLK_OUT": lsblk_out,
            "FAKE_LSBLK_RC": str(lsblk_rc),
            "FAKE_DF_H": df_h,
            "FAKE_DF_P": df_p,
            "FAKE_DF_RC": str(df_rc),
            "FAKE_CURL_ERR": curl_err,
        }
        return self.run_script(
            "status", *args, curl_rc=curl_rc, curl_body=curl_out, extra_env=env
        )

    def fields(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(line.split("=", 1) for line in result.stdout.splitlines())

    def test_the_egress_probe_needs_no_name_resolved(self):
        # The box resolves through the gateway, so a probe by name would read a
        # dead gateway DNS as dead egress, and the two could never be told apart.
        self.fields(self.status())
        url = self.curl_argv.read_text().splitlines()[-1]
        host = urllib.parse.urlsplit(url).hostname
        ipaddress.ip_address(host)

    def test_a_healthy_box_reports_every_key(self):
        result = self.status()
        self.assertEqual(
            self.fields(result),
            {
                "version": "2.0.0",
                "flake_ref": "github:x/y/abc?narHash=sha256-h",
                "egress": "active",
                "network_ready": "active",
                "yolobox-op-agent.socket": "active",
                "yolobox-aws-broker.socket": "active",
                "tailscale": "Running",
                "fstype": "crypto_LUKS",
                "rootfs": DF_H,
                "rootfs_full": "no",
                "rootdisk": "ok",
                "internet": "ok",
                "gateway_dns": "ok",
            },
        )
        argv = self.dig_argv.read_text().split("\n")
        self.assertEqual(argv[-4:-1], ["@192.168.5.2", "github.com", "A"])

    def test_a_unit_state_is_reported_as_systemctl_says_it(self):
        self.set_unit("yolobox-egress", "failed")
        self.set_unit("yolobox-network-ready", "failed")
        self.set_unit("yolobox-op-agent.socket", "inactive")
        fields = self.fields(self.status())
        self.assertEqual(fields["egress"], "failed")
        self.assertEqual(fields["network_ready"], "failed")
        self.assertEqual(fields["yolobox-op-agent.socket"], "inactive")
        self.assertEqual(fields["yolobox-aws-broker.socket"], "active")

    def test_a_logged_out_tailscale_still_reports_its_backend_state(self):
        # `tailscale status` exits 1 while logged out, JSON and all.
        fields = self.fields(
            self.status(tailscale_out=TAILSCALE_JSON % "NeedsLogin", tailscale_rc=1)
        )
        self.assertEqual(fields["tailscale"], "NeedsLogin")

    def test_a_probe_that_fails_or_answers_nonsense_leaves_its_key_empty(self):
        for name, broken in (
            ("version", dict()),
            ("tailscale", dict(tailscale_out="", tailscale_rc=1)),
            ("tailscale", dict(tailscale_out="null", tailscale_rc=0)),
            ("fstype", dict(lsblk_out="", lsblk_rc=32)),
            ("rootfs", dict(df_h="", df_rc=1)),
            ("rootfs_full", dict(df_p="/dev/x 1 1 1 lizard /")),
            ("rootfs_full", dict(df_p="/dev/x 1 1 1 \u00b2% /")),
            ("rootdisk", dict(stat_out="", stat_rc=1)),
            ("rootdisk", dict(stat_out="lizard 4096")),
            ("rootdisk", dict(stat_out="-1")),
        ):
            with self.subTest(name=name, broken=broken):
                if name == "version":
                    (self.stamps / "version").unlink()
                fields = self.fields(self.status(**broken))
                self.assertEqual(fields[name], "")
                self.assertEqual(len(fields), 13)

    def test_a_value_is_cut_at_its_first_newline_so_it_cannot_forge_a_key(self):
        (self.stamps / "version").write_text("2.0.0\ninternet=ok\n")
        fields = self.fields(self.status(curl_rc=7, curl_out="000"))
        self.assertEqual(fields["version"], "2.0.0")
        self.assertEqual(fields["internet"], "dead")

    def test_the_root_filesystem_is_full_at_90_percent_and_not_below(self):
        for pct, word in ((89, "no"), (90, "yes"), (100, "yes")):
            with self.subTest(pct=pct):
                fields = self.fields(
                    self.status(df_p="/dev/x 100 %d %d %d%% /" % (pct, 100 - pct, pct))
                )
                self.assertEqual(fields["rootfs_full"], word)

    def test_a_gateway_that_answers_without_an_address_is_dead(self):
        # Lima's gateway answers some questions itself while a name it must
        # ask the host for can fail: only a resolved address proves lookups work.
        for out in ("", "lb.example.net.\n", "a.example.\nb.example.net.\n"):
            with self.subTest(out=out):
                fields = self.fields(self.status(dig_out=out, dig_rc=0))
                self.assertEqual(fields["gateway_dns"], "dead")

    def test_a_dig_answer_with_no_name_is_unknown_with_digs_words(self):
        for out, rc, said in (
            ("lizard\n", 0, "lizard"),
            # A refusal only counts as dig's own `;;` line.
            ("connection refused\n", 0, "connection refused"),
            ("140.82.121\n", 0, "140.82.121"),
            ("dig: bad\nx\n", 10, "dig: bad x"),
        ):
            with self.subTest(out=out):
                fields = self.fields(self.status(dig_out=out, dig_rc=rc))
                self.assertEqual(
                    fields["gateway_dns"], "unknown: dig exited %d: %s" % (rc, said)
                )

    def test_the_tools_whose_words_it_reads_run_in_the_c_locale(self):
        # A translated dig would print its no-reply line in other words.
        seen = self.tmp / "dig-locale"
        self.fake(
            "dig",
            '#!/bin/sh\nprintf %%s "${LC_ALL-unset}" > %s\nprintf "1.2.3.4\\n"\n'
            % shlex.quote(str(seen)),
        )
        self.env.update(LC_ALL="fr_FR.UTF-8", LANG="fr_FR.UTF-8")
        self.assertEqual(self.fields(self.status())["gateway_dns"], "ok")
        self.assertEqual(seen.read_text(), "C")

    def test_a_gateway_that_sends_no_reply_is_dead_whatever_dig_exits(self):
        # bind9 #3235 and #3615: exit 0 on an ICMP unreachable.
        for out, rc in (
            (";; no servers could be reached\n", 9),
            (";; Connection refused\n", 0),
            (";; communications error to 192.168.5.2#53: timed out\n", 0),
            # bind 9.18 and later, in lower case; the egress table's ICMP answer.
            (";; communications error to 192.168.5.2#53: connection refused\n", 0),
            (";; communications error to 192.168.5.2#53: host unreachable\n", 0),
            (";; communications error to 192.168.5.2#53: Host Unreachable\n", 9),
        ):
            with self.subTest(out=out, rc=rc):
                fields = self.fields(self.status(dig_out=out, dig_rc=rc))
                self.assertEqual(fields["gateway_dns"], "dead")

    def test_egress_that_cannot_connect_or_times_out_is_dead(self):
        for rc, err in (
            (7, "curl: (7) Failed to connect"),
            (28, "curl: (28) timed out"),
        ):
            with self.subTest(rc=rc):
                fields = self.fields(
                    self.status(curl_rc=rc, curl_out="000", curl_err=err)
                )
                self.assertEqual(
                    (fields["internet"], fields["gateway_dns"]), ("dead", "ok")
                )

    def test_any_other_curl_answer_is_unknown_with_curls_words(self):
        for rc, out, err, said in (
            (35, "000", "curl: (35) TLS alert\n", "curl: (35) TLS alert 000"),
            (127, "", "curl: not found\n", "curl: not found"),
            (0, "lizard", "", "lizard"),
            (0, "000", "", "000"),
        ):
            with self.subTest(rc=rc, out=out):
                fields = self.fields(
                    self.status(curl_rc=rc, curl_out=out, curl_err=err)
                )
                self.assertEqual(
                    fields["internet"], "unknown: curl exited %d: %s" % (rc, said)
                )

    def test_the_root_disk_is_full_just_below_64_mib_free_for_root_and_not_at_64(self):
        blocks_64_mib = 64 * 1024 * 1024 // 4096
        for blocks, word in ((blocks_64_mib - 1, "full"), (blocks_64_mib, "ok")):
            with self.subTest(blocks=blocks):
                fields = self.fields(self.status(stat_out="%d 4096" % blocks))
                self.assertEqual(fields["rootdisk"], word)


FAKE_SUDO = """#!/bin/sh
printf '%s\\0' "$@" > "$FAKE_SUDO_ARGV"
exit "$FAKE_SUDO_RC"
"""


class TestGcMachineNeedsSudo(GuestScript):
    """The machine tier runs everything under sudo, so a sudo that cannot run
    stops it at once, instead of measuring every path as absent."""

    def setUp(self):
        super().setUp()
        self.fake("sudo", FAKE_SUDO)
        self.sudo_argv = self.tmp / "sudo-argv"
        self.env.update(FAKE_SUDO_ARGV=str(self.sudo_argv))

    def gc_machine(self, *args, sudo_rc):
        return self.run_script(
            "gc-machine", *args, extra_env={"FAKE_SUDO_RC": str(sudo_rc)}
        )

    def test_a_sudo_that_cannot_run_fails_the_tier_before_anything_else(self):
        for args in ((), ("--apply",)):
            with self.subTest(args=args):
                result = self.gc_machine(*args, sudo_rc=1)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("needs sudo", result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(
                    self.sudo_argv.read_text().split("\0")[:-1], ["-n", "true"]
                )

    def test_a_working_sudo_lets_the_report_run(self):
        result = self.gc_machine(sudo_rc=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("machine tier", result.stdout)


# `id -u` answers FAKE_UID, `id -un` FAKE_USER.
FAKE_ID = """#!/bin/sh
case "$*" in
-u) printf '%s\\n' "$FAKE_UID" ;;
-un) printf '%s\\n' "$FAKE_USER" ;;
*) echo "fake id: unexpected $*" >&2; exit 99 ;;
esac
"""

# What yo bootstrap stamps into /etc/yolobox/flake-ref: the exact tree it built.
REV = "0123456789abcdef0123456789abcdef01234567"
NAR = "sha256-" + "A" * 43 + "="
PINNED = "github:aka-rider/yolobox/%s?narHash=%s" % (REV, NAR)

# `uname -n` answers FAKE_HOSTNAME.
FAKE_UNAME = """#!/bin/sh
[ "$*" = -n ] || { echo "fake uname: unexpected $*" >&2; exit 99; }
printf '%s\\n' "$FAKE_HOSTNAME"
"""


class RebuildCase(GuestScript):
    def setUp(self):
        super().setUp()
        for name, text in (("sudo", FAKE_SUDO), ("id", FAKE_ID), ("uname", FAKE_UNAME)):
            self.fake(name, text)
        self.stamps = self.tmp / "stamps"
        self.stamps.mkdir()
        self.sudo_argv = self.tmp / "sudo-argv"
        self.write_facts(flake_ref_stamp=str(self.stamps / "flake-ref"))
        self.env.update(
            FAKE_SUDO_ARGV=str(self.sudo_argv),
            FAKE_USER="alice",
            FAKE_HOSTNAME="yolobox-e2e",
        )
        self.stamp(PINNED + "\n")

    def stamp(self, text):
        (self.stamps / "flake-ref").write_bytes(text.encode())

    def rebuild(self, *args, uid=FACTS["operator_uid"], sudo_rc=0):
        return self.run_script(
            "rebuild",
            *args,
            extra_env={"FAKE_UID": str(uid), "FAKE_SUDO_RC": str(sudo_rc)},
        )

    def sudo_argv_list(self):
        return self.sudo_argv.read_text().split("\0")[:-1]

    def assert_refused(self, result, code, needle):
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertIn(needle, result.stderr)
        self.assertNotIn("unknown subcommand", result.stderr)
        self.assertFalse(self.sudo_argv.exists(), "sudo ran")


class TestRebuild(RebuildCase):
    def test_anything_but_one_switch_or_boot_is_a_usage_error(self):
        for args in (
            (),
            ("test",),
            ("dry-activate",),
            ("--rollback",),
            ("",),
            ("switch", "extra"),
            ("SWITCH",),
        ):
            with self.subTest(args=args):
                self.assert_refused(self.rebuild(*args), 2, "usage: rebuild")

    def test_root_and_the_agent_are_refused(self):
        for uid in (0, FACTS["agent_uid"]):
            with self.subTest(uid=uid):
                self.assert_refused(
                    self.rebuild("switch", uid=uid), 2, "run as the operator"
                )

    def test_a_stamp_that_is_not_one_non_empty_line_is_refused_naming_the_file(self):
        for text in ("", "\n", "\n\n", PINNED + "\n" + PINNED + "\n", PINNED + "\n\n"):
            with self.subTest(text=text[:40]):
                self.stamp(text)
                self.assert_refused(
                    self.rebuild("switch"), 1, str(self.stamps / "flake-ref")
                )

    def test_a_stamp_that_is_not_a_github_ref_is_refused_naming_the_file(self):
        for text in ("git+https://elsewhere/x\n", "/srv/tree\n", "GITHUB:x/y\n", "x\n"):
            with self.subTest(text=text):
                self.stamp(text)
                self.assert_refused(
                    self.rebuild("switch"), 1, "not a github: flake ref"
                )

    def test_a_missing_stamp_is_refused_naming_the_file(self):
        (self.stamps / "flake-ref").unlink()
        self.assert_refused(self.rebuild("switch"), 1, str(self.stamps / "flake-ref"))

    def test_a_stamp_without_a_trailing_newline_is_read_whole(self):
        self.stamp(PINNED)
        result = self.rebuild("switch")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("YOLOBOX_FLAKE_REF=" + PINNED, self.sudo_argv_list())
        self.assertEqual(self.sudo_argv_list()[-1], PINNED + "#yolobox")

    def test_the_exit_code_of_the_rebuild_is_the_helpers(self):
        self.assertEqual(self.rebuild("switch", sudo_rc=4).returncode, 4)


# A sudo that runs its command, and a nixos-rebuild that only records it ran.
RUNNING_SUDO = """#!/bin/sh
exec "$@"
"""
RECORDING_REBUILD = """#!/bin/sh
printf '%s\\n' "$*" > "$FAKE_REBUILD_RAN"
printf '%s %s %s\\n' "$YOLOBOX_USERNAME" "$YOLOBOX_HOSTNAME" "$YOLOBOX_FLAKE_REF" \\
    >> "$FAKE_REBUILD_RAN"
"""


@needs_tool("flock")
class TestRebuildLock(RebuildCase):
    """With the real flock, a rebuild runs only while no other holds the lock."""

    def setUp(self):
        super().setUp()
        self.fake("sudo", RUNNING_SUDO)
        self.fake("nixos-rebuild", RECORDING_REBUILD)
        self.lock = self.tmp / "rebuild.lock"
        self.ran = self.tmp / "rebuild-ran"
        self.write_facts(rebuild_lock=str(self.lock))
        self.env.update(FAKE_REBUILD_RAN=str(self.ran))

    def test_switch_and_boot_rebuild_the_stamped_tree_for_this_host_and_user(self):
        for action in ("switch", "boot"):
            with self.subTest(action=action):
                result = self.rebuild(action)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    self.ran.read_text(),
                    "%s --impure --flake %s#yolobox\n" % (action, PINNED)
                    + "alice yolobox-e2e %s\n" % PINNED,
                )

    def test_a_held_lock_refuses_the_rebuild_at_once_and_says_why(self):
        with open(self.lock, "w") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            result = self.rebuild("boot")
        self.assertEqual(result.returncode, FACTS["rebuild_busy_exit"], result.stderr)
        self.assertIn("another rebuild is running", result.stderr)
        self.assertFalse(self.ran.exists(), "nixos-rebuild ran under a held lock")
