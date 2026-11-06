"""Behaviour of nix/guest/yolobox-guest.sh, run for real under bash.

Nix builds the script with writeShellApplication, which prepends
`set -o errexit -o nounset -o pipefail` and puts `runtimeInputs` on PATH. Here
bash gets the same three options, and PATH holds a scratch directory with a
fake `curl` ahead of the system tools (and, per class, fake `dig`, `stat`,
`sudo` and `id`). Only subcommands that need nothing but
what macOS and Linux share are exercised, except `gc-user --deep`, which runs
only where GNU find's `-printf` exists (CI, the guest): the other subcommands
(`projects`, `gc-machine`) use it or sudo too.

Tools whose absence skips tests: GNU find (the `gc-user` classes), flock (the
rebuild lock).
"""

import fcntl
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

GUEST_SCRIPT = (
    Path(__file__).resolve().parent.parent / "nix" / "guest" / "yolobox-guest.sh"
)

FAKE_CURL = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_CURL_ARGV"
printf '%s' "$FAKE_CURL_BODY"
exit "$FAKE_CURL_RC"
"""

BROKER_SOCK = "/run/yolobox/aws-broker.sock"


class GuestScript(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        curl = bin_dir / "curl"
        curl.write_text(FAKE_CURL)
        curl.chmod(0o755)
        self.curl_argv = self.tmp / "curl-argv"
        self.env = {
            # The ambient PATH stays after the fakes: the script needs mktemp,
            # find and the rest, and on NixOS they are not in /usr/bin.
            "PATH": os.pathsep.join(
                [str(bin_dir), os.environ.get("PATH", "/usr/bin:/bin")]
            ),
            "HOME": str(self.home),
            "YOLOBOX_AWS_BROKER_SOCK": BROKER_SOCK,
            "FAKE_CURL_ARGV": str(self.curl_argv),
            "FAKE_CURL_BODY": "",
            "FAKE_CURL_RC": "0",
        }

    def run_script(self, *args, curl_rc=0, curl_body="", extra_env=None):
        env = dict(self.env, FAKE_CURL_RC=str(curl_rc), FAKE_CURL_BODY=curl_body)
        env.update(extra_env or {})
        return subprocess.run(
            [
                "bash",
                "-o",
                "errexit",
                "-o",
                "nounset",
                "-o",
                "pipefail",
                str(GUEST_SCRIPT),
                *args,
            ],
            capture_output=True,
            text=True,
            env=env,
            # A subcommand that loops forever (landing-dir on a relative path
            # once did) must fail the test, not hang the suite.
            timeout=20,
        )

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

    def test_a_path_with_no_existing_ancestor_lands_at_home(self):
        result = self.run_script("landing-dir", "/yolobox-test-no-such-root/deep")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "%s\n" % self.home)


FAKE_AWS = """#!/bin/sh
cat >/dev/null
echo "arn:aws:sts::1:assumed-role/r/$AWS_PROFILE"
"""


class TestAwsCheck(GuestScript):
    def check(self, config):
        aws = self.tmp / "bin" / "aws"
        aws.write_text(FAKE_AWS)
        aws.chmod(0o755)
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


class TestUnknownSubcommand(GuestScript):
    def test_an_unknown_subcommand_exits_64_with_the_marker_yo_looks_for(self):
        result = self.run_script("bogus")
        self.assertEqual(result.returncode, 64)
        self.assertIn("unknown subcommand 'bogus'", result.stderr)


def has_gnu_find():
    try:
        probe = subprocess.run(["find", "--version"], capture_output=True, text=True)
    except OSError:
        return False
    return probe.returncode == 0 and "GNU" in probe.stdout


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


@unittest.skipUnless(has_gnu_find(), "gc-user needs GNU find's -printf")
class TestGcUserGoCaches(GuestScript):
    def setUp(self):
        super().setUp()
        bin_dir = self.tmp / "bin"
        for name in ("podman", "npm"):
            (bin_dir / name).write_text("#!/bin/sh\n")
            (bin_dir / name).chmod(0o755)
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


@unittest.skipUnless(has_gnu_find(), "gc-user --deep needs GNU find's -printf")
class TestGcUserDeep(GuestScript):
    def setUp(self):
        super().setUp()
        bin_dir = self.tmp / "bin"
        for name, body in (
            ("podman", FAKE_NOOP),
            ("npm", FAKE_NOOP),
            ("devbox", FAKE_DEVBOX),
            ("cargo", FAKE_CARGO),
            ("make", FAKE_MAKE),
            ("go", FAKE_NOOP),
        ):
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        self.log = self.tmp / "log"
        sh_dir = self.tmp / "sh-only"
        sh_dir.mkdir()
        (sh_dir / "sh").symlink_to(shutil.which("sh"))
        self.env.update(
            FAKE_LOG=str(self.log), FAKE_BIN=str(bin_dir), FAKE_SH_DIR=str(sh_dir)
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

DNS_REPLY = ";; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 1\n"
DNS_SERVFAIL = ";; ->>HEADER<<- opcode: QUERY, status: SERVFAIL, id: 1\n"


class TestNetCheckSkipsTheGatewayProbeOnlyWhenTheBoxBlocksIt(GuestScript):
    def setUp(self):
        super().setUp()
        bin_dir = Path(self.env["PATH"].split(os.pathsep)[0])
        dig = bin_dir / "dig"
        dig.write_text(FAKE_DIG)
        dig.chmod(0o755)
        stat = bin_dir / "stat"
        stat.write_text(FAKE_STAT)
        stat.chmod(0o755)
        self.dig_argv = self.tmp / "dig-argv"
        self.blocked = self.tmp / "gateway-blocked"
        self.env.update(
            FAKE_DIG_ARGV=str(self.dig_argv),
            YOLOBOX_GATEWAY_BLOCKED=str(self.blocked),
        )

    def net_check(
        self,
        *args,
        curl_rc=0,
        dig_out=DNS_REPLY,
        dig_rc=0,
        stat_out="1000000 4096",
        stat_rc=0,
    ):
        return self.run_script(
            "net-check",
            *args,
            curl_rc=curl_rc,
            extra_env={
                "FAKE_DIG_OUT": dig_out,
                "FAKE_DIG_RC": str(dig_rc),
                "FAKE_STAT_OUT": stat_out,
                "FAKE_STAT_RC": str(stat_rc),
            },
        )

    def test_without_the_marker_the_gateway_is_asked_for_the_root_name_servers(self):
        result = self.net_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "egress=ok\ngateway=ok\nrootdisk=ok\n")
        argv = self.dig_argv.read_text().split("\n")
        self.assertEqual(argv[-4:-1], ["@192.168.5.2", ".", "NS"])

    def test_a_dns_error_reply_still_proves_the_gateway_answers(self):
        result = self.net_check(dig_out=DNS_SERVFAIL, dig_rc=0)
        self.assertEqual(result.stdout, "egress=ok\ngateway=ok\nrootdisk=ok\n")

    def test_a_gateway_that_sends_no_reply_is_reported_dead(self):
        result = self.net_check(dig_out=";; no servers could be reached\n", dig_rc=9)
        self.assertEqual(result.stdout, "egress=ok\ngateway=dead\nrootdisk=ok\n")

    def test_dig_exiting_zero_without_a_reply_is_still_dead(self):
        # bind9 #3235 and #3615: exit 0 on an ICMP unreachable.
        result = self.net_check(dig_out=";; Connection refused\n", dig_rc=0)
        self.assertEqual(result.stdout, "egress=ok\ngateway=dead\nrootdisk=ok\n")

    def test_egress_that_fails_is_reported_dead_whatever_the_gateway_does(self):
        result = self.net_check(curl_rc=6)
        self.assertEqual(result.stdout, "egress=dead\ngateway=ok\nrootdisk=ok\n")

    def test_a_box_with_the_marker_skips_the_gateway_probe_without_running_it(self):
        self.blocked.write_text("")
        result = self.net_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "egress=ok\ngateway=skipped\nrootdisk=ok\n")
        self.assertEqual(result.stderr, "")
        self.assertFalse(self.dig_argv.exists())

    def test_a_root_disk_with_no_space_left_for_root_is_named_full(self):
        # The DNS failure a full disk causes (dhcpcd cannot write resolv.conf)
        # leaves the gateway answering and egress dead: the shape of a VPN.
        result = self.net_check(curl_rc=6, stat_out="0 4096")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "egress=dead\ngateway=ok\nrootdisk=full\n")

    def test_the_root_disk_is_full_just_below_64_mib_free_and_not_at_64(self):
        blocks_64_mib = 64 * 1024 * 1024 // 4096
        for blocks, word in ((blocks_64_mib - 1, "full"), (blocks_64_mib, "ok")):
            with self.subTest(blocks=blocks):
                result = self.net_check(stat_out="%d 4096" % blocks)
                self.assertIn("rootdisk=%s\n" % word, result.stdout)

    def test_a_stat_that_fails_or_answers_nonsense_is_an_error_not_a_pass(self):
        for out, rc in (("", 1), ("", 0), ("lizard 4096", 0), ("-1", 0)):
            with self.subTest(out=out, rc=rc):
                result = self.net_check(stat_out=out, stat_rc=rc)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_an_argument_is_a_usage_error(self):
        for arg in ("8080", "", "lizard"):
            with self.subTest(arg=arg):
                result = self.net_check(arg)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("usage: net-check", result.stderr)


FAKE_SUDO = """#!/bin/sh
printf '%s\\0' "$@" > "$FAKE_SUDO_ARGV"
exit "$FAKE_SUDO_RC"
"""

# `id -u` answers FAKE_UID, `id -un` FAKE_USER.
FAKE_ID = """#!/bin/sh
case "$*" in
-u) printf '%s\\n' "$FAKE_UID" ;;
-un) printf '%s\\n' "$FAKE_USER" ;;
*) echo "fake id: unexpected $*" >&2; exit 99 ;;
esac
"""

REF = "github:aka-rider/yolobox/v1.9.1"


class RebuildCase(GuestScript):
    def setUp(self):
        super().setUp()
        bin_dir = Path(self.env["PATH"].split(os.pathsep)[0])
        for name, text in (("sudo", FAKE_SUDO), ("id", FAKE_ID)):
            tool = bin_dir / name
            tool.write_text(text)
            tool.chmod(0o755)
        self.stamps = self.tmp / "stamps"
        self.stamps.mkdir()
        self.sudo_argv = self.tmp / "sudo-argv"
        self.env.update(
            YOLOBOX_STAMPS=str(self.stamps),
            FAKE_SUDO_ARGV=str(self.sudo_argv),
            FAKE_USER="alice",
        )
        self.stamp("flake-ref", REF + "\n")
        self.stamp("hostname", "yolobox-e2e\n")

    def stamp(self, name, text):
        (self.stamps / name).write_bytes(text.encode())

    def rebuild(self, *args, uid=501, sudo_rc=0):
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
    def test_switch_and_boot_rebuild_from_the_stamps_as_the_operator(self):
        for action in ("switch", "boot"):
            with self.subTest(action=action):
                result = self.rebuild(action)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    self.sudo_argv_list(),
                    [
                        "env",
                        "YOLOBOX_USERNAME=alice",
                        "YOLOBOX_HOSTNAME=yolobox-e2e",
                        "YOLOBOX_FLAKE_REF=" + REF,
                        "flock",
                        "-n",
                        "-E",
                        "75",
                        "/run/yolobox-rebuild.lock",
                        "nixos-rebuild",
                        action,
                        "--impure",
                        "--refresh",
                        "--flake",
                        REF + "#yolobox",
                    ],
                )

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
        for uid in (0, 1000):
            with self.subTest(uid=uid):
                self.assert_refused(
                    self.rebuild("switch", uid=uid), 2, "run as the operator"
                )

    def test_a_flake_ref_that_is_not_one_github_ref_is_refused_naming_the_file(self):
        for text in (
            "",
            "\n",
            REF + "\n" + REF + "\n",
            "path:/x\n",
            "github:x/y\n",
            "github:x/y/z#evil\n",
            "github:x/y/-z\n",
            REF + " \n",
            "x" * 4096 + "\n",
        ):
            with self.subTest(text=text[:40]):
                self.stamp("flake-ref", text)
                self.assert_refused(
                    self.rebuild("switch"), 1, str(self.stamps / "flake-ref")
                )

    def test_a_missing_flake_ref_is_refused_naming_the_file(self):
        (self.stamps / "flake-ref").unlink()
        self.assert_refused(self.rebuild("switch"), 1, str(self.stamps / "flake-ref"))

    def test_a_missing_or_empty_hostname_is_refused_naming_the_file(self):
        path = self.stamps / "hostname"
        for text in ("", "\n", None):
            with self.subTest(text=text):
                if text is None:
                    path.unlink()
                else:
                    self.stamp("hostname", text)
                self.assert_refused(self.rebuild("switch"), 1, str(path))

    def test_a_stamp_without_a_trailing_newline_is_read_whole(self):
        self.stamp("flake-ref", REF)
        self.stamp("hostname", "yolobox-e2e")
        result = self.rebuild("switch")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("YOLOBOX_FLAKE_REF=" + REF, self.sudo_argv_list())
        self.assertIn("YOLOBOX_HOSTNAME=yolobox-e2e", self.sudo_argv_list())

    def test_the_exit_code_of_the_rebuild_is_the_helpers(self):
        self.assertEqual(self.rebuild("switch", sudo_rc=4).returncode, 4)


# A sudo that runs its command, and a nixos-rebuild that only records it ran.
RUNNING_SUDO = """#!/bin/sh
exec "$@"
"""
RECORDING_REBUILD = """#!/bin/sh
printf '%s\\n' "$*" > "$FAKE_REBUILD_RAN"
"""


@unittest.skipUnless(shutil.which("flock"), "needs flock")
class TestRebuildLock(RebuildCase):
    """With the real flock, a rebuild runs only while no other holds the lock."""

    def setUp(self):
        super().setUp()
        bin_dir = Path(self.env["PATH"].split(os.pathsep)[0])
        for name, text in (
            ("sudo", RUNNING_SUDO),
            ("nixos-rebuild", RECORDING_REBUILD),
        ):
            tool = bin_dir / name
            tool.write_text(text)
            tool.chmod(0o755)
        self.lock = self.tmp / "rebuild.lock"
        self.ran = self.tmp / "rebuild-ran"
        self.env.update(
            YOLOBOX_REBUILD_LOCK=str(self.lock), FAKE_REBUILD_RAN=str(self.ran)
        )

    def test_a_free_lock_lets_the_rebuild_run_with_its_variables(self):
        result = self.rebuild("boot")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.ran.read_text(), "boot --impure --refresh --flake %s#yolobox\n" % REF
        )

    def test_a_held_lock_refuses_the_rebuild_at_once_and_says_why(self):
        with open(self.lock, "w") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            result = self.rebuild("boot")
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertIn("another rebuild is running", result.stderr)
        self.assertFalse(self.ran.exists(), "nixos-rebuild ran under a held lock")
