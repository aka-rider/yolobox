"""The egress rule files keep the guest off the host and the LAN.

The fixtures are the rendered files, copied from a build in the guest. The
unit tests here run the checker on them and on broken copies. CI runs the same
checker on a fresh render and diffs the fixtures against it (verify.yml), so a
fixture cannot drift from nix/egress.nix.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import egress_rules
from test_yo import FIXTURES, yo

RULES = (FIXTURES / "egress.nft").read_text()
RECOVERY = (FIXTURES / "egress-recovery.nft").read_text()
LASTRESORT = (FIXTURES / "egress-lastresort.nft").read_text()
TUNNEL = (FIXTURES / "egress-tunnel.nft").read_text()

FIRST_RULE = 'oifname { "lo", "tailscale0" } accept\n'
V4_REJECT = (
    "    ip daddr { 10.0.0.0/8, 100.64.0.0/10, 169.254.0.0/16, 172.16.0.0/12,"
    " 192.0.0.0/24, 192.168.0.0/16, 198.18.0.0/15, 224.0.0.0/4, 240.0.0.0/4 }"
    " counter reject with icmpx type admin-prohibited\n"
)
DHCP = "udp sport 68 udp dport 67 ip daddr { 255.255.255.255, 192.168.5.2 } accept\n"
DNS_UDP = "ip daddr 192.168.5.2 udp dport 53 accept\n"


def broken(text, old, new):
    """`text` with `old` replaced; fails when `old` is absent, so a fixture
    change cannot turn a mutation into a no-op."""
    assert old in text, old
    return text.replace(old, new)


class TestFullRules(unittest.TestCase):
    def test_the_rendered_file_passes(self):
        egress_rules.check_rules(RULES)

    def test_lan_allow_entries_pass(self):
        lan = "ip daddr { 192.168.1.20/32 } accept\n    ip6 daddr { fd00:1::/64 } accept\n"
        egress_rules.check_rules(broken(RULES, "    \n", "    " + lan))

    def test_a_lan_entry_naming_the_gateway_fails(self):
        lan = "ip daddr { 192.168.5.2/32 } accept\n"
        with self.assertRaisesRegex(egress_rules.RuleError, "unexpected rule"):
            egress_rules.check_rules(broken(RULES, "    \n", "    " + lan))

    def test_every_blocked_range_must_be_rejected(self):
        for old, new in (
            ("169.254.0.0/16, ", ""),
            ("198.18.0.0/15, ", ""),
            (", 240.0.0.0/4 } counter", " } counter"),
            ("fc00::/7, ", ""),
            (", 64:ff9b:1::/48 } counter", " } counter"),
            ("ff00::/8, ", ""),
        ):
            with self.subTest(removed=old):
                with self.assertRaisesRegex(egress_rules.RuleError, "blocked ranges"):
                    egress_rules.check_rules(broken(RULES, old, new))

    def test_a_range_nobody_asked_to_block_must_not_appear(self):
        with self.assertRaisesRegex(egress_rules.RuleError, "blocked ranges"):
            egress_rules.check_rules(
                broken(RULES, "10.0.0.0/8,", "10.0.0.0/8, 8.8.8.8,")
            )

    def test_dhcp_without_a_destination_fails(self):
        # The agent may bind port 68, so this would let it send UDP to port 67
        # of the host or the LAN.
        with self.assertRaisesRegex(egress_rules.RuleError, "DHCP"):
            egress_rules.check_rules(
                broken(RULES, DHCP, "udp sport 68 udp dport 67 accept\n")
            )

    def test_the_gateway_takes_dns_and_dhcp_and_nothing_else(self):
        with self.assertRaisesRegex(egress_rules.RuleError, "unexpected rule"):
            egress_rules.check_rules(
                broken(RULES, DNS_UDP, DNS_UDP + "    ip daddr 192.168.5.2 accept\n")
            )
        with self.assertRaisesRegex(egress_rules.RuleError, "DNS"):
            egress_rules.check_rules(
                broken(RULES, DNS_UDP, "ip daddr 192.168.5.2 udp dport 54 accept\n")
            )

    def test_a_reject_before_the_accepts_fails(self):
        moved = broken(RULES, V4_REJECT, "")
        moved = broken(moved, "    " + FIRST_RULE, "    " + FIRST_RULE + V4_REJECT)
        with self.assertRaisesRegex(egress_rules.RuleError, "unexpected rule"):
            egress_rules.check_rules(moved)

    def test_the_lo_and_tailnet_accept_must_come_first(self):
        swapped = broken(RULES, "    " + FIRST_RULE, "")
        swapped = broken(
            swapped,
            "    ct direction reply accept\n",
            "    ct direction reply accept\n    " + FIRST_RULE,
        )
        with self.assertRaisesRegex(egress_rules.RuleError, "first rule"):
            egress_rules.check_rules(swapped)

    def test_a_tunnel_interface_judged_only_by_its_oif_fails(self):
        # The first design: a DNAT into the tunnel kept the oif of its
        # first route, so the accept missed the packets it was for.
        with self.assertRaisesRegex(egress_rules.RuleError, "first rule"):
            egress_rules.check_rules(
                broken(
                    RULES, FIRST_RULE, 'oifname { "lo", "tailscale0", "wg0" } accept\n'
                )
            )

    def test_the_default_file_does_not_wave_dnat_through(self):
        with self.assertRaisesRegex(egress_rules.RuleError, "unexpected rule"):
            egress_rules.check_rules(
                broken(
                    RULES,
                    "    ct direction reply accept\n",
                    "    ct direction reply accept\n    ct status dnat accept\n",
                )
            )

    def test_the_tunnel_file_is_not_the_default_file(self):
        with self.assertRaisesRegex(egress_rules.RuleError, "the chains are"):
            egress_rules.check_rules(TUNNEL)

    def test_a_missing_reply_accept_fails(self):
        with self.assertRaisesRegex(egress_rules.RuleError, "replies"):
            egress_rules.check_rules(
                broken(RULES, "    ct direction reply accept\n", "")
            )

    def test_the_hook_must_run_before_another_table_at_minus_10(self):
        for priority in ("-10", "0", "-200"):
            with self.subTest(priority=priority):
                with self.assertRaisesRegex(egress_rules.RuleError, "priority -20"):
                    egress_rules.check_rules(
                        broken(RULES, "priority -20", "priority " + priority)
                    )

    def test_a_file_that_does_not_replace_the_table_fails(self):
        with self.assertRaisesRegex(egress_rules.RuleError, "frame"):
            egress_rules.check_rules(
                broken(RULES, "delete table inet yolobox-egress\n", "")
            )

    def test_the_recovery_file_is_not_the_full_file(self):
        with self.assertRaisesRegex(egress_rules.RuleError, "the chains are"):
            egress_rules.check_rules(RECOVERY)

    def test_an_empty_file_fails(self):
        with self.assertRaisesRegex(egress_rules.RuleError, "frame"):
            egress_rules.check_rules("")

    def test_the_table_is_the_one_yo_inspects(self):
        self.assertEqual(egress_rules.TABLE, yo.EGRESS_TABLE)


TUNNEL_FIRST = 'oifname { "lo", "tailscale0", "wg0" } accept\n'
DEFER = "    ct status dnat accept\n"
POST_ACCEPT = 'ct status dnat oifname { "lo", "tailscale0", "wg0" } accept\n'
POST_V4_DROP = (
    "    ct status dnat ip daddr { 10.0.0.0/8, 100.64.0.0/10, 169.254.0.0/16,"
    " 172.16.0.0/12, 192.0.0.0/24, 192.168.0.0/16, 198.18.0.0/15, 224.0.0.0/4,"
    " 240.0.0.0/4 } counter drop\n"
)


class TestTunnelRules(unittest.TestCase):
    def test_the_rendered_file_passes(self):
        egress_rules.check_tunnel_rules(TUNNEL)

    def test_the_first_rule_must_keep_lo_and_tailnet(self):
        for rule in (
            'oifname { "tailscale0", "wg0" } accept\n',
            'oifname { "lo", "wg0" } accept\n',
            'oifname { "lo", "tailscale0", "w g" } accept\n',
            FIRST_RULE,
        ):
            with self.subTest(rule=rule):
                with self.assertRaisesRegex(egress_rules.RuleError, "first rule"):
                    egress_rules.check_tunnel_rules(broken(TUNNEL, TUNNEL_FIRST, rule))

    def test_a_dnat_judged_only_in_output_fails(self):
        # Without the postrouting chain a DNS query DNATed into the tunnel is
        # rejected by the oif of its first route (nix/egress.md).
        without = broken(TUNNEL, DEFER, "")
        without = without[: without.index("  chain postrouting")] + "}\n"
        with self.assertRaisesRegex(egress_rules.RuleError, "chains are"):
            egress_rules.check_tunnel_rules(without)
        with self.assertRaisesRegex(egress_rules.RuleError, "postrouting"):
            egress_rules.check_tunnel_rules(broken(TUNNEL, DEFER, ""))

    def test_postrouting_judges_by_the_same_interfaces(self):
        for new in (
            'ct status dnat oifname { "lo", "tailscale0", "wg0", "enp0s1" } accept\n',
            'ct status dnat oifname { "lo", "tailscale0" } accept\n',
            "ct status dnat accept\n",
            'oifname { "lo", "tailscale0", "wg0" } accept\n',
        ):
            with self.subTest(new=new):
                with self.assertRaisesRegex(egress_rules.RuleError, "postrouting"):
                    egress_rules.check_tunnel_rules(broken(TUNNEL, POST_ACCEPT, new))

    def test_postrouting_drops_every_blocked_range(self):
        for old, new, message in (
            (
                POST_V4_DROP,
                POST_V4_DROP.replace("169.254.0.0/16, ", ""),
                "exactly the blocked ranges",
            ),
            (
                POST_V4_DROP,
                POST_V4_DROP.replace("ct status dnat ", ""),
                "exactly the blocked ranges",
            ),
            (
                POST_V4_DROP,
                POST_V4_DROP.replace("drop", "accept"),
                "not the ip rule",
            ),
            (POST_V4_DROP, "", "interfaces, then the ranges"),
        ):
            with self.subTest(new=new):
                with self.assertRaisesRegex(egress_rules.RuleError, message):
                    egress_rules.check_tunnel_rules(broken(TUNNEL, old, new))

    def test_postrouting_runs_with_the_output_chain_at_minus_20(self):
        with self.assertRaisesRegex(egress_rules.RuleError, "priority -20"):
            egress_rules.check_tunnel_rules(
                broken(TUNNEL, "postrouting priority -20", "postrouting priority -10")
            )

    def test_the_output_chain_keeps_its_rejects(self):
        with self.assertRaisesRegex(egress_rules.RuleError, "not the ip rule"):
            egress_rules.check_tunnel_rules(broken(TUNNEL, V4_REJECT, ""))


class TestRecoveryRules(unittest.TestCase):
    def test_the_rendered_file_passes(self):
        egress_rules.check_recovery(RECOVERY)

    def test_accepting_the_tailnet_fails(self):
        with self.assertRaisesRegex(egress_rules.RuleError, "recovery accepts"):
            egress_rules.check_recovery(
                broken(
                    RECOVERY,
                    'oifname "lo" accept',
                    'oifname { "lo", "tailscale0" } accept',
                )
            )

    def test_accepting_dns_fails(self):
        with self.assertRaisesRegex(egress_rules.RuleError, "recovery accepts"):
            egress_rules.check_recovery(
                broken(
                    RECOVERY,
                    "    ct direction reply accept\n",
                    "    ct direction reply accept\n    " + DNS_UDP,
                )
            )

    def test_dhcp_without_a_destination_fails(self):
        with self.assertRaisesRegex(egress_rules.RuleError, "recovery accepts"):
            egress_rules.check_recovery(
                broken(RECOVERY, DHCP, "udp sport 68 udp dport 67 accept\n")
            )

    def test_a_conditional_final_reject_fails(self):
        with self.assertRaisesRegex(egress_rules.RuleError, "unconditional"):
            egress_rules.check_recovery(
                broken(
                    RECOVERY,
                    "    counter reject",
                    "    ip daddr 10.0.0.0/8 counter reject",
                )
            )

    def test_a_chain_that_does_not_end_in_a_reject_fails(self):
        with self.assertRaisesRegex(egress_rules.RuleError, "recovery accepts"):
            egress_rules.check_recovery(
                broken(
                    RECOVERY,
                    "    counter reject with icmpx type admin-prohibited\n",
                    "",
                )
            )

    def test_the_wrong_priority_fails(self):
        with self.assertRaisesRegex(egress_rules.RuleError, "priority -20"):
            egress_rules.check_recovery(
                broken(RECOVERY, "priority -20", "priority -10")
            )

    def test_the_full_file_is_not_the_recovery_file(self):
        with self.assertRaisesRegex(egress_rules.RuleError, "the chains are"):
            egress_rules.check_recovery(RULES)


class TestLastResortRules(unittest.TestCase):
    def test_the_rendered_file_passes(self):
        egress_rules.check_lastresort(LASTRESORT)

    def test_it_drops_by_policy(self):
        with self.assertRaisesRegex(egress_rules.RuleError, "policy drop"):
            egress_rules.check_lastresort(
                broken(LASTRESORT, "policy drop", "policy accept")
            )

    def test_the_wrong_priority_fails(self):
        with self.assertRaisesRegex(egress_rules.RuleError, "priority -20"):
            egress_rules.check_lastresort(
                broken(LASTRESORT, "priority -20", "priority -10")
            )

    def test_anything_beyond_lo_established_flows_and_dhcp_fails(self):
        for name, extra in (
            ("the tailnet", 'oifname "tailscale0" accept'),
            ("dns", "ip daddr 192.168.5.2 udp dport 53 accept"),
            ("a reject", "counter reject with icmpx type admin-prohibited"),
            ("a log", 'log prefix "yolobox-egress: " level warn'),
            ("a rate limit", "limit rate 6/minute accept"),
            ("a reply match the recovery tier uses", "ct direction reply accept"),
        ):
            with self.subTest(extra=name):
                with self.assertRaisesRegex(
                    egress_rules.RuleError, "last-resort tier accepts"
                ):
                    egress_rules.check_lastresort(
                        broken(
                            LASTRESORT,
                            "    udp sport 68",
                            "    " + extra + "\n    udp sport 68",
                        )
                    )

    def test_a_missing_accept_fails(self):
        for rule in (
            'oifname "lo" accept\n',
            "ct state established,related accept\n",
            "udp sport 68 udp dport 67 accept\n",
        ):
            with self.subTest(missing=rule):
                with self.assertRaisesRegex(
                    egress_rules.RuleError, "last-resort tier accepts"
                ):
                    egress_rules.check_lastresort(broken(LASTRESORT, "    " + rule, ""))

    def test_a_file_that_does_not_replace_the_table_fails(self):
        with self.assertRaisesRegex(egress_rules.RuleError, "frame"):
            egress_rules.check_lastresort(
                broken(LASTRESORT, "delete table inet yolobox-egress\n", "")
            )

    def test_the_other_tiers_are_not_the_last_resort_file(self):
        for name, text in (("full", RULES), ("recovery", RECOVERY), ("tunnel", TUNNEL)):
            with self.subTest(tier=name):
                with self.assertRaisesRegex(egress_rules.RuleError, "the chains are"):
                    egress_rules.check_lastresort(text)

    def test_each_tier_has_a_chain_name_of_its_own(self):
        names = [
            egress_rules.chains(text).keys() for text in (RULES, RECOVERY, LASTRESORT)
        ]
        flat = [name for keys in names for name in keys]
        self.assertEqual(len(flat), len(set(flat)), flat)

    def test_the_last_resort_file_is_not_the_recovery_file(self):
        with self.assertRaisesRegex(egress_rules.RuleError, "the chains are"):
            egress_rules.check_recovery(LASTRESORT)


NFT_FAKE = """#!/bin/sh
echo "nft $*" >> "$FAKE_LOG"
case "$1" in
  --file)
    case " $FAKE_FAILING " in *" $2 "*) echo "Error: could not process rule" >&2; exit 1 ;; esac
    ;;
  list)
    if [ -z "${FAKE_LISTING:-}" ]; then
      echo "Error: No such file or directory" >&2
      exit 1
    fi
    printf '%s\\n' "$FAKE_LISTING"
    ;;
esac
"""


class EgressScript(unittest.TestCase):
    """The two helpers of yolobox-egress.service, run for real under bash with
    the errexit/nounset/pipefail writeShellApplication adds and a fake nft."""

    DIR = Path(__file__).resolve().parent.parent / "nix" / "egress"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        nft = bin_dir / "nft"
        nft.write_text(NFT_FAKE)
        nft.chmod(0o755)
        self.log = self.tmp / "log"
        self.log.touch()
        self.env = {
            "PATH": os.pathsep.join(
                [str(bin_dir), os.environ.get("PATH", "/usr/bin:/bin")]
            ),
            "FAKE_LOG": str(self.log),
        }

    def run_script(self, name, *args, **env):
        return subprocess.run(
            [
                "bash",
                "-o",
                "errexit",
                "-o",
                "nounset",
                "-o",
                "pipefail",
                str(self.DIR / name),
                *args,
            ],
            capture_output=True,
            text=True,
            env=dict(self.env, **env),
            timeout=20,
        )

    def nft_calls(self):
        return self.log.read_text().splitlines()


class TestLoadTiers(EgressScript):
    def load(self, **env):
        return self.run_script(
            "yolobox-egress-load-tiers.sh", "recovery.nft", "lastresort.nft", **env
        )

    def test_the_recovery_table_is_loaded_alone_when_it_loads(self):
        result = self.load()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.nft_calls(), ["nft --file recovery.nft"])

    def test_the_last_resort_table_follows_a_recovery_table_that_will_not_load(self):
        result = self.load(FAKE_FAILING="recovery.nft")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.nft_calls(),
            ["nft --file recovery.nft", "nft --file lastresort.nft"],
        )
        self.assertIn(
            "<3>yolobox-egress: the recovery table failed to load", result.stderr
        )
        self.assertIn("last-resort table is in force", result.stderr)

    def test_no_table_at_all_fails_the_script_so_the_unit_fails(self):
        result = self.load(FAKE_FAILING="recovery.nft lastresort.nft")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(
            self.nft_calls(),
            ["nft --file recovery.nft", "nft --file lastresort.nft"],
        )
        self.assertNotIn("is in force", result.stderr)

    def test_without_both_files_it_prints_usage_and_loads_nothing(self):
        for args in ((), ("only-one",), ("a", "b", "c")):
            with self.subTest(args=args):
                result = self.run_script("yolobox-egress-load-tiers.sh", *args)
                self.assertEqual(result.returncode, 2)
                self.assertIn("usage", result.stderr)
                self.assertEqual(self.nft_calls(), [])


class TestReportFailure(EgressScript):
    def report(self, listing):
        return self.run_script("yolobox-egress-failed.sh", FAKE_LISTING=listing)

    def test_each_tier_in_force_is_named_at_the_critical_level(self):
        for chain, wanted in (
            ("output_lastresort", "last-resort table is in force"),
            ("output_recovery", "recovery table is in force"),
        ):
            with self.subTest(chain=chain):
                result = self.report(
                    "table inet yolobox-egress {\n\tchain %s {\n" % chain
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(
                    "<2>yolobox-egress: the egress rules failed to load", result.stderr
                )
                self.assertIn(wanted, result.stderr)
                self.assertNotIn("NO TABLE", result.stderr)

    def test_a_failed_unit_over_a_loaded_full_table_is_not_called_a_fallback(self):
        result = self.report("table inet yolobox-egress {\n\tchain output {\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("neither fallback chain", result.stderr)

    def test_no_table_is_said_in_capitals_with_what_nft_answered(self):
        result = self.report("")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("NO TABLE IS LOADED", result.stderr)
        self.assertIn("can reach the host and the LAN", result.stderr)
        self.assertIn("No such file or directory", result.stderr)

    def test_every_case_ends_with_the_way_back(self):
        for listing in ("", "chain output_recovery {", "chain output {"):
            with self.subTest(listing=listing):
                result = self.report(listing)
                self.assertIn("nixos-rebuild switch --rollback", result.stderr)
