"""The egress rule files keep the guest off the host and the LAN.

The fixtures are the rendered files, copied from a build in the guest. The
unit tests here run the checker on them and on broken copies. CI runs the same
checker on a fresh render and diffs the fixtures against it (verify.yml), so a
fixture cannot drift from nix/egress.nix.
"""

import unittest

import egress_rules
from test_yo import FIXTURES, yo

RULES = (FIXTURES / "egress.nft").read_text()
RECOVERY = (FIXTURES / "egress-recovery.nft").read_text()
TUNNEL = (FIXTURES / "egress-tunnel.nft").read_text()

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

    def test_a_tunnel_interface_judged_only_by_its_oif_fails(self):
        # The design of 279f822: a DNAT into the tunnel kept the oif of its
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
        with self.assertRaises(egress_rules.RuleError):
            egress_rules.check_rules(TUNNEL)

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


TUNNEL_FIRST = 'oifname { "lo", "tailscale0", "wg0" } accept\n'
DEFER = "    ct status dnat accept\n"
POST_ACCEPT = 'ct status dnat oifname { "lo", "tailscale0", "wg0" } accept\n'
POST_V4_DROP = (
    "    ct status dnat ip daddr { 10.0.0.0/8, 100.64.0.0/10, 169.254.0.0/16,"
    " 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/4, 255.255.255.255 } counter drop\n"
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
        for old, new in (
            (POST_V4_DROP, POST_V4_DROP.replace("169.254.0.0/16, ", "")),
            (POST_V4_DROP, POST_V4_DROP.replace("ct status dnat ", "")),
            (POST_V4_DROP, POST_V4_DROP.replace("drop", "accept")),
            (POST_V4_DROP, ""),
        ):
            with self.subTest(new=new):
                with self.assertRaises(egress_rules.RuleError):
                    egress_rules.check_tunnel_rules(broken(TUNNEL, old, new))

    def test_postrouting_runs_with_the_output_chain_at_minus_20(self):
        with self.assertRaisesRegex(egress_rules.RuleError, "priority -20"):
            egress_rules.check_tunnel_rules(
                broken(TUNNEL, "postrouting priority -20", "postrouting priority -10")
            )

    def test_the_output_chain_keeps_its_rejects(self):
        with self.assertRaises(egress_rules.RuleError):
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
