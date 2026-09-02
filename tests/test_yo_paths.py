import unittest
from pathlib import Path
from unittest import mock

from test_yo import FakeHome, yo


class TestAgentPaths(unittest.TestCase):
    def test_splits_on_nul_and_drops_empty_entries(self):
        proc = mock.Mock(stdout=b"/a/b\0/c/d\0\0")
        with mock.patch.object(yo, "ssh_run", return_value=proc) as fake_ssh_run:
            result = yo.agent_paths(["find"])
        self.assertEqual(result, ["/a/b", "/c/d"])
        fake_ssh_run.assert_called_once_with(yo.AGENT, ["find"], check=True, binary=True)

    def test_a_newline_inside_one_entry_stays_in_that_entry(self):
        proc = mock.Mock(stdout=b"a/b\nwith-newline\0c/d\0")
        with mock.patch.object(yo, "ssh_run", return_value=proc):
            result = yo.agent_paths(["find"])
        self.assertEqual(result, ["a/b\nwith-newline", "c/d"])


class TestPickProject(FakeHome):
    def test_fzf_argv_carries_nul_framing_and_a_newline_survives_round_trip(self):
        projects = [
            yo.AGENT_HOME + "/a/normal",
            yo.AGENT_HOME + "/b/weird\nname",
        ]
        picker_calls = []

        def fake_picker_run(argv, **kwargs):
            picker_calls.append(argv)
            entries = kwargs["input"].decode().split("\0")
            chosen = entries[1]
            return mock.Mock(returncode=0, stdout=(chosen + "\0").encode())

        with mock.patch.object(yo, "require_tool"):
            with mock.patch.object(yo, "agent_paths", return_value=projects):
                with mock.patch.object(yo, "guest_path", side_effect=lambda p: p) as fake_guest_path:
                    with mock.patch.object(yo.subprocess, "run", side_effect=fake_picker_run):
                        result = yo.pick_project("weird")

        self.assertIn("--read0", picker_calls[0])
        self.assertIn("--print0", picker_calls[0])
        expected = yo.AGENT_HOME + "/b/weird\nname"
        fake_guest_path.assert_called_once_with(expected)
        self.assertEqual(result, expected)


class TestSeedGitconfig(FakeHome):
    def test_a_root_named_with_an_embedded_newline_is_one_entry(self):
        weird_root = "group\nname"
        identity_dir = Path(self.home) / weird_root / "sub"
        identity_dir.mkdir(parents=True)
        (identity_dir / ".gitconfig").write_text("[user]\n")

        with mock.patch.object(yo, "agent_paths", return_value=[weird_root]):
            with mock.patch.object(yo, "ssh_run") as fake_ssh_run:
                with mock.patch.object(yo, "tar_stream", return_value=b"") as fake_tar_stream:
                    yo.seed_gitconfig()

        fake_tar_stream.assert_called_once()
        members = fake_tar_stream.call_args[0][0]
        self.assertEqual(len(members), 1)
        self.assertIn(weird_root, members[0][1])
        fake_ssh_run.assert_called_once()

    def test_rejects_roots_with_a_slash_or_dot_names(self):
        with mock.patch.object(yo, "agent_paths", return_value=["a/b", ".", "..", ""]):
            with mock.patch.object(yo, "ssh_run") as fake_ssh_run:
                yo.seed_gitconfig()
        fake_ssh_run.assert_not_called()


class TestVersionDrift(unittest.TestCase):
    def test_equal_versions_report_no_drift(self):
        self.assertIsNone(yo.version_drift("1.2.3", "1.2.3"))

    def test_unknown_guest_reports_no_drift(self):
        self.assertIsNone(yo.version_drift("1.2.3", None))

    def test_different_versions_name_both_and_suggest_bootstrap(self):
        message = yo.version_drift("1.2.3", "1.1.0")
        self.assertIsNotNone(message)
        self.assertIn("1.2.3", message)
        self.assertIn("1.1.0", message)
        self.assertIn("yo bootstrap", message)

    def test_dev_host_reports_no_drift(self):
        self.assertIsNone(yo.version_drift("dev", "1.1.0"))


if __name__ == "__main__":
    unittest.main()
