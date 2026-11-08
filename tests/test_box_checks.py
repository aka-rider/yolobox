"""tests/box-hardening-check.sh refuses what it must, run for real under bash.

The script's `main` call is cut off and its functions sourced, with the box
paths it names (/run/wrappers/bin, /run/current-system/sw/bin, the facts
file, the cidata binary, its /tmp scratch directories) pointed at a scratch
directory. A fake `sudo` drops sudo's own options and runs the command, so
"as the agent" runs as the test's own account; the fakes for getcap, stat,
systemctl, journalctl, ssh, ssh-add, sshd, getent, id, strace, findmnt, ls
and ps answer what each test bakes in. Each check family gets one run
on a box that meets it, and then the answers that must not pass: a tool
answer that is wrong, or a malformed input. That the checks pass on a real
hardened box is the live run's to prove.

Tools whose absence skips the module: GNU find (`-printf`) and GNU stat (`-c`).
"""

import os
import shlex
import shutil
import subprocess
import unittest

from shell_harness import REPO, ShellCase, facts_sh, is_gnu

SCRIPT = REPO / "tests" / "box-hardening-check.sh"

# The script's constants and literals the harness points into the scratch
# directory, each expected exactly once.
FACTS_LINE = "readonly FACTS=/etc/yolobox/facts.sh"
WRAPPERS_LINE = "readonly WRAPPERS=/run/wrappers/bin"
SYSBIN_LINE = "readonly SYSBIN=/run/current-system/sw/bin"
CIDATA_BINARY = "/mnt/lima-cidata/lima-guestagent"
WORK_TEMPLATE = "/tmp/hk-check.XXXXXX"
MAIN_CALL = 'main "$@" </dev/null\n'

FAKE_SUDO = """#!/bin/sh
while [ $# -gt 0 ]; do
    case $1 in
    -n) shift ;;
    -u | -iu) shift 2 ;;
    *) break ;;
    esac
done
exec "$@"
"""

# What nix/hardening.nix adds to /etc/yolobox/facts.sh beside facts.json's.
FACTS = {
    "YOLOBOX_AGENT_GROUPS": "users",
    "YOLOBOX_WRAPPERS": "newgidmap=cap_setgid,cap_setpcap newuidmap=cap_setpcap,cap_setuid sudo= sudoedit= unix_chkpwd=",
    "YOLOBOX_SYSCTLS": "kernel.kptr_restrict=2",
    "YOLOBOX_KERNEL_PARAMS": "slab_nomerge",
    "YOLOBOX_PROC_OPTIONS": "nosuid noexec nodev hidepid=invisible gid=1",
}

# The real tools the agent-side helpers run with PATH set to the fake bin.
REAL_TOOLS = (
    "env",
    "bash",
    "mktemp",
    "rm",
    "kill",
    "mkdir",
    "touch",
    "mv",
    "ln",
)

OPERATOR = "op"
ALLOWUSERS = "not allowed because not listed in AllowUsers"


class BoxCheck(ShellCase):
    """The harness: the script's functions, fakes on PATH, a snippet to run."""

    timeout = 60

    def setUp(self):
        if not (is_gnu("find") and is_gnu("stat")):
            self.skipTest("needs GNU find and stat")
        super().setUp()
        # A symlink, as /run/wrappers/bin is on the box (to the generation's
        # directory): find does not follow a start point without a trailing slash.
        (self.tmp / "wrappers.real").mkdir()
        self.wrappers = self.tmp / "wrappers"
        self.wrappers.symlink_to(self.tmp / "wrappers.real")
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.facts = self.tmp / "facts.sh"
        self.write_facts(FACTS)
        self.fake("sudo", FAKE_SUDO)
        for tool in REAL_TOOLS:
            real = shutil.which(tool)
            if real is None:
                self.skipTest("needs %s" % tool)
            (self.bin_dir / tool).symlink_to(real)
        self.real_stat = shutil.which("stat")

    def write_facts(self, facts):
        """The box's facts file: facts.json's, then `facts`, hardening.nix's."""
        self.facts.write_text(
            facts_sh()
            + "".join("%s=%s\n" % (key, shlex.quote(v)) for key, v in facts.items())
        )

    def functions(self):
        text = SCRIPT.read_text()
        for old, new in (
            (FACTS_LINE, "readonly FACTS=%s" % self.facts),
            (WRAPPERS_LINE, "readonly WRAPPERS=%s" % self.wrappers),
            (SYSBIN_LINE, "readonly SYSBIN=%s" % self.bin_dir),
            (CIDATA_BINARY, str(self.tmp / "lima-guestagent")),
            (WORK_TEMPLATE, str(self.tmp / "hk-check.XXXXXX")),
            (MAIN_CALL, ""),
        ):
            self.assertEqual(text.count(old), 1, old)
            text = text.replace(old, new)
        return text

    def run_check(self, snippet, env=None):
        program = "%s\nload_facts\nAGENT_HOME=%s\n%s\n" % (
            self.functions(),
            self.home,
            snippet,
        )
        return subprocess.run(
            ["bash", "-c", program],
            capture_output=True,
            text=True,
            env=self.environment(env or {}),
            timeout=60,
        )

    def environment(self, overrides):
        """The fake bin first on PATH, the scratch home; `overrides` win."""
        env = dict(
            os.environ,
            PATH=self.env["PATH"],
            HOME=str(self.home),
        )
        env.update(overrides)
        return env

    def assert_fail(self, result, text):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL: " + text, result.stderr)

    # -- the facts file --------------------------------------------------

    def test_a_missing_facts_file_fails(self):
        self.facts.unlink()
        self.assert_fail(self.run_check("true"), "%s is missing" % self.facts)

    def test_a_facts_file_without_a_key_fails(self):
        for key in FACTS:
            with self.subTest(key=key):
                self.write_facts({k: v for k, v in FACTS.items() if k != key})
                self.assert_fail(
                    self.run_check("true"), "%s does not set %s" % (self.facts, key)
                )

    # -- the wrapper set -----------------------------------------------

    def wrapper(self, name, mode):
        path = self.wrappers / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(mode)

    def box_wrappers(self, caps=None, owner="root"):
        """The five kept wrappers with their modes, plus fake getcap and stat.

        `caps` maps a name to what getcap prints after the path; the kept
        capability wrappers are filled in. stat reports `owner`, wheel for
        sudo's pair, and each file's real mode.
        """
        for name in ("sudo", "sudoedit"):
            self.wrapper(name, 0o4510)
        self.wrapper("unix_chkpwd", 0o4511)
        self.wrapper("newuidmap", 0o511)
        self.wrapper("newgidmap", 0o511)
        texts = {
            "newuidmap": " cap_setuid,cap_setpcap=ep",
            "newgidmap": " cap_setgid,cap_setpcap=ep",
        }
        texts.update(caps or {})
        cases = "".join(
            '*/%s) printf "%%s%s\\n" "$1" ;;\n' % (name, text)
            for name, text in texts.items()
        )
        self.fake("getcap", "#!/bin/sh\ncase $1 in\n%sesac\nexit 0\n" % cases)
        self.fake(
            "stat",
            "#!/bin/sh\n"
            "case $3 in */sudo | */sudoedit) group=wheel ;; *) group=root ;; esac\n"
            'printf "%s %s %s\\n" "'
            + owner
            + '" "$group" "$('
            + self.real_stat
            + ' -c %a "$3")"\n',
        )

    def test_a_capability_text_without_names_fails(self):
        # "=ep" grants every capability; a check that read it as none would pass.
        self.box_wrappers(caps={"unix_chkpwd": " =ep"})
        result = self.run_check("check_wrappers")
        self.assert_fail(result, "getcap %s/unix_chkpwd printed" % self.wrappers)

    def test_other_capabilities_than_the_box_states_fail(self):
        self.box_wrappers(
            caps={"newuidmap": " cap_setuid,cap_setpcap,cap_sys_admin=ep"}
        )
        result = self.run_check("check_wrappers")
        self.assert_fail(result, "wrapper newuidmap has capabilities")

    def test_a_capability_wrapper_that_is_also_setuid_fails(self):
        self.box_wrappers()
        (self.wrappers / "newuidmap").chmod(0o4511)
        result = self.run_check("check_wrappers")
        self.assert_fail(result, "wrapper newuidmap is setuid on top")

    def test_a_setgid_wrapper_fails(self):
        self.box_wrappers()
        (self.wrappers / "unix_chkpwd").chmod(0o6511)
        result = self.run_check("check_wrappers")
        self.assert_fail(result, "wrapper unix_chkpwd is setgid")

    def test_a_sudo_executable_by_others_fails(self):
        self.box_wrappers()
        (self.wrappers / "sudo").chmod(0o4511)
        result = self.run_check("check_wrappers")
        self.assert_fail(result, "sudo is mode 4511")

    def test_a_wrapper_not_owned_by_root_fails(self):
        self.box_wrappers(owner="nobody")
        result = self.run_check("check_wrappers")
        self.assert_fail(result, "wrapper newgidmap is owned by nobody")

    def test_an_unexpected_wrapper_fails(self):
        self.box_wrappers()
        self.wrapper("su", 0o4511)
        result = self.run_check("check_wrappers")
        self.assert_fail(result, "%s differs from the kept set" % self.wrappers)

    def test_a_wrapper_directory_with_nothing_in_it_fails_closed(self):
        result = self.run_check("check_wrappers")
        self.assert_fail(result, "found no wrappers under %s" % self.wrappers)

    # -- sudo and the accounts -----------------------------------------

    def test_sudo_that_runs_for_the_agent_fails(self):
        (self.wrappers / "sudo").write_text("#!/bin/sh\nexit 0\n")
        (self.wrappers / "sudo").chmod(0o755)
        self.assert_fail(
            self.run_check("check_sudo_refuses_agent"), "the agent ran sudo"
        )

    def accounts(self, root="!", agent="!", groups="users"):
        self.fake(
            "getent",
            '#!/bin/sh\ncase $2 in root) printf "root:%s:1::::::\\n" ;; agent) printf "agent:%s:1::::::\\n" ;; esac\n'
            % (root, agent),
        )
        self.fake(
            "id",
            '#!/bin/sh\ncase $1 in -Gn) printf "%s\\n" ;; -u) printf "1000\\n" ;; esac\n'
            % groups,
        )

    def test_a_password_hash_on_root_fails(self):
        self.accounts(root="$6$salt$hash")
        self.assert_fail(
            self.run_check("check_accounts_locked"),
            "the password of root is not locked",
        )

    def test_a_group_the_box_does_not_state_fails(self):
        self.accounts(groups="users wheel")
        self.assert_fail(
            self.run_check("check_accounts_locked"), "agent is in groups 'users wheel'"
        )

    # -- /proc hidepid --------------------------------------------------

    PROC_OPTIONS = "rw,nosuid,nodev,noexec,relatime,hidepid=invisible,gid=1"

    def proc(self, options=PROC_OPTIONS, ls="enoent", ps_users=("agent",)):
        self.fake("findmnt", '#!/bin/sh\nprintf "%s\\n"\n' % options)
        self.fake(
            "ls",
            {
                "enoent": '#!/bin/sh\nprintf "ls: cannot access %s: No such file or directory\\n" "$1" >&2\nexit 2\n',
                "eacces": '#!/bin/sh\nprintf "ls: cannot open directory %s: Permission denied\\n" "$1" >&2\nexit 2\n',
                "lists": '#!/bin/sh\nprintf "cmdline\\nenviron\\n"\n',
            }[ls],
        )
        self.fake(
            "ps",
            "#!/bin/sh\n" + "".join('printf "%-8s\\n"\n' % u for u in ps_users),
        )

    def test_a_missing_findmnt_fails(self):
        # PATH holds the fake bin alone, so no findmnt of the host's steps in.
        self.proc()
        (self.bin_dir / "findmnt").unlink()
        self.assert_fail(
            self.run_check("check_hidepid", env={"PATH": str(self.bin_dir)}),
            "findmnt -no OPTIONS /proc failed",
        )

    def test_an_empty_options_string_fails(self):
        self.proc(options="")
        self.assert_fail(
            self.run_check("check_hidepid"), "findmnt printed no options for /proc"
        )

    def test_a_proc_without_hidepid_fails(self):
        self.proc(options="rw,nosuid,nodev,noexec,relatime")
        self.assert_fail(
            self.run_check("check_hidepid"),
            "/proc is mounted without hidepid=invisible",
        )

    def test_a_proc_with_another_gid_fails(self):
        self.proc(options="rw,nosuid,nodev,noexec,relatime,hidepid=invisible,gid=100")
        self.assert_fail(
            self.run_check("check_hidepid"), "/proc is mounted without gid=1"
        )

    def test_an_agent_that_lists_pid_1_fails(self):
        self.proc(ls="lists")
        self.assert_fail(self.run_check("check_hidepid"), "the agent listed /proc/1")

    def test_a_pid_that_is_visible_but_unreadable_fails(self):
        self.proc(ls="eacces")
        self.assert_fail(
            self.run_check("check_hidepid"),
            "ls /proc/1 as the agent failed, but not with ENOENT",
        )

    def test_a_ps_that_shows_another_account_fails(self):
        self.proc(ps_users=("agent", "root"))
        self.assert_fail(
            self.run_check("check_hidepid"),
            "ps as the agent lists the processes of 'agent root'",
        )

    def test_a_ps_that_shows_nothing_fails(self):
        self.proc(ps_users=())
        self.assert_fail(
            self.run_check("check_hidepid"),
            "ps as the agent lists the processes of ''",
        )

    # -- the agent's session services under hidepid ----------------------

    def session_services(self, herdr="active", loginctl_rc=0, busctl_rc=0):
        self.fake(
            "systemctl",
            '#!/bin/sh\nprintf "%s\\n"\n[ "%s" = active ]\n' % (herdr, herdr),
        )
        self.fake("loginctl", "#!/bin/sh\nexit %d\n" % loginctl_rc)
        self.fake("busctl", "#!/bin/sh\nexit %d\n" % busctl_rc)

    def test_a_herdr_server_that_is_not_active_fails(self):
        self.session_services(herdr="failed")
        self.assert_fail(
            self.run_check("check_agent_session_services"),
            "herdr-server is failed under the agent's user manager",
        )

    def test_a_logind_that_lost_the_agent_fails(self):
        self.session_services(loginctl_rc=1)
        self.assert_fail(
            self.run_check("check_agent_session_services"),
            "loginctl user-status agent failed as the agent",
        )

    def test_a_system_bus_that_refuses_the_agent_fails(self):
        self.session_services(busctl_rc=1)
        self.assert_fail(
            self.run_check("check_agent_session_services"),
            "busctl status failed as the agent",
        )

    # -- the 1Password proxy, sshd, containers ---------------------------

    def proxy(self, protect_system="strict", private_network="yes", keys_rc=0):
        self.fake(
            "systemctl",
            "#!/bin/sh\n"
            'printf "ProtectSystem=%s\\nProtectHome=yes\\nPrivateNetwork=%s\\nRestrictAddressFamilies=AF_UNIX\\n"\n'
            % (protect_system, private_network),
        )
        self.fake("ssh-add", "#!/bin/sh\nexit %d\n" % keys_rc)

    def test_a_proxy_with_a_network_fails(self):
        self.proxy(private_network="no")
        self.assert_fail(
            self.run_check("check_proxy_confined"),
            "yolobox-op-agent.service has PrivateNetwork=no, want yes",
        )

    def test_a_dead_socket_behind_the_proxy_fails(self):
        self.proxy(keys_rc=2)
        self.assert_fail(self.run_check("check_proxy_confined"), "ssh-add -L through")

    def test_sshd_that_permits_root_login_fails(self):
        self.fake(
            "sshd",
            '#!/bin/sh\nprintf "PermitRootLogin prohibit-password\\nSubsystem sftp internal-sftp\\n"\n',
        )
        self.assert_fail(
            self.run_check("check_sshd_settings"), "sshd -T lacks 'permitrootlogin no'"
        )

    def podman(self, gateway_rc):
        # nc's last two words are the host and port.
        self.fake(
            "podman",
            "#!/bin/sh\n"
            "for a; do host=$port; port=$a; done\n"
            "case $host in 192.168.5.2) exit %d ;; *) exit 0 ;; esac\n" % gateway_rc,
        )

    def test_a_container_that_reaches_the_gateway_fails(self):
        self.podman(gateway_rc=0)
        self.assert_fail(
            self.run_check("check_container_egress"),
            "a rootless container connected to 192.168.5.2:22",
        )

    # -- link kinds -----------------------------------------------------

    def link_tools(self, refusal):
        # unshare runs the command after its options; ip parses as iproute2
        # does (`link DEV` only before the name: after `type KIND` it is the
        # kind's unknown option, a usage error) and refuses every kind but the
        # bridge; lsmod lists nothing.
        self.fake("unshare", '#!/bin/sh\nshift\nexec "$@"\n')
        self.fake(
            "ip",
            "#!/bin/sh\n"
            "for a; do\n"
            '  if [ -n "$kind" ] && [ "$a" = link ]; then\n'
            '    printf \'%%s: unknown command "link"?\\n\' "$kind" >&2; exit 255\n'
            "  fi\n"
            "  case $prev in type) kind=$a ;; esac; prev=$a\n"
            "done\n"
            'case $kind in bridge) exit 0 ;; *) printf "%s\\n" >&2; exit 2 ;; esac\n'
            % refusal,
        )
        self.fake("lsmod", "#!/bin/sh\nexit 0\n")

    def test_a_link_kind_refused_for_its_options_fails(self):
        self.link_tools('Error: argument "id" is required.')
        self.assert_fail(
            self.run_check("check_link_kinds"),
            "ip link add type vxlan failed, but not with the kernel refusing the kind",
        )

    # -- tmpfiles and the agent's symlink -------------------------------

    def test_tmpfiles_that_follows_the_symlink_fails_and_still_restores(self):
        self.fake("systemd-tmpfiles", "#!/bin/sh\nexit 0\n")
        result = self.run_check(
            "stage_helpers; trap cleanup EXIT; check_tmpfiles_refuses_agent_symlink"
        )
        self.assert_fail(result, "systemd-tmpfiles --create succeeded")
        self.assertFalse((self.home / ".config" / "zed").exists())

    # -- the logins off the gateway --------------------------------------

    def ssh_fakes(
        self, out="Permission denied (publickey).", rc=255, keys_rc=0, logs_in=()
    ):
        """ssh refuses every target with `out` and `rc`; the user@addr
        targets in `logs_in` log in instead."""
        self.fake(
            "ssh",
            "#!/bin/sh\n"
            "for a; do case $a in *@*) target=$a ;; esac; done\n"
            "case $target in\n"
            + "".join("%s) exit 0 ;;\n" % target for target in logs_in)
            + '*) printf "%s\\n" >&2; exit %d ;;\n' % (out, rc)
            + "esac\n",
        )
        self.fake(
            "ssh-add",
            '#!/bin/sh\nprintf "ssh-ed25519 AAAA key\\n"\nexit %d\n' % keys_rc,
        )

    def journal(self, lines):
        self.fake(
            "journalctl",
            "#!/bin/sh\n"
            'case " $* " in\n'
            '*" --sync "*) exit 0 ;;\n'
            '*" --show-cursor "*) printf "Accepted publickey\\n-- cursor: s=1;i=2\\n" ;;\n'
            '*" --after-cursor="*) printf "%s\\n" ;;\n'
            "esac\n" % "\\n".join(lines),
        )

    PROBED = [
        (user, addr)
        for user in (OPERATOR, "agent")
        for addr in ("127.0.0.1", "192.168.5.15")
    ]

    def refusals(self, but=None):
        return [
            "User %s from %s %s" % (user, addr, ALLOWUSERS)
            for user, addr in self.PROBED
            if (user, addr) != but
        ]

    def test_a_refusal_the_journal_does_not_explain_fails(self):
        for user, addr in self.PROBED:
            with self.subTest(user=user, addr=addr):
                self.ssh_fakes()
                self.journal(
                    self.refusals(but=(user, addr))
                    + ["Connection closed by authenticating user %s %s" % (user, addr)]
                )
                result = self.run_check(
                    "check_logins_refused_off_gateway %s" % OPERATOR
                )
                self.assert_fail(
                    result,
                    "sshd refused %s at %s, but its journal does not say AllowUsers"
                    % (user, addr),
                )

    def test_a_login_from_the_agent_fails(self):
        for user, addr in self.PROBED:
            with self.subTest(user=user, addr=addr):
                self.ssh_fakes(logs_in=("%s@%s" % (user, addr),))
                self.journal(self.refusals())
                result = self.run_check(
                    "check_logins_refused_off_gateway %s" % OPERATOR
                )
                self.assert_fail(
                    result, "the agent logged in as %s at %s" % (user, addr)
                )

    def test_a_connection_closed_before_authentication_fails(self):
        self.ssh_fakes(out="Connection closed by 127.0.0.1 port 22")
        self.journal(self.refusals())
        result = self.run_check("check_logins_refused_off_gateway %s" % OPERATOR)
        self.assert_fail(
            result, "ssh as op at 127.0.0.1 failed, but not with Permission denied"
        )

    def test_an_unreachable_ssh_agent_fails(self):
        self.ssh_fakes(keys_rc=2)
        result = self.run_check("check_logins_refused_off_gateway %s" % OPERATOR)
        self.assert_fail(result, "ssh-add -L exited 2")

    # -- the agent's own authorized_keys -------------------------------

    def agent_sshd_config(
        self,
        keys_file="none",
        command="/etc/ssh/agent-authorized-keys",
        command_user="root",
        owner_mode="root 555",
    ):
        self.fake(
            "sshd",
            "#!/bin/sh\n"
            # OpenSSH 10's canonical case, which the check reads case-insensitively.
            'printf "AuthorizedKeysFile %s\\nAuthorizedKeysCommand %s\\nAuthorizedKeysCommandUser %s\\n"\n'
            % (keys_file, command, command_user),
        )
        self.fake("stat", '#!/bin/sh\nprintf "%s\\n"\n' % owner_mode)

    def test_sshd_that_reads_the_agents_authorized_keys_fails(self):
        self.agent_sshd_config(keys_file=".ssh/authorized_keys")
        self.assert_fail(
            self.run_check("check_agent_authorized_keys_ignored"),
            "sshd -T for agent from 192.168.5.2 lacks 'authorizedkeysfile none'",
        )

    def test_a_command_run_as_the_agent_fails(self):
        self.agent_sshd_config(command_user="agent")
        self.assert_fail(
            self.run_check("check_agent_authorized_keys_ignored"),
            "sshd -T for agent from 192.168.5.2 lacks 'authorizedkeyscommanduser root'",
        )

    def test_a_command_the_agent_could_write_fails(self):
        self.agent_sshd_config(owner_mode="agent 755")
        self.assert_fail(
            self.run_check("check_agent_authorized_keys_ignored"),
            "/etc/ssh/agent-authorized-keys is agent 755, want root 555",
        )

    # -- the Lima guest agent -------------------------------------------

    def unit(
        self,
        load="loaded",
        active="inactive",
        result="no",
        timestamp="Mon 2026-01-05 10:00:00 UTC",
    ):
        self.fake(
            "systemctl",
            "#!/bin/sh\n"
            "case $3 in\n"
            'LoadState) printf "%s\\n" ;;\n'
            'ActiveState) printf "%s\\n" ;;\n'
            'ConditionResult) printf "%s\\n" ;;\n'
            'ConditionTimestamp) printf "%s\\n" ;;\n'
            "esac\n" % (load, active, result, timestamp),
        )

    def test_a_missing_unit_fails(self):
        # systemctl show prints inactive and ConditionResult=no for any name.
        self.unit(load="not-found", timestamp="")
        self.assert_fail(
            self.run_check("check_guestagent"), "lima-guestagent.service is not loaded"
        )

    def test_a_condition_never_evaluated_fails(self):
        self.unit(timestamp="")
        self.assert_fail(
            self.run_check("check_guestagent"),
            "the cidata has no guest agent, but systemd never evaluated the condition",
        )

    def test_an_inactive_unit_where_the_binary_exists_fails(self):
        (self.tmp / "lima-guestagent").touch()
        self.unit()
        self.assert_fail(
            self.run_check("check_guestagent"),
            "Lima's cidata ships the guest agent, but",
        )

    # -- strace -p ------------------------------------------------------

    def test_a_refused_attach_fails(self):
        self.fake(
            "strace",
            '#!/bin/sh\nprintf "strace: attach: ptrace(PTRACE_ATTACH, %s): Operation not permitted\\n" "$2" >&2\nexit 1\n',
        )
        result = self.run_check(
            "stage_helpers; export WORK; trap cleanup EXIT; check_strace"
        )
        self.assert_fail(result, "strace -p on an agent process did not attach")

    # -- a box that meets each check -----------------------------------

    def assert_passes(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("FAIL", result.stderr)
        self.assertIn("ok: ", result.stdout)

    def test_the_wrapper_set_passes_on_a_box_that_keeps_exactly_it(self):
        self.box_wrappers()
        self.assert_passes(self.run_check("check_wrappers"))

    def test_sudo_that_refuses_the_agent_passes(self):
        (self.wrappers / "sudo").write_text(
            "#!/bin/sh\necho 'sudo: Permission denied' >&2\nexit 1\n"
        )
        (self.wrappers / "sudo").chmod(0o755)
        self.assert_passes(self.run_check("check_sudo_refuses_agent"))

    def test_locked_accounts_in_the_stated_groups_pass(self):
        self.accounts()
        self.assert_passes(self.run_check("check_accounts_locked"))

    def test_a_proc_that_hides_other_pids_from_the_agent_passes(self):
        self.proc()
        self.assert_passes(self.run_check("check_hidepid"))

    def test_session_services_that_answer_the_agent_pass(self):
        self.session_services()
        self.assert_passes(self.run_check("check_agent_session_services"))

    def test_a_confined_proxy_with_a_live_socket_passes(self):
        self.proxy()
        self.assert_passes(self.run_check("check_proxy_confined"))

    def test_sshd_with_internal_sftp_and_no_root_login_passes(self):
        self.fake(
            "sshd",
            '#!/bin/sh\nprintf "PermitRootLogin no\\nSubsystem sftp internal-sftp\\n"\n',
        )
        self.assert_passes(self.run_check("check_sshd_settings"))

    def test_a_container_that_cannot_reach_the_gateway_passes(self):
        self.podman(gateway_rc=1)
        self.assert_passes(self.run_check("check_container_egress"))

    def test_link_kinds_the_kernel_refuses_pass(self):
        self.link_tools("RTNETLINK answers: Operation not supported")
        self.assert_passes(self.run_check("check_link_kinds"))

    def test_logins_off_the_gateway_that_sshd_refuses_pass(self):
        self.ssh_fakes()
        self.journal(self.refusals())
        self.assert_passes(
            self.run_check("check_logins_refused_off_gateway %s" % OPERATOR)
        )

    def test_the_agents_keys_read_from_roots_command_only_pass(self):
        self.agent_sshd_config()
        self.assert_passes(self.run_check("check_agent_authorized_keys_ignored"))

    def test_a_guest_agent_skipped_where_the_cidata_has_none_passes(self):
        self.unit()
        self.assert_passes(self.run_check("check_guestagent"))

    def test_tmpfiles_that_refuses_the_symlink_and_then_runs_clean_passes(self):
        self.fake(
            "systemd-tmpfiles",
            "#!/bin/sh\n"
            'if [ -e "$0.ran" ]; then exit 0; fi\n'
            'touch "$0.ran"\n'
            "echo 'Detected unsafe path transition' >&2\n"
            "exit 1\n",
        )
        self.assert_passes(
            self.run_check(
                "stage_helpers; trap cleanup EXIT; check_tmpfiles_refuses_agent_symlink"
            )
        )

    def test_strace_that_attaches_and_follows_the_target_passes(self):
        self.fake(
            "strace",
            "#!/bin/sh\n"
            'echo "strace: Process $2 attached"\n'
            'echo "+++ exited with 0 +++"\n',
        )
        self.assert_passes(
            self.run_check(
                "stage_helpers; export WORK; trap cleanup EXIT; check_strace"
            )
        )


if __name__ == "__main__":
    unittest.main()
