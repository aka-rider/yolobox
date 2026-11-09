"""Facts spelled in files that cannot read facts.json, and repo-wide rules.

facts.json is the one spelling of every literal yo, the box and the scripts
share. A few files are static and read by a tool that knows nothing of it
(lima's yaml, libvirt's domain template, the Linux host's hook and nft table),
so each class here compares one such file to facts.json or to the constant yo
reads, and a change to one side alone fails here instead of on a user's box.

Tools whose absence skips tests: git (the shellcheck-list test).
"""

import ast
import collections
import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import shell_harness
from shell_harness import FACTS, REPO, require_tool
from test_yo import yo

GUEST_SCRIPT = REPO / "nix" / "guest" / "yolobox-guest.sh"
YOLOBOX_YAML = REPO / "lima" / "yolobox.yaml"
DOMAIN_XML = REPO / "libvirt" / "domain.xml"
HOST_SETUP = REPO / "linux" / "host-setup.sh"
HOOK = REPO / "linux" / "yolobox-libvirt-hook.sh"
HOST_NFT = REPO / "linux" / "yolobox-host.nft"
HOST_UNIT = REPO / "linux" / "yolobox-host.service"
EGRESS_NFT = REPO / "nix" / "egress" / "yolobox-egress.nft"
VERIFY_WORKFLOW = REPO / ".github" / "workflows" / "verify.yml"


def package_text():
    """The source of every module of the yolobox_cli package."""
    return "\n".join(p.read_text() for p in sorted(yo.PKG_DIR.glob("*.py")))


def found(pattern, text, what):
    """The first group of the one match of `pattern` in `text`."""
    matches = re.findall(pattern, text, re.M)
    assert len(matches) == 1, "%s: want one match of %r, got %r" % (
        what,
        pattern,
        matches,
    )
    return matches[0]


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


def uncommented(path: Path) -> str:
    """The file's text without its `#` comment lines (nix, systemd units)."""
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
            uncommented(REPO / "nix" / "tailscale.nix"),
        )
        self.assertIsNotNone(found, "tailscaled has no negated LogFilterPatterns")
        return found.group(1)

    def test_a_relayed_contact_is_dropped(self):
        self.assertRegex(self.DERP, self.pattern())

    def test_a_direct_contact_and_path_changes_are_kept(self):
        for line in (self.DIRECT, self.NOW_USING):
            with self.subTest(line=line):
                self.assertNotRegex(line, self.pattern())


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


class TestLimaYamlStatesTheFacts(unittest.TestCase):
    """lima reads lima/yolobox.yaml once, at creation, and knows nothing of
    facts.json or of yo's constants."""

    def text(self):
        return YOLOBOX_YAML.read_text()

    def test_the_port_forwards_are_yos_in_order(self):
        # lima takes the first matching rule, so the order is the setting.
        parsed = parse_yaml_list_entries(yaml_block(self.text(), "portForwards"))
        self.assertEqual(parsed, yo.LIMA_PORT_FORWARDS)

    def test_the_operator_uid_and_ssh_port_are_the_facts(self):
        self.assertEqual(
            [line.strip() for line in yaml_block(self.text(), "user") if line.strip()],
            ["uid: %d" % FACTS["operator_uid"]],
        )
        self.assertIn("  localPort: %d" % FACTS["ssh_local_port"], self.text())

    def test_the_vm_has_no_host_mounts(self):
        # The whole design is "no host mounts, git is the only bridge".
        self.assertEqual(yaml_top_level_scalars(self.text(), "mounts"), ["[]"])

    def test_the_defaults_that_leak_into_the_guest_are_pinned_off(self):
        text = self.text()
        self.assertEqual(yaml_top_level_scalars(text, "vmType"), ["vz"])
        self.assertEqual(yaml_top_level_scalars(text, "propagateProxyEnv"), ["false"])
        ssh = {
            key.strip(): yaml_scalar(value)
            for key, _, value in (
                line.partition(":")
                for line in yaml_block(text, "ssh")
                if ":" in line and not line.strip().startswith("#")
            )
        }
        self.assertIs(ssh.get("loadDotSSHPubKeys"), False)
        self.assertIs(ssh.get("overVsock"), False)

    def test_the_x86_64_image_is_the_libvirt_seed(self):
        images = parse_yaml_list_entries(yaml_block(self.text(), "images"))
        (image,) = [i for i in images if i.get("arch") == "x86_64"]
        self.assertEqual(image["digest"], "sha512:" + yo.SEED_SHA512)
        self.assertEqual(image["location"].rsplit("/", 1)[1], yo.SEED_NAME)


class TestLimaMirrorsOnlyDevPortsOntoTheHostLoopback(unittest.TestCase):
    def first_rule_for(self, port, proto):
        for rule in yo.LIMA_PORT_FORWARDS:
            if rule.get("proto") != proto:
                continue
            first, last = rule.get("guestPortRange", [1, 65535])
            if first <= port <= last:
                return rule
        self.fail("no %s rule matches port %d" % (proto, port))

    def test_a_listener_outside_the_dev_ports_or_on_an_oauth_port_is_never_mirrored(
        self,
    ):
        # Login tools that take an ephemeral port (Claude Code) must need no
        # deny-list entry, and a guest listener must not squat a callback port.
        for port in (1, 1023, 1455, 8085, 18000, 49152, 54545, 65535):
            with self.subTest(port=port):
                self.assertTrue(self.first_rule_for(port, "tcp").get("ignore"))

    def test_a_udp_listener_is_never_mirrored(self):
        # tailscaled's port collides with a host's own Tailscale, and port 68
        # intercepts the host's DHCP.
        for port in (1, 68, 1024, 41641, 49151, 65535):
            with self.subTest(port=port):
                self.assertTrue(self.first_rule_for(port, "udp").get("ignore"))

    def test_no_rule_publishes_beyond_the_hosts_loopback(self):
        for rule in yo.LIMA_PORT_FORWARDS:
            with self.subTest(rule=rule):
                self.assertEqual(rule.get("hostIP", "127.0.0.1"), "127.0.0.1")


def shell_value(path, name):
    """`NAME="value"` or `readonly NAME="${SEAM:-value}"` in a shell script."""
    return found(
        r'^(?:readonly )?%s="(?:\$\{\w+:-)?([^"}]+)\}?"$' % name,
        path.read_text(),
        path.name,
    )


def address_set(path, rule):
    """The addresses of the one `{ a, b }` set `rule` matches in an nft file."""
    return {a.strip() for a in found(rule, path.read_text(), path.name).split(",")}


class TestLinuxHostFilesStateTheFacts(unittest.TestCase):
    """The domain template, the passt hook, host-setup.sh and the host table
    are static files libvirt, systemd and nft read on a host where nothing
    renders facts.json into them."""

    def test_the_domain_gives_the_guest_lima_s_address_plan_and_nic(self):
        domain = DOMAIN_XML.read_text()
        route = found(r"<route [^>]*gateway='([^']+)'", domain, "domain.xml")
        address = found(r"<ip [^>]*address='([^']+)'", domain, "domain.xml")
        self.assertEqual((route, address), (FACTS["gateway"], FACTS["guest_addr"]))
        # systemd names a PCI NIC enp<bus>s<slot> (function 0 has no suffix).
        bus, slot = found(
            r"<address type='pci' domain='0x0+' bus='0x([0-9a-f]+)'"
            r" slot='0x([0-9a-f]+)' function='0x0'/>\s*</interface>",
            domain,
            "domain.xml",
        )
        self.assertEqual("enp%ds%d" % (int(bus, 16), int(slot, 16)), FACTS["guest_nic"])

    def test_the_hook_and_host_setup_name_the_vm_account_and_the_passt_dir(self):
        for script in (HOOK, HOST_SETUP):
            with self.subTest(script=script.name):
                self.assertEqual(shell_value(script, "VM_USER"), FACTS["vm_user"])
                self.assertEqual(shell_value(script, "PASST_DIR"), FACTS["passt_dir"])

    def test_the_hook_looks_for_the_namespace_the_domain_carries(self):
        self.assertEqual(shell_value(HOOK, "DOMAIN_NS"), FACTS["domain_ns"])
        self.assertIn("xmlns:yolobox='%s'" % FACTS["domain_ns"], DOMAIN_XML.read_text())

    def test_the_host_table_judges_the_vm_account_and_is_the_file_its_unit_loads(self):
        self.assertIn(
            'meta skuid "%s" goto vm' % FACTS["vm_user"], HOST_NFT.read_text()
        )
        dest = shell_value(HOST_SETUP, "NFT_DEST").replace(
            "$CONF_DIR", shell_value(HOST_SETUP, "CONF_DIR")
        )
        self.assertEqual(Path(dest).name, HOST_NFT.name)
        self.assertIn("ExecStart=/usr/sbin/nft -f %s\n" % dest, HOST_UNIT.read_text())

    def test_host_setup_judges_the_resolver_the_preflight_judges(self):
        self.assertEqual(
            shell_value(HOST_SETUP, "RESOLV_CONF"), FACTS["host_resolv_conf"]
        )
        self.assertEqual(
            shell_value(HOST_SETUP, "RESOLV_CONF_TYPE"), FACTS["host_resolv_conf_type"]
        )

    def test_the_host_unit_applies_the_passt_rule_host_setup_keeps_out_of_the_initrd(
        self,
    ):
        dest = shell_value(HOST_SETUP, "PASST_TMPFILES").replace(
            "$CONF_DIR", shell_value(HOST_SETUP, "CONF_DIR")
        )
        # dracut copies /etc/tmpfiles.d into the initrd, which has no qemu account.
        self.assertFalse(dest.startswith("/etc/tmpfiles.d/"), dest)
        self.assertIn(
            "ExecStart=/usr/bin/systemd-tmpfiles --create %s\n" % dest,
            HOST_UNIT.read_text(),
        )

    def test_the_host_table_blocks_what_the_guest_table_blocks(self):
        """A guest that deletes its own table is still kept off the same ranges."""
        for family, extra in (("ip", set()), ("ip6", {"::1"})):
            with self.subTest(family=family):
                host = address_set(
                    HOST_NFT, r"^\s*%s daddr \{ ([^}]*) \} reject$" % family
                )
                guest = address_set(
                    EGRESS_NFT, r"^define private_%s = \{ ([^}]*) \}$" % family
                )
                self.assertEqual(host, guest | extra)

    def test_the_guest_table_lets_dns_reach_the_gateway_only(self):
        dns = r"^\s*ip daddr (\S+) (?:udp|tcp) dport 53 accept$"
        self.assertEqual(
            set(re.findall(dns, EGRESS_NFT.read_text(), re.M)), {FACTS["gateway"]}
        )

    def test_the_cidata_home_is_the_operator_home_the_box_declares(self):
        lima_env = yo.cidata_files("alice", "ssh-ed25519 AAAA", "x")["lima.env"]
        home = "/home/alice" + FACTS["operator_home_suffix"]
        self.assertIn("LIMA_CIDATA_HOME=%s\n" % home, lima_env)


class TestEtcFilesBaseNixWritesAreUnderEtc(unittest.TestCase):
    """base.nix names each such file `lib.removePrefix "/etc/" facts.KEY`,
    which leaves a path elsewhere whole and so writes a different file."""

    def test_every_fact_base_nix_writes_into_etc_lies_under_etc(self):
        keys = re.findall(
            r'lib\.removePrefix "/etc/" facts\.(\w+)',
            (REPO / "nix" / "base.nix").read_text(),
        )
        self.assertTrue(keys)
        for key in keys:
            with self.subTest(key=key):
                self.assertTrue(FACTS[key].startswith("/etc/"), FACTS[key])


class TestEachDriverModuleImportsWithoutTheOther(unittest.TestCase):
    """A host ships only its own driver module."""

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
                    # Installed beside the package, as nix and brew ship it.
                    shutil.copy(REPO / "facts.json", tmp)
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


class TestPackagersPatchLiteralsConfigHoldsOnce(unittest.TestCase):
    def test_every_text_nix_and_brew_rewrite_is_one_line_of_config(self):
        # brew's inreplace does not fail on a miss: a reformatted literal would
        # ship a brew yo that reports "dev".
        nix = (REPO / "nix" / "pkgs" / "yolobox.nix").read_text()
        brew = (REPO / "homebrew" / "yolobox.rb").read_text()
        patched = re.findall(r"--replace-fail '([^']+)' '", nix) + re.findall(
            r"inreplace [^,]+, '([^']+)'", brew
        )
        self.assertGreaterEqual(len(patched), 3, patched)
        lines = (yo.PKG_DIR / "config.py").read_text().splitlines()
        for literal in patched:
            with self.subTest(literal=literal):
                self.assertEqual(lines.count(literal), 1)


class TestRuffVersionIsPinnedOnce(unittest.TestCase):
    def required(self):
        toml = (REPO / "ruff.toml").read_text()
        return found(r'^required-version = "==([\d.]+)"$', toml, "ruff.toml")

    def test_verify_workflow_installs_the_version_ruff_toml_requires(self):
        installed = re.findall(
            r'^\s+version: "([\d.]+)"$', VERIFY_WORKFLOW.read_text(), re.M
        )
        self.assertEqual(installed, [self.required()] * 2)

    def test_the_commands_agents_md_gives_run_the_version_ruff_toml_requires(self):
        commands = re.findall(
            r"ruff(?:@|==)(\d+\.\d+\.\d+)", (REPO / "AGENTS.md").read_text()
        )
        self.assertGreaterEqual(len(commands), 2)
        self.assertEqual(set(commands), {self.required()})


class TestVerifyWorkflowCoversTheScripts(unittest.TestCase):
    def workflow(self):
        return VERIFY_WORKFLOW.read_text()

    def shellchecked(self):
        listed = found(r"^\s+run: shellcheck (.+)$", self.workflow(), "verify.yml")
        return sorted(listed.split())

    def test_every_listed_script_exists(self):
        for script in self.shellchecked():
            with self.subTest(script=script):
                self.assertTrue((REPO / script).is_file())

    def test_every_shell_script_of_the_repo_is_shellchecked(self):
        self.assertEqual(
            self.shellchecked(),
            sorted(str(path.relative_to(REPO)) for path in tracked("*.sh")),
        )

    def test_every_script_package_the_box_exposes_is_built_by_ci(self):
        exposed = set()
        for path in (REPO / "nix").glob("*.nix"):
            exposed.update(
                re.findall(r"system\.build\.(yolobox\w+) =", uncommented(path))
            )
        step = found(
            r"(?s)name: Build the shellchecked script packages\n(.*?)(?=\n      - name:|\n {6}#|\Z)",
            self.workflow(),
            "verify.yml",
        )
        self.assertTrue(exposed)
        self.assertEqual(set(re.findall(r"system\.build\.(yolobox\w+)", step)), exposed)


class TestFailClosedPieces(unittest.TestCase):
    """A table that stopping its unit could remove, or an sshd that refuses its
    config, leaves the box open or unreachable."""

    def test_the_guest_egress_unit_has_no_stop_hook_and_the_network_requires_it(self):
        egress = uncommented(REPO / "nix" / "egress.nix")
        self.assertNotIn("ExecStop", egress)
        for service in ("dhcpcd", "tailscaled"):
            with self.subTest(service=service):
                self.assertRegex(
                    egress,
                    r"systemd\.services\.%s = \{[^}]*requires = \[ unit \]" % service,
                )

    def test_the_host_table_unit_has_no_stop_hook_and_libvirt_requires_it(self):
        unit = uncommented(HOST_UNIT)
        self.assertNotIn("ExecStop", unit)
        self.assertRegex(unit, r"(?m)^RequiredBy=virtqemud\.service$")

    def test_the_initrd_sshd_global_keywords_come_before_its_match_block(self):
        # A global keyword after `Match` belongs to the block, and sshd refuses
        # the whole config.
        luks = uncommented(REPO / "nix" / "luks.nix")
        for keyword in (
            "PerSourcePenalties no",
            "VersionAddendum ${facts.initrd_ssh_banner_mark}",
        ):
            with self.subTest(keyword=keyword):
                self.assertLess(luks.index(keyword), luks.index("Match "))

    def test_the_claude_launcher_watches_with_path_changed(self):
        # One download is thousands of writes, past the path unit's trigger limit.
        harnesses = uncommented(REPO / "nix" / "harnesses.nix")
        self.assertIn("PathChanged", harnesses)
        self.assertNotIn("PathModified", harnesses)


class TestPinnedActionsNameTheirFullTag(unittest.TestCase):
    """Every action in the workflows is pinned to a commit SHA, and the comment
    beside it is the tag the SHA was resolved from. A bare major (`# v7`)
    names a tag the owner moves, so the next
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


def tracked(*patterns):
    """The tracked files (with the untracked ones git does not ignore) that
    match `patterns`, as paths."""
    require_tool("git")
    listing = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "--cached", "--others"]
        + ["--exclude-standard", *patterns],
        capture_output=True,
        text=True,
    )
    # A tree unpacked from `git archive` (a box's copy) has no index to list.
    require_tool(
        "git",
        have=listing.returncode == 0,
        why="needs a git checkout (git ls-files: %s)" % listing.stderr.strip(),
    )
    return [REPO / name for name in listing.stdout.split()]


def scripts():
    """Every file that holds shell: the scripts, nix (whose strings are
    scripts) and the workflows' run blocks."""
    return tracked("*.sh", "*.nix", ".github/workflows/*.yml")


def logical_lines(path):
    """(number, line) per line of the file, a line that ends in `|` or `\\`
    joined with the next, so a pipe split across lines reads as one."""
    joined, start, lines = "", None, []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        start = start or number
        stripped = line.rstrip()
        if stripped.endswith("\\") or stripped.endswith("|"):
            joined += stripped.rstrip("\\") + " "
            continue
        lines.append((start, joined + line))
        joined, start = "", None
    return lines


def shell_code(line):
    """The line without a trailing `# comment` (a `#` after a blank)."""
    return re.split(r"(?:^|\s)#", line, maxsplit=1)[0]


class TestScriptsKeepTheirResultsHonest(unittest.TestCase):
    """Shell idioms that turn a failure into a wrong answer."""

    def offenders(self, pattern):
        return [
            "%s:%d: %s" % (path.relative_to(REPO), number, line.strip())
            for path in scripts()
            for number, line in logical_lines(path)
            if re.search(pattern, shell_code(line))
        ]

    def test_no_script_pipes_into_grep_q(self):
        # grep -q exits at its first match, the producer dies of SIGPIPE, and
        # under pipefail a present thing reads as missing.
        self.assertEqual(self.offenders(r"\|\s*grep\s(?:[^|;&]*\s)?-[A-Za-z]*q"), [])

    def test_no_mktemp_result_is_assigned_inside_another_command(self):
        # Nested in another $( ), or after local/export, its failure is lost
        # and the variable holds an empty path that a later rm deletes from.
        nested = r"\$\([^)]*\$\(\s*mktemp\b"
        declared = r"\b(?:local|export|readonly|declare)\s+[^=\s]+=\"?\$\(\s*mktemp\b"
        self.assertEqual(self.offenders(nested), [])
        self.assertEqual(self.offenders(declared), [])

    def test_every_sort_runs_in_the_c_locale(self):
        # sort orders by the locale's collation, and nix and the comparisons
        # downstream order bytewise. A sort is in the C locale when its own
        # command line says so, or after the script exports it at top level.
        offenders = [
            "%s:%d: %s" % (path.relative_to(REPO), number, line.strip())
            for path in scripts()
            for number, line in logical_lines(path)
            if re.search(r"(?:^|[|;&(`]|\$\()\s*sort\b", shell_code(line))
            and number < c_locale_exported_at(path)
        ]
        self.assertEqual(offenders, [])

    def test_only_a_top_level_export_puts_the_script_in_the_c_locale(self):
        cases = {
            "export LC_ALL=C\nsort x\n": 1,
            "# export LC_ALL=C\nsort x\n": NEVER,
            "f() {\n    export LC_ALL=C\n}\nsort x\n": NEVER,
            "cat <<EOF\nexport LC_ALL=C\nEOF\nsort x\n": NEVER,
            "cat <<'EOF'\nexport LC_ALL=C\nEOF\nexport LC_ALL=C\n": 4,
            "echo 'export LC_ALL=C'\n": NEVER,
            "sort x\nexport LC_ALL=C\n": 2,
        }
        for text, line in cases.items():
            with self.subTest(text=text):
                with tempfile.TemporaryDirectory() as scratch:
                    script = Path(scratch, "s.sh")
                    script.write_text(text)
                    self.assertEqual(c_locale_exported_at(script), line)


# A script that never exports LC_ALL=C at top level.
NEVER = sys.maxsize
HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")


def c_locale_exported_at(path):
    """The number of the script's first top-level `export LC_ALL=C` line: at
    the start of a line, outside every heredoc. NEVER when it has none."""
    ends = []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if ends:
            if line.strip() == ends[0]:
                ends.pop(0)
            continue
        if re.fullmatch(r"export LC_ALL=C(?:\s+#.*)?", line):
            return number
        ends = [match.group(2) for match in HEREDOC_RE.finditer(shell_code(line))]
    return NEVER


def spelled(word, text):
    """Each place `text` spells `word` as a whole word (a `-` joins words)."""
    return re.findall(r"(?<![\w-])%s(?![\w-])" % re.escape(word), text)


class TestTheBannerMarksAreTheFacts(unittest.TestCase):
    """yo tells the box's sshds apart by the word each one's banner carries,
    so each word is rendered by nix from its fact and read by yo only from it."""

    MARKS = sorted(key for key in FACTS if key.endswith("_ssh_banner_mark"))

    def test_nix_renders_each_mark_as_a_version_addendum_once(self):
        nix = "\n".join(uncommented(path) for path in tracked("*.nix"))
        for key in self.MARKS:
            with self.subTest(key=key):
                rendered = re.findall(
                    r"VersionAddendum(?: | = )\$?\{?facts\.%s\b" % key, nix
                )
                self.assertEqual(len(rendered), 1, rendered)
                self.assertEqual(spelled(FACTS[key], nix), [])

    def test_yo_spells_each_mark_only_through_its_fact(self):
        sources = python_sources()
        for key in self.MARKS:
            with self.subTest(key=key):
                for path, text in sources.items():
                    self.assertEqual(spelled(FACTS[key], text), [], path)
                lookups = [
                    path
                    for path, text in sources.items()
                    for _ in re.findall(r"FACTS\[\"%s\"\]" % key, text)
                ]
                self.assertEqual(lookups, ["config.py"])


def python_sources():
    """{name: text} of every Python file yo ships: the package and aws-broker."""
    sources = {p.name: p.read_text() for p in sorted(yo.PKG_DIR.glob("*.py"))}
    sources["aws-broker"] = (REPO / "aws-broker").read_text()
    return sources


def nodes(source, kind):
    """(function, node) for every node of `kind` in the module, by the name of
    the innermost def around it ("<module>" at the top level)."""
    found = []

    def walk(node, function):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child.name)
            else:
                if isinstance(child, kind):
                    found.append((function, child))
                walk(child, function)

    walk(ast.parse(source), "<module>")
    return found


def calls(source):
    return nodes(source, ast.Call)


def called_name(call):
    """`open`, `os.open`, `X.search`: the dotted name a call is made through.
    A receiver that is no name (`Path(p).open`) is left empty: `.open`."""
    parts, node = [], call.func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    parts.append(node.id if isinstance(node, ast.Name) else "")
    return ".".join(reversed(parts))


def sites(predicate):
    """{(source, function): how many calls there `predicate` accepts}, so a
    second call in an allowed function is a site of its own."""
    return dict(
        collections.Counter(
            (name, function)
            for name, text in python_sources().items()
            for function, call in calls(text)
            if predicate(call)
        )
    )


def call_arg(call, index, keyword):
    """The call's argument at `index`, or its `keyword` one; None for neither."""
    if len(call.args) > index:
        return call.args[index]
    return next((k.value for k in call.keywords if k.arg == keyword), None)


def writes_by_mode(mode):
    """Whether an open's mode writes: a literal with w, a, x or +, or any mode
    that is not a literal, which nothing here can read."""
    if mode is None:
        return False
    if not isinstance(mode, ast.Constant) or not isinstance(mode.value, str):
        return True
    return bool(set(mode.value) & set("wax+"))


# Each writes its target in place, never through a sibling and a rename.
COPIES = {
    "shutil.copy",
    "shutil.copy2",
    "shutil.copyfile",
    "shutil.copyfileobj",
    "shutil.copytree",
    "shutil.move",
}
OS_OPEN_WRITE_FLAGS = {"O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC"}


def opens_for_writing(call):
    name = called_name(call)
    if name in ("open", "io.open", "os.fdopen"):
        return writes_by_mode(call_arg(call, 1, "mode"))
    if name == "os.open":
        flags = call_arg(call, 1, "flags")
        named = {n.attr for n in ast.walk(flags) if isinstance(n, ast.Attribute)}
        literal = isinstance(flags, ast.Attribute) or isinstance(flags, ast.BinOp)
        return bool(named & OS_OPEN_WRITE_FLAGS) or not literal
    if name.endswith(".open"):
        # Path.open(mode): the mode comes first.
        return writes_by_mode(call_arg(call, 0, "mode"))
    return (
        name in COPIES
        or name.endswith((".write_text", ".write_bytes"))
        or name in ("tempfile.mkstemp", "tempfile.NamedTemporaryFile")
    )


class TestFilesAreWrittenWhole(unittest.TestCase):
    """Every file another process or a later run reads is written through
    proc.atomic_file, so no reader ever sees half of it."""

    ALLOWED = {
        # The chokepoint itself: its temporary file and the descriptor it
        # writes through.
        ("proc.py", "atomic_file"): 2,
        # A lock file: its content is never read, only its flock.
        ("common.py", "held_flock"): 1,
        # The broker's log, a stream the broker appends to while it runs.
        ("aws.py", "start_aws_broker"): 2,
        ("aws-broker", "reopen"): 1,
        # ssh's own log (-E) of the agent's login, which ssh appends to and yo
        # reads once ssh has exited.
        ("sshx.py", "broken_agent_login"): 1,
        # /dev/null, for the daemon's standard streams.
        ("aws-broker", "main"): 1,
    }

    def test_no_file_is_opened_for_writing_outside_atomic_file(self):
        self.assertEqual(sites(opens_for_writing), self.ALLOWED)

    def test_every_way_to_open_for_writing_is_seen(self):
        writes = [
            "open(p, 'w')",
            "open(p, mode='a')",
            "open(p, mode)",
            "io.open(p, 'r+')",
            "Path(p).open('w')",
            "path.open(mode='x')",
            "path.open(how)",
            "os.open(p, os.O_WRONLY | os.O_CREAT)",
            "os.open(p, flags)",
            "os.fdopen(fd, 'wb')",
            "shutil.copy(a, b)",
            "shutil.copyfile(a, b)",
            "shutil.move(a, b)",
            "Path(p).write_text(t)",
            "tempfile.mkstemp()",
        ]
        reads = [
            "open(p)",
            "open(p, 'rb')",
            "Path(p).open()",
            "os.open(p, os.O_RDONLY)",
        ]
        cases = [(w, True) for w in writes] + [(r, False) for r in reads]
        for source, writing in cases:
            with self.subTest(source=source):
                call = ast.parse(source, mode="eval").body
                self.assertEqual(opens_for_writing(call), writing)


def searches(call):
    name = called_name(call)
    return name.endswith((".search", ".match")) or name in ("re.search", "re.match")


class TestNamesAreJudgedWhole(unittest.TestCase):
    """A name, path, address or rule is judged by fullmatch, whole words or
    parsed membership: a search or a prefix match accepts what merely contains
    it. A search may only pull a value out of a tool's output."""

    ALLOWED = {
        # The daemon's version out of `virsh version --daemon`.
        ("libvirt_host.py", "parse_daemon_version"): 1,
        # MemTotal's number out of /proc/meminfo.
        ("libvirt_host.py", "host_memory_bytes"): 1,
        # The uid field out of each `ss -e` line, and its inode field.
        ("libvirt_host.py", "listener_uids"): 2,
        # The Max memory number out of `virsh dominfo`.
        ("libvirt.py", "active_domains_memory"): 1,
        # The master's pid out of `ssh -O check`.
        ("sshx.py", "master_pid"): 1,
    }

    def test_every_search_extracts_a_value_from_a_tools_output(self):
        self.assertEqual(sites(searches), self.ALLOWED)


def builds_an_unpinned_login(call):
    """A call that makes the proof of an initrd, or an unpinned ssh argv,
    whose user is whatever the caller passes."""
    name = called_name(call).rsplit(".", 1)[-1]
    return name in ("InitrdEndpoint", "unpinned_argv")


class TestRootLogsInOnlyThroughTheInitrdGate(unittest.TestCase):
    """Only unlock.initrd_endpoint() proves an initrd, after its listener and
    its banner, and only root_argv() turns that proof into a root login. The
    unpinned builder takes any user, so it is called only by the operator's
    login, which names the host account, and by the two that take the proof:
    root_argv() and the printed remedy, text for the operator to paste."""

    def test_no_other_function_builds_an_unpinned_login(self):
        self.assertEqual(
            sites(builds_an_unpinned_login),
            {
                ("unlock.py", "initrd_endpoint"): 1,
                ("unlock.py", "operator_argv"): 1,
                ("unlock.py", "root_argv"): 1,
                ("unlock.py", "initrd_root_ssh_command"): 1,
            },
        )


def ssh_command_lines(source):
    """(function, list) for every list literal that starts with "ssh"."""
    return [
        (function, node)
        for function, node in nodes(source, ast.List)
        if node.elts
        and isinstance(node.elts[0], ast.Constant)
        and node.elts[0].value == "ssh"
    ]


class TestEveryGuestCommandGoesThroughSshx(unittest.TestCase):
    """An ssh command line is built in sshx alone, whose guest_argv() starts
    every command yo reads with env LC_ALL=C; the others there run none
    (control_argv) or an interactive session in the operator's locale. Outside
    it only the initrd's unpinned login and `ssh -G`, which reaches no box."""

    def test_no_other_function_builds_an_ssh_command_line(self):
        found = collections.Counter(
            (name, function)
            for name, text in python_sources().items()
            for function, _ in ssh_command_lines(text)
        )
        self.assertEqual(
            dict(found),
            {
                ("sshx.py", "guest_argv"): 1,
                ("sshx.py", "interactive_argv"): 1,
                ("sshx.py", "control_argv"): 1,
                ("unlock.py", "unpinned_argv"): 1,
                ("sshconfig.py", "ssh_config_reaches_box"): 1,
            },
        )


class TestEveryRefusalHasAPositiveControl(unittest.TestCase):
    """A live check that asserts the box refuses something first runs the
    same probe, through the same path, on something the box allows: a
    positive_control whose command runs the same program the same way (the
    agent's env, sudo, podman), or perl_probe_control for a perl probe. A
    probe that cannot succeed at all is refused for reasons of its own."""

    SCRIPT = REPO / "tests" / "box-hardening-check.sh"
    # Nothing the box admits over ssh is reachable from inside it, so sshd's
    # journal, which names AllowUsers, is the proof instead.
    WITHOUT_CONTROL = {"check_logins_refused_off_gateway"}

    def checks(self):
        text = self.SCRIPT.read_text()
        return dict(re.findall(r"(?ms)^(check_\w+)\(\) \{\n(.*?)^\}$", text))

    def test_each_refusal_comes_after_a_control_of_the_same_probe(self):
        refusing, uncontrolled = set(), []
        for name, body in self.checks().items():
            controls = []
            for start, kind, probe in probe_sites(body):
                if kind == "control":
                    controls.append(probe)
                    continue
                refusing.add(name)
                if probe not in controls:
                    uncontrolled.append((name, probe))
        self.assertGreaterEqual(len(refusing), 8, sorted(refusing))
        self.assertEqual(
            sorted(uncontrolled),
            [("check_logins_refused_off_gateway", ("agent_ssh", "ssh"))],
        )

    def test_every_refusal_idiom_is_read_with_its_probe(self):
        agent_ls = ("as_agent_bare", "SYSBIN/ls")
        perl = ("as_agent_bare", "SYSBIN/perl")
        for body, sites in (
            (
                'if out=$(as_agent_bare "${SYSBIN}/ls" /x 2>&1); then\n  fail "y"\n',
                [("refusal", agent_ls)],
            ),
            (
                'out=$(as_agent_bare "${SYSBIN}/ls" /x) || status=$?\n'
                '[ "${status}" -ne 0 ] || fail "y"\n',
                [("refusal", agent_ls)],
            ),
            (
                '! as_agent_bare "${SYSBIN}/ls" /x || fail "y"\n',
                [("refusal", agent_ls)],
            ),
            (
                'if as_agent_bare "${SYSBIN}/ls" /x; then\n  fail "y"\n',
                [("refusal", agent_ls)],
            ),
            ('agent_probe "y" "${WORK}/t.pl"\n', [("refusal", perl)]),
            (
                'positive_control "x" 0 \\\n  as_agent_bare "${SYSBIN}/ls" /\n',
                [("control", agent_ls)],
            ),
            ("perl_probe_control\n", [("control", perl)]),
            (
                'out=$(as_agent_bare "${WRAPPERS}/sudo" -n true) || status=$?\n'
                '[ "${status}" -ne 0 ] || fail "y"\n',
                [("refusal", ("as_agent_bare", "WRAPPERS"))],
            ),
            (
                'if out=$(sudo -n systemd-tmpfiles --create); then\n  fail "y"\n',
                [("refusal", ("sudo", "systemd-tmpfiles"))],
            ),
            # Not the agent's or root's probe: the box's own state.
            ('if grep -q "^m " <<<"${mods}"; then\n  fail "y"\n', []),
        ):
            with self.subTest(body=body):
                found = [(kind, probe) for _, kind, probe in probe_sites(body)]
                self.assertEqual(found, sites)


# What runs a probe as the agent or as root, and the probe's program after it.
PROBE_RUNNER_RE = re.compile(r"\b(as_agent_bare|agent_podman|agent_ssh|sudo -n)\b")
PROBE_PROGRAM_RE = re.compile(r"\$\{(SYSBIN|WRAPPERS)\}/([\w.-]+)")
PERL_PROBE = ("as_agent_bare", "SYSBIN/perl")
PROBE_SITE_RES = (
    ("control", re.compile(r"^[ \t]*positive_control \"[^\"]*\" \d+ (.*)$", re.M)),
    ("control", re.compile(r"^[ \t]*(perl_probe_control)$", re.M)),
    ("refusal", re.compile(r"^[ \t]*(agent_probe) ", re.M)),
    ("refusal", re.compile(r"^[ \t]*if \w+=\$\((.*)\); then\n[ \t]*fail ", re.M)),
    (
        "refusal",
        re.compile(
            r"^[ \t]*\w+=\$\((.*)\) \|\| status=\$\?\n"
            r"[ \t]*\[ \"\$\{status\}\" -ne 0 \] \|\| fail ",
            re.M,
        ),
    ),
    ("refusal", re.compile(r"^[ \t]*! (.*) \|\| fail ", re.M)),
    # `if CMD; then fail`, CMD neither an assignment nor a test.
    (
        "refusal",
        re.compile(
            r"^[ \t]*if (?!\w+=\$\()(?![\[!])(.*); then\n[ \t]*fail ",
            re.M,
        ),
    ),
)


def probe_of(command):
    """(runner, program) of a probe's command, or None for a command that is no
    probe of the agent's or root's."""
    if command in ("agent_probe", "perl_probe_control"):
        return PERL_PROBE
    runner = PROBE_RUNNER_RE.search(command)
    if runner is None:
        return None
    after = command[runner.end() :]
    program = PROBE_PROGRAM_RE.search(after)
    if runner.group(1) == "sudo -n":
        return ("sudo", after.split()[0])
    if runner.group(1) != "as_agent_bare":
        return (runner.group(1), runner.group(1).split("_")[1])
    # A wrapper is the path itself: exec'ing any of them is the one question.
    if program.group(1) == "WRAPPERS":
        return ("as_agent_bare", "WRAPPERS")
    return ("as_agent_bare", "SYSBIN/" + program.group(2))


def probe_sites(body):
    """(offset, "control" or "refusal", probe) of each probe in a check's body,
    in order, its continued lines joined."""
    joined = re.sub(r"\\\n[ \t]*", " ", body)
    sites = [
        (match.start(), kind, probe_of(match.group(1)))
        for kind, pattern in PROBE_SITE_RES
        for match in pattern.finditer(joined)
    ]
    return sorted(site for site in sites if site[2] is not None)


class TestCiFailsASkipWhoseToolIsThere(unittest.TestCase):
    """Every tool skip goes through shell_harness.missing_tool_reason, and CI
    turns one whose tools are all on PATH into a failure."""

    def reason(self, forbid, tools, have=None):
        with mock.patch.object(shell_harness, "FORBID_SKIP", forbid):
            return shell_harness.missing_tool_reason(tools, have)

    def test_a_tool_on_path_that_the_probe_judged_missing_fails_under_ci(self):
        with self.assertRaisesRegex(AssertionError, "the probe that judged it"):
            self.reason(True, "sh", have=False)
        self.assertEqual(self.reason(False, "sh", have=False), "needs sh")

    def test_a_tool_that_is_missing_is_still_a_skip_under_ci(self):
        self.assertEqual(self.reason(True, "lizard-yo-test"), "needs lizard-yo-test")
        self.assertIsNone(self.reason(True, "sh"))

    def test_no_test_skips_for_a_tool_but_through_the_harness(self):
        offenders = [
            "%s:%d" % (path.name, line)
            for path in sorted((REPO / "tests").glob("test_*.py"))
            for line in direct_tool_skips(path.read_text())
        ]
        self.assertEqual(offenders, [])

    def test_a_direct_skip_is_seen_however_it_is_spelled(self):
        for source, lines in (
            ("self.skipTest('no git')", [1]),
            ("raise unittest.SkipTest('x')", [1]),
            ("@skipUnless(\n    shutil.which('gawk'), 'x')\ndef f(): pass", [1]),
            ("@skipIf(not is_gnu('find'), 'x')\nclass C: pass", [1]),
            ("@unittest.skipUnless(bash_version() >= (5, 1), 'x')\ndef f(): pass", [1]),
            ("@unittest.skipUnless(HAS_PID_WATCH, 'no kqueue')\ndef f(): pass", []),
            ("@unittest.skipIf(os.geteuid() == 0, 'root')\ndef f(): pass", []),
            ("require_tool('git')", []),
        ):
            with self.subTest(source=source):
                self.assertEqual(direct_tool_skips(source), lines)


# What a skip that judges a tool calls, which needs_tool and require_tool do.
TOOL_PROBES = {"which", "WHICH", "is_gnu", "bash_version"}


def direct_tool_skips(source):
    """The line of each skip in `source` that does not go through the harness:
    any skipTest or SkipTest, and a skipIf or skipUnless that probes a tool.
    A skip for a Python feature or for root is not a tool's."""
    lines = []
    for _, call in calls(source):
        name = called_name(call).rsplit(".", 1)[-1]
        probed = {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(call)
            if isinstance(node, (ast.Name, ast.Attribute))
        }
        if name in ("skipTest", "SkipTest") or (
            name in ("skipIf", "skipUnless") and probed & TOOL_PROBES
        ):
            lines.append(call.lineno)
    return sorted(lines)


class TestNoTestReachesTheOperatorsHome(unittest.TestCase):
    """Every test runs under shell_harness's scratch $HOME, so none reaches the
    operator's real 1Password agent, ~/.ssh, ~/.lima or ~/.local/state."""

    def test_yo_resolves_its_state_under_the_scratch_home(self):
        self.assertTrue(shell_harness.REAL_HOMES)
        for path in (yo.state.state_dir(), yo.state.instance_dir(), yo.LIMA.op_sock()):
            with self.subTest(path=path):
                resolved = Path(path).resolve()
                self.assertTrue(resolved.is_relative_to(shell_harness.SCRATCH_HOME))

    def test_asking_the_real_1password_agent_fails_the_test(self):
        scratch_sock = Path(yo.LIMA.op_sock()).relative_to(shell_harness.SCRATCH_HOME)
        real_sock = Path(shell_harness.REAL_HOMES[0]) / scratch_sock
        with self.assertRaises(shell_harness.RealHomeTouched):
            yo.agent.luks_key(str(real_sock))

    def test_reading_the_real_ssh_config_fails_the_test(self):
        with self.assertRaises(shell_harness.RealHomeTouched):
            (Path(shell_harness.REAL_HOMES[0]) / ".ssh" / "config").read_text()

    @staticmethod
    def outcome(body):
        """The failures and errors of a one-test case whose test runs `body`,
        run under the harness as any test is."""

        class Case(unittest.TestCase):
            def test(self):
                body()

        result = unittest.TestResult()
        Case("test").run(result)
        return [text for _, text in result.failures + result.errors]

    def test_a_thread_that_reaches_the_real_home_fails_its_test(self):
        def touch():
            (Path(shell_harness.REAL_HOMES[0]) / ".ssh" / "config").read_text()

        def body():
            # The thread's traceback is printed; the test is about its verdict.
            with contextlib.redirect_stderr(io.StringIO()):
                thread = threading.Thread(target=touch)
                thread.start()
                thread.join()

        (failure,) = self.outcome(body)
        self.assertIn("a thread reached the real home", failure)

    def test_a_test_that_prints_fails_with_what_it_printed(self):
        for body, said in (
            (lambda: print("on stdout"), "on stdout"),
            (lambda: yo.ui.err("yo: on stderr"), "yo: on stderr"),
            (lambda: os.write(2, b"by fd\n"), "by fd"),
            (lambda: subprocess.run(["echo", "by a child"]), "by a child"),
        ):
            with self.subTest(said=said):
                (failure,) = self.outcome(body)
                self.assertIn("the test printed", failure)
                self.assertIn(said, failure)
        self.assertEqual(self.outcome(lambda: None), [])

    def test_bash_under_shellcase_expands_tilde_to_the_scratch_home(self):
        case = shell_harness.ShellCase()
        case.setUp()
        self.addCleanup(case.doCleanups)
        script = case.tmp / "tilde.sh"
        script.write_text("echo ~\n")
        ran = case.run_bash(script)
        self.assertEqual(ran.stdout, shell_harness.SCRATCH_HOME + "\n")

    def test_ssh_without_a_config_of_its_own_never_runs(self):
        # ssh takes ~ from the account database, so only -F keeps it off ~/.ssh.
        require_tool("ssh")
        refused = subprocess.run(["ssh", "-G", "x"], capture_output=True, text=True)
        self.assertEqual(refused.returncode, 97)
        self.assertIn("ssh without -F reads the real ~/.ssh", refused.stderr)
        ran = subprocess.run(
            ["ssh", "-F", "/dev/null", "-G", "x"], capture_output=True, text=True
        )
        self.assertEqual(ran.returncode, 0, ran.stderr)

    def test_every_test_module_loads_the_harness(self):
        # A module that imports neither runs, alone, under the real $HOME.
        loaders = {"shell_harness", "test_yo"}
        unguarded = [
            path.name
            for path in sorted((REPO / "tests").glob("test_*.py"))
            if not loaders
            & {
                node.module if isinstance(node, ast.ImportFrom) else alias.name
                for node in ast.parse(path.read_text()).body
                if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in node.names
            }
        ]
        self.assertEqual(unguarded, [])


if __name__ == "__main__":
    unittest.main()
