"""Tests that bind one file of the repo to another.

Each class reads a second file off disk (a nix module, the guest script, the
brew formula) and compares it to what `yo` declares, so a change
to one side alone fails here instead of on a user's box.
"""

import contextlib
import io
import json
import os
import re
import unittest
from pathlib import Path

from test_yo import yo

REPO = Path(yo.__file__).parent
YOLOBOX_YAML = REPO / "lima" / "yolobox.yaml"
GUEST_SCRIPT = REPO / "nix" / "guest" / "yolobox-guest.sh"


def yaml_scalar(value):
    value = value.strip().strip('"')
    if value == "true":
        return True
    if value == "false":
        return False
    if value.isdigit():
        return int(value)
    return value


def yaml_block(text, key):
    """The lines indented under a top-level `key:` line."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == key + ":":
            start = i + 1
            break
    if start is None:
        return []
    end = len(lines)
    for i in range(start, len(lines)):
        line = lines[i]
        if line.strip() and not line[0].isspace():
            end = i
            break
    return lines[start:end]


def yaml_top_level_scalars(text, key):
    """Every value given to a top-level `key:` (exactly one is expected)."""
    return re.findall(r"^%s:[ \t]*(.*?)[ \t]*$" % re.escape(key), text, re.M)


def parse_yaml_list_entries(lines):
    entries = []
    current = None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            if current is not None:
                entries.append(current)
            current = {}
            stripped = stripped[2:].strip()
        if current is None or not stripped or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        current[key.strip()] = yaml_scalar(value)
    if current is not None:
        entries.append(current)
    return entries


def canonical_forwards(entries):
    return sorted(json.dumps(entry, sort_keys=True) for entry in entries)


def word_in(word, text):
    """Whole-word match: `pip` is not found inside `pip-tools`."""
    return re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(word), text) is not None


class TestLimaYamlMatchesPortForwardsConstant(unittest.TestCase):
    def test_full_port_forwards_match_lima_port_forwards_constant(self):
        parsed = parse_yaml_list_entries(
            yaml_block(YOLOBOX_YAML.read_text(), "portForwards")
        )
        self.assertEqual(
            canonical_forwards(parsed), canonical_forwards(yo.LIMA_PORT_FORWARDS)
        )

    def test_the_vm_has_no_host_mounts(self):
        # The whole design is "no host mounts, git is the only bridge".
        text = YOLOBOX_YAML.read_text()
        self.assertEqual(yaml_top_level_scalars(text, "mounts"), ["[]"])

    def test_the_hardening_keys_are_set(self):
        text = YOLOBOX_YAML.read_text()
        self.assertEqual(yaml_top_level_scalars(text, "vmType"), ["vz"])
        self.assertEqual(yaml_top_level_scalars(text, "propagateProxyEnv"), ["false"])
        ssh = {
            key.strip(): value.strip()
            for key, value in (
                line.split(":", 1)
                for line in yaml_block(text, "ssh")
                if ":" in line and not line.strip().startswith("#")
            )
        }
        self.assertEqual(ssh.get("loadDotSSHPubKeys"), "false")
        # The initrd sshd that unlocks the disk only speaks TCP, so lima must
        # not pick vsock for the port yo dials.
        self.assertEqual(ssh.get("overVsock"), "false")
        self.assertEqual(ssh.get("localPort"), str(yo.SSH_LOCAL_PORT))


class TestLuksFilesMatchYoConstants(unittest.TestCase):
    def text(self, *parts):
        return REPO.joinpath(*parts).read_text()

    def test_luks_nix_names_the_device_label_and_mapper_yo_uses(self):
        text = self.text("nix", "luks.nix")
        self.assertIn('device = "%s";' % yo.LUKS_DEVICE, text)
        self.assertIn('label = "%s";' % yo.LUKS_LABEL, text)
        self.assertIn("boot.initrd.luks.devices.%s = " % yo.LUKS_MAPPER, text)

    def test_apps_nix_names_the_three_scripts_and_the_device(self):
        text = self.text("nix", "luks", "apps.nix")
        for name in (
            yo.INITRD_UNLOCK_HELPER,
            "yolobox-luks-migrate",
            "yolobox-initrd-authorized-keys",
        ):
            self.assertIn(name, text)
        self.assertIn('LUKS_DEVICE = "%s";' % yo.LUKS_DEVICE, text)

    def test_the_migrate_script_posts_the_ask_id_yo_looks_for(self):
        text = self.text("nix", "luks", "yolobox-luks-migrate.sh")
        self.assertIn("--id=%s" % yo.MIGRATE_ASK_ID, text)

    def test_the_unlock_script_prints_the_line_yo_reads_the_ask_id_from(self):
        text = self.text("nix", "luks", "yolobox-unlock.sh")
        self.assertIn("answered id=", text)

    def test_the_flake_imports_the_luks_module(self):
        self.assertIn("./nix/luks.nix", self.text("flake.nix"))


class TestAgentEnvMatchesYoSockets(unittest.TestCase):
    def assigned(self, name):
        text = (REPO / "nix" / "lib" / "agent-env.nix").read_text()
        found = re.findall(r'^\s*%s = "([^"]*)";' % re.escape(name), text, re.M)
        self.assertEqual(len(found), 1, "%s must be assigned exactly once" % name)
        return found[0]

    def test_agent_side_aws_broker_socket_is_the_awsBrokerSock_assignment(self):
        self.assertEqual(self.assigned("awsBrokerSock"), yo.AGENT_AWS_BROKER_SOCK)

    def test_ssh_auth_sock_is_the_op_proxy_socket(self):
        self.assertEqual(self.assigned("SSH_AUTH_SOCK"), "/run/yolobox/op-agent.sock")

    def test_guest_aws_config_path_matches_agent_env_nix(self):
        suffix = yo.GUEST_AWS_CONFIG_PATH[len(yo.AGENT_HOME) :]
        self.assertEqual(yo.AGENT_HOME, "/home/agent")
        self.assertEqual(self.assigned("AWS_CONFIG_FILE"), "${agentHome}%s" % suffix)


class TestBaseNixMatchesYoConstants(unittest.TestCase):
    def base_nix_text(self):
        return (REPO / "nix" / "base.nix").read_text()

    def test_op_and_aws_broker_guest_sockets_compose_from_base_nix(self):
        text = self.base_nix_text()
        op_dir = "/run/yolobox-op"
        self.assertIn(op_dir, text)
        for guest_sock in (yo.OP_GUEST_SOCK, yo.AWS_BROKER_GUEST_SOCK):
            self.assertTrue(guest_sock.startswith(op_dir + "/"))
            target = guest_sock[len(op_dir + "/") :]
            self.assertIn('"%s"' % target, text)

    def test_proxy_socket_unit_names_appear_in_base_nix(self):
        text = self.base_nix_text()
        for unit in yo.PROXY_SOCKET_UNITS:
            self.assertTrue(unit.endswith(".socket"))
            name = unit[: -len(".socket")]
            self.assertIn(name, text)


class TestOnlyBaseNixSeesTheOperator(unittest.TestCase):
    # CONSTITUTION.md: every User=, ConditionUser= and home path in the other
    # modules names agentUser. A module that takes `username` (the operator)
    # can aim one of those at the operator's account instead.
    IDENTIFIER_RE = re.compile(r"(?<![\w-])username(?![\w-])")

    def test_no_module_but_base_nix_names_the_operator_variable(self):
        for path in sorted((REPO / "nix").rglob("*.nix")):
            if path.name == "base.nix":
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                with self.subTest(path=str(path.relative_to(REPO)), line=number):
                    self.assertIsNone(self.IDENTIFIER_RE.search(line), line)


class TestGcHelpNamesEverythingItDeletes(unittest.TestCase):
    # Each thing the guest script deletes, as `guest_deletion_targets` spells
    # it, mapped to the words `yo gc --help` must use to name it. A target the
    # script gains without an entry here fails the key comparison below; a
    # help text that loses a word fails the per-word check.
    HELP_WORDS = {
        "$HOME/.npm": ["npm"],
        ".cargo/registry": ["cargo", "registry"],
        ".cargo/git": ["cargo", "git"],
        "/root/.cache/nix": ["root's", "nix", "caches"],
        "$HOME/.cache/nix": ["operator's", "nix", "caches"],
        "/var/log/journal": ["journald"],
        "/nix/store": ["nix", "garbage", "optimise"],
        "/var/lib/systemd/coredump": ["coredumps"],
        "podman": ["podman"],
        "go caches": ["Go", "module", "build", "cache"],
    }

    def script_text(self):
        return GUEST_SCRIPT.read_text()

    def script_array(self, name):
        # e.g. BUILD_DIRS=(node_modules target .next)
        match = re.search(r"^%s=\(([^)]+)\)" % name, self.script_text(), re.M)
        self.assertIsNotNone(match, f"{name} not found in shell script")
        entries = match.group(1).split()
        self.assertTrue(entries, f"{name} appears to be empty")
        return entries

    def local_array(self, name):
        pattern = r"^\s*local %s=\(([^)]*)\)" % name
        match = re.search(pattern, self.script_text(), re.M)
        self.assertIsNotNone(match, f"{name} not found in shell script")
        return [entry.strip('"') for entry in match.group(1).split()]

    def guest_deletion_targets(self):
        text = self.script_text()
        targets = set(self.local_array("user_paths"))
        targets.update(
            "." + path for path in re.findall(r'add_cache "\$HOME/\.([^"$]+)"', text)
        )
        targets.update(
            entry.replace("${HOME}", "$HOME")
            for entry in self.local_array("nix_caches")
        )
        targets.update(
            entry for entry in self.local_array("machine_paths") if "[@]" not in entry
        )
        self.assertIn("podman system prune", text)
        targets.add("podman")
        self.assertIn("pkg/mod", text)
        targets.add("go caches")
        return targets

    def gc_help(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
            yo.build_parser().parse_args(["gc", "--help"])
        self.assertEqual(cm.exception.code, 0)
        return " ".join(buf.getvalue().split())

    def parenthetical(self, help_text, anchor):
        match = re.search(re.escape(anchor) + r" \(([^)]*)\)", help_text)
        self.assertIsNotNone(match, f"`yo gc --help` has no '{anchor} (...)' list")
        return [name.strip() for name in match.group(1).split(",")]

    def test_help_lists_exactly_the_local_caches(self):
        listed = self.parenthetical(self.gc_help(), "the tool caches under ~/.cache")
        self.assertCountEqual(listed, self.script_array("LOCAL_CACHES"))

    def test_help_names_every_directory_gc_deep_removes_or_cleans(self):
        help_text = self.gc_help()
        names = self.script_array("REMOVED_OUTRIGHT") + [
            entry.split(":")[0] for entry in self.script_array("NEEDS_MANIFEST")
        ]
        for name in names:
            self.assertTrue(
                word_in(name, help_text),
                f"`yo gc --help` does not name the directory '{name}'",
            )

    def test_help_names_the_manifest_each_directory_needs(self):
        help_text = self.gc_help()
        for entry in self.script_array("NEEDS_MANIFEST"):
            manifest = entry.split(":")[1]
            self.assertTrue(
                word_in(manifest, help_text),
                f"`yo gc --help` does not name the manifest '{manifest}'",
            )

    def clean_commands_the_script_runs(self):
        # `in_project_env "${root}" make clean`: the tool and `clean` straight
        # after the root. `make -n clean`, the dry run, has `-n` in between
        # and is not a clean.
        found = re.findall(r'in_project_env "[^"]*" (\w+) clean\b', self.script_text())
        self.assertTrue(found, "the script runs no clean command; this test is stale")
        return sorted({f"{tool} clean" for tool in found})

    def test_help_names_each_clean_command_the_script_runs(self):
        help_text = self.gc_help()
        for command in self.clean_commands_the_script_runs():
            self.assertIn(
                command, help_text, f"`yo gc --help` does not name '{command}'"
            )

    def test_every_deletion_target_of_the_script_has_a_help_mapping(self):
        self.assertEqual(self.guest_deletion_targets(), set(self.HELP_WORDS))

    def test_help_names_every_deletion_target(self):
        help_text = self.gc_help()
        for target, words in self.HELP_WORDS.items():
            for word in words:
                self.assertTrue(
                    word_in(word, help_text),
                    f"`yo gc --help` does not name '{word}' (for {target})",
                )

    def test_word_matching_does_not_accept_a_longer_hyphenated_name(self):
        self.assertFalse(word_in("pip", "uv, pip-tools, pnpm"))
        self.assertTrue(word_in("pip", "uv, pip, pip-tools"))


class TestSiblingFilesAreInstalled(unittest.TestCase):
    def siblings_read_by_yo(self):
        text = (REPO / "yo").read_text()
        found = set()
        for match in re.finditer(r'SCRIPT_DIR((?:\s*/\s*"[^"]+")+)', text):
            found.add("/".join(re.findall(r'"([^"]+)"', match.group(1))))
        self.assertTrue(found, "yo reads no sibling file; this test is stale")
        return found

    def brew_installed(self):
        text = (REPO / "homebrew" / "yolobox.rb").read_text()
        installed = set()
        for line in re.findall(r"^\s*libexec\.install ([^\n]+)", text, re.M):
            installed.update(
                os.path.basename(name) for name in re.findall(r'"([^"]+)"', line)
            )
        for directory, name in re.findall(
            r'\(libexec/"([^"]+)"\)\.install "([^"]+)"', text
        ):
            installed.add(directory + "/" + os.path.basename(name))
        return installed

    def nix_installed(self):
        text = (REPO / "nix" / "pkgs" / "yolobox.nix").read_text()
        return set(
            re.findall(
                r'^\s*install -D\w+ (\S+) "\$out/libexec/yolobox/\1"', text, re.M
            )
        )

    def test_the_brew_formula_installs_every_sibling_file_yo_reads(self):
        self.assertLessEqual(self.siblings_read_by_yo(), self.brew_installed())

    def test_the_nix_package_installs_every_sibling_file_yo_reads(self):
        self.assertLessEqual(self.siblings_read_by_yo(), self.nix_installed())


class TestGuestSubcommandsMatchTheCaseTable(unittest.TestCase):
    def invoked_by_yo(self):
        text = (REPO / "yo").read_text()
        subs = set(
            re.findall(
                r'(?:guest_helper|agent_paths)\(\s*(?:[A-Z]+,\s*)?"([a-z-]+)"', text
            )
        )
        # guest_aws_config_text writes `<helper> aws-creds <profile>` into the
        # guest's AWS config rather than calling guest_helper.
        subs.update(re.findall(r"credential_process = %s ([a-z-]+) %s", text))
        self.assertTrue(subs)
        return subs

    def case_labels(self):
        text = GUEST_SCRIPT.read_text()
        main = text[text.index("\nmain() {") :]
        table = main[main.index('case "${sub}" in') : main.index("\n    *)")]
        return set(re.findall(r"^    ([a-z-]+)\)", table, re.M))

    def test_every_subcommand_yo_invokes_exists_in_the_guest_script(self):
        self.assertEqual(self.invoked_by_yo() - self.case_labels(), set())

    def test_every_guest_script_subcommand_is_invoked_by_yo(self):
        self.assertEqual(self.case_labels() - self.invoked_by_yo(), set())


class TestGuestProfilePatternMatchesYo(unittest.TestCase):
    def test_aws_creds_accepts_exactly_the_profile_names_yo_allow_lists(self):
        found = re.findall(
            r'\[\[ "\$\{profile\}" =~ (\S+) \]\]', GUEST_SCRIPT.read_text()
        )
        self.assertEqual(len(found), 1, "aws-creds must check the profile name once")
        self.assertEqual(found[0], yo.AWS_PROFILE_NAME_RE.pattern)
