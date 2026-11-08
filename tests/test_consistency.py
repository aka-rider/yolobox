"""Facts spelled in files that cannot read facts.json, and repo-wide rules.

facts.json is the one spelling of every literal yo, the box and the scripts
share. A few files are static and read by a tool that knows nothing of it
(lima's yaml, libvirt's domain template, the Linux host's hook and nft table),
so each class here compares one such file to facts.json or to the constant yo
reads, and a change to one side alone fails here instead of on a user's box.

Tools whose absence skips tests: git (the shellcheck-list test).
"""

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from shell_harness import FACTS, REPO
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
        if shutil.which("git") is None:
            self.skipTest("needs git")
        listing = subprocess.run(
            [
                "git",
                "-C",
                str(REPO),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "*.sh",
            ],
            capture_output=True,
            text=True,
        )
        if listing.returncode != 0:
            self.skipTest("not a git checkout: %s" % listing.stderr.strip())
        self.assertEqual(self.shellchecked(), sorted(listing.stdout.split()))

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

    def test_the_initrd_sshd_global_keyword_comes_before_its_match_block(self):
        # A global keyword after `Match` belongs to the block, and sshd refuses
        # the whole config.
        luks = uncommented(REPO / "nix" / "luks.nix")
        self.assertLess(luks.index("PerSourcePenalties no"), luks.index("Match "))

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


if __name__ == "__main__":
    unittest.main()
