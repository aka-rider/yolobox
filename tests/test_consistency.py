"""Tests that bind one file of the repo to another.

Each class reads a second file off disk (a nix module, the guest script, the
brew formula) and compares it to what `yo` declares, so a change
to one side alone fails here instead of on a user's box.

Tools whose absence skips tests: git and a checkout of the repo (the
shellcheck-list and Nix-pattern classes), GNU stat and install (the host-setup
rerun class).
"""

import ast
import base64
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import egress_rules
from test_yo import yo

REPO = yo.REPO
YOLOBOX_YAML = REPO / "lima" / "yolobox.yaml"
GUEST_SCRIPT = REPO / "nix" / "guest" / "yolobox-guest.sh"


def package_texts():
    """The source of every module of the yolobox_cli package, by file name."""
    return {path.name: path.read_text() for path in sorted(yo.PKG_DIR.glob("*.py"))}


def package_text():
    return "\n".join(package_texts().values())


def yaml_scalar(value):
    value = value.strip().strip('"')
    if value == "true":
        return True
    if value == "false":
        return False
    if value.isdigit():
        return int(value)
    if re.fullmatch(r"\[\s*\d+\s*(,\s*\d+\s*)*\]", value):
        return [int(number) for number in re.findall(r"\d+", value)]
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
    # In order: lima takes the first matching rule, so the order is the setting.
    return [json.dumps(entry, sort_keys=True) for entry in entries]


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

    def test_no_guest_udp_listener_is_mirrored_onto_the_mac(self):
        # tailscaled's UDP 41641 would otherwise fight a host's own Tailscale
        # for the same loopback port, on every hostagent start. Lima's default
        # guestIP (127.0.0.1) leaves listeners bound to 0.0.0.0 and :: mirrored,
        # and 0.0.0.0 alone leaves loopback ones: both lines are needed.
        parsed = parse_yaml_list_entries(
            yaml_block(YOLOBOX_YAML.read_text(), "portForwards")
        )
        self.assertIn(
            {
                "proto": "udp",
                "guestIP": "0.0.0.0",
                "guestIPMustBeZero": False,
                "ignore": True,
            },
            parsed,
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


class TestYamlScalar(unittest.TestCase):
    def test_a_flow_list_of_integers_is_a_list_of_ints(self):
        self.assertEqual(yaml_scalar("[1024, 49151]"), [1024, 49151])
        self.assertEqual(yaml_scalar("[ 1 ]"), [1])

    def test_anything_else_in_brackets_stays_a_string(self):
        for value in ("[]", "lizard", "[1, ]", "[a, 1]", "[1.5]"):
            with self.subTest(value=value):
                self.assertEqual(yaml_scalar(value), value)


class TestHostLoopbackMirroring(unittest.TestCase):
    def rules(self):
        return parse_yaml_list_entries(
            yaml_block(YOLOBOX_YAML.read_text(), "portForwards")
        )

    def index_of(self, rules, **fields):
        return [
            i
            for i, rule in enumerate(rules)
            if all(rule.get(key) == value for key, value in fields.items())
        ]

    def test_the_range_rule_comes_before_both_catch_alls(self):
        rules = self.rules()
        (range_rule,) = self.index_of(
            rules, proto="tcp", guestPortRange=yo.MIRRORED_TCP_PORTS
        )
        (tcp_catch_all,) = self.index_of(
            rules, proto="tcp", ignore=True, guestIP="0.0.0.0", guestPortRange=None
        )
        (udp_catch_all,) = self.index_of(rules, proto="udp", ignore=True)
        self.assertLess(range_rule, tcp_catch_all)
        self.assertLess(range_rule, udp_catch_all)

    def test_every_deny_list_rule_comes_before_the_range_rule(self):
        rules = self.rules()
        (range_rule,) = self.index_of(
            rules, proto="tcp", guestPortRange=yo.MIRRORED_TCP_PORTS
        )
        for port_range in yo.OAUTH_CALLBACK_PORT_RANGES:
            with self.subTest(port_range=port_range):
                (denied,) = self.index_of(
                    rules, proto="tcp", ignore=True, guestPortRange=port_range
                )
                self.assertLess(denied, range_rule)

    def test_a_listener_in_the_macos_ephemeral_range_is_never_mirrored(self):
        # Login tools that take an ephemeral port (Claude Code, whose old fixed
        # port was 54545) must not need a deny-list entry: lima's first matching
        # TCP rule for such a port is an ignore, so a guest listener there never
        # reaches the host's 127.0.0.1.
        for port in (49152, 54545, 65535):
            with self.subTest(port=port):
                self.assertTrue(self.first_tcp_rule_for(port).get("ignore"))

    def first_tcp_rule_for(self, port):
        for rule in self.rules():
            if "guestSocket" in rule or rule.get("proto") != "tcp":
                continue
            first, last = rule.get("guestPortRange", [1, 65535])
            if first <= port <= last:
                return rule
        self.fail("no tcp rule matches port %d" % port)

    def test_no_forward_rule_publishes_beyond_the_hosts_loopback(self):
        for rule in self.rules():
            with self.subTest(rule=rule):
                self.assertEqual(rule.get("hostIP", "127.0.0.1"), "127.0.0.1")


def luks_facts():
    """nix/luks/facts.nix as a dict: `name = "text";` and `name = 123;` lines."""
    text = (REPO / "nix" / "luks" / "facts.nix").read_text()
    found = re.findall(r'^\s*(\w+) = "?([^";]+)"?;', text, re.M)
    return {name: yaml_scalar(value) for name, value in found}


class TestLuksFilesMatchYoConstants(unittest.TestCase):
    # The label is the one fact yo does not know: the script writes it and
    # reads it back, and nothing on the Mac does.
    LABEL = "yolobox-luks"

    def text(self, *parts):
        return REPO.joinpath(*parts).read_text()

    def test_facts_nix_holds_exactly_the_literals_yo_and_lima_state(self):
        facts = luks_facts()
        self.assertEqual(
            facts,
            {
                "device": yo.LUKS_DEVICE,
                "label": self.LABEL,
                "mapper": yo.LUKS_MAPPER,
            },
        )

    def test_luks_nix_takes_device_label_and_mapper_from_facts_nix(self):
        text = self.text("nix", "luks.nix")
        self.assertIn("inherit (import ./luks/facts.nix) device label mapper;", text)
        self.assertIn("boot.initrd.luks.devices.${mapper} = ", text)

    def test_the_root_filesystem_is_the_mapper_facts_nix_names(self):
        self.assertIn(
            'device = "/dev/mapper/%s";' % luks_facts()["mapper"],
            self.text("nix", "base.nix"),
        )

    def test_the_headroom_yo_warns_about_is_the_one_the_migration_needs(self):
        script = self.text("nix", "luks", "yolobox-luks-migrate.sh")
        (percent,) = re.findall(r"^headroom_percent=(\d+)$", script, re.M)
        (header_mib,) = re.findall(r"^header_room_mib=(\d+)$", script, re.M)
        self.assertEqual(yo.IN_PLACE_HEADROOM_PERCENT, int(percent))
        # need = region + header_room, and the region already holds one.
        self.assertEqual(yo.IN_PLACE_HEADER_ROOM_MIB, 2 * int(header_mib))

    def test_the_label_luks_nix_hands_the_migrate_script_is_the_documented_one(self):
        luks = self.text("nix", "luks.nix")
        (args,) = re.findall(r"exec \$\{lib\.getExe apps\.migrate\} (.*?)\";", luks)
        self.assertEqual(args, "${device} ${label} ${mapper}")
        self.assertEqual(luks_facts()["label"], self.LABEL)
        self.assertIn("`%s`" % self.LABEL, self.text("docs", "disk-encryption.md"))
        script = self.text("nix", "luks", "yolobox-luks-migrate.sh")
        for position, name in enumerate(("device", "label", "mapper"), start=1):
            self.assertIn("\n%s=$%d\n" % (name, position), script)

    def test_apps_nix_names_the_three_scripts_and_takes_its_facts_from_facts_nix(self):
        text = self.text("nix", "luks", "apps.nix")
        for name in (
            yo.INITRD_UNLOCK_HELPER,
            "yolobox-luks-migrate",
            "yolobox-initrd-authorized-keys",
        ):
            self.assertIn(name, text)
        self.assertIn("facts = import ./facts.nix;", text)
        self.assertIn("LUKS_DEVICE = facts.device;", text)
        self.assertIn(
            'YOLOBOX_UNLOCK_UNTIL_UNIT = "systemd-cryptsetup@${facts.mapper}.service";',
            text,
        )
        # The port differs per instance, so no initrd script may bake one in.
        self.assertNotIn("SSH_LOCAL_PORT", text)
        self.assertNotIn("sshLocalPort", text)

    def test_the_unlock_header_table_and_yo_agree_on_the_exit_codes(self):
        script = self.text("nix", "luks", "yolobox-unlock.sh")
        table = {int(n) for n in re.findall(r"^#   exit (\d+)  \S", script, re.M)}
        used = {
            int(n) for n in re.findall(r"(?:^\s*|; )exit (\d+)(?:'|$)", script, re.M)
        }
        yo_codes = {
            0,
            yo.HELPER_EXIT_WRONG_KEY,
            yo.HELPER_EXIT_MIGRATION_FAILED,
            yo.HELPER_EXIT_WATCH_LOST,
            yo.HELPER_EXIT_NO_PASSPHRASE,
            yo.HELPER_EXIT_ROOT_MOUNT_FAILED,
            yo.HELPER_EXIT_INTERNAL,
        }
        self.assertEqual(table, yo_codes)
        self.assertEqual(used, table)

    def test_the_migrate_script_posts_the_ask_id_yo_looks_for(self):
        text = self.text("nix", "luks", "yolobox-luks-migrate.sh")
        self.assertIn("--id=%s" % yo.MIGRATE_ASK_ID, text)

    def test_the_unlock_script_prints_the_line_yo_reads_the_ask_id_from(self):
        text = self.text("nix", "luks", "yolobox-unlock.sh")
        self.assertIn("answered id=", text)

    def test_the_flake_imports_the_luks_module(self):
        self.assertIn("./nix/luks.nix", self.text("flake.nix"))


class TestLuksStorePathsCoverTheScripts(unittest.TestCase):
    # The initrd builder follows ELF dependencies only, so a tool a script runs
    # that is not listed by hand is missing in the initrd, and the box sits
    # there waiting.
    def test_every_runtime_input_has_its_bin_directory_in_the_initrd(self):
        apps = (REPO / "nix" / "luks" / "apps.nix").read_text()
        luks = (REPO / "nix" / "luks.nix").read_text()
        inputs = set()
        for block in re.findall(r"runtimeInputs = \[(.*?)\];", apps, re.S):
            inputs.update(re.findall(r"pkgs\.([\w-]+)", block))
        self.assertTrue(inputs, "found no runtimeInputs in nix/luks/apps.nix")
        for name in sorted(inputs):
            with self.subTest(package=name):
                self.assertIn('"${pkgs.%s}/bin"' % name, luks)


class TestInitrdSshdConfigOrder(unittest.TestCase):
    # A global keyword after a Match line belongs to the block; sshd refuses
    # it there and the initrd, with no rollback, never gets its sshd.
    def test_per_source_penalties_precede_the_match_block(self):
        text = (REPO / "nix" / "luks.nix").read_text()
        found = re.search(r"extraConfig = \'\'(.*?)\'\';", text, re.S)
        self.assertIsNotNone(found, "no extraConfig in nix/luks.nix")
        config = found.group(1)
        self.assertIn("PerSourcePenalties no", config)
        self.assertIn("Match User root", config)
        self.assertLess(
            config.index("PerSourcePenalties no"), config.index("Match User root")
        )


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
            if path == REPO / "nix" / "base.nix":
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
        with contextlib.redirect_stdout(buf):
            self.assertEqual(yo.main(["help", "gc"]), 0)
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

    def test_yo_cp_drops_exactly_the_names_gc_knows(self):
        gc_names = set(self.script_array("BUILD_DIRS")) | set(
            self.script_array("REMOVED_OUTRIGHT")
        )
        self.assertEqual(set(yo.BUILD_OUTPUT), gc_names)

    def test_yo_cp_requires_the_same_manifests_gc_requires(self):
        gc_manifests = dict(
            entry.split(":") for entry in self.script_array("NEEDS_MANIFEST")
        )
        yo_manifests = {
            name: manifest for name, manifest in yo.BUILD_OUTPUT.items() if manifest
        }
        self.assertEqual(yo_manifests, gc_manifests)

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


PLATFORMS = ("darwin", "linux")
# Files a host ships only when it runs the driver that reads them; everything
# else is shipped to both.
PLATFORM_ONLY = {
    "yolobox_cli/lima.py": "darwin",
    "lima/": "darwin",
    "yolobox_cli/libvirt.py": "linux",
    "yolobox_cli/libvirt_host.py": "linux",
    "libvirt/": "linux",
    "linux/": "linux",
}


def platforms_of(path):
    for prefix, platform in PLATFORM_ONLY.items():
        if path == prefix or (prefix.endswith("/") and path.startswith(prefix)):
            return {platform}
    return set(PLATFORMS)


def package_files():
    return {"yolobox_cli/" + name for name in package_texts()}


class TestEachPlatformShipsItsOwnFiles(unittest.TestCase):
    """macOS runs Lima and Linux runs libvirt, and each ships only the files its
    driver reads: the other driver's module, config and host files stay out."""

    def siblings_read_by_yo(self):
        found = set()
        for match in re.finditer(r'SCRIPT_DIR((?:\s*/\s*"[^"]+")+)', package_text()):
            found.add("/".join(re.findall(r'"([^"]+)"', match.group(1))))
        self.assertTrue(found, "yo reads no sibling file; this test is stale")
        return found

    def brew_shipped(self):
        text = (REPO / "homebrew" / "yolobox.rb").read_text()
        shipped = set()
        for line in re.findall(r"^\s*libexec\.install ([^\n]+)", text, re.M):
            shipped.update(re.findall(r'"([^"]+)"', line))
        for directory, name in re.findall(
            r'\(libexec/"([^"]+)"\)\.install "([^"]+)"', text
        ):
            shipped.add(directory + "/" + os.path.basename(name))
        for directory, pattern, excluded in re.findall(
            r'\(libexec/"([^"]+)"\)\.install Dir\["([^"]+)"\](?: - \[([^\]]*)\])?',
            text,
        ):
            matched = {str(m.relative_to(REPO)) for m in REPO.glob(pattern)}
            matched -= set(re.findall(r'"([^"]+)"', excluded))
            shipped.update(directory + "/" + os.path.basename(m) for m in matched)
        return shipped

    def nix_text(self):
        return (REPO / "nix" / "pkgs" / "yolobox.nix").read_text()

    def nix_shipped(self, platform):
        text = self.nix_text()
        branch = {"darwin": "darwinFiles", "linux": "linuxFiles"}[platform]
        branches = {
            name: body
            for name, body in re.findall(r"(\w+Files) = \'\'(.*?)\'\';", text, re.S)
        }
        self.assertEqual(set(branches), {"darwinFiles", "linuxFiles"})
        self.assertIn(
            "if stdenvNoCC.hostPlatform.isDarwin then darwinFiles else linuxFiles", text
        )
        script = text.split("stdenvNoCC.mkDerivation", 1)[1]
        # The branch is spliced in after the copy of the package, so removals in
        # it apply to the copied files.
        script = script.replace(
            "${if stdenvNoCC.hostPlatform.isDarwin then darwinFiles else linuxFiles}",
            branches[branch],
        )
        shipped = set()
        for line in script.splitlines():
            line = line.strip()
            if match := re.fullmatch(
                r'install -D\w+ (\S+) "\$out/libexec/yolobox/(\S+)"', line
            ):
                shipped.add(match.group(2))
            elif line == 'cp -r yolobox_cli "$out/libexec/yolobox/yolobox_cli"':
                shipped |= package_files()
            elif match := re.fullmatch(r'rm "\$out/libexec/yolobox/(\S+)"', line):
                shipped.discard(match.group(1))
        return shipped

    def siblings_read_by_host_setup(self):
        """host-setup.sh installs these from beside itself, as `$HERE/NAME`."""
        found = {
            "linux/" + name
            for name in re.findall(
                r"\$HERE/([\w.-]+)", (REPO / "linux" / "host-setup.sh").read_text()
            )
        }
        self.assertTrue(found, "host-setup.sh reads no sibling; this test is stale")
        return found

    def needed(self):
        return (
            {"yo", "aws-broker"}
            | self.siblings_read_by_yo()
            | self.siblings_read_by_host_setup()
            | package_files()
        )

    def test_the_brew_formula_is_for_macos_only(self):
        self.assertIn(
            "depends_on :macos", (REPO / "homebrew" / "yolobox.rb").read_text()
        )

    def test_every_platform_ships_every_file_its_driver_reads(self):
        for platform in PLATFORMS:
            shipped = (
                self.brew_shipped()
                if platform == "darwin"
                else self.nix_shipped(platform)
            )
            for path in sorted(self.needed()):
                if platform in platforms_of(path):
                    with self.subTest(platform=platform, path=path):
                        self.assertIn(path, shipped)

    def test_the_nix_package_ships_the_same_for_darwin_as_brew_does(self):
        self.assertEqual(self.nix_shipped("darwin"), self.brew_shipped())

    def test_no_platform_ships_a_file_of_the_other_one(self):
        shipped = {
            "darwin": self.brew_shipped(),
            "linux": self.nix_shipped("linux"),
        }
        for platform, paths in shipped.items():
            for path in sorted(paths):
                with self.subTest(platform=platform, path=path):
                    self.assertIn(platform, platforms_of(path))


def names_tests_patch():
    """Every name a test patches or rebinds on the package, plus the ones the
    patch convention names outright."""
    names = {
        "YO_VERSION",
        "HOST_PLATFORM",
        "AGENT_REPLY_TIMEOUT",
        "PORT_RANGE_FILE",
        "LIBVIRT_STOP_TIMEOUT",
    }
    for path in sorted(Path(__file__).resolve().parent.glob("test_*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "object"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "patch"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
            ):
                names.add(node.args[1].value)
    return names


class TestPackageImportsFollowTheConvention(unittest.TestCase):
    # Modules call each other as `module.attr`, so a test patches the one global
    # the code reads. A name imported by name is a second copy a patch misses.
    def test_no_module_spells_the_package_name(self):
        for name, text in package_texts().items():
            with self.subTest(module=name):
                self.assertNotIn("yolobox_cli", text)

    def test_names_imported_from_a_sibling_are_classes_or_unpatched_constants(self):
        patched = names_tests_patch()
        imported = []
        for name, text in package_texts().items():
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                    imported += [(name, alias.name) for alias in node.names]
        self.assertTrue(imported, "no module imports a name; this test is stale")
        for module, imported_name in imported:
            with self.subTest(module=module, name=imported_name):
                self.assertIsNotNone(
                    re.fullmatch(r"[A-Z][A-Za-z0-9]*|[A-Z][A-Z0-9_]*", imported_name),
                    "only classes and constants are imported by name",
                )
                self.assertNotIn(imported_name, patched)


class TestDriversAreImportedOnlyByCli(unittest.TestCase):
    """A host ships only its own driver module, so nothing but `driver_for`
    may name the other one, and the rest of yo reaches a driver through
    `state.driver()`. `libvirt_host` is the libvirt driver's second file, and
    only `libvirt` imports it."""

    def drivers_imported_by(self, trees):
        imported = set()
        for tree in trees:
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level == 1:
                    names = (
                        [node.module] if node.module else [a.name for a in node.names]
                    )
                    imported |= {
                        name
                        for name in names
                        if name in ("lima", "libvirt", "libvirt_host")
                    }
        return imported

    def test_no_module_but_cli_imports_a_driver_module(self):
        for name, text in package_texts().items():
            if name != "cli.py":
                with self.subTest(module=name):
                    allowed = {"libvirt_host"} if name == "libvirt.py" else set()
                    self.assertEqual(
                        self.drivers_imported_by([ast.parse(text)]), allowed
                    )

    def test_cli_imports_a_driver_module_only_inside_a_function(self):
        top_level = ast.parse(package_texts()["cli.py"]).body
        self.assertEqual(
            self.drivers_imported_by(
                n for n in top_level if not isinstance(n, ast.FunctionDef)
            ),
            set(),
        )

    def test_each_driver_module_imports_without_the_other(self):
        for kept, removed in (
            ("lima", ("libvirt.py", "libvirt_host.py")),
            ("libvirt", ("lima.py",)),
        ):
            with self.subTest(kept=kept):
                with tempfile.TemporaryDirectory() as tmp:
                    shutil.copytree(
                        yo.PKG_DIR,
                        Path(tmp) / "yolobox_cli",
                        ignore=shutil.ignore_patterns(*removed, "__pycache__"),
                    )
                    done = subprocess.run(
                        [
                            sys.executable,
                            "-B",
                            "-I",
                            "-c",
                            "import sys; sys.path.insert(0, sys.argv[1]);"
                            "from yolobox_cli import cli, %s" % kept,
                            tmp,
                        ],
                        capture_output=True,
                        text=True,
                    )
                self.assertEqual(done.returncode, 0, done.stderr)


class TestPackagersPatchTheVersionLiteral(unittest.TestCase):
    def brew(self):
        return (REPO / "homebrew" / "yolobox.rb").read_text()

    def nix(self):
        return (REPO / "nix" / "pkgs" / "yolobox.nix").read_text()

    def version_literal_files(self):
        return {
            "yolobox_cli/" + name
            for name, text in package_texts().items()
            if 'YO_VERSION = "dev"' in text.splitlines()
        }

    def test_both_packagers_patch_the_file_that_holds_the_version_literal(self):
        (brew_path,) = re.findall(r'inreplace libexec/"([^"]+)"', self.brew())
        (nix_path,) = re.findall(
            r'substituteInPlace "\$out/libexec/yolobox/([^"]+)"', self.nix()
        )
        self.assertEqual({brew_path}, self.version_literal_files())
        self.assertEqual({nix_path}, self.version_literal_files())


class TestGuestSubcommandsMatchTheCaseTable(unittest.TestCase):
    def invoked_by_yo(self):
        text = package_text()
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

    # Subcommands a person runs inside the box, never `yo`: `rebuild` reads the
    # box's own stamps, and `yo bootstrap` passes the host's facts instead.
    RUN_INSIDE_THE_BOX = {"rebuild"}

    def test_every_guest_script_subcommand_is_invoked_by_yo(self):
        self.assertEqual(
            self.case_labels() - self.invoked_by_yo() - self.RUN_INSIDE_THE_BOX, set()
        )

    def test_a_subcommand_run_inside_the_box_is_in_the_case_table(self):
        self.assertEqual(self.RUN_INSIDE_THE_BOX - self.case_labels(), set())


def guest_script_function(name: str) -> str:
    text = GUEST_SCRIPT.read_text()
    start = text.index("\n%s() {\n" % name)
    return text[start : text.index("\n}\n", start)]


def guest_script_constant(name: str) -> str:
    found = re.search(r"^%s=(.*)$" % re.escape(name), GUEST_SCRIPT.read_text(), re.M)
    assert found, "the guest script has no constant %s" % name
    return found.group(1).strip("'\"")


class TestClaudeLauncherWatch(unittest.TestCase):
    """harnesses.nix watches the launcher with PathChanged. PathModified fires
    on every write, and one claude download is thousands of writes, past the
    path unit's trigger limit (nix/harnesses.md)."""

    def test_the_path_unit_watches_with_path_changed_not_path_modified(self):
        text = (REPO / "nix" / "harnesses.nix").read_text()
        unit = re.search(
            r"systemd\.user\.paths\.yolobox-claude-launcher = \{(.*?)\n    \};",
            text,
            re.S,
        )
        self.assertIsNotNone(unit, "harnesses.nix declares no launcher path unit")
        self.assertIn("pathConfig.PathChanged", unit.group(1))
        self.assertNotIn("PathModified", unit.group(1))


class TestGuestRebuildMatchesYoAndTheBox(unittest.TestCase):
    """`yolobox-guest rebuild` repeats facts yo and base.nix own: the flake ref
    shape, the nix variables and the operator's uid."""

    def test_the_flake_ref_shape_is_yos(self):
        self.assertEqual(
            guest_script_constant("FLAKE_REF_RE"), yo.GITHUB_FLAKE_RE.pattern
        )

    def test_the_hostname_shape_is_yos(self):
        self.assertEqual(
            guest_script_constant("HOSTNAME_RE"), yo.HOSTNAME_LABEL_RE.pattern
        )

    def test_the_rebuild_sets_exactly_the_variables_yo_sets(self):
        names = set(
            re.findall(r'"(YOLOBOX_[A-Z_]+)=', guest_script_function("cmd_rebuild"))
        )
        yo_names = {
            value
            for name, value in vars(yo.release).items()
            if name.startswith("NIX_ENV_")
        }
        self.assertTrue(yo_names)
        self.assertEqual(names, yo_names)

    def test_the_operator_uid_is_yos_and_base_nixs(self):
        uid = int(guest_script_constant("OPERATOR_UID"))
        self.assertEqual(uid, yo.config.OPERATOR_GUEST_UID)
        block = re.search(
            r"users\.users\.\$\{username\} = \{(.*?)\n  \};",
            (REPO / "nix" / "base.nix").read_text(),
            re.S,
        )
        self.assertIsNotNone(block, "base.nix declares no operator account")
        self.assertEqual("uid = %d;" % uid, re.search(r"uid = \d+;", block.group(1))[0])

    def test_the_rebuild_passes_exactly_yos_nixos_rebuild_flags(self):
        found = re.search(
            r'nixos-rebuild "\$\{action\}" (.*?) --flake',
            guest_script_function("cmd_rebuild"),
        )
        self.assertIsNotNone(found, "cmd_rebuild runs no nixos-rebuild")
        self.assertEqual(tuple(found.group(1).split()), yo.release.NIX_REBUILD_FLAGS)
        self.assertIn("--refresh", yo.release.NIX_REBUILD_FLAGS)

    def test_the_rebuild_lock_and_its_busy_exit_are_yos(self):
        # Both sides take one lock, or an interrupted bootstrap's nixos-rebuild
        # runs beside a hand rebuild. The path has a test override in the script.
        lock = re.fullmatch(
            r"\$\{YOLOBOX_REBUILD_LOCK:-(.*)\}", guest_script_constant("REBUILD_LOCK")
        )
        self.assertIsNotNone(lock, "REBUILD_LOCK lost its override spelling")
        self.assertEqual(lock.group(1), yo.release.REBUILD_LOCK)
        self.assertEqual(
            int(guest_script_constant("REBUILD_BUSY_EXIT")),
            yo.release.REBUILD_BUSY_EXIT,
        )

    def test_the_stamps_the_rebuild_reads_are_the_ones_base_nix_writes(self):
        body = guest_script_function("cmd_rebuild")
        read = set(re.findall(r'read_stamp "\$\{YOLOBOX_STAMPS\}/([a-z-]+)"', body))
        base = (REPO / "nix/base.nix").read_text()
        self.assertEqual(read, {"flake-ref", "hostname"})
        for name in read:
            self.assertIn('environment.etc."yolobox/%s".text' % name, base)


class TestStage2SshdIsToldApartWithoutALogin(unittest.TestCase):
    """yo reads the stage-2 sshd's banner word instead of logging in as root,
    and the gateway, where every host connection comes from, is exempt from
    sshd's per-source penalties (nix/base.md, "sshd penalties")."""

    def base(self):
        return nix_code(REPO / "nix" / "base.nix")

    def setting(self, name):
        found = re.search(
            r'services\.openssh\.settings\.%s = "([^"]+)";' % name, self.base()
        )
        self.assertIsNotNone(found, "base.nix does not set %s" % name)
        return found.group(1)

    def test_stage2_appends_the_word_yo_reads(self):
        self.assertEqual(
            self.setting("VersionAddendum"), yo.config.STAGE2_SSH_BANNER_MARK
        )

    def test_the_initrds_sshd_carries_no_banner_word(self):
        luks = nix_code(REPO / "nix" / "luks.nix")
        self.assertNotIn("VersionAddendum", luks)
        self.assertNotIn(yo.config.STAGE2_SSH_BANNER_MARK, luks)

    def test_the_exempt_address_is_the_one_the_operator_logs_in_from(self):
        allow = re.search(r'AllowUsers = \[ "\$\{username\}@([0-9.]+)"', self.base())
        self.assertIsNotNone(allow, "base.nix admits the operator from no address")
        self.assertEqual(self.setting("PerSourcePenaltyExemptList"), allow.group(1))


class TestMigrateUnitDescriptionIsTheUnits(unittest.TestCase):
    """yo tells a person to look for the migrate unit in the boot log, so the
    text it quotes is the unit's description with the device filled in."""

    def test_the_quoted_text_is_how_the_unit_describes_itself(self):
        found = re.search(
            r'description = "(Encrypt or finish encrypting \$\{device\} in place)',
            nix_code(REPO / "nix" / "luks.nix"),
        )
        self.assertIsNotNone(found, "luks.nix has no migrate unit description")
        self.assertEqual(
            found.group(1).replace("${device}", yo.LUKS_DEVICE),
            yo.unlock.MIGRATE_UNIT_DESCRIPTION,
        )


class TestGithubHostKeysAreThePublishedOnes(unittest.TestCase):
    """nix/base.nix pins github.com's host keys; each must have the fingerprint
    GitHub publishes, computed as ssh-keygen -l does (SHA-256 of the key blob,
    base64 without padding)."""

    PUBLISHED = {
        "ssh-ed25519": "+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU",
        "ecdsa-sha2-nistp256": "p2QAMXNIC1TJYWeIOttrVc98/R1BUFWu3/LiyKgUfQM",
        "ssh-rsa": "uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s",
    }

    def keys(self):
        found = re.findall(
            r'hostNames = \[ "github\.com" \];\s*publicKey = "(\S+) (\S+)";',
            nix_code(REPO / "nix" / "base.nix"),
        )
        self.assertTrue(found, "base.nix pins no github.com host key")
        return found

    def test_every_published_key_type_is_pinned_once(self):
        self.assertEqual(
            sorted(kind for kind, _ in self.keys()), sorted(self.PUBLISHED)
        )

    def test_each_key_has_its_published_fingerprint(self):
        for kind, blob in self.keys():
            with self.subTest(kind=kind):
                digest = hashlib.sha256(base64.b64decode(blob)).digest()
                fingerprint = base64.b64encode(digest).decode().rstrip("=")
                self.assertEqual(fingerprint, self.PUBLISHED[kind])


class TestThePasstHookNamesWhatYoNames(unittest.TestCase):
    """linux/yolobox-libvirt-hook.sh spells the passt directory, the VM account
    and the domain namespace on its own; they must be yo's and the template's."""

    def hook_value(self, name):
        found = re.search(
            r'^readonly %s="(?:\$\{[A-Z_]+:-)?([^"}]+)\}?"$' % name,
            (REPO / "linux" / "yolobox-libvirt-hook.sh").read_text(),
            re.M,
        )
        self.assertIsNotNone(found, "the hook sets no %s" % name)
        return found.group(1)

    def test_the_directory_account_and_namespace_are_yos(self):
        self.assertEqual(self.hook_value("PASST_DIR"), yo.libvirt_host.PASST_DIR)
        self.assertEqual(self.hook_value("VM_USER"), yo.libvirt_host.VM_USER)
        self.assertEqual(self.hook_value("DOMAIN_NS"), yo.libvirt_host.DOMAIN_NS)

    def test_the_template_carries_the_namespace_the_hook_looks_for(self):
        self.assertIn(
            "xmlns:yolobox='%s'" % self.hook_value("DOMAIN_NS"),
            (REPO / "libvirt" / "domain.xml").read_text(),
        )

    def test_host_setup_installs_the_hook_where_libvirt_reads_it(self):
        self.assertEqual(
            host_setup_assignment("PASST_HOOK"),
            "/etc/libvirt/hooks/qemu.d/50-yolobox-passt",
        )

    def test_the_hook_refuses_any_owner_but_the_one_host_setup_creates(self):
        self.assertEqual(
            self.hook_value("DIR_OWNER"), host_setup_assignment("PASST_OWNER")
        )


class TestTheIncludeLineServesBothDrivers(unittest.TestCase):
    def test_it_globs_exactly_each_drivers_instance_files(self):
        words = yo.config.SSH_CONFIG_INCLUDE_LINE.split()
        self.assertEqual(words[0], "Include")
        self.assertEqual(
            sorted(words[1:]),
            sorted(
                "~/%s/*/ssh.config" % driver.instances_dir
                for driver in (yo.LimaDriver, yo.LibvirtDriver)
            ),
        )


class TestGuestProfilePatternMatchesYo(unittest.TestCase):
    def test_aws_creds_accepts_exactly_the_profile_names_yo_allow_lists(self):
        found = re.findall(
            r'\[\[ "\$\{profile\}" =~ (\S+) \]\]', GUEST_SCRIPT.read_text()
        )
        self.assertEqual(len(found), 1, "aws-creds must check the profile name once")
        self.assertEqual(found[0], yo.PROFILE_NAME_RE.pattern)


def nix_code(path: Path) -> str:
    """The file's text without its comment lines."""
    return "\n".join(
        line
        for line in path.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )


class TestTailscaledLogFilter(unittest.TestCase):
    """The filter drops the relayed-path line and nothing else."""

    DERP = "magicsock: new contact: peer=[+1OFL] usec=13217671625 cached=false via=derp"
    DIRECT = DERP.replace("via=derp", "via=direct")
    NOW_USING = "magicsock: disco: node [+1OFL] d:1b4eb41609986a64 now using 203.0.113.7:41641 mtu=1360"

    def pattern(self):
        found = re.search(
            r'systemd\.services\.tailscaled\.serviceConfig\.LogFilterPatterns\s*=\s*"~([^"]+)";',
            nix_code(REPO / "nix" / "tailscale.nix"),
        )
        self.assertIsNotNone(found, "tailscaled has no negated LogFilterPatterns")
        return found.group(1)

    def test_a_relayed_contact_is_dropped(self):
        self.assertRegex(self.DERP, self.pattern())

    def test_a_direct_contact_and_path_changes_are_kept(self):
        for line in (self.DIRECT, self.NOW_USING):
            with self.subTest(line=line):
                self.assertNotRegex(line, self.pattern())


class TestContainerOutputStaysOutOfTheJournal(unittest.TestCase):
    """Compose healthchecks flooded the journal (nix/podman.md)."""

    PODMAN_NIX = REPO / "nix" / "podman.nix"
    BASE_NIX = REPO / "nix" / "base.nix"

    def test_containers_log_to_capped_files_not_to_the_journal(self):
        code = nix_code(self.PODMAN_NIX)
        self.assertIn('log_driver = "k8s-file"', code)
        self.assertRegex(code, r"log_size_max\s*=\s*\d")

    def test_podman_events_stay_out_of_the_journal(self):
        self.assertIn('events_logger = "file"', nix_code(self.PODMAN_NIX))

    def test_journald_has_a_size_cap(self):
        self.assertRegex(
            nix_code(self.BASE_NIX),
            r'services\.journald\.settings\.Journal\.SystemMaxUse\s*=\s*"\d+[KMG]"',
        )

    def test_no_module_uses_the_removed_journald_extraconfig(self):
        for path in sorted((REPO / "nix").rglob("*.nix")):
            with self.subTest(file=path.name):
                self.assertNotIn("services.journald.extraConfig", nix_code(path))


class TestHomeLinksBelongToTheirUser(unittest.TestCase):
    """tmpfiles makes an L line's link root-owned unless the line names a user."""

    def test_no_tmpfiles_link_into_a_home_leaves_its_owner_unset(self):
        unowned = re.compile(r'"L\+?\s+\S*(home|\{home\})\S*\s+-\s+-\s+-')
        for path in sorted((REPO / "nix").rglob("*.nix")):
            with self.subTest(file=path.name):
                self.assertIsNone(unowned.search(nix_code(path)))


class TestRuffVersionIsPinnedOnce(unittest.TestCase):
    def required(self):
        found = re.search(
            r'^required-version = "==([\d.]+)"$',
            (REPO / "ruff.toml").read_text(),
            re.M,
        )
        self.assertIsNotNone(found)
        return found.group(1)

    def test_claude_md_spells_the_version_ruff_toml_requires(self):
        spelled = re.findall(
            r"\bruff(?:@|==)([\d.]+)", (REPO / "CLAUDE.md").read_text()
        )
        self.assertTrue(spelled, "CLAUDE.md names no ruff version; this test is stale")
        self.assertEqual(set(spelled), {self.required()})

    def test_verify_workflow_installs_the_version_ruff_toml_requires(self):
        required = self.required()
        installed = re.findall(
            r'^\s+version: "([\d.]+)"$',
            (REPO / ".github" / "workflows" / "verify.yml").read_text(),
            re.M,
        )
        self.assertEqual(installed, [required] * 2)


class TestTailscaleSocketIsOperatorOnly(unittest.TestCase):
    """The agent is not in wheel, so the socket directory must be wheel-only."""

    def test_the_socket_directory_is_root_and_wheel_only(self):
        self.assertIn(
            'systemd.services.tailscaled.serviceConfig.RuntimeDirectoryMode = "0750";',
            nix_code(REPO / "nix" / "tailscale.nix"),
        )

    def test_the_socket_directory_is_handed_to_wheel_after_start(self):
        # systemd re-applies root:root to the runtime directory for every command
        # it spawns, so only ExecStartPost survives (a past regression).
        self.assertIn(
            'serviceConfig.ExecStartPost = [ "+${pkgs.coreutils}/bin/chgrp wheel /run/tailscale" ];',
            nix_code(REPO / "nix" / "tailscale.nix"),
        )

    def test_the_chgrp_is_not_in_exec_start_pre(self):
        self.assertNotRegex(
            nix_code(REPO / "nix" / "tailscale.nix"),
            r"ExecStartPre[^\n]*chgrp",
        )


class TestTailscaleLeavesTheUplinkAtPort22(unittest.TestCase):
    """openFirewall = true opens the tunnel's UDP port on every interface, enp0s1 included."""

    def test_the_module_does_not_ask_nixos_to_open_the_tunnel_port(self):
        self.assertRegex(
            nix_code(REPO / "nix" / "tailscale.nix"),
            r"\bopenFirewall = false;",
        )


class TestShellcheckListsAgree(unittest.TestCase):
    """CLAUDE.md tells a contributor which scripts to shellcheck; CI checks the
    ones in verify.yml. A script added to one list only is either skipped by CI
    or never run by the person changing it."""

    COMMAND = re.compile(r"shellcheck -S warning ([^#\n]+)")

    def scripts_in(self, path):
        found = self.COMMAND.findall(path.read_text())
        self.assertEqual(len(found), 1, "%s: want one shellcheck command" % path)
        return found[0].split()

    def test_claude_md_and_ci_list_the_same_scripts(self):
        claude = self.scripts_in(REPO / "CLAUDE.md")
        ci = self.scripts_in(REPO / ".github" / "workflows" / "verify.yml")
        self.assertEqual(sorted(set(claude)), sorted(set(ci)))
        self.assertEqual(len(claude), len(set(claude)), "a script listed twice")

    def test_the_lists_hold_every_shell_script_the_repo_tracks(self):
        if not shutil.which("git") or not (REPO / ".git").exists():
            self.skipTest("needs git and a checkout (CLAUDE.md copies the tree out)")
        tracked = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", "*.sh"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        self.assertEqual(
            sorted(set(self.scripts_in(REPO / "CLAUDE.md"))), sorted(tracked)
        )

    def test_every_listed_script_exists(self):
        for script in self.scripts_in(REPO / "CLAUDE.md"):
            with self.subTest(script=script):
                self.assertTrue((REPO / script).is_file())


class TestEgressTierChainNames(unittest.TestCase):
    """The chain name is how every reader tells the three tiers apart: `yo
    status` reads it, the OnFailure script prints by it, and the rule checker
    expects it. A name changed in one place leaves the others reporting the
    wrong tier, or none."""

    CHAIN_HEADER = re.compile(r"\bchain (\w+) \{")
    # postrouting and the forward chains are not tiers: yo status reads only
    # the output chain's name.
    NOT_A_TIER = {"postrouting", "forward", "forward_recovery"}

    def status_names(self):
        names = set()
        for header in yo.EGRESS_TIER_CHAINS:
            (name,) = self.CHAIN_HEADER.findall(header)
            names.add(name)
        return names

    def test_nix_defines_exactly_the_chains_status_knows(self):
        found = set(self.CHAIN_HEADER.findall(nix_code(REPO / "nix" / "egress.nix")))
        self.assertEqual(found - self.NOT_A_TIER, self.status_names())

    def test_the_rule_checker_expects_exactly_the_chains_status_knows(self):
        self.assertEqual(set(egress_rules.HOOKS) - self.NOT_A_TIER, self.status_names())

    def test_the_failure_script_names_every_fallback_chain_status_knows(self):
        # The full table's chain, plain `output`, is no fallback: it is what the
        # script's default branch means by "neither fallback chain".
        script = (REPO / "nix" / "egress" / "yolobox-egress-failed.sh").read_text()
        cases = set(re.findall(r'\*"chain (\w+) \{"\*\)', script))
        self.assertEqual(cases, self.status_names() - {"output"})


class TestEgressUnitNeverOpensTheBox(unittest.TestCase):
    """The unit that loads nix/egress.nix's rules. The rules themselves are
    checked in their rendered form (tests/test_egress_rules.py); these are the
    unit properties no rule file shows."""

    CODE = nix_code(REPO / "nix" / "egress.nix")

    def service_block(self, name):
        found = re.search(
            r"systemd\.services\.%s = \{(.*?)\n    \};" % name, self.CODE, re.S
        )
        self.assertIsNotNone(found, "egress.nix has no unit %s" % name)
        return found.group(1)

    def test_the_unit_is_the_one_yo_inspects(self):
        self.assertIn("systemd.services.%s = {" % yo.EGRESS_UNIT, self.CODE)

    def test_the_unit_has_no_stop_hook(self):
        # A stop hook would open the box whenever the unit stops or restarts.
        for hook in ("ExecStop", "preStop", "postStop"):
            with self.subTest(hook=hook):
                self.assertNotIn(hook, self.CODE)

    def test_the_unit_loads_a_fallback_first_and_the_full_rules_to_replace_it(self):
        unit = self.service_block("yolobox-egress")
        self.assertIn(
            'ExecStartPre = [ "${loadTiers}/bin/yolobox-egress-load-tiers'
            ' ${recoveryFile} ${lastResortFile}" ];',
            unit,
        )
        self.assertIn('ExecStart = "${nft} --file ${ruleFile}";', unit)

    def test_a_unit_that_fails_says_so_through_the_failure_unit(self):
        self.assertIn(
            'systemd.services.yolobox-egress.onFailure = [ "yolobox-egress-failed.service" ];',
            self.CODE,
        )
        self.assertIn(
            'ExecStart = "${reportFailure}/bin/yolobox-egress-failed";', self.CODE
        )

    def test_tailscaled_is_gated_on_the_table_and_dhcpcd_is_not(self):
        self.assertIn('"+${nft} list table ${table}"', self.service_block("tailscaled"))
        dhcpcd = self.service_block("dhcpcd")
        self.assertNotIn("ExecStartPre", dhcpcd)
        self.assertNotIn("list table", dhcpcd)
        self.assertIn('after = [ "yolobox-egress.service" ];', dhcpcd)


def backticked_dot_names(text):
    return set(re.findall(r"`(\.[a-z]+)`", text))


class TestLandingControlNamesMatchTheirTwoStories(unittest.TestCase):
    """The names `yo cp` refuses to land are spelled in three places."""

    def constitution_rule(self):
        (rule,) = [
            line
            for line in (REPO / "CONSTITUTION.md").read_text().splitlines()
            if "download land a version-control" in line
        ]
        return rule

    def yo_md_sentence(self):
        text = (REPO / "yo.md").read_text()
        (sentence,) = [
            sentence
            for sentence in re.split(r"(?<=\.) (?=[A-Z])", text)
            if "LANDING_CONTROL_NAMES" in sentence
        ]
        return sentence

    def test_the_constitution_rule_lists_exactly_the_names_yo_drops(self):
        self.assertEqual(
            backticked_dot_names(self.constitution_rule()),
            set(yo.LANDING_CONTROL_NAMES),
        )

    def test_yo_md_lists_exactly_the_names_yo_drops(self):
        self.assertEqual(
            backticked_dot_names(self.yo_md_sentence()),
            set(yo.LANDING_CONTROL_NAMES),
        )


class TestThereIsNoDirenvInTheBox(unittest.TestCase):
    """An .envrc is code the agent writes and the operator would run on `cd`.

    The patterns are specific on purpose: nix/lsp.nix says "There is no
    direnv" and says ".envrc" in comments, which are not read here, and
    `load_direnv = "disabled"` is the allowed spelling.
    """

    FORBIDDEN = (
        "programs.direnv",
        "DIRENV_CONFIG",
        "direnv exec",
        'load_direnv = "direct"',
        "envrc",
    )

    def box_files(self):
        files = sorted((REPO / "nix").rglob("*.nix"))
        files.append(REPO / "flake.nix")
        self.assertTrue(files)
        return files

    def test_no_nix_module_loads_or_mentions_direnv_machinery(self):
        for path in self.box_files():
            text = nix_code(path) if path.suffix == ".nix" else path.read_text()
            for pattern in self.FORBIDDEN:
                with self.subTest(file=str(path.relative_to(REPO)), pattern=pattern):
                    self.assertNotIn(pattern, text)


class TestSudoIsReachableByWheelOnly(unittest.TestCase):
    def test_base_nix_sets_exec_wheel_only(self):
        self.assertIn(
            "security.sudo.execWheelOnly = true;",
            nix_code(REPO / "nix" / "base.nix"),
        )


HARDENING_NIX = REPO / "nix" / "hardening.nix"
HARDENING_MD = REPO / "nix" / "hardening.md"
HARDENING_CHECK = REPO / "tests" / "box-hardening-check.sh"


def quoted_names(text):
    return re.findall(r'"([^"]+)"', text)


def hardening_nix_list(name):
    """The strings of `name = [ "a" "b" ];` in nix/hardening.nix."""
    (body,) = re.findall(
        r"\b%s = \[(.*?)\];" % re.escape(name), nix_code(HARDENING_NIX), re.S
    )
    return quoted_names(body)


def hardening_nix_sysctls():
    """Every key and value of boot.kernel.sysctl in nix/hardening.nix.

    Each entry of the block must be one `"key" = <integer>;` line. A line of
    any other shape (a generator, an attribute set, mkForce) fails the count, so
    it cannot hide from the check script comparison. The one other spelling is
    a statement outside the block, `boot.kernel.sysctl."key" = lib.mkForce
    <integer>;`, for a value nixos-lima also sets.
    """
    code = nix_code(HARDENING_NIX)
    (block,) = re.findall(r"boot\.kernel\.sysctl = \{\n(.*?)\n\s*\};", code, re.S)
    entries = re.findall(r'^\s*"[^"]+"\s*=', block, re.M)
    parsed = re.findall(r'^\s*"([\w.]+)" = (\d+);', block, re.M)
    assert len(parsed) == len(entries), "an unparseable sysctl line in hardening.nix"
    forced_entries = re.findall(r'boot\.kernel\.sysctl\."[^"]+"\s*=', code)
    forced = re.findall(r'boot\.kernel\.sysctl\."([\w.]+)" = lib\.mkForce (\d+);', code)
    assert len(forced) == len(forced_entries), "an unparseable forced sysctl line"
    return dict(parsed + forced)


class TestNixPatternsAreEre(unittest.TestCase):
    """builtins.match and strMatching take POSIX ERE over the whole string,
    which has no \\s, \\S, \\d or \\w: inside a bracket expression `[\\s\\S]` is
    the three characters backslash, s and S. nix/hardening.nix once spelled
    its forbidden-capability test that way and the assertion never fired."""

    PATTERN = re.compile(r'(?:builtins\.match|strMatching)\s+"((?:[^"\\]|\\.)*)"')
    PERL_CLASS = re.compile(r"\\[sSdDwW]")

    def nix_files(self):
        if not shutil.which("git") or not (REPO / ".git").exists():
            self.skipTest("needs git and a checkout (CLAUDE.md copies the tree out)")
        names = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", "*.nix"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        self.assertIn("nix/hardening.nix", names)
        return [REPO / name for name in names]

    def test_no_pattern_uses_a_perl_character_class(self):
        for path in self.nix_files():
            for pattern in self.PATTERN.findall(path.read_text()):
                with self.subTest(file=path.name, pattern=pattern):
                    self.assertIsNone(self.PERL_CLASS.search(pattern))

    def test_the_check_sees_the_pattern_hardening_nix_once_had(self):
        old = r'builtins.match "[\\s\\S]*(cap_sys_admin)[\\s\\S]*" caps'
        (pattern,) = self.PATTERN.findall(old)
        self.assertIsNotNone(self.PERL_CLASS.search(pattern))


class TestHardeningWrapperAllowlistHasOneSpelling(unittest.TestCase):
    def test_the_check_script_allows_the_wrappers_hardening_nix_keeps(self):
        (spelled,) = re.findall(
            r'^readonly WRAPPER_ALLOWLIST="([^"]*)"$',
            HARDENING_CHECK.read_text(),
            re.M,
        )
        (table,) = re.findall(
            r"\bkeptWrappers = \{(.*?)\};", nix_code(HARDENING_NIX), re.S
        )
        kept = re.findall(r"^\s*(\w+) =", table, re.M)
        self.assertIn("sudo", kept)
        self.assertEqual(sorted(spelled.split()), sorted(kept))

    def test_the_check_script_expects_the_capabilities_hardening_nix_states(self):
        text = HARDENING_CHECK.read_text()
        for name in ("newuidmap", "newgidmap"):
            (nix_caps,) = re.findall(
                r'^\s*%s = capRoot "([^"]*)"' % name,
                nix_code(HARDENING_NIX),
                re.M,
            )
            self.assertIn("%s) expected_caps=%s ;;" % (name, nix_caps), text)


class TestHardeningSysctlsHaveOneSpelling(unittest.TestCase):
    def check_script_sysctls(self):
        text = HARDENING_CHECK.read_text()
        (body,) = re.findall(r"\ncheck_sysctls\(\) \{\n(.*?)\n\}\n", text, re.S)
        return dict(re.findall(r"^\s+([a-z0-9_.]+)=(\d+)[ ;]", body, re.M))

    def test_the_check_script_reads_back_every_sysctl_hardening_nix_sets(self):
        sysctls = hardening_nix_sysctls()
        self.assertEqual(sysctls["kernel.kptr_restrict"], "2")
        # The forced spelling must reach the comparison too: the port floor is
        # what stops the agent binding sshd's port.
        self.assertEqual(sysctls["net.ipv4.ip_unprivileged_port_start"], "1024")
        checked = self.check_script_sysctls()
        for name, value in sysctls.items():
            with self.subTest(sysctl=name):
                self.assertEqual(checked.get(name), value)


class TestHardeningKernelParamsHaveOneSpelling(unittest.TestCase):
    def check_script_params(self):
        text = HARDENING_CHECK.read_text()
        (body,) = re.findall(r"\ncheck_cmdline\(\) \{\n(.*?)\n\}\n", text, re.S)
        (loop,) = re.findall(r"for param in ([^;]*); do", body)
        return loop.replace("\\\n", " ").split()

    def test_the_check_script_expects_the_kernel_params_hardening_nix_sets(self):
        params = hardening_nix_list("boot.kernelParams")
        self.assertIn("slab_nomerge", params)
        # nohibernate is added by security.protectKernelImage, not by the list.
        self.assertEqual(
            sorted(self.check_script_params()), sorted(params + ["nohibernate"])
        )


class TestHardeningModuleJournalNamesEveryBlockedModule(unittest.TestCase):
    def journal_blocked_names(self):
        text = HARDENING_MD.read_text()
        (section,) = re.findall(r"\n### Kernel modules\n(.*?)\n### ", text, re.S)
        names = set()
        for row in section.splitlines():
            cells = [cell.strip() for cell in row.split("|")]
            if len(cells) > 3 and cells[2] == "blocked":
                names.update(re.findall(r"`([^`]+)`", cells[1]))
        return names

    def test_the_journal_has_a_blocked_row_for_each_module_in_blocked_modules(self):
        blocked = hardening_nix_list("blockedModules")
        self.assertIn("bluetooth", blocked)
        self.assertEqual(self.journal_blocked_names(), set(blocked))


class TestZedSettingsGoThroughTheWrapper(unittest.TestCase):
    def test_zed_settings_render_names_the_wrapper(self):
        code = nix_code(REPO / "nix" / "lsp.nix")
        start = code.index('environment.etc."yolobox/lsp/zed-settings.json"')
        end = code.index("};", code.index("lsp.basedpyright.binary", start))
        self.assertIn('"${lspWrapper}"', code[start:end])


DOMAIN_XML = REPO / "libvirt" / "domain.xml"
HOST_NFT = REPO / "linux" / "yolobox-host.nft"
EGRESS_NIX = REPO / "nix" / "egress.nix"
LIBVIRT_CHECK = REPO / "tests" / "box-libvirt-check.sh"


def nix_binding(path: Path, name: str) -> str:
    """The string a nix file binds `name` to, as `name = "value";`."""
    found = re.search(r'^\s*%s = "([^"]*)";' % re.escape(name), path.read_text(), re.M)
    assert found, "%s binds no string named %s" % (path, name)
    return found.group(1)


def shell_constant(path: Path, name: str) -> str:
    found = re.search(r"^readonly %s=(\S+)$" % re.escape(name), path.read_text(), re.M)
    assert found, "%s has no readonly %s" % (path, name)
    return found.group(1)


def address_set(text: str) -> set:
    return {item.strip() for item in text.split(",")}


class TestLibvirtNetworkMirrorsLima(unittest.TestCase):
    """libvirt/domain.xml gives the guest Lima's address plan, so nix/egress.nix
    and its fixtures, which spell the gateway, need no change on a Linux host."""

    GUEST = "192.168.5.15"

    def domain(self):
        return DOMAIN_XML.read_text()

    def test_the_route_is_the_gateway_egress_nix_exempts(self):
        found = re.search(r"<route [^>]*gateway='([^']+)'", self.domain())
        self.assertIsNotNone(found, "the domain template has no route")
        self.assertEqual(found.group(1), nix_binding(EGRESS_NIX, "limaGateway"))
        self.assertEqual(found.group(1), egress_rules.GATEWAY)

    def test_the_guest_address_is_the_one_the_guest_checks_use(self):
        found = re.search(r"<ip [^>]*address='([^']+)'", self.domain())
        self.assertIsNotNone(found, "the domain template has no ip")
        self.assertEqual(found.group(1), self.GUEST)
        for script in (HARDENING_CHECK, LIBVIRT_CHECK):
            with self.subTest(script=script.name):
                self.assertEqual(
                    shell_constant(script, "GATEWAY_ADDR"), egress_rules.GATEWAY
                )
        self.assertEqual(shell_constant(HARDENING_CHECK, "GUEST_ADDR"), self.GUEST)

    def test_the_operator_may_log_in_from_the_gateway_only(self):
        text = (REPO / "nix" / "base.nix").read_text()
        allowed = re.search(
            r'AllowUsers\s*=[^;]*"\$\{username\}@([0-9.]+)"', text, re.S
        )
        self.assertIsNotNone(allowed, "base.nix has no operator AllowUsers entry")
        self.assertEqual(allowed.group(1), egress_rules.GATEWAY)
        for rule in re.findall(r"iptables -[ICD] nixos-fw ! -i enp0s1 -s (\S+)", text):
            self.assertEqual(rule, egress_rules.GATEWAY)


class TestHostTableBlocksWhatTheGuestTableBlocks(unittest.TestCase):
    """linux/yolobox-host.nft rejects the ranges nix/egress.nix does, so a guest
    that deletes its own table is still kept off them."""

    def host_set(self, family):
        found = re.search(
            r"^\s*%s daddr \{ ([^}]*) \} reject$" % family,
            HOST_NFT.read_text(),
            re.M,
        )
        self.assertIsNotNone(found, "the host table has no %s reject" % family)
        return address_set(found.group(1))

    def chain_rules(self, name):
        body = re.search(
            r"^  chain %s \{\n(.*?)^  \}$" % name, HOST_NFT.read_text(), re.M | re.S
        )
        self.assertIsNotNone(body, "the host table has no chain %s" % name)
        return [line.strip() for line in body.group(1).splitlines()]

    def test_the_output_chain_accepts_by_policy_and_sends_only_the_vm_uid_on(self):
        """`meta skuid` breaks for a packet with no full socket (neighbour
        discovery, MLD, IGMP, kernel NFS), and then neither `==` nor `!=`
        matches. A `!=` exemption would let those host packets fall through to
        the rejects, so the only rule is a positive match, and the policy
        accepts what it does not match."""
        rules = self.chain_rules("output")
        self.assertEqual(
            rules,
            [
                "type filter hook output priority 0; policy accept;",
                'meta skuid "%s" goto vm' % yo_vm_user(),
            ],
        )

    def test_blocked_v4_ranges_are_equal(self):
        self.assertEqual(
            self.host_set("ip"), address_set(nix_binding(EGRESS_NIX, "blockedV4"))
        )

    def test_blocked_v6_ranges_are_equal_plus_the_loopback(self):
        self.assertEqual(
            self.host_set("ip6"),
            address_set(nix_binding(EGRESS_NIX, "blockedV6")) | {"::1"},
        )


class TestTheGuestNeverLearnsTheHostOs(unittest.TestCase):
    """Each setting that differs between hosts gates on the fact it needs, never
    on which OS runs the VM (nix/base.md, "No host OS in the guest")."""

    SPELLINGS = ("YOLOBOX_HOST_OS", "hostOs", "host-os")

    def box_sources(self):
        nix = REPO / "nix"
        return [
            REPO / "flake.nix",
            *sorted(nix.rglob("*.nix")),
            *sorted(nix.rglob("*.sh")),
        ]

    def test_no_box_source_names_the_host_os(self):
        sources = self.box_sources()
        self.assertIn(GUEST_SCRIPT, sources)
        for path in sources:
            text = path.read_text()
            for spelling in self.SPELLINGS:
                with self.subTest(path=str(path.relative_to(REPO)), spelling=spelling):
                    self.assertNotIn(spelling, text)

    def test_yo_passes_nix_no_host_os(self):
        self.assertNotIn(
            "YOLOBOX_HOST_OS", " ".join(yo.nix_env("alice", "github:x/y/z"))
        )


class TestHostnameHasOneSpelling(unittest.TestCase):
    """flake.nix validates YOLOBOX_HOSTNAME and yo derives it from -m; the rule
    and the name must be the same on both sides."""

    def flake_text(self):
        return (REPO / "flake.nix").read_text()

    def test_yo_names_the_variable_flake_reads(self):
        self.assertIn('builtins.getEnv "%s"' % yo.NIX_ENV_HOSTNAME, self.flake_text())

    def test_flake_matches_the_label_rule_yo_checks(self):
        matched = re.search(r'builtins\.match "([^"]+)" n', self.flake_text())
        self.assertIsNotNone(matched, "flake.nix does not match YOLOBOX_HOSTNAME")
        self.assertEqual(matched.group(1), yo.HOSTNAME_LABEL_RE.pattern)

    def test_the_default_hostname_of_flake_is_the_default_machine(self):
        self.assertIn('if n == "" then "%s"' % yo.DEFAULT_INSTANCE, self.flake_text())

    def test_claude_md_names_the_variable(self):
        self.assertIn(yo.NIX_ENV_HOSTNAME, (REPO / "CLAUDE.md").read_text())


class TestFlakeRefHasOneSpelling(unittest.TestCase):
    """flake.nix validates YOLOBOX_FLAKE_REF and yo builds it; the rule and the
    name must be the same on both sides."""

    def flake_text(self):
        return (REPO / "flake.nix").read_text()

    def test_yo_names_the_variable_flake_reads(self):
        self.assertIn('builtins.getEnv "%s"' % yo.NIX_ENV_FLAKE_REF, self.flake_text())

    def test_flake_throws_when_the_ref_is_unset_and_names_the_variable(self):
        thrown = re.search(
            r'else throw "(%s) is ' % yo.NIX_ENV_FLAKE_REF, self.flake_text()
        )
        self.assertIsNotNone(thrown, "flake.nix does not throw on YOLOBOX_FLAKE_REF")

    def test_the_box_stamps_the_ref_unconditionally(self):
        stamp = re.search(
            r'environment\.etc\."yolobox/flake-ref"(.*)',
            (REPO / "nix/base.nix").read_text(),
        )
        self.assertIsNotNone(stamp, "base.nix does not stamp the flake ref")
        self.assertTrue(stamp.group(1).lstrip().startswith(".text"), stamp.group(1))

    def test_flake_matches_the_ref_shape_yo_accepts(self):
        matched = re.search(r'builtins\.match "([^"]+)" r', self.flake_text())
        self.assertIsNotNone(matched, "flake.nix does not match YOLOBOX_FLAKE_REF")
        self.assertEqual(matched.group(1), yo.GITHUB_FLAKE_RE.pattern)


class TestLimaImagesMatchTheLibvirtSeed(unittest.TestCase):
    """Lima picks the images: entry for the Mac's CPU and libvirt downloads its
    own copy of the x86_64 one; both must name the same file and digest."""

    def images(self):
        return parse_yaml_list_entries(yaml_block(YOLOBOX_YAML.read_text(), "images"))

    def test_every_image_pins_a_sha512_digest(self):
        images = self.images()
        self.assertTrue(images)
        for image in images:
            with self.subTest(arch=image.get("arch")):
                self.assertRegex(image.get("digest", ""), r"sha512:[0-9a-f]{128}")

    def test_the_x86_64_image_is_the_libvirt_seed(self):
        (image,) = [i for i in self.images() if i.get("arch") == "x86_64"]
        self.assertEqual(image["digest"], "sha512:" + yo.SEED_SHA512)
        self.assertEqual(image["location"].rsplit("/", 1)[1], yo.SEED_NAME)


class TestCidataDescribesTheOperatorBaseNixDeclares(unittest.TestCase):
    """lima-init creates the operator from lima.env, and base.nix declares the
    same account. A different uid or home would split it into two accounts."""

    USER = "alice"

    def lima_env(self):
        text = yo.cidata_files(self.USER, "ssh-ed25519 AAAA", "x")["lima.env"]
        return dict(line.split("=", 1) for line in text.splitlines())

    def operator_block(self):
        text = (REPO / "nix" / "base.nix").read_text()
        found = re.search(r"users\.users\.\$\{username\} = \{(.*?)\n  \};", text, re.S)
        self.assertIsNotNone(found, "base.nix declares no operator account")
        return found.group(1)

    def test_uid_is_equal(self):
        uid = re.search(r"uid = (\d+);", self.operator_block())
        self.assertEqual(self.lima_env()["LIMA_CIDATA_UID"], uid.group(1))

    def test_the_lima_yaml_pins_the_same_uid(self):
        # Without the pin lima creates the operator with the Mac account's uid.
        pinned = yaml_block(YOLOBOX_YAML.read_text(), "user")
        uid = [line.split(":", 1)[1].strip() for line in pinned if "uid:" in line]
        nix_uid = re.search(r"uid = (\d+);", self.operator_block()).group(1)
        self.assertEqual(uid, [nix_uid])
        self.assertEqual(int(nix_uid), yo.config.OPERATOR_GUEST_UID)

    def test_home_is_equal(self):
        home = re.search(r'home = "([^"]+)";', self.operator_block())
        self.assertEqual(
            self.lima_env()["LIMA_CIDATA_HOME"],
            home.group(1).replace("${username}", self.USER),
        )


# ----------------------------------------------------------------------------
# Facts spelled in more than one file. `yo` is read as TEXT, one helper per
# fact, so the day `yo` becomes a package only these helpers change.


def yo_source_text() -> str:
    """The text of everything that defines `yo`'s module-level constants."""
    return "\n".join(p.read_text() for p in sorted(yo.PKG_DIR.glob("*.py")))


def yo_string_constant(name: str) -> str:
    found = re.search(r'^%s = "([^"]*)"$' % re.escape(name), yo_source_text(), re.M)
    assert found, "yo has no string constant %s" % name
    return found.group(1)


def yo_int_constant(name: str) -> int:
    found = re.search(r"^%s = (\d+)$" % re.escape(name), yo_source_text(), re.M)
    assert found, "yo has no integer constant %s" % name
    return int(found.group(1))


def yo_vm_user() -> str:
    return yo_string_constant("VM_USER")


def yo_passt_dir() -> str:
    return yo_string_constant("PASST_DIR")


def yo_agent_user() -> str:
    return yo_string_constant("AGENT_USER")


def yo_agent_home() -> str:
    return yo_string_constant("AGENT_HOME")


def yo_helper_no_subcommand() -> int:
    return yo_int_constant("HELPER_NO_SUBCOMMAND")


def yo_helper_marker() -> str:
    return yo_string_constant("HELPER_MARKER")


HOST_SETUP = REPO / "linux" / "host-setup.sh"
HOST_UNIT = REPO / "linux" / "yolobox-host.service"
BASE_NIX = REPO / "nix" / "base.nix"


def host_setup_assignment(name: str) -> str:
    """`NAME="value"` in linux/host-setup.sh, with `$CONF_DIR` expanded."""
    text = HOST_SETUP.read_text()
    found = re.search(r'^%s="([^"]*)"$' % re.escape(name), text, re.M)
    assert found, "host-setup.sh sets no %s" % name
    conf_dir = re.search(r'^CONF_DIR="([^"]*)"$', text, re.M)
    assert conf_dir, "host-setup.sh sets no CONF_DIR"
    return found.group(1).replace("$CONF_DIR", conf_dir.group(1))


def host_unit_loaded_file() -> str:
    found = re.search(r"^ExecStart=\S*nft -f (\S+)$", HOST_UNIT.read_text(), re.M)
    assert found, "yolobox-host.service has no `nft -f` ExecStart"
    return found.group(1)


def host_table_skuid() -> str:
    found = re.search(r'meta skuid "([^"]+)" goto vm$', HOST_NFT.read_text(), re.M)
    assert found, "the host table sends no uid to the vm chain"
    return found.group(1)


def agent_account_block() -> str:
    found = re.search(
        r"users\.users\.\$\{agentUser\} = \{(.*?)\n  \};", BASE_NIX.read_text(), re.S
    )
    assert found, "base.nix declares no agent account"
    return found.group(1)


def nix_agent_uid() -> str:
    found = re.search(r"\buid = (\d+);", agent_account_block())
    assert found, "the agent account in base.nix sets no uid"
    return found.group(1)


def flake_agent_user() -> str:
    return nix_binding(REPO / "flake.nix", "agentUser")


def libvirt_nic_name() -> str:
    """The name systemd gives the PCI address the domain template puts the
    network interface at: bus and slot, function 0 (no `f` suffix)."""
    found = re.search(
        r"<interface .*?<address type='pci' domain='0x([0-9a-f]+)'"
        r" bus='0x([0-9a-f]+)' slot='0x([0-9a-f]+)' function='0x([0-9a-f]+)'/>"
        r".*?</interface>",
        DOMAIN_XML.read_text(),
        re.S,
    )
    assert found, "the domain template puts its interface at no PCI address"
    domain, bus, slot, function = (int(group, 16) for group in found.groups())
    assert domain == 0 and function == 0, "the NIC name would need a prefix or suffix"
    return "enp%ds%d" % (bus, slot)


def nix_nic_names() -> set:
    names = set()
    for path in (REPO / "nix").rglob("*.nix"):
        names |= set(re.findall(r"\benp\d+s\d+\b", nix_code(path)))
    return names


class TestHostAccountAndTableAreOneFact(unittest.TestCase):
    """The account libvirt runs QEMU and passt as is named in four places; the
    host table judges only that uid, so a rename in one place alone leaves the
    guest's traffic unjudged."""

    def test_yo_host_setup_and_the_host_table_name_the_same_account(self):
        self.assertEqual(yo_vm_user(), host_setup_assignment("VM_USER"))
        self.assertEqual(host_table_skuid(), yo_vm_user())

    def test_the_passt_directory_is_the_one_yo_checks_and_its_rule_spells_the_mask(
        self,
    ):
        """An ACL entry is only as wide as the mask, and yo checks the effective
        permission of this entry in this directory."""
        self.assertEqual(yo_passt_dir(), host_setup_assignment("PASST_DIR"))
        rule = re.search(
            r"^a\+ \$PASST_DIR - - - - (\S+)$", HOST_SETUP.read_text(), re.M
        )
        assert rule, "host-setup.sh has no `a+` rule for the passt directory"
        self.assertEqual(rule.group(1), "u:$VM_USER:rwx,m::rwx")

    def test_the_unit_loads_the_file_host_setup_installs(self):
        self.assertEqual(host_unit_loaded_file(), host_setup_assignment("NFT_DEST"))

    def test_host_setup_installs_the_unit_where_the_story_says(self):
        self.assertEqual(
            host_setup_assignment("UNIT_DEST"),
            "/etc/systemd/system/" + HOST_UNIT.name,
        )

    def test_the_installed_table_is_named_like_the_repo_file(self):
        self.assertEqual(Path(host_setup_assignment("NFT_DEST")).name, HOST_NFT.name)


class TestHostSetupCanBeRerun(unittest.TestCase):
    """A second run of linux/host-setup.sh changes nothing on a healthy host and
    finishes what a run that died left half done (linux/host-setup.md), and a
    damaged checkout is refused before the host is touched. The script's own
    definitions and then a slice of its steps run here under bash, with fake
    systemctl, virsh, nft and restorecon; they skip without GNU stat and
    install."""

    def test_the_unit_reloads_by_loading_the_file_it_starts_with(self):
        """A reload does not propagate to virtqemud, a restart does."""
        unit = HOST_UNIT.read_text()
        start = re.search(r"^ExecStart=(.+)$", unit, re.M)
        reload_ = re.search(r"^ExecReload=(.+)$", unit, re.M)
        assert start, "yolobox-host.service has no ExecStart"
        self.assertIsNotNone(reload_, "yolobox-host.service has no ExecReload")
        self.assertEqual(reload_.group(1), start.group(1))

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
        if subprocess.run(["stat", "--version"], capture_output=True).returncode:
            self.skipTest("needs GNU stat and install")
        text = HOST_SETUP.read_text()
        definitions = text[: text.index("[[ $EUID -eq 0 ]]")]
        steps = text[text.index(first) : text.index(last)]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        bin_dir = root / "bin"
        bin_dir.mkdir()
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
            fake = bin_dir / name
            fake.write_text("#!/bin/sh\n%s\n" % body)
            fake.chmod(0o755)
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
                steps.replace("/usr/sbin/nft", str(bin_dir / "nft")),
            )
        )
        calls = root / "calls"
        calls.touch()
        result = subprocess.run(
            ["bash", str(script)],
            capture_output=True,
            text=True,
            env={
                "PATH": "%s:%s" % (bin_dir, os.environ["PATH"]),
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
            self.run_steps("(d)", "(b)"),
            ["enable --now virtqemud.socket virtstoraged.socket virtnetworkd.socket"],
        )
        result, calls = self.run_slice('step "(d)', 'step "(b)', libvirtd=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("monolithic libvirtd.service", result.stderr)
        self.assertEqual(calls, [])

    def test_the_default_network_is_switched_off_on_a_host_that_speaks_german(self):
        self.assertEqual(
            self.run_steps("(c)", "(e)"),
            ["net-autostart --disable default", "net-destroy default"],
        )

    def test_virtqemud_restarts_when_it_started_before_the_hook_was_written(self):
        self.assertEqual(
            self.run_steps("(l)", "(g)", started="@1000"),
            ["try-restart virtqemud.service"],
        )

    def test_a_run_that_died_before_the_restart_is_finished_by_the_next(self):
        """The hook is installed and identical, so the restart cannot hang on
        this run having installed it."""
        self.assertEqual(
            self.run_steps("(l)", "(g)", hook_mtime=2000, started="@1000"),
            ["try-restart virtqemud.service"],
        )

    def test_virtqemud_is_left_alone_when_it_started_after_the_hook(self):
        self.assertEqual(
            self.run_steps("(l)", "(g)", hook_mtime=2000, started="@3000"), []
        )

    def test_a_virtqemud_that_never_ran_is_left_alone(self):
        self.assertEqual(self.run_steps("(l)", "(g)", started=""), [])

    def test_a_start_time_it_cannot_read_stops_the_run(self):
        """Skipping the restart would leave the hook unloaded, silently."""
        for started in ("Thu 2026-11-05 10:00:00 UTC", "@", "@12a", "1000", "n/a"):
            with self.subTest(started=started):
                result, calls = self.run_slice(
                    'step "(l)', 'step "(g)', started=started
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("cannot read virtqemud's start time", result.stderr)
                self.assertIn(started, result.stderr)
                self.assertEqual(calls, [])

    def test_an_active_table_unit_is_reloaded_even_when_no_file_changed(self):
        """The loaded table may have been deleted by hand under an active unit.
        Never restarted: the restart would propagate to virtqemud."""
        self.assertEqual(
            self.run_steps("(g)", "(i)", active=True, table_installed=True),
            [
                "daemon-reload",
                "enable yolobox-host.service",
                "reload yolobox-host.service",
            ],
        )

    def test_an_inactive_table_unit_is_enabled_and_started(self):
        self.assertEqual(
            self.run_steps("(g)", "(i)", active=False, table_installed=True),
            ["daemon-reload", "enable --now yolobox-host.service"],
        )


class TestAgentAccountIsOneFact(unittest.TestCase):
    def test_yo_and_the_flake_name_the_same_agent(self):
        self.assertEqual(yo_agent_user(), flake_agent_user())

    def test_the_agent_home_is_the_one_base_nix_gives_the_account(self):
        home = re.search(r'\bhome = "([^"]+)";', agent_account_block())
        self.assertIsNotNone(home, "the agent account in base.nix sets no home")
        self.assertEqual(
            yo_agent_home(), home.group(1).replace("${agentUser}", flake_agent_user())
        )

    def test_the_acceptance_check_uses_the_agent_uid_base_nix_declares(self):
        self.assertEqual(shell_constant(HARDENING_CHECK, "AGENT_UID"), nix_agent_uid())

    def test_the_acceptance_check_names_the_keys_command_base_nix_gives_sshd(self):
        # base.nix spells the command in sshd's own syntax, inside extraConfig,
        # not as a nix binding, so this reads the sshd line.
        found = re.findall(
            r"^\s*AuthorizedKeysCommand (\S+)$",
            (REPO / "nix" / "base.nix").read_text(),
            re.M,
        )
        self.assertEqual(len(found), 1, "base.nix must set AuthorizedKeysCommand once")
        self.assertEqual(
            shell_constant(HARDENING_CHECK, "AGENT_KEYS_COMMAND"), found[0]
        )


class TestGuestHelperAnswersUnknownSubcommandsAsYoExpects(unittest.TestCase):
    """`yo` reads exit 64 plus a marker on stderr as "this box predates the
    subcommand" (version skew), so the guest script must produce both."""

    def run_guest(self, *args):
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
            timeout=20,
        )

    def test_the_exit_code_and_the_marker_are_the_ones_yo_looks_for(self):
        result = self.run_guest("no-such-subcommand")
        self.assertEqual(result.returncode, yo_helper_no_subcommand())
        self.assertIn(yo_helper_marker(), result.stderr)


class TestGuestNicNameIsOneFact(unittest.TestCase):
    """base.nix names the NIC (firewall rule, anti-spoof rule). On libvirt the
    name follows from the PCI slot in libvirt/domain.xml, so moving the slot
    would leave the rules naming an interface that does not exist."""

    def test_the_nix_modules_name_the_interface_the_domain_slot_produces(self):
        self.assertEqual(nix_nic_names(), {libvirt_nic_name()})


class TestSshdSettingsHoldTheirStories(unittest.TestCase):
    """Three sshd lines in nix/base.nix each carry a story elsewhere: yo cp's
    "no guest shell runs" needs the in-process sftp server, root never logs
    in to stage 2, and the agent's Match block must not swallow later lines."""

    def base_nix(self):
        return nix_code(REPO / "nix" / "base.nix")

    def test_the_sftp_server_is_the_in_process_one(self):
        self.assertIn(
            'services.openssh.sftpServerExecutable = "internal-sftp";', self.base_nix()
        )

    def test_root_login_is_off_in_stage_2(self):
        self.assertIn('PermitRootLogin = "no";', self.base_nix())

    def test_the_agent_match_block_is_closed(self):
        (extra,) = re.findall(
            r"services\.openssh\.extraConfig = ''\n(.*?)'';", self.base_nix(), re.S
        )
        lines = [line.strip() for line in extra.splitlines() if line.strip()]
        self.assertEqual(lines[0], "Match User ${agentUser}")
        self.assertEqual(lines[-1], "Match all")


class TestEveryUserUnitIsTheAgents(unittest.TestCase):
    """CONSTITUTION: every ConditionUser= names agentUser. Each user unit a
    module declares, as a block or as one dotted line, must carry the
    condition, or the operator's user manager runs it too (nix/podman.md)."""

    UNIT_RE = re.compile(r"systemd\.user\.(services|sockets|timers|paths)\.([\w-]+)")
    CONDITION = "ConditionUser = agentUser;"

    def block_after(self, text, start):
        """The text of the `{ ... }` that starts at the first brace after start."""
        begin = text.index("{", start)
        depth = 0
        for i in range(begin, len(text)):
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            if depth == 0:
                return text[begin : i + 1]
        self.fail("unbalanced braces after offset %d" % start)

    def conditioned_units(self, text):
        units = set()
        for m in self.UNIT_RE.finditer(text):
            kind, name = m.groups()
            rest = text[m.end() :].lstrip()
            if rest.startswith("= {") and self.CONDITION in self.block_after(
                text, m.end()
            ):
                units.add((kind, name))
            elif rest.startswith(".unitConfig.ConditionUser = agentUser;"):
                units.add((kind, name))
        return units

    def test_every_user_unit_names_the_agent(self):
        for path in sorted((REPO / "nix").rglob("*.nix")):
            text = nix_code(path)
            declared = {m.groups() for m in self.UNIT_RE.finditer(text)}
            if not declared:
                continue
            with self.subTest(file=path.name):
                self.assertEqual(declared - self.conditioned_units(text), set())

    def test_the_check_sees_an_unconditioned_unit(self):
        text = 'systemd.user.sockets.x.wantedBy = [ "sockets.target" ];'
        self.assertEqual(self.conditioned_units(text), set())
        block = "systemd.user.services.x = { unitConfig.ConditionUser = agentUser; };"
        self.assertEqual(self.conditioned_units(block), {("services", "x")})


class TestOperatorNamePatternHasOneSpelling(unittest.TestCase):
    """The operator's name reaches sshd config, tmpfiles rules and shell
    scripts, so nix/base.nix asserts its shape; yo refuses the same names on
    the host, for both drivers, before it writes cidata (docs/accounts.md)."""

    def test_base_nix_and_yo_accept_the_same_names(self):
        (pattern,) = re.findall(
            r'builtins\.match "([^"]+)" username', nix_code(REPO / "nix" / "base.nix")
        )
        self.assertEqual(pattern, yo.config.OPERATOR_ACCOUNT_RE.pattern)


class TestProcMountOptionsHaveOneSpelling(unittest.TestCase):
    """nix/hardening.nix adds hidepid and wheel as the gid= group to /proc's
    mount (procOptions), on top of nixpkgs' three defaults, which it must not
    restate or the mount carries each twice; tests/box-hardening-check.sh reads
    the whole mount back (PROC_OPTIONS). The gid is read from the wheel group
    in nix and spelled as the number findmnt prints in the script, where 1 is
    nixpkgs' ids.gids.wheel."""

    WHEEL_GID = 1
    GID_FROM_WHEEL = "gid=${toString config.users.groups.wheel.gid}"
    NIXPKGS_DEFAULTS = ["nosuid", "noexec", "nodev"]

    def test_the_check_script_expects_the_proc_options_hardening_nix_sets(self):
        options = hardening_nix_list("procOptions")
        self.assertIn("hidepid=invisible", options)
        self.assertIn(self.GID_FROM_WHEEL, options)
        self.assertIn(
            'boot.specialFileSystems."/proc".options = procOptions;',
            nix_code(HARDENING_NIX),
        )
        (spelled,) = re.findall(
            r'^readonly PROC_OPTIONS="([^"]*)"$', HARDENING_CHECK.read_text(), re.M
        )
        rendered = [
            "gid=%d" % self.WHEEL_GID if o == self.GID_FROM_WHEEL else o
            for o in options
        ]
        self.assertEqual(spelled.split(), self.NIXPKGS_DEFAULTS + rendered)


class TestRevStampsAreOneFact(unittest.TestCase):
    """yo bootstrap writes the commit and NAR hash stamps that yolobox-guest's
    `rebuild` reads: the paths, and the shape each value must have before it
    reaches an argv, are spelled on both sides. The stamps are state under
    /var/lib, which base.nix declares to tmpfiles and never renders into the
    system, so a docs-only commit keeps the generation."""

    def test_the_state_directory_and_the_two_files_are_yos(self):
        state_dir = re.fullmatch(
            r"\$\{YOLOBOX_STATE_DIR:-(.*)\}", guest_script_constant("YOLOBOX_STATE_DIR")
        )
        self.assertIsNotNone(state_dir, "YOLOBOX_STATE_DIR lost its override spelling")
        self.assertEqual(state_dir.group(1), yo.release.STATE_DIR)
        read = set(
            re.findall(
                r'"\$\{YOLOBOX_STATE_DIR\}/([a-z-]+)"',
                guest_script_function("pinned_flake"),
            )
        )
        yo_files = {
            os.path.basename(path)
            for path in (yo.release.REV_STAMP, yo.release.NARHASH_STAMP)
        }
        self.assertEqual(read, yo_files)
        for path in (yo.release.REV_STAMP, yo.release.NARHASH_STAMP):
            self.assertEqual(os.path.dirname(path), yo.release.STATE_DIR)

    def test_the_value_shapes_are_yos(self):
        self.assertEqual(guest_script_constant("REV_RE"), yo.release.COMMIT_RE.pattern)
        self.assertEqual(
            guest_script_constant("NARHASH_RE"), yo.release.NARHASH_RE.pattern
        )

    def test_base_nix_declares_the_directory_and_renders_neither_stamp(self):
        base = (REPO / "nix" / "base.nix").read_text()
        self.assertIn('"d %s 0755 root root -"' % yo.release.STATE_DIR, base)
        for path in (yo.release.REV_STAMP, yo.release.NARHASH_STAMP):
            self.assertNotIn(path, base)
            self.assertNotIn('"yolobox/%s"' % os.path.basename(path), base)


class TestYoSourceRevLiteral(unittest.TestCase):
    """nix stamps the commit a yo was built from into config.py (as it does
    the version), so a yo built from main builds that tree and not the tag its
    .version names. Brew builds from a tarball with no commit, so it must not
    touch the literal: a brew yo stays `dev` there and builds its tag."""

    def literal_lines(self, name):
        text = (REPO / "yolobox_cli" / "config.py").read_text()
        return text.splitlines().count('%s = "dev"' % name)

    def test_config_has_exactly_one_line_that_is_the_dev_source_rev_literal(self):
        self.assertEqual(self.literal_lines("YO_SOURCE_REV"), 1)

    def test_nix_patches_the_literal_from_the_flakes_own_rev_and_brew_does_not(self):
        nix = (REPO / "nix" / "pkgs" / "yolobox.nix").read_text()
        self.assertIn(
            """--replace-fail 'YO_SOURCE_REV = "dev"' 'YO_SOURCE_REV = "${sourceRev}"'""",
            nix,
        )
        self.assertIn("\n, sourceRev\n", nix, "yolobox.nix takes no sourceRev")
        flake = (REPO / "flake.nix").read_text()
        self.assertIn('sourceRev = self.rev or "dev";', flake)
        self.assertNotIn(
            "YO_SOURCE_REV", (REPO / "homebrew" / "yolobox.rb").read_text()
        )


class TestLibvirtCheckNamesWhatYoNames(unittest.TestCase):
    """tests/box-libvirt-check.sh takes a machine name and judges it with two
    bash regexes before it touches a domain; yo judges the same name with
    cli.INSTANCE_NAME_RE and config.HOSTNAME_LABEL_RE. A name one accepts and
    the other refuses would make the check run against a domain yo never
    defines, or refuse one it did."""

    SCRIPT = REPO / "tests" / "box-libvirt-check.sh"

    def bash_regex(self, subject: str) -> str:
        (pattern,) = re.findall(
            r"\[\[ %s =~ \^(.+?)\$ \]\]" % re.escape(subject), self.SCRIPT.read_text()
        )
        return pattern

    def test_the_name_regex_is_yos(self):
        # bash ERE has no (?:...): the non-capturing group is a plain group there.
        expected = yo.cli.INSTANCE_NAME_RE.pattern.replace("(?:", "(")
        self.assertEqual(self.bash_regex("${NAME}"), expected)

    def test_the_hostname_regex_is_yos(self):
        self.assertEqual(
            self.bash_regex('"yolobox-${NAME,,}"'), yo.config.HOSTNAME_LABEL_RE.pattern
        )


class TestPinnedActionsNameTheirFullTag(unittest.TestCase):
    """Every action in the workflows is pinned to a commit SHA, and the comment
    beside it is the tag the SHA was resolved from (release.md, "Pinned
    actions"). A bare major (`# v7`) names a tag the owner moves, so the next
    person resolving it cannot tell which release the SHA is; the full X.Y.Z
    can be checked against the action's releases."""

    WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.yml"))
    USES = re.compile(r"^\s*(?:- )?uses: (\S+)(.*)$", re.M)
    PINNED = re.compile(r"^(?P<action>[^@\s]+)@(?P<sha>[0-9a-f]{40})$")
    TAG = re.compile(r"^ # v\d+\.\d+\.\d+$")

    def test_every_action_is_a_sha_with_its_full_tag(self):
        seen = 0
        for path in self.WORKFLOWS:
            for action, rest in self.USES.findall(path.read_text()):
                if action.startswith("./"):
                    continue
                seen += 1
                with self.subTest(workflow=path.name, action=action):
                    self.assertRegex(action, self.PINNED)
                    self.assertRegex(rest, self.TAG)
        self.assertGreater(seen, 0)
