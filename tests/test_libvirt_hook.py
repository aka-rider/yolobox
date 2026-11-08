"""linux/yolobox-libvirt-hook.sh, run under bash the way libvirt runs it:
`HOOK <domain> <operation> <sub-operation> <extra>` with the domain XML on
stdin, and a fake setfacl and stat first on PATH: the first records its argv,
the second answers for the directory's owner.

Tools whose absence skips tests: none beyond bash.
"""

import unittest

from shell_harness import FACTS, REPO, ShellCase

HOOK = REPO / "linux" / "yolobox-libvirt-hook.sh"
TEMPLATE = REPO / "libvirt" / "domain.xml"

FAKE_SETFACL = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_SETFACL_CALLS"
exit "$FAKE_SETFACL_RC"
"""
# `stat -c %U DIR`: the owner libvirt gives the directory, unless a test says
# otherwise.
FAKE_STAT = """#!/bin/sh
printf '%s\\n' "$FAKE_DIR_OWNER"
"""

FOREIGN_XML = "<domain type='kvm'><name>other</name><devices/></domain>"


class TestPasstAclHook(ShellCase):
    def setUp(self):
        super().setUp()
        self.fake("setfacl", FAKE_SETFACL)
        self.fake("stat", FAKE_STAT)
        self.passt_dir = self.tmp / "passt"
        self.passt_dir.mkdir()
        self.calls = self.tmp / "calls"
        # libvirt passes no PATH: the hook sets its own, with this seam.
        self.env.update(
            YOLOBOX_HOOK_PATH=self.env["PATH"],
            YOLOBOX_HOOK_PASST_DIR=str(self.passt_dir),
            FAKE_SETFACL_CALLS=str(self.calls),
            FAKE_SETFACL_RC="0",
            FAKE_DIR_OWNER="qemu",
        )
        # A yolobox domain as libvirt hands it over: the template, filled in.
        self.yolobox_xml = TEMPLATE.read_text().replace("$name", "box")

    def hook(self, operation, sub, xml, setfacl_rc=0, owner="qemu"):
        return self.run_bash(
            HOOK,
            "yolobox-box",
            operation,
            sub,
            "-",
            input=xml,
            env=dict(FAKE_SETFACL_RC=str(setfacl_rc), FAKE_DIR_OWNER=owner),
        )

    def setfacl_calls(self):
        if not self.calls.exists():
            return []
        return self.calls.read_text().splitlines()

    def assert_fixed_once(self, result):
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        # -P: the ACL goes on the directory itself, never through a symlink.
        self.assertEqual(
            self.setfacl_calls(),
            ["-P -m u:%s:rwx,m::rwx %s" % (FACTS["vm_user"], self.passt_dir)],
        )

    def assert_left_alone(self, result, code, said):
        """The hook touched nothing, said why on stderr, and exited `code`."""
        self.assertEqual((result.returncode, result.stdout), (code, ""))
        self.assertIn(said, result.stderr)
        self.assertEqual(self.setfacl_calls(), [])

    def test_a_symlink_or_a_file_where_the_directory_should_be_is_never_relabelled(
        self,
    ):
        # setfacl follows a symlink by default: a link to /etc planted there
        # would hand yolobox-vm rwx on /etc.
        self.passt_dir.rmdir()
        for make in (
            lambda: self.passt_dir.symlink_to(self.tmp),
            lambda: self.passt_dir.write_text(""),
        ):
            self.passt_dir.unlink(missing_ok=True)
            make()
            with self.subTest(
                what="symlink" if self.passt_dir.is_symlink() else "file"
            ):
                said = "%s is not a directory" % self.passt_dir
                self.assert_left_alone(
                    self.hook("prepare", "begin", self.yolobox_xml), 1, said
                )
                self.assert_left_alone(
                    self.hook("started", "begin", FOREIGN_XML), 0, said
                )

    def test_a_directory_another_account_owns_is_never_relabelled(self):
        said = "%s is owned by alice, not qemu" % self.passt_dir
        self.assert_left_alone(
            self.hook("prepare", "begin", self.yolobox_xml, owner="alice"), 1, said
        )
        self.assert_left_alone(
            self.hook("release", "end", FOREIGN_XML, owner="alice"), 0, said
        )

    def test_a_yolobox_domain_about_to_start_gets_the_acl(self):
        self.assert_fixed_once(self.hook("prepare", "begin", self.yolobox_xml))

    def test_a_foreign_domain_about_to_start_is_left_alone(self):
        result = self.hook("prepare", "begin", FOREIGN_XML)
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertEqual(self.setfacl_calls(), [])

    def test_any_domain_started_or_released_restores_the_acl(self):
        for operation, sub in (("started", "begin"), ("release", "end")):
            with self.subTest(operation=operation):
                self.calls.unlink(missing_ok=True)
                self.assert_fixed_once(self.hook(operation, sub, FOREIGN_XML))

    def test_any_other_operation_prints_nothing_and_changes_nothing(self):
        for operation, sub in (
            ("migrate", "begin"),
            ("restore", "begin"),
            ("prepare", "end"),
            ("stopped", "end"),
            ("lizard", ""),
        ):
            with self.subTest(operation=operation, sub=sub):
                result = self.hook(operation, sub, self.yolobox_xml)
                self.assertEqual((result.returncode, result.stdout), (0, ""))
                self.assertEqual(self.setfacl_calls(), [])

    def test_a_failed_setfacl_stops_a_yolobox_domain_from_starting(self):
        result = self.hook("prepare", "begin", self.yolobox_xml, setfacl_rc=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("setfacl on %s failed" % self.passt_dir, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_a_failed_setfacl_is_only_logged_for_any_other_domain(self):
        # A hook left behind with yolobox-vm deleted must not stop every VM.
        for operation, sub in (("started", "begin"), ("release", "end")):
            with self.subTest(operation=operation):
                result = self.hook(operation, sub, FOREIGN_XML, setfacl_rc=1)
                self.assertEqual((result.returncode, result.stdout), (0, ""))
                self.assertIn("setfacl on %s failed" % self.passt_dir, result.stderr)

    def test_before_the_directory_exists_there_is_nothing_to_fix(self):
        self.passt_dir.rmdir()
        result = self.hook("started", "begin", FOREIGN_XML)
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertEqual(self.setfacl_calls(), [])


if __name__ == "__main__":
    unittest.main()
