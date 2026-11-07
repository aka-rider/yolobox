"""tests/box-hardening-check.sh's checks, run for real under bash with fakes.

The script's `main` call is cut off and its functions sourced, with the box
paths it names (/run/wrappers/bin, /run/current-system/sw/bin, the cidata
binary, its /tmp scratch directories) pointed at a scratch directory. A fake
`sudo` drops sudo's own options and runs the command, so "as the agent" runs
as the test's own account; the fakes for getcap, stat, systemctl, journalctl,
ssh, ssh-add, getent, id and strace answer what each test bakes in. Only the
checks that judge a tool's answer are covered: the ones that need a kernel
(sysctls, modules, io_uring) are the live box's.

Tools whose absence skips tests: GNU find (`-printf`).
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "tests" / "box-hardening-check.sh"

# The script's constants and literals the harness points into the scratch
# directory, each expected exactly once.
WRAPPERS_LINE = "readonly WRAPPERS=/run/wrappers/bin"
SYSBIN_LINE = "readonly SYSBIN=/run/current-system/sw/bin"
CIDATA_BINARY = "/mnt/lima-cidata/lima-guestagent"
WORK_TEMPLATE = "/tmp/hk-check.XXXXXX"
KEY_TEMPLATE = "/tmp/hk-agent-key.XXXXXX"
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

# The real tools the agent-side helpers run with PATH set to the fake bin.
REAL_TOOLS = ("env", "bash", "mktemp", "rm", "kill", "mkdir", "touch", "cat", "grep")

OPERATOR = "op"
ALLOWUSERS = "not allowed because not listed in AllowUsers"


def gnu_find():
    try:
        out = subprocess.run(
            ["find", "--version"], capture_output=True, text=True, timeout=20
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "GNU" in out


class BoxCheck(unittest.TestCase):
    """The harness: the script's functions, fakes on PATH, a snippet to run."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.wrappers = self.tmp / "wrappers"
        self.wrappers.mkdir()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.fake("sudo", FAKE_SUDO)
        for tool in REAL_TOOLS:
            real = shutil.which(tool)
            self.assertIsNotNone(real, tool)
            (self.bin / tool).symlink_to(real)
        self.real_stat = shutil.which("stat")
        self.assertIsNotNone(self.real_stat)

    def fake(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def functions(self):
        text = SCRIPT.read_text()
        for old, new in (
            (WRAPPERS_LINE, "readonly WRAPPERS=%s" % self.wrappers),
            (SYSBIN_LINE, "readonly SYSBIN=%s" % self.bin),
            (CIDATA_BINARY, str(self.tmp / "lima-guestagent")),
            (WORK_TEMPLATE, str(self.tmp / "hk-check.XXXXXX")),
            (KEY_TEMPLATE, str(self.tmp / "hk-agent-key.XXXXXX")),
            (MAIN_CALL, ""),
        ):
            self.assertEqual(text.count(old), 1, old)
            text = text.replace(old, new)
        return text

    def run_check(self, snippet, env=None):
        program = "%s\nAGENT_HOME=%s\n%s\n" % (self.functions(), self.home, snippet)
        return subprocess.run(
            ["bash", "-c", program],
            capture_output=True,
            text=True,
            env=dict(
                os.environ,
                PATH=os.pathsep.join([str(self.bin), os.environ.get("PATH", "")]),
                HOME=str(self.home),
                **(env or {}),
            ),
            timeout=60,
        )

    def assert_ok(self, result, text):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(text, result.stdout)

    def assert_fail(self, result, text):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL: " + text, result.stderr)

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

    def test_the_kept_wrappers_pass(self):
        self.box_wrappers()
        result = self.run_check("parse_extra_wrappers; check_wrappers")
        self.assert_ok(result, "ok: every wrapper is root's")

    def test_a_capability_text_without_names_fails(self):
        # "=ep" grants every capability; the old check read it as none.
        self.box_wrappers(caps={"unix_chkpwd": " =ep"})
        result = self.run_check("parse_extra_wrappers; check_wrappers")
        self.assert_fail(result, "getcap %s/unix_chkpwd printed" % self.wrappers)

    def test_the_older_getcap_form_is_read(self):
        self.box_wrappers(caps={"newuidmap": " = cap_setuid,cap_setpcap+ep"})
        result = self.run_check("parse_extra_wrappers; check_wrappers")
        self.assert_ok(result, "ok: every wrapper is root's")

    def test_a_capability_wrapper_that_is_also_setuid_fails(self):
        self.box_wrappers()
        (self.wrappers / "newuidmap").chmod(0o4511)
        result = self.run_check("parse_extra_wrappers; check_wrappers")
        self.assert_fail(result, "wrapper newuidmap is setuid on top")

    def test_a_setgid_wrapper_fails(self):
        self.box_wrappers()
        (self.wrappers / "unix_chkpwd").chmod(0o6511)
        result = self.run_check("parse_extra_wrappers; check_wrappers")
        self.assert_fail(result, "wrapper unix_chkpwd is setgid")

    def test_a_sudo_executable_by_others_fails(self):
        self.box_wrappers()
        (self.wrappers / "sudo").chmod(0o4511)
        result = self.run_check("parse_extra_wrappers; check_wrappers")
        self.assert_fail(result, "sudo is mode 4511")

    def test_a_wrapper_not_owned_by_root_fails(self):
        self.box_wrappers(owner="nobody")
        result = self.run_check("parse_extra_wrappers; check_wrappers")
        self.assert_fail(result, "wrapper newgidmap is owned by nobody")

    def test_an_extra_wrapper_with_its_capabilities_passes_in_any_order(self):
        self.box_wrappers(caps={"dumpcap": " cap_net_admin,cap_net_raw=ep"})
        self.wrapper("dumpcap", 0o511)
        result = self.run_check(
            "parse_extra_wrappers; check_wrappers",
            env={"EXTRA_WRAPPERS": "dumpcap=cap_net_raw,cap_net_admin"},
        )
        self.assert_ok(result, "ok: wrapper set is exactly: dumpcap newgidmap")

    def test_an_unexpected_wrapper_fails(self):
        self.box_wrappers()
        self.wrapper("su", 0o4511)
        result = self.run_check("parse_extra_wrappers; check_wrappers")
        self.assert_fail(result, "%s differs from the allowlist" % self.wrappers)

    def test_an_extra_wrapper_on_the_allowlist_is_refused(self):
        result = self.run_check(
            "parse_extra_wrappers", env={"EXTRA_WRAPPERS": "dumpcap sudo"}
        )
        self.assert_fail(result, "EXTRA_WRAPPERS names sudo")

    def test_a_malformed_extra_wrapper_is_refused(self):
        for entry in ("=cap_net_raw", "dumpcap=", "dump;cap"):
            with self.subTest(entry=entry):
                result = self.run_check(
                    "parse_extra_wrappers", env={"EXTRA_WRAPPERS": entry}
                )
                self.assert_fail(result, "EXTRA_WRAPPERS entry")

    # -- sudo and the accounts -----------------------------------------

    def test_sudo_that_refuses_the_agent_passes(self):
        self.fake("sudo", FAKE_SUDO)
        (self.wrappers / "sudo").write_text(
            '#!/bin/sh\nprintf "sudo: unable to execute: Permission denied\\n" >&2\nexit 1\n'
        )
        (self.wrappers / "sudo").chmod(0o755)
        self.assert_ok(
            self.run_check("check_sudo_refuses_agent"), "ok: the agent cannot run sudo"
        )

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

    def test_locked_accounts_pass(self):
        self.accounts(root="!", agent="*")
        self.assert_ok(self.run_check("check_accounts_locked"), "ok: agent is uid 1000")

    def test_a_password_hash_on_root_fails(self):
        self.accounts(root="$6$salt$hash")
        self.assert_fail(
            self.run_check("check_accounts_locked"),
            "the password of root is not locked",
        )

    def test_a_supplementary_group_on_the_agent_fails(self):
        self.accounts(groups="users wheel")
        self.assert_fail(
            self.run_check("check_accounts_locked"), "agent is in groups 'users wheel'"
        )

    # -- the operator's login ------------------------------------------

    def ssh_fakes(
        self,
        agent_rc=0,
        operator_out="Permission denied (publickey).",
        operator_rc=255,
        keys_rc=0,
    ):
        self.fake(
            "ssh",
            "#!/bin/sh\n"
            "for a; do case $a in *@*) target=$a ;; esac; done\n"
            "case $target in\n"
            'agent@*) printf "Permission denied (publickey).\\n" >&2; exit %d ;;\n'
            '*) printf "%s\\n" >&2; exit %d ;;\n'
            "esac\n" % (agent_rc, operator_out, operator_rc),
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

    def refusals(self):
        return [
            "User %s from %s %s" % (OPERATOR, addr, ALLOWUSERS)
            for addr in ("127.0.0.1", "192.168.5.15")
        ]

    def test_an_allowusers_refusal_in_the_journal_passes(self):
        self.ssh_fakes()
        self.journal(self.refusals())
        result = self.run_check("check_operator_login_refused %s" % OPERATOR)
        self.assert_ok(
            result, "ok: sshd refuses op at 192.168.5.15 because of AllowUsers"
        )

    def test_a_refusal_the_journal_does_not_explain_fails(self):
        self.ssh_fakes()
        self.journal(["Connection closed by authenticating user op 127.0.0.1"])
        result = self.run_check("check_operator_login_refused %s" % OPERATOR)
        self.assert_fail(
            result,
            "sshd refused op at 127.0.0.1, but its journal does not say AllowUsers",
        )

    def test_a_forwarded_key_the_box_does_not_authorize_is_a_skip(self):
        self.ssh_fakes(agent_rc=255)
        self.journal(self.refusals())
        result = self.run_check("check_operator_login_refused %s" % OPERATOR)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SKIP: sshd's refusal of op was NOT tested", result.stderr)
        self.assertNotIn("ok: sshd refuses", result.stdout)

    def test_an_operator_login_from_the_agent_fails(self):
        self.ssh_fakes(operator_out="", operator_rc=0)
        self.journal(self.refusals())
        result = self.run_check("check_operator_login_refused %s" % OPERATOR)
        self.assert_fail(result, "the agent logged in as op at 127.0.0.1")

    def test_a_connection_closed_before_authentication_fails(self):
        self.ssh_fakes(operator_out="Connection closed by 127.0.0.1 port 22")
        self.journal(self.refusals())
        result = self.run_check("check_operator_login_refused %s" % OPERATOR)
        self.assert_fail(
            result, "ssh as op at 127.0.0.1 failed, but not with Permission denied"
        )

    def test_an_unreachable_ssh_agent_fails(self):
        self.ssh_fakes(keys_rc=2)
        result = self.run_check("check_operator_login_refused %s" % OPERATOR)
        self.assert_fail(result, "ssh-add -L exited 2")

    # -- the agent's own authorized_keys -------------------------------

    def planted_key_fakes(self, ssh_rc):
        self.fake(
            "ssh-keygen",
            "#!/bin/sh\n"
            "for a; do case $prev in -f) f=$a ;; -C) c=$a ;; esac; prev=$a; done\n"
            'printf "private\\n" >"$f"; printf "ssh-ed25519 AAAA %s\\n" "$c" >"$f.pub"\n',
        )
        self.fake(
            "ssh",
            '#!/bin/sh\nprintf "Permission denied (publickey).\\n" >&2\nexit %d\n'
            % ssh_rc,
        )

    def test_a_planted_key_that_is_refused_passes_and_is_taken_out(self):
        self.planted_key_fakes(ssh_rc=255)
        authorized = self.home / ".ssh" / "authorized_keys"
        authorized.parent.mkdir()
        authorized.write_text("ssh-ed25519 BBBB theirs\n")
        result = self.run_check("stage_helpers; check_agent_authorized_keys_ignored")
        self.assert_ok(
            result,
            "ok: a key the agent writes to its own authorized_keys does not log it in",
        )
        self.assertEqual(authorized.read_text(), "ssh-ed25519 BBBB theirs\n")

    def test_a_planted_key_that_logs_in_fails(self):
        self.planted_key_fakes(ssh_rc=0)
        result = self.run_check(
            "stage_helpers; trap cleanup EXIT; check_agent_authorized_keys_ignored"
        )
        self.assert_fail(result, "the agent logged in with a key it wrote")
        self.assertFalse((self.home / ".ssh" / "authorized_keys").exists())

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

    def test_a_skipped_unit_on_a_box_without_the_binary_passes(self):
        self.unit()
        self.assert_ok(
            self.run_check("check_guestagent"), "ok: lima-guestagent.service is skipped"
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

    def test_an_active_unit_where_the_binary_exists_passes(self):
        (self.tmp / "lima-guestagent").touch()
        self.unit(active="active")
        self.assert_ok(
            self.run_check("check_guestagent"), "ok: lima-guestagent.service is active"
        )

    def test_an_inactive_unit_where_the_binary_exists_fails(self):
        (self.tmp / "lima-guestagent").touch()
        self.unit()
        self.assert_fail(
            self.run_check("check_guestagent"),
            "Lima's cidata ships the guest agent, but",
        )

    # -- strace -p ------------------------------------------------------

    def test_an_attached_strace_that_follows_the_exit_passes(self):
        # A real strace prints the exit line once the target is gone; the
        # fake prints both at once, and the check still has to let the target
        # go (the fifo) before it reads on, or the cat it started would hang.
        self.fake(
            "strace",
            "#!/bin/sh\n"
            'printf "strace: Process %s attached\\n" "$2" >&2\n'
            'printf "+++ exited with 0 +++\\n" >&2\n',
        )
        result = self.run_check(
            "stage_helpers; export WORK; trap cleanup EXIT; check_strace"
        )
        self.assert_ok(result, "ok: strace -p attaches")

    def test_a_refused_attach_fails(self):
        self.fake(
            "strace",
            '#!/bin/sh\nprintf "strace: attach: ptrace(PTRACE_ATTACH, %s): Operation not permitted\\n" "$2" >&2\nexit 1\n',
        )
        result = self.run_check(
            "stage_helpers; export WORK; trap cleanup EXIT; check_strace"
        )
        self.assert_fail(result, "strace -p on an agent process did not attach")


if not gnu_find():
    BoxCheck = unittest.skip("needs GNU find")(BoxCheck)  # noqa: F811


if __name__ == "__main__":
    unittest.main()
