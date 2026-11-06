import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from test_yo import FakeHome, initrd_probe, yo

ED25519_BLOB = yo.ssh_string(b"ssh-ed25519") + yo.ssh_string(b"\x01" * 32)
OTHER_BLOB = yo.ssh_string(b"ssh-ed25519") + yo.ssh_string(b"\x02" * 32)
RSA_BLOB = yo.ssh_string(b"ssh-rsa") + yo.ssh_string(b"\x03" * 8)
RAW_SIGNATURE = bytes(range(64))


def identities_answer(pairs):
    body = struct.pack(">I", len(pairs))
    for blob, comment in pairs:
        body += yo.ssh_string(blob) + yo.ssh_string(comment.encode())
    return bytes([yo.SSH_AGENT_IDENTITIES_ANSWER]) + body


def signature_answer(algorithm, raw):
    signature = yo.ssh_string(algorithm) + yo.ssh_string(raw)
    return bytes([yo.SSH_AGENT_SIGN_RESPONSE]) + yo.ssh_string(signature)


class TestDeriveLuksPassphrase(unittest.TestCase):
    def test_the_derivation_is_pinned_byte_for_byte(self):
        # Changing this literal, LUKS_SIGN_DATA, LUKS_KDF_INFO or the
        # derivation changes the passphrase of every existing disk, and there
        # is no recovery slot: every encrypted box becomes unopenable.
        self.assertEqual(
            yo.derive_luks_passphrase(bytes(range(64))),
            b"c458a9e0a398126f0803952afdad3dd7446381cdbd58ffe6bda010dddc56e352",
        )
        self.assertEqual(yo.LUKS_SIGN_DATA, b"yolobox luks v1")
        self.assertEqual(yo.LUKS_KDF_INFO, b"yolobox luks passphrase v1")

    def test_differs_between_two_signatures(self):
        self.assertNotEqual(
            yo.derive_luks_passphrase(RAW_SIGNATURE),
            yo.derive_luks_passphrase(bytes(reversed(RAW_SIGNATURE))),
        )


class TestParseAgentMessages(unittest.TestCase):
    def test_identities_round_trip(self):
        pairs = [(ED25519_BLOB, "yolobox-disk"), (OTHER_BLOB, "other ünïcode")]
        body = identities_answer(pairs)[1:]
        self.assertEqual(yo.parse_agent_identities(body), pairs)

    def test_identities_with_no_keys_is_an_empty_list(self):
        self.assertEqual(yo.parse_agent_identities(struct.pack(">I", 0)), [])

    def test_a_truncated_identity_list_is_refused(self):
        body = identities_answer([(ED25519_BLOB, "yolobox-disk")])[1:-3]
        with self.assertRaisesRegex(
            yo.YoError, "the ssh agent sent a truncated message"
        ):
            yo.parse_agent_identities(body)

    def test_signature_is_the_raw_64_bytes(self):
        body = signature_answer(b"ssh-ed25519", RAW_SIGNATURE)[1:]
        self.assertEqual(yo.parse_agent_signature(body), RAW_SIGNATURE)

    def test_an_rsa_signature_is_refused(self):
        body = signature_answer(b"ssh-rsa", RAW_SIGNATURE)[1:]
        with self.assertRaises(yo.YoError) as caught:
            yo.parse_agent_signature(body)
        self.assertIn("Ed25519", caught.exception.message)

    def test_a_63_byte_signature_is_refused(self):
        body = signature_answer(b"ssh-ed25519", RAW_SIGNATURE[:63])[1:]
        with self.assertRaisesRegex(
            yo.YoError, "the ssh agent returned a 63-byte Ed25519"
        ):
            yo.parse_agent_signature(body)

    def test_an_empty_signature_body_is_refused(self):
        with self.assertRaisesRegex(
            yo.YoError, "the ssh agent sent a truncated message"
        ):
            yo.parse_agent_signature(b"")


class TestPickLuksKey(unittest.TestCase):
    def test_no_key_with_the_title_says_how_to_create_it(self):
        with self.assertRaises(yo.YoError) as caught:
            yo.pick_luks_key([(OTHER_BLOB, "github")])
        self.assertIn("yolobox-disk", caught.exception.message)
        self.assertIn("SSH Key", caught.exception.message)

    def test_one_key_with_the_title_is_picked(self):
        picked = yo.pick_luks_key(
            [(OTHER_BLOB, "github"), (ED25519_BLOB, "yolobox-disk")]
        )
        self.assertEqual(picked, ED25519_BLOB)

    def test_two_keys_with_the_title_are_ambiguous(self):
        with self.assertRaises(yo.YoError) as caught:
            yo.pick_luks_key(
                [(ED25519_BLOB, "yolobox-disk"), (OTHER_BLOB, "yolobox-disk")]
            )
        self.assertIn("2 keys", caught.exception.message)

    def test_a_key_of_another_type_is_refused(self):
        with self.assertRaises(yo.YoError) as caught:
            yo.pick_luks_key([(RSA_BLOB, "yolobox-disk")])
        self.assertIn("Ed25519", caught.exception.message)


class FakeAgent(unittest.TestCase):
    """A unix-socket ssh agent serving two keys until tearDown closes it."""

    raw_reply = None

    def setUp(self):
        self.requests = []
        self.stopping = False
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.sock_path = os.path.join(self.dir, "a.sock")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.sock_path)
        self.listener.listen()
        self.thread = threading.Thread(target=self.serve)
        self.thread.start()
        self.addCleanup(self.stop_serving)

    def stop_serving(self):
        # Closing a listener does not wake an accept() blocked in another
        # thread on Linux, so a last connection does.
        self.stopping = True
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as waker:
            waker.connect(self.sock_path)
        self.thread.join()
        self.listener.close()

    def serve(self):
        while True:
            conn, _ = self.listener.accept()
            with conn:
                if self.stopping:
                    return
                self.answer(conn)

    def answer(self, conn):
        length = struct.unpack(">I", conn.recv(4))[0]
        request = b""
        while len(request) < length:
            request += conn.recv(length - len(request))
        self.requests.append(request)
        if self.raw_reply is not None:
            conn.sendall(self.raw_reply)
        elif request[0] == yo.SSH_AGENT_REQUEST_IDENTITIES:
            reply = identities_answer(
                [(OTHER_BLOB, "github"), (ED25519_BLOB, "yolobox-disk")]
            )
            conn.sendall(yo.ssh_string(reply))
        else:
            blob, _ = yo.read_ssh_string(request, 1)
            if blob == ED25519_BLOB:
                reply = signature_answer(b"ssh-ed25519", RAW_SIGNATURE)
            else:
                reply = bytes([yo.SSH_AGENT_FAILURE])
            conn.sendall(yo.ssh_string(reply))


class TestSshAgentClient(FakeAgent):
    def test_lists_the_agents_identities(self):
        self.assertEqual(
            yo.ssh_agent_identities(self.sock_path),
            [(OTHER_BLOB, "github"), (ED25519_BLOB, "yolobox-disk")],
        )

    def test_signs_with_a_known_key(self):
        signature = yo.ssh_agent_sign(self.sock_path, ED25519_BLOB, yo.LUKS_SIGN_DATA)
        self.assertEqual(signature, RAW_SIGNATURE)

    def test_the_sign_request_is_the_fixed_bytes_with_no_flags(self):
        yo.ssh_agent_sign(self.sock_path, ED25519_BLOB, yo.LUKS_SIGN_DATA)
        self.assertEqual(
            self.requests,
            [
                bytes([13])
                + yo.ssh_string(ED25519_BLOB)
                + yo.ssh_string(b"yolobox luks v1")
                + b"\x00\x00\x00\x00"
            ],
        )

    def test_a_declined_signature_names_the_prompt_and_no_one_command(self):
        with self.assertRaises(yo.YoError) as caught:
            yo.ssh_agent_sign(self.sock_path, RSA_BLOB, yo.LUKS_SIGN_DATA)
        self.assertIn("approve the prompt", caught.exception.message)
        self.assertIn("run the same yo command again", caught.exception.message)
        self.assertNotIn("yo up", caught.exception.message)

    def test_luks_passphrase_signs_with_the_titled_key(self):
        with (
            mock.patch.object(yo.box.driver, "op_sock", return_value=self.sock_path),
            mock.patch.object(yo.ui, "err") as fake_err,
        ):
            passphrase = yo.luks_passphrase()
        self.assertEqual(passphrase, yo.derive_luks_passphrase(RAW_SIGNATURE))
        self.assertIn("Touch ID", fake_err.call_args[0][0])


class TestSshAgentClientFailures(FakeAgent):
    def test_an_empty_reply_is_refused(self):
        self.raw_reply = struct.pack(">I", 0)
        with self.assertRaisesRegex(yo.YoError, "1Password's SSH agent at"):
            yo.ssh_agent_identities(self.sock_path)

    def test_a_connection_closed_mid_reply_is_refused(self):
        self.raw_reply = struct.pack(">I", 10) + b"\x0c"
        with self.assertRaises(yo.YoError) as caught:
            yo.ssh_agent_identities(self.sock_path)
        self.assertIn("hung up", caught.exception.message)
        self.assertNotIn("not answering", caught.exception.message)

    def sign_failure(self, blob=ED25519_BLOB):
        with self.assertRaises(yo.YoError) as caught:
            yo.ssh_agent_sign(self.sock_path, blob, yo.LUKS_SIGN_DATA)
        return caught.exception.message

    def assert_names_the_approval(self, message):
        self.assertIn("declined or timed out", message)
        # Up, bootstrap and disk-grow all derive the passphrase: no one command is named.
        self.assertIn("run the same yo command again", message)
        self.assertNotIn("yo up", message)
        self.assertNotIn("not answering", message)

    def test_a_sign_request_the_agent_drops_says_the_approval_was_not_given(self):
        # 1Password answers the key listing, then drops the connection of the
        # sign request when its prompt is declined or times out.
        self.raw_reply = b""
        self.assert_names_the_approval(self.sign_failure())

    def test_a_sign_request_the_agent_resets_says_the_same(self):
        connection = mock.MagicMock()
        connection.__enter__.return_value = connection
        connection.recv.side_effect = ConnectionResetError("reset by peer")
        with mock.patch.object(yo.agent.socket, "socket", return_value=connection):
            self.assert_names_the_approval(self.sign_failure())

    def test_a_sign_request_the_agent_refuses_to_take_says_the_same(self):
        connection = mock.MagicMock()
        connection.__enter__.return_value = connection
        connection.sendall.side_effect = BrokenPipeError("broken pipe")
        with mock.patch.object(yo.agent.socket, "socket", return_value=connection):
            self.assert_names_the_approval(self.sign_failure())

    def test_a_key_listing_the_agent_resets_does_not_blame_an_approval(self):
        connection = mock.MagicMock()
        connection.__enter__.return_value = connection
        connection.recv.side_effect = ConnectionResetError("reset by peer")
        with mock.patch.object(yo.agent.socket, "socket", return_value=connection):
            with self.assertRaises(yo.YoError) as caught:
                yo.ssh_agent_identities(self.sock_path)
        self.assertIn("hung up", caught.exception.message)
        self.assertNotIn("declined", caught.exception.message)

    def test_a_missing_socket_is_refused_without_a_prompt_hint(self):
        with self.assertRaises(yo.YoError) as caught:
            yo.ssh_agent_identities(os.path.join(self.dir, "nothing.sock"))
        self.assertIn("not answering", caught.exception.message)
        self.assertNotIn("approve", caught.exception.message)

    def test_a_reply_that_never_comes_names_the_prompt_to_approve(self):
        # The timeout is the behaviour under test, so this one waits for real.
        hang = threading.Event()
        self.addCleanup(hang.set)
        self.raw_reply = None
        with (
            mock.patch.object(yo.agent, "AGENT_REPLY_TIMEOUT", 0.2),
            mock.patch.object(FakeAgent, "answer", lambda agent, conn: hang.wait(5)),
        ):
            with self.assertRaises(yo.YoError) as caught:
                yo.ssh_agent_identities(self.sock_path)
        self.assertIn("approve its prompt", caught.exception.message)


class TestLuksPassphraseWithoutOnePassword(FakeHome):
    def test_no_socket_means_1password_is_not_running(self):
        with self.assertRaises(yo.YoError) as caught:
            yo.luks_passphrase()
        self.assertIn("1Password is not running", caught.exception.message)


class TestClassifyInitrdAttempt(unittest.TestCase):
    def test_a_command_that_ran_is_ran_whatever_it_exited_with(self):
        for code in (0, 1, 2, 3, 130):
            with self.subTest(code=code):
                self.assertEqual(
                    yo.classify_initrd_attempt(code, "x"), yo.SshOutcome.RAN
                )

    def test_permission_denied_on_a_connection_is_refused(self):
        self.assertEqual(
            yo.classify_initrd_attempt(
                255, "root@127.0.0.1: Permission denied (publickey)."
            ),
            yo.SshOutcome.REFUSED,
        )

    def test_nothing_listening_shapes(self):
        for text in (
            "ssh: connect to host 127.0.0.1 port 60022: Connection refused",
            "read: Connection reset by peer",
            "kex_exchange_identification: read: Connection reset by peer",
            "Connection closed by 127.0.0.1 port 60022",
            "Connection to 127.0.0.1 closed by remote host.",
            "client_loop: send disconnect: Broken pipe",
            "Connection timed out during banner exchange",
            "ssh: connect to host 127.0.0.1 port 60022: Operation timed out",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    yo.classify_initrd_attempt(255, text), yo.SshOutcome.NOT_LISTENING
                )

    def test_an_unknown_ssh_failure_is_an_error(self):
        for text in ("", "Bad configuration option: lizard"):
            with self.subTest(text=text):
                self.assertEqual(
                    yo.classify_initrd_attempt(255, text), yo.SshOutcome.ERROR
                )


class TestClassifyInitrdProbe(unittest.TestCase):
    """Only a proved idle wait for the passphrase may be powered off by `yo down`."""

    CRYPTSETUP_ASK = "cryptsetup:/dev/disk/by-uuid/1234"
    states = yo.InitrdState

    def test_a_box_idle_at_the_disks_passphrase_prompt_is_waiting(self):
        self.assertEqual(
            yo.classify_initrd_probe(initrd_probe("active", [self.CRYPTSETUP_ASK])),
            self.states.WAITING_FOR_PASSPHRASE,
        )

    def test_a_migration_still_waiting_for_its_passphrase_has_written_nothing(self):
        self.assertEqual(
            yo.classify_initrd_probe(initrd_probe("activating", [yo.MIGRATE_ASK_ID])),
            self.states.WAITING_FOR_PASSPHRASE,
        )

    def test_a_migration_that_has_its_passphrase_is_migrating(self):
        # The unit runs for hours after the ask is gone; the ask is the only
        # moment before its first write.
        for asks in ([], [self.CRYPTSETUP_ASK]):
            with self.subTest(asks=asks):
                self.assertEqual(
                    yo.classify_initrd_probe(initrd_probe("activating", asks)),
                    self.states.MIGRATING,
                )

    def test_a_running_unlock_helper_means_someone_is_about_to_answer(self):
        for migrate, asks in (
            ("active", [self.CRYPTSETUP_ASK]),
            ("activating", [yo.MIGRATE_ASK_ID]),
        ):
            with self.subTest(migrate=migrate):
                self.assertEqual(
                    yo.classify_initrd_probe(
                        initrd_probe(migrate, asks, unlock_running=True)
                    ),
                    self.states.UNLOCKING,
                )

    def test_a_migration_in_progress_wins_over_a_running_unlock_helper(self):
        self.assertEqual(
            yo.classify_initrd_probe(initrd_probe("activating", [], True)),
            self.states.MIGRATING,
        )

    def test_a_failed_migration_is_named_apart_whatever_else_is_pending(self):
        for asks in ([], [self.CRYPTSETUP_ASK]):
            with self.subTest(asks=asks):
                self.assertEqual(
                    yo.classify_initrd_probe(initrd_probe("failed", asks)),
                    self.states.MIGRATION_FAILED,
                )

    def test_a_finished_migration_with_nothing_asking_is_unlocked_idle(self):
        # What the initrd's emergency mode looks like after the root mount failed.
        self.assertEqual(
            yo.classify_initrd_probe(initrd_probe("active")),
            self.states.UNLOCKED_IDLE,
        )

    def test_everything_else_is_unknown(self):
        for name, output in (
            ("unit not found", initrd_probe("inactive", [self.CRYPTSETUP_ASK])),
            ("no unit state at all", initrd_probe("", [self.CRYPTSETUP_ASK])),
            ("an ask of someone else", initrd_probe("active", ["other:thing"])),
            (
                "a stranger's ask beside the disk's",
                initrd_probe("active", [self.CRYPTSETUP_ASK, "other:thing"]),
            ),
            ("empty output", ""),
            ("lizard", "lizard\n"),
            ("no end marker", "migrate=active\n"),
            (
                "a line outside any section",
                "migrate=active\nlizard\nprobe=end\n",
            ),
            (
                "an ask section that never ends",
                "migrate=active\nask-begin\nId=%s\nprobe=end\n" % self.CRYPTSETUP_ASK,
            ),
            (
                "output after the end marker",
                initrd_probe("active", [self.CRYPTSETUP_ASK]) + "migrate=active\n",
            ),
            (
                "two answers for the unit",
                "migrate=active\nmigrate=active\nprobe=end\n",
            ),
            (
                "an Id outside an ask section",
                "migrate=active\nId=%s\nprobe=end\n" % self.CRYPTSETUP_ASK,
            ),
        ):
            with self.subTest(name=name):
                self.assertEqual(yo.classify_initrd_probe(output), self.states.UNKNOWN)


class TestInitrdProbeCommand(unittest.TestCase):
    """The command itself, run by a real sh against a stand-in initrd."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.asks = os.path.join(self.root, "ask-password")
        os.mkdir(self.asks)
        self.fifos = os.path.join(self.root, "yolobox-unlock.*")
        bin_dir = os.path.join(self.root, "bin")
        os.mkdir(bin_dir)
        systemctl = os.path.join(bin_dir, "systemctl")
        with open(systemctl, "w") as handle:
            handle.write("#!/bin/sh\necho $MIGRATE_UNIT_STATE\n")
        os.chmod(systemctl, 0o755)
        # The initrd's PATH holds no more than cat and the fake systemctl,
        # and /bin is not where every host keeps cat (NixOS has none there).
        os.symlink(shutil.which("cat"), os.path.join(bin_dir, "cat"))
        self.bin_dir = bin_dir

    def ask(self, name, text):
        with open(os.path.join(self.asks, name), "w") as handle:
            handle.write(text)

    def probe(self, migrate="active"):
        command = yo.initrd_probe_command(self.asks, self.fifos)
        completed = subprocess.run(
            ["/bin/sh", "-c", command],
            capture_output=True,
            text=True,
            env={
                "PATH": self.bin_dir,
                "MIGRATE_UNIT_STATE": migrate,
            },
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return yo.classify_initrd_probe(completed.stdout)

    def test_a_box_with_nothing_pending_is_unlocked_idle(self):
        self.assertEqual(self.probe(), yo.InitrdState.UNLOCKED_IDLE)

    def test_an_ask_file_without_a_final_newline_still_reads_as_the_prompt(self):
        self.ask(
            "ask.1", "[Ask]\nMessage=Please enter passphrase\nId=cryptsetup:/dev/x"
        )
        self.ask("ask.2", "[Ask]\nId=%s\nMessage=m" % yo.MIGRATE_ASK_ID)
        self.assertEqual(self.probe(), yo.InitrdState.WAITING_FOR_PASSPHRASE)

    def test_a_leftover_unlock_fifo_is_seen_beside_an_ask_without_a_final_newline(
        self,
    ):
        self.ask("ask.1", "[Ask]\nId=cryptsetup:/dev/x")
        with open(os.path.join(self.root, "yolobox-unlock.Ab12Cd"), "w"):
            pass
        self.assertEqual(self.probe(), yo.InitrdState.UNLOCKING)

    def test_a_running_migration_with_no_ask_left_is_migrating(self):
        self.assertEqual(self.probe("activating"), yo.InitrdState.MIGRATING)


class TestInitrdState(FakeHome):
    def reading_after(self, completed):
        with mock.patch.object(yo.proc, "run", return_value=completed) as run:
            result = yo.initrd_state()
        self.assertEqual(run.call_args.args[0][-1], yo.INITRD_PROBE_COMMAND)
        return result

    def test_the_probe_runs_in_the_initrd_and_its_output_is_judged(self):
        completed = subprocess.CompletedProcess([], 0, initrd_probe("activating"), "")
        self.assertEqual(
            self.reading_after(completed),
            yo.InitrdReading(yo.InitrdState.MIGRATING, ""),
        )

    def test_an_ssh_that_failed_is_unknown_and_says_why(self):
        completed = subprocess.CompletedProcess(
            [],
            255,
            initrd_probe("active", ["cryptsetup:/dev/vda2"]),
            "Host key lizard\n",
        )
        reading = self.reading_after(completed)
        self.assertEqual(reading.state, yo.InitrdState.UNKNOWN)
        self.assertIn("exited 255", reading.cause)
        self.assertIn("Host key lizard", reading.cause)

    def test_an_ssh_that_failed_in_silence_still_gives_a_cause(self):
        reading = self.reading_after(subprocess.CompletedProcess([], 255, "", ""))
        self.assertIn("no message", reading.cause)

    def test_output_no_state_matches_is_unknown_and_quoted(self):
        reading = self.reading_after(subprocess.CompletedProcess([], 0, "lizard\n", ""))
        self.assertEqual(reading.state, yo.InitrdState.UNKNOWN)
        self.assertIn("lizard", reading.cause)


class TestInitrdSshArgv(FakeHome):
    def write_ssh_config(self, text):
        os.makedirs(yo.instance_dir())
        with open(yo.ssh_cfg(), "w") as handle:
            handle.write(text)

    def test_takes_host_port_and_key_from_limas_block(self):
        self.write_ssh_config(
            'Host lima-yolobox\n  Hostname 127.0.0.2\n  Port 61234\n  IdentityFile "/k/user"\n'
        )
        argv = yo.initrd_ssh_argv("true")
        self.assertEqual(argv[-2:], ["127.0.0.2", "true"])
        self.assertIn("Port=61234", argv)
        self.assertEqual(argv[argv.index("-i") + 1], "/k/user")

    def test_offers_every_key_of_limas_block_in_order(self):
        self.write_ssh_config(
            'Host lima-yolobox\n  Hostname 127.0.0.2\n  Port 61234\n  IdentityFile "/k/user"\n'
            '  IdentityFile "/k/second"\n  Port 1\n'
        )
        argv = yo.initrd_ssh_argv("true")
        self.assertEqual(
            [argv[i + 1] for i, arg in enumerate(argv) if arg == "-i"],
            ["/k/user", "/k/second"],
        )
        self.assertIn("Port=61234", argv)

    def test_falls_back_to_limas_defaults_without_the_file(self):
        argv = yo.initrd_ssh_argv("true")
        self.assertEqual(argv[-2:], ["127.0.0.1", "true"])
        self.assertIn("Port=%d" % yo.SSH_LOCAL_PORT, argv)
        self.assertEqual(
            argv[argv.index("-i") + 1], os.path.join(self.home, ".lima/_config/user")
        )

    def test_logs_in_as_root_unless_told_otherwise(self):
        self.assertIn("User=root", yo.initrd_ssh_argv("true"))
        self.assertIn("User=someone", yo.initrd_ssh_argv("true", user="someone"))

    def test_pins_nothing_and_never_reads_a_config_file(self):
        argv = yo.initrd_ssh_argv("true")
        self.assertEqual(argv[argv.index("-F") + 1], "/dev/null")
        self.assertNotIn(yo.ssh_cfg(), argv)
        for index in range(0, len(yo.FIRST_CONTACT_OPTS), 2):
            self.assertIn(yo.FIRST_CONTACT_OPTS[index + 1], argv)
        self.assertIn("BatchMode=yes", argv)

    def test_an_unreadable_config_is_an_error_not_a_fallback(self):
        os.makedirs(yo.ssh_cfg())
        with self.assertRaisesRegex(yo.YoError, "cannot read"):
            yo.initrd_ssh_argv("true")


if __name__ == "__main__":
    unittest.main()
