"""Behaviour of nix/guest/yolobox-guest.sh, run for real under bash.

Nix builds the script with writeShellApplication, which prepends
`set -o errexit -o nounset -o pipefail` and puts `runtimeInputs` on PATH. Here
bash gets the same three options, and PATH holds a scratch directory with a
fake `curl` ahead of the system tools. Only subcommands that need nothing but
what macOS and Linux share are exercised, except `gc-user --deep`, which runs
only where GNU find's `-printf` exists (CI, the guest): the other subcommands
(`projects`, `gc-machine`) use it or sudo too.
"""

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

# direnv exec DIR CMD...
FAKE_DIRENV = """#!/bin/sh
echo "direnv $(pwd) $*" >> "$FAKE_LOG"
shift 2
exec "$@"
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


@unittest.skipUnless(has_gnu_find(), "gc-user --deep needs GNU find's -printf")
class TestGcUserDeep(GuestScript):
    def setUp(self):
        super().setUp()
        bin_dir = self.tmp / "bin"
        for name, body in (
            ("podman", FAKE_NOOP),
            ("npm", FAKE_NOOP),
            ("devbox", FAKE_DEVBOX),
            ("direnv", FAKE_DIRENV),
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

    def test_make_clean_runs_through_direnv_when_only_an_envrc_exists(self):
        root = self.project("p", {"Makefile": CLEAN_MAKEFILE, ".envrc": ""})
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("direnv %s exec %s make clean" % (root, root), self.log_lines())
        self.assertFalse(any(line.startswith("devbox ") for line in self.log_lines()))

    def test_a_makefile_without_a_clean_rule_gets_no_make_clean_and_no_message(self):
        root = self.project("p", {"Makefile": "all:\n\ttrue\n"})
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("make %s clean" % root, self.log_lines())
        self.assertNotIn(str(root), result.stdout)
        self.assertNotIn("FAIL", result.stderr)

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
