import os
import shutil
import socket
import struct
import tempfile
import threading
import unittest
from unittest import mock

from test_yo import FakeHome, yo

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
        with self.assertRaises(yo.YoError):
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
        with self.assertRaises(yo.YoError):
            yo.parse_agent_signature(body)

    def test_an_empty_signature_body_is_refused(self):
        with self.assertRaises(yo.YoError):
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

    def test_a_declined_signature_names_the_prompt(self):
        with self.assertRaises(yo.YoError) as caught:
            yo.ssh_agent_sign(self.sock_path, RSA_BLOB, yo.LUKS_SIGN_DATA)
        self.assertIn("approve the prompt", caught.exception.message)

    def test_luks_passphrase_signs_with_the_titled_key(self):
        with (
            mock.patch.object(yo, "op_sock", return_value=self.sock_path),
            mock.patch.object(yo, "err") as fake_err,
        ):
            passphrase = yo.luks_passphrase()
        self.assertEqual(passphrase, yo.derive_luks_passphrase(RAW_SIGNATURE))
        self.assertIn("Touch ID", fake_err.call_args[0][0])


class TestSshAgentClientFailures(FakeAgent):
    def test_an_empty_reply_is_refused(self):
        self.raw_reply = struct.pack(">I", 0)
        with self.assertRaises(yo.YoError):
            yo.ssh_agent_identities(self.sock_path)

    def test_a_connection_closed_mid_reply_is_refused(self):
        self.raw_reply = struct.pack(">I", 10) + b"\x0c"
        with self.assertRaises(yo.YoError) as caught:
            yo.ssh_agent_identities(self.sock_path)
        self.assertIn("not answering", caught.exception.message)

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
            mock.patch.object(yo, "AGENT_REPLY_TIMEOUT", 0.2),
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


class TestInitrdSshArgv(FakeHome):
    def write_ssh_config(self, text):
        os.makedirs(yo.lima_dir())
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
        with self.assertRaises(yo.YoError):
            yo.initrd_ssh_argv("true")


if __name__ == "__main__":
    unittest.main()
