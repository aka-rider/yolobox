import unittest
from unittest import mock

from test_yo import FakeHome, yo


class TestAgentPaths(unittest.TestCase):
    def test_splits_on_nul_and_drops_empty_entries(self):
        proc = mock.Mock(returncode=0, stdout=b"/a/b\0/c/d\0\0", stderr="")
        with mock.patch.object(yo, "ssh_run", return_value=proc) as fake_ssh_run:
            result = yo.agent_paths("projects")
        self.assertEqual(result, ["/a/b", "/c/d"])
        fake_ssh_run.assert_called_once_with(
            yo.AGENT, [yo.HELPER, "projects"], binary=True
        )

    def test_a_newline_inside_one_entry_stays_in_that_entry(self):
        proc = mock.Mock(returncode=0, stdout=b"a/b\nwith-newline\0c/d\0", stderr="")
        with mock.patch.object(yo, "ssh_run", return_value=proc):
            result = yo.agent_paths("projects")
        self.assertEqual(result, ["a/b\nwith-newline", "c/d"])


class TestAgentPathsWalkFailures(unittest.TestCase):
    def test_an_unreachable_vm_is_an_error_not_a_find_failure(self):
        proc = mock.Mock(returncode=255, stdout=b"", stderr="ssh: connect refused\n")
        with mock.patch.object(yo, "ssh_run", return_value=proc):
            with self.assertRaises(yo.YoError) as caught:
                yo.agent_paths("projects", check=False)
        self.assertIn("cannot reach the VM", caught.exception.message)
        self.assertNotIn("find exit", caught.exception.message)

    def test_the_helpers_stderr_is_shown_with_a_partial_listing(self):
        proc = mock.Mock(
            returncode=1,
            stdout=b"/a\0",
            stderr="find: '/x': Permission denied\n",
        )
        with mock.patch.object(yo, "ssh_run", return_value=proc):
            with mock.patch.object(yo, "err") as fake_err:
                result = yo.agent_paths("projects", check=False)
        self.assertEqual(result, ["/a"])
        shown = [call.args[0] for call in fake_err.call_args_list]
        self.assertIn("find: '/x': Permission denied", shown)


class TestPickProject(FakeHome):
    def pick_with_fzf_exit(self, code):
        projects = [yo.AGENT_HOME + "/a/normal"]
        with (
            mock.patch.object(yo, "require_tool"),
            mock.patch.object(yo, "agent_paths", return_value=projects),
            mock.patch.object(yo, "err") as fake_err,
            mock.patch.object(
                yo.subprocess,
                "run",
                return_value=mock.Mock(returncode=code, stdout=b""),
            ),
        ):
            with self.assertRaises(yo.YoError) as caught:
                yo.pick_project("q")
        return caught.exception, fake_err

    def test_fzf_exit_1_is_no_match(self):
        failure, fake_err = self.pick_with_fzf_exit(1)
        self.assertEqual(failure.code, 1)
        self.assertIsNone(failure.message)
        self.assertIn("no project matches", fake_err.call_args.args[0])

    def test_fzf_exit_130_is_a_silent_cancel(self):
        failure, fake_err = self.pick_with_fzf_exit(130)
        self.assertEqual(failure.code, 130)
        fake_err.assert_not_called()

    def test_any_other_fzf_failure_is_reported_as_such(self):
        for code in (2, 126, 127):
            with self.subTest(code=code):
                failure, _ = self.pick_with_fzf_exit(code)
                self.assertEqual(failure.message, "fzf failed (exit %d)" % code)

    def test_agent_paths_is_reached_with_the_projects_subcommand(self):
        proc = mock.Mock(
            returncode=0, stdout=(yo.AGENT_HOME + "/a/normal\0").encode(), stderr=""
        )
        with mock.patch.object(yo, "require_tool"):
            with mock.patch.object(yo, "ssh_run", return_value=proc) as fake_ssh_run:
                with mock.patch.object(
                    yo.subprocess,
                    "run",
                    return_value=mock.Mock(returncode=0, stdout=b"a/normal\0"),
                ):
                    yo.pick_project("normal")
        fake_ssh_run.assert_called_once_with(
            yo.AGENT, [yo.HELPER, "projects"], binary=True
        )

    def test_a_partial_listing_still_reaches_fzf_and_warns_via_err(self):
        proc = mock.Mock(
            returncode=1,
            stdout=(yo.AGENT_HOME + "/a/normal\0").encode(),
            stderr="find: '/home/agent/.local/foo': Permission denied\n",
        )
        picker_calls = []

        def fake_picker_run(argv, **kwargs):
            picker_calls.append(argv)
            return mock.Mock(returncode=0, stdout=b"a/normal\0")

        with mock.patch.object(yo, "require_tool"):
            with mock.patch.object(yo, "ssh_run", return_value=proc):
                with mock.patch.object(yo, "err") as fake_err:
                    with mock.patch.object(
                        yo.subprocess, "run", side_effect=fake_picker_run
                    ):
                        result = yo.pick_project("normal")

        self.assertEqual(len(picker_calls), 1)
        self.assertTrue(fake_err.called)
        self.assertEqual(result, yo.AGENT_HOME + "/a/normal")

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
                with mock.patch.object(
                    yo, "guest_path", side_effect=lambda p: p
                ) as fake_guest_path:
                    with mock.patch.object(
                        yo.subprocess, "run", side_effect=fake_picker_run
                    ):
                        result = yo.pick_project("weird")

        self.assertIn("--read0", picker_calls[0])
        self.assertIn("--print0", picker_calls[0])
        expected = yo.AGENT_HOME + "/b/weird\nname"
        fake_guest_path.assert_called_once_with(expected)
        self.assertEqual(result, expected)


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
