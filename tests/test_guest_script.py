"""Behaviour of nix/guest/yolobox-guest.sh, run for real under bash.

Nix builds the script with writeShellApplication, which prepends
`set -o errexit -o nounset -o pipefail` and puts `runtimeInputs` on PATH. Here
bash gets the same three options, and PATH holds a scratch directory with a
fake `curl` ahead of the system tools. Only subcommands that need nothing but
what macOS and Linux share are exercised, except `gc-user --deep`, which runs
only where GNU find's `-printf` exists (CI, the guest): the other subcommands
(`projects`, `gc-machine`) use it or sudo too.
"""

import json
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


# `id -u` is the only thing these stubs fake: the root check needs a choice,
# and the real id answers everything else.
REAL_ID = shutil.which("id")
FAKE_ID = (
    "#!/bin/sh\n"
    'if [ "$1" = -u ]; then printf \'%s\\n\' "$FAKE_UID"; else exec "'
    + REAL_ID
    + '" "$@"; fi\n'
)

FAKE_TAILSCALE = """#!/bin/sh
printf '%s' "$FAKE_TS_JSON"
printf '%s' "$FAKE_TS_ERR" >&2
exit "$FAKE_TS_RC"
"""

FAKE_NFT = """#!/bin/sh
printf '%s' "$FAKE_NFT_ERR" >&2
exit "$FAKE_NFT_RC"
"""

EXIT1_ONLINE = {"ID": "n-exit1", "Online": True, "TailscaleIPs": ["100.101.102.103"]}


def tailnet_node(host, *, selected=False):
    return {
        "HostName": host,
        "DNSName": host + ".example-tailnet.ts.net.",
        "ExitNode": selected,
        "ExitNodeOption": True,
    }


def tailscale_status(backend="Running", peers=(), exit_status=None):
    document = {
        "BackendState": backend,
        "Peer": {"nodekey:%d" % i: peer for i, peer in enumerate(peers)},
    }
    if exit_status is not None:
        document["ExitNodeStatus"] = exit_status
    return json.dumps(document)


FAKE_TIMEOUT = """#!/bin/sh
printf '%s\n' "$@" > "$FAKE_TIMEOUT_ARGV"
exit "$FAKE_TIMEOUT_RC"
"""

# Documents that are not the one shape NETWORK_STATE accepts: a non-empty
# exitNode string and no other key. Nix writes the file only for a box that
# names an exit node, so null is damage too.
DAMAGED_STATE_DOCUMENTS = (
    "",
    "not json",
    "null",
    "[]",
    '"exit1"',
    "{}",
    '{"exitNode": 7}',
    '{"exitNode": null}',
    '{"exitNode": ""}',
    '{"exitNode": "exit1", "killSwitch": true}',
    '{"killSwitch": true, "exitNode": "exit1"}',
)


class TestEgressStatus(GuestScript):
    def setUp(self):
        super().setUp()
        bin_dir = Path(self.env["PATH"].split(os.pathsep)[0])
        for name, body in (
            ("id", FAKE_ID),
            ("tailscale", FAKE_TAILSCALE),
            ("nft", FAKE_NFT),
        ):
            stub = bin_dir / name
            stub.write_text(body)
            stub.chmod(0o755)
        self.state = self.tmp / "network.json"
        self.write_state({"exitNode": "exit1"})
        self.env.update(
            YOLOBOX_NETWORK_STATE=str(self.state),
            FAKE_UID="0",
            FAKE_TS_JSON=tailscale_status(
                peers=[tailnet_node("exit1", selected=True)],
                exit_status=EXIT1_ONLINE,
            ),
            FAKE_TS_RC="0",
            FAKE_TS_ERR="",
            FAKE_NFT_RC="0",
            FAKE_NFT_ERR="",
        )

    def write_state(self, document):
        text = document if isinstance(document, str) else json.dumps(document)
        self.state.write_text(text)

    def egress(self, **fakes):
        return self.run_script("egress-status", extra_env=fakes)

    def assert_one_line(self, result):
        self.assertEqual(len(result.stdout.splitlines()), 1, result.stdout)

    def test_a_box_with_no_state_file_did_not_opt_in_and_exits_zero_neutrally(self):
        self.state.unlink()
        result = self.egress()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("kill switch not enabled", result.stdout)
        self.assertNotIn("BLOCKED", result.stdout)
        self.assertNotIn("WARNING", result.stdout)
        self.assert_one_line(result)

    def test_egress_is_ok_through_the_desired_exit_node_with_the_kill_switch_armed(
        self,
    ):
        result = self.egress()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "egress: ok — via exit node exit1; kill switch: armed\n",
        )

    def test_configuration_that_is_right_but_a_probe_that_fails_is_blocked_with_the_policy_hint(
        self,
    ):
        # curl exit 6: could not resolve or reach the host.
        result = self.egress(FAKE_CURL_RC="6")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(
            result.stdout,
            "egress: BLOCKED — desired: internet via exit node exit1; actual: "
            "exit1 selected and online, but traffic does not get through (probe "
            "https://github.com failed). Fix: check the tailnet policy grants this "
            "node autogroup:internet via exit1\n",
        )

    def test_a_probe_that_times_out_is_blocked_never_ok(self):
        # curl exit 28: the --max-time bound ran out, the blackhole case.
        result = self.egress(FAKE_CURL_RC="28")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("BLOCKED", result.stdout)
        self.assertNotIn("egress: ok", result.stdout)
        self.assert_one_line(result)

    def test_a_probe_tool_that_cannot_run_is_blocked_never_ok(self):
        # The shell's "command not found" exit: the probe could not run at all.
        result = self.egress(FAKE_CURL_RC="127")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("BLOCKED", result.stdout)
        self.assertNotIn("egress: ok", result.stdout)

    def test_the_probe_is_bounded_by_its_timeout_constant(self):
        self.egress()
        self.assertEqual(
            self.curl_argv.read_text().split("\n"),
            ["-sS", "-o", "/dev/null", "--max-time", "5", "https://github.com", ""],
        )

    def test_net_check_and_egress_status_send_the_same_probe(self):
        # The probe is one function, so the two subcommands cannot judge it differently.
        self.run_script("net-check", "8080")
        net_check_argv = self.curl_argv.read_text()
        self.curl_argv.unlink()
        self.egress()
        self.assertEqual(self.curl_argv.read_text(), net_check_argv)

    def test_a_missing_kill_switch_table_is_a_warning_that_exits_two(self):
        result = self.egress(
            FAKE_NFT_RC="1", FAKE_NFT_ERR="Error: No such file or directory\n"
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("WARNING kill switch not armed", result.stdout)
        self.assertIn(
            "Fix: yo ssh sudo systemctl start yolobox-killswitch", result.stdout
        )
        self.assert_one_line(result)

    def test_without_root_the_table_is_unknown_and_egress_is_still_judged(self):
        result = self.egress(
            FAKE_UID="501",
            FAKE_NFT_RC="1",
            FAKE_NFT_ERR="Error: No such file or directory\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("kill switch: unknown (needs root)", result.stdout)

    def test_an_nft_failure_that_is_not_a_missing_table_is_unknown_not_absent(self):
        result = self.egress(
            FAKE_NFT_RC="1",
            FAKE_NFT_ERR="Error: Could not process rule: Operation not permitted\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "kill switch: unknown (nft: Error: Could not process rule: "
            "Operation not permitted)",
            result.stdout,
        )
        self.assertNotIn("WARNING", result.stdout)

    def test_a_state_file_of_any_other_shape_is_blocked_not_read_as_no_exit_node(
        self,
    ):
        for document in DAMAGED_STATE_DOCUMENTS:
            with self.subTest(document=document):
                self.write_state(document)
                result = self.egress()
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("BLOCKED — desired state unreadable", result.stdout)
                self.assertIn("Fix: yo bootstrap", result.stdout)
                self.assert_one_line(result)

    def test_tailscale_that_is_not_running_is_blocked_naming_the_backend(self):
        result = self.egress(FAKE_TS_JSON=tailscale_status(backend="NeedsLogin"))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("actual: tailscale is NeedsLogin", result.stdout)
        self.assertIn("Fix: yo ssh sudo tailscale up", result.stdout)

    def test_a_tailscale_that_cannot_answer_is_blocked_with_its_stderr_and_a_restart(
        self,
    ):
        result = self.egress(
            FAKE_TS_RC="1", FAKE_TS_ERR="failed to connect to local tailscaled\n"
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("failed to connect to local tailscaled", result.stdout)
        self.assertIn("Fix: yo ssh sudo systemctl restart tailscaled", result.stdout)
        self.assert_one_line(result)

    def test_tailscale_output_that_is_not_status_json_is_blocked_as_unreadable(self):
        for output in ("", "garbage", "[]"):
            with self.subTest(output=output):
                result = self.egress(FAKE_TS_JSON=output)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("tailscale status unreadable", result.stdout)
                self.assert_one_line(result)

    def test_no_exit_node_selected_is_blocked_with_the_restart_of_the_exit_node_unit(
        self,
    ):
        result = self.egress(
            FAKE_TS_JSON=tailscale_status(peers=[tailnet_node("exit1")])
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(
            result.stdout,
            "egress: BLOCKED — desired: internet via exit node exit1; actual: none selected. "
            "Fix: yo ssh sudo systemctl restart yolobox-tailscale-exit-node\n",
        )

    def test_a_different_exit_node_selected_names_both_the_actual_and_the_desired(
        self,
    ):
        peers = [tailnet_node("exit1"), tailnet_node("exit2", selected=True)]
        result = self.egress(
            FAKE_TS_JSON=tailscale_status(peers=peers, exit_status=EXIT1_ONLINE)
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("desired: internet via exit node exit1", result.stdout)
        self.assertIn("actual: exit2 selected", result.stdout)
        self.assertIn(
            "Fix: yo ssh sudo systemctl restart yolobox-tailscale-exit-node",
            result.stdout,
        )

    def test_a_selected_exit_node_that_is_offline_is_blocked(self):
        offline = {"ID": "n-exit1", "Online": False, "TailscaleIPs": []}
        for exit_status in (None, offline):
            with self.subTest(exit_status=exit_status):
                result = self.egress(
                    FAKE_TS_JSON=tailscale_status(
                        peers=[tailnet_node("exit1", selected=True)],
                        exit_status=exit_status,
                    )
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("actual: exit1 selected but offline", result.stdout)

    def test_a_desired_node_missing_from_the_tailnet_is_blocked_by_name(self):
        documents = (
            tailscale_status(peers=[tailnet_node("exit2")]),
            tailscale_status(peers=[]),
            '{"BackendState": "Running"}',
        )
        for document in documents:
            with self.subTest(document=document):
                result = self.egress(FAKE_TS_JSON=document)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("no node named exit1 in the tailnet", result.stdout)

    def test_the_desired_node_is_matched_by_the_first_label_of_its_dns_name(self):
        peer = {
            "HostName": "Exit1-Box",
            "DNSName": "exit1.example-tailnet.ts.net.",
            "ExitNode": True,
            "ExitNodeOption": True,
        }
        result = self.egress(
            FAKE_TS_JSON=tailscale_status(peers=[peer], exit_status=EXIT1_ONLINE)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("via exit node exit1", result.stdout)


class TestNetCheckProbesTheGatewayOnlyWithoutTheKillSwitch(GuestScript):
    def setUp(self):
        super().setUp()
        bin_dir = Path(self.env["PATH"].split(os.pathsep)[0])
        timeout = bin_dir / "timeout"
        timeout.write_text(FAKE_TIMEOUT)
        timeout.chmod(0o755)
        self.probe_argv = self.tmp / "timeout-argv"
        self.state = self.tmp / "network.json"
        self.env.update(
            FAKE_TIMEOUT_ARGV=str(self.probe_argv),
            YOLOBOX_NETWORK_STATE=str(self.state),
        )

    def net_check(self, *, curl_rc=0, probe_rc=0):
        return self.run_script(
            "net-check",
            "8080",
            curl_rc=curl_rc,
            extra_env={"FAKE_TIMEOUT_RC": str(probe_rc)},
        )

    def test_without_a_state_file_the_gateway_is_probed_on_the_port(self):
        result = self.net_check(probe_rc=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "egress=ok\ngateway=ok\n")
        self.assertIn("/dev/tcp/192.168.5.2/8080", self.probe_argv.read_text())

    def test_a_gateway_that_does_not_answer_is_reported_dead(self):
        result = self.net_check(probe_rc=1)
        self.assertEqual(result.stdout, "egress=ok\ngateway=dead\n")

    def test_egress_that_fails_is_reported_dead_whatever_the_gateway_does(self):
        result = self.net_check(curl_rc=6, probe_rc=0)
        self.assertEqual(result.stdout, "egress=dead\ngateway=ok\n")

    def test_a_kill_switch_box_skips_the_gateway_probe_without_running_it(self):
        self.state.write_text('{"exitNode": "exit1"}')
        result = self.net_check(probe_rc=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "egress=ok\ngateway=skipped\n")
        self.assertEqual(result.stderr, "")
        self.assertFalse(self.probe_argv.exists())

    def test_a_damaged_state_file_still_skips_the_probe_and_warns_on_stderr(self):
        for document in DAMAGED_STATE_DOCUMENTS:
            with self.subTest(document=document):
                self.state.write_text(document)
                result = self.net_check(probe_rc=0)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "egress=ok\ngateway=skipped\n")
                self.assertIn(str(self.state), result.stderr)
                self.assertIn("unreadable", result.stderr)
                self.assertFalse(self.probe_argv.exists())
