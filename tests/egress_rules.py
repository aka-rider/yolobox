"""Checks on the rendered egress rule files (nix/egress.md).

The rule files are what the kernel runs, so this reads their text and not the
Nix that builds them. `check_rules`, `check_tunnel_rules` and `check_recovery`
raise `RuleError` at the first property a file lacks. The host has no nix, so
the unit tests feed them fixtures copied from a render
(tests/test_egress_rules.py); CI runs this file against a fresh render, and
diffs the fixtures against it so they cannot drift
(.github/workflows/verify.yml):

    python3 tests/egress_rules.py <egressRules> <egressRecoveryRules> <tunnel egressRules>
"""

import re
import sys

TABLE = "inet yolobox-egress"
HOOK = "type filter hook output priority -20; policy accept;"
POSTROUTING_HOOK = "type filter hook postrouting priority -20; policy accept;"
GATEWAY = "192.168.5.2"

LO_AND_TAILNET = 'oifname { "lo", "tailscale0" } accept'
# lo and the tailnet, then one or more yolobox.network.tunnelInterfaces.
TUNNEL_IFACES = re.compile(
    r'oifname (\{ "lo", "tailscale0"(?:, "[a-zA-Z0-9_.-]{1,15}")+ \}) accept'
)
# With tunnel interfaces the output hook cannot tell where a DNATed packet
# leaves, so it leaves the packet to the postrouting chain (nix/egress.md).
REWRITTEN = "ct status dnat accept"
LO = 'oifname "lo" accept'
REPLY = "ct direction reply accept"
DHCP = "udp sport 68 udp dport 67 ip daddr { 255.255.255.255, %s } accept" % GATEWAY
DNS = [
    "ip daddr %s udp dport 53 accept" % GATEWAY,
    "ip daddr %s tcp dport 53 accept" % GATEWAY,
]
ND = "icmpv6 type { nd-router-solicit, nd-neighbor-solicit, nd-neighbor-advert } accept"
MLD = "icmpv6 type { mld-listener-report, mld2-listener-report } ip6 daddr ff02::/16 accept"
# A lanAllow entry: the user's own opt-in, so any address set but the gateway.
LAN = re.compile(r"ip6? daddr \{ [0-9a-fA-F:./, ]+ \} accept")
REJECT = "counter reject with icmpx type admin-prohibited"

BLOCKED_V4 = {
    "10.0.0.0/8",
    "100.64.0.0/10",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "224.0.0.0/4",
    "255.255.255.255",
}
BLOCKED_V6 = {"fc00::/7", "fe80::/10", "ff00::/8"}


class RuleError(AssertionError):
    pass


def require(condition, message):
    if not condition:
        raise RuleError(message)


def chains(text):
    """{name: [hook line, rules...]} after checking the frame."""
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    require(
        lines[:3] == ["table " + TABLE, "delete table " + TABLE, "table %s {" % TABLE]
        and lines[-1:] == ["}"],
        "the file does not open with the replace-the-table frame",
    )
    found, name, body = {}, None, []
    for line in lines[3:-1]:
        opening = re.fullmatch(r"chain ([\w-]+) \{", line)
        if name is None:
            require(opening is not None, "outside any chain: %s" % line)
            name, body = opening.group(1), []
        elif line == "}":
            found[name] = body
            name = None
        else:
            require(opening is None, "nested chain: %s" % line)
            body.append(line)
    require(name is None, "the file does not close chain %s" % name)
    return found


def chain_rules(found, names):
    """The rules of the output chain, after checking the file has exactly
    `names` and that each hooks at priority -20."""
    require(
        sorted(found) == sorted(names),
        "the chains are %s, not %s" % (sorted(found), sorted(names)),
    )
    hooks = {"output": HOOK, "postrouting": POSTROUTING_HOOK}
    for name in names:
        require(
            found[name][:1] == [hooks[name]],
            "chain %s is not a filter hook at priority -20, policy accept" % name,
        )
    return found["output"][1:]


def blocked_set(rule, family, ending):
    found = re.fullmatch(r"%s daddr \{ ([^}]*) \} %s" % (family, ending), rule)
    require(found is not None, "not the %s rule expected here: %s" % (family, rule))
    return set(found.group(1).split(", "))


def check_rules(text):
    """The full rule file: lo and the tailnet leave, the host and private
    networks are rejected, and only the gateway's DNS and DHCP are let through."""
    rules = chain_rules(chains(text), ["output"])
    require(
        rules[:1] == [LO_AND_TAILNET], "the first rule is not the lo+tailscale0 accept"
    )
    check_output(rules[1:])


def check_tunnel_rules(text):
    """The full rule file with tunnel interfaces: they join lo and the tailnet
    in the first rule, and a DNATed packet is judged in postrouting, by the
    interface it leaves by, against the same interfaces and ranges."""
    found = chains(text)
    rules = chain_rules(found, ["output", "postrouting"])
    first = TUNNEL_IFACES.fullmatch(rules[0]) if rules else None
    require(first is not None, "the first rule is not lo+tailscale0+tunnels accept")
    require(
        rules[1:3] == [REPLY, REWRITTEN],
        "a DNATed packet is not left to postrouting right after the replies",
    )
    check_output(rules[1:2] + rules[3:])
    post = found["postrouting"][1:]
    require(
        post[:2] == [REPLY, "ct status dnat oifname %s accept" % first.group(1)],
        "postrouting does not accept a DNATed packet by the same interfaces",
    )
    require(len(post) == 6, "postrouting is not the interfaces, then the ranges")
    check_blocked(post[2:], "ct status dnat ", "counter drop")


def check_output(accepts_and_tail):
    """The output chain after its first rule."""
    require(len(accepts_and_tail) >= 4, "the file has no reject tail")
    accepts, tail = accepts_and_tail[:-4], accepts_and_tail[-4:]
    require(REPLY in accepts, "replies are not accepted")
    require(
        accepts.count(DHCP) == 1,
        "DHCP is not accepted once, to broadcast and the gateway only",
    )
    require(all(d in accepts for d in DNS), "the gateway's DNS is not accepted")
    for rule in accepts:
        known = rule in [REPLY, DHCP, ND, MLD, *DNS]
        lan = LAN.fullmatch(rule) is not None and GATEWAY not in rule
        require(known or lan, "unexpected rule before the rejects: %s" % rule)
    require(
        [r for r in accepts if "dport 67" in r] == [DHCP],
        "a port 67 rule other than the DHCP one",
    )
    require(
        [r for r in accepts if GATEWAY in r] == [DHCP, *DNS],
        "the gateway is addressed by more than its DHCP and port 53 accepts",
    )
    check_blocked(tail, "", REJECT)


def check_blocked(tail, prefix, verdict):
    """A log and a `verdict` rule for each family, naming exactly the blocked
    ranges."""
    log_v4, final_v4, log_v6, final_v6 = tail
    for family, blocked, log, final in (
        ("ip", BLOCKED_V4, log_v4, final_v4),
        ("ip6", BLOCKED_V6, log_v6, final_v6),
    ):
        for rule, ending in (
            (log, r"limit rate .* log .*"),
            (final, re.escape(verdict)),
        ):
            require(
                rule.startswith(prefix)
                and blocked_set(rule[len(prefix) :], family, ending) == blocked,
                "the %s rule does not name exactly the blocked ranges: %s"
                % (family, rule),
            )


def check_recovery(text):
    """The recovery file: only lo, replies, DHCP and ND, then an unconditional reject."""
    rules = chain_rules(chains(text), ["output"])
    require(
        rules[:-2] == [LO, REPLY, DHCP, ND],
        "recovery accepts something other than lo, replies, DHCP and ND",
    )
    require(
        rules[-2].startswith("limit rate") and " log " in rules[-2],
        "recovery has no rate-limited log before the reject",
    )
    require(rules[-1] == REJECT, "recovery does not end with an unconditional reject")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(
            "usage: egress_rules.py <egressRules> <egressRecoveryRules>"
            " <tunnel egressRules>"
        )
    rules_path, recovery_path, tunnel_path = sys.argv[1:]
    try:
        with open(rules_path) as f:
            check_rules(f.read())
        with open(recovery_path) as f:
            check_recovery(f.read())
        with open(tunnel_path) as f:
            check_tunnel_rules(f.read())
    except RuleError as e:
        sys.exit("egress rules: %s" % e)
    print("egress rules: ok")
