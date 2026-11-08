import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from test_yo import (
    FakeHome,
    yo,
    refuse_tcp_to_the_host,
)


def setUpModule():
    refuse_tcp_to_the_host()


# A real Ed25519 key's public half and its signature over LUKS_SIGN_DATA,
# made once with a reference implementation: luks_passphrase verifies the
# signature against the listed key before it becomes the disk's passphrase.
ED25519_PUBLIC_KEY = bytes.fromhex(
    "ca44a299cb4e18752db50ab77c88d5f5b3808a1181e0b38f8a3b0be3af83a179"
)
ED25519_BLOB = yo.ssh_string(b"ssh-ed25519") + yo.ssh_string(ED25519_PUBLIC_KEY)
OTHER_BLOB = yo.ssh_string(b"ssh-ed25519") + yo.ssh_string(b"\x02" * 32)
RSA_BLOB = yo.ssh_string(b"ssh-rsa") + yo.ssh_string(b"\x03" * 8)
RAW_SIGNATURE = bytes.fromhex(
    "b2151d9e50e6688a8d851470161db5bcf57255d3e6256eb6a60626f6608cb1b2"
    "4ab53c39aa938262134010564f993b552d37d3ffe3f8f3367575d559c64f9200"
)

# RFC 8032, section 7.1, TEST 1 and TEST 2: public key, message, signature.
RFC_8032_VECTORS = (
    (
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
        "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
)


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


class TestEd25519Verify(unittest.TestCase):
    def test_the_rfc_8032_vectors_verify(self):
        for public_key, message, signature in RFC_8032_VECTORS:
            with self.subTest(public_key=public_key):
                self.assertTrue(
                    yo.ed25519_verify(
                        bytes.fromhex(public_key),
                        bytes.fromhex(message),
                        bytes.fromhex(signature),
                    )
                )

    def test_a_signature_over_other_data_or_by_another_key_is_refused(self):
        public_key, message, signature = (bytes.fromhex(v) for v in RFC_8032_VECTORS[1])
        self.assertFalse(yo.ed25519_verify(public_key, b"", signature))
        self.assertFalse(yo.ed25519_verify(public_key, message + b"\0", signature))
        other_key = bytes.fromhex(RFC_8032_VECTORS[0][0])
        self.assertFalse(yo.ed25519_verify(other_key, message, signature))

    def test_every_single_bit_flip_in_the_signature_is_refused(self):
        public_key, message, signature = (bytes.fromhex(v) for v in RFC_8032_VECTORS[1])
        for byte in (0, 31, 32, 63):
            for bit in (0, 7):
                flipped = bytearray(signature)
                flipped[byte] ^= 1 << bit
                with self.subTest(byte=byte, bit=bit):
                    self.assertFalse(yo.ed25519_verify(public_key, message, flipped))

    def test_malformed_inputs_are_refused_not_errors(self):
        public_key, message, signature = (bytes.fromhex(v) for v in RFC_8032_VECTORS[1])
        s_at_the_order = signature[:32] + yo.ED25519_ORDER.to_bytes(32, "little")
        for key, sig in (
            (b"", signature),
            (public_key[:-1], signature),
            (b"\xff" * 32, signature),  # not a point: y past the field
            (public_key, b""),
            (public_key, signature[:-1]),
            (public_key, signature + b"\0"),
            (public_key, b"\xff" * 32 + signature[32:]),  # R past the field
            (public_key, s_at_the_order),
            (public_key, bytes(64)),
        ):
            with self.subTest(key=key.hex(), sig=sig.hex()):
                self.assertFalse(yo.ed25519_verify(key, message, sig))


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

    def luks_passphrase(self):
        with (
            mock.patch.object(yo.box.driver, "op_sock", return_value=self.sock_path),
            mock.patch.object(yo.ui, "err") as fake_err,
        ):
            passphrase = yo.luks_passphrase()
        self.assertIn("Touch ID", fake_err.call_args[0][0])
        return passphrase

    def test_luks_passphrase_signs_with_the_titled_key(self):
        self.assertEqual(
            self.luks_passphrase(), yo.derive_luks_passphrase(RAW_SIGNATURE)
        )

    def test_a_signature_the_listed_key_did_not_make_never_becomes_a_passphrase(
        self,
    ):
        # The signature is the disk's only keyslot: one from another key would
        # encrypt the disk under a passphrase nothing can derive again.
        self.raw_reply = yo.ssh_string(
            signature_answer(b"ssh-ed25519", bytes(reversed(RAW_SIGNATURE)))
        )
        with mock.patch.object(yo.agent, "ssh_agent_identities") as listing:
            listing.return_value = [(ED25519_BLOB, "yolobox-disk")]
            with self.assertRaises(yo.YoError) as caught:
                self.luks_passphrase()
        self.assertIn("not the yolobox-disk key's", caught.exception.message)

    def test_the_passphrase_is_zeroed_when_the_command_ends(self):
        held = []

        @yo.agent.clearing_passphrases
        def command():
            held.append(self.luks_passphrase())
            raise yo.YoError("stopped half-way")

        with self.assertRaises(yo.YoError):
            command()
        self.assertEqual(held[0], bytearray(64))
        self.assertEqual(yo.agent.live_passphrases, [])


class TestSshAgentClientFailures(FakeAgent):
    def test_an_empty_reply_is_refused(self):
        self.raw_reply = struct.pack(">I", 0)
        with self.assertRaisesRegex(yo.YoError, "reply of 0 bytes"):
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


class TestInitrdAskPending(FakeHome):
    """`yo down` may power off a box in its initrd only while a passphrase ask
    nobody has answered waits there: the one moment nothing writes to the disk."""

    def probe_says(self, asks):
        """What the probe prints in an initrd whose ask directory holds `asks`."""
        with tempfile.TemporaryDirectory() as ask_dir:
            for name in asks:
                with open(os.path.join(ask_dir, name), "w") as handle:
                    handle.write("[Ask]\nId=cryptsetup:/dev/x")
            command = yo.unlock.INITRD_ASK_PROBE.replace(
                yo.unlock.ASK_PASSWORD_DIR, ask_dir
            )
            completed = subprocess.run(
                ["/bin/sh", "-c", command], capture_output=True, text=True
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout

    def pending_after(self, completed):
        with mock.patch.object(yo.proc, "run", return_value=completed) as run:
            result = yo.unlock.initrd_ask_pending()
        self.assertEqual(run.call_args.args[0][-1], yo.unlock.INITRD_ASK_PROBE)
        return result

    def test_the_probe_sees_an_ask_whether_or_not_it_ends_in_a_newline(self):
        self.assertTrue(self.pending_after(self.completed(self.probe_says(["ask.1"]))))
        self.assertTrue(
            self.pending_after(self.completed(self.probe_says(["ask.1", "ask.2"])))
        )

    def test_an_empty_ask_directory_is_no_ask(self):
        self.assertFalse(self.pending_after(self.completed(self.probe_says([]))))

    def test_an_ssh_that_failed_is_an_error_that_says_why_never_a_verdict(self):
        for completed in (
            subprocess.CompletedProcess([], 255, "ask=pending\n", "Host key lizard\n"),
            subprocess.CompletedProcess([], 255, "", ""),
            subprocess.CompletedProcess([], 0, "lizard\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ):
            with self.subTest(completed=completed):
                with self.assertRaises(yo.YoError) as caught:
                    self.pending_after(completed)
                self.assertIn("cannot look inside", caught.exception.message)
                self.assertIn(
                    "exited %d" % completed.returncode, caught.exception.message
                )
                if completed.stderr:
                    self.assertIn(completed.stderr.strip(), caught.exception.message)

    @staticmethod
    def completed(stdout):
        return subprocess.CompletedProcess([], 0, stdout, "")


class TestInitrdSshArgv(FakeHome):
    FACTS = yo.SshFacts(61234, "op", "/k/user", "none")

    def test_dials_the_facts_port_on_loopback_with_their_key(self):
        argv = yo.initrd_ssh_argv("true", self.FACTS)
        self.assertEqual(argv[-2:], ["127.0.0.1", "true"])
        self.assertIn("Port=61234", argv)
        self.assertEqual(argv[argv.index("-i") + 1], "/k/user")
        self.assertIn("IdentityAgent=none", argv)

    def test_logs_in_as_root_unless_told_otherwise(self):
        self.assertIn("User=root", yo.initrd_ssh_argv("true", self.FACTS))
        self.assertIn(
            "User=someone", yo.initrd_ssh_argv("true", self.FACTS, user="someone")
        )

    def test_pins_nothing_and_never_reads_a_config_file(self):
        argv = yo.initrd_ssh_argv("true", self.FACTS)
        self.assertEqual(argv[argv.index("-F") + 1], "/dev/null")
        self.assertNotIn(yo.ssh_cfg(), argv)
        for index in range(0, len(yo.FIRST_CONTACT_OPTS), 2):
            self.assertIn(yo.FIRST_CONTACT_OPTS[index + 1], argv)
        self.assertIn("BatchMode=yes", argv)


if __name__ == "__main__":
    unittest.main()
