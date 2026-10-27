"""The egress rule files keep the guest off the host and the LAN.

The fixtures are the rendered files, copied from a build in the guest. The
unit tests here run the checker on them and on broken copies. CI runs the same
checker on a fresh render and diffs the fixtures against it (verify.yml), so a
fixture cannot drift from nix/egress.nix.
"""

import unittest
from pathlib import Path

import egress_rules
from test_yo import yo

FIXTURES = Path(__file__).parent / "fixtures"
RULES = (FIXTURES / "egress.nft").read_text()
RECOVERY = (FIXTURES / "egress-recovery.nft").read_text()

FIRST_RULE = 'oifname { "lo", "tailscale0" } accept\n'
V4_REJECT = (
    "    ip daddr { 10.0.0.0/8, 100.64.0.0/10, 169.254.0.0/16, 172.16.0.0/12,"
    " 192.168.0.0/16, 224.0.0.0/4, 255.255.255.255 } counter reject with icmpx"
    " type admin-prohibited\n"
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
            (", 255.255.255.255 } counter", " } counter"),
            ("fc00::/7, ", ""),
            (", ff00::/8 } counter", " } counter"),
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
        with self.assertRaises(egress_rules.RuleError):
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

    def test_a_missing_reply_accept_fails(self):
        with self.assertRaisesRegex(egress_rules.RuleError, "replies"):
            egress_rules.check_rules(
                broken(RULES, "    ct direction reply accept\n", "")
            )

    def test_the_hook_must_run_before_a_kill_switch_at_minus_10(self):
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
        with self.assertRaises(egress_rules.RuleError):
            egress_rules.check_rules(RECOVERY)

    def test_an_empty_file_fails(self):
        with self.assertRaises(egress_rules.RuleError):
            egress_rules.check_rules("")

    def test_the_table_is_the_one_yo_inspects(self):
        self.assertEqual(egress_rules.TABLE, yo.EGRESS_TABLE)


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
        with self.assertRaises(egress_rules.RuleError):
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
        with self.assertRaises(egress_rules.RuleError):
            egress_rules.check_recovery(RULES)
