"""Behaviour of nix/guest/herdr-agent-lease.sh, run for real under bash and
socat against a fake herdr API server on a unix socket.

herdr keeps an SSH agent registered only while the connection that registered
it stays open, so the script's whole job is one request, then silence, then an
error exit once herdr hangs up. The fake server reads what the script wrote
and ends the connection on its own terms, so no test waits on a clock. Tools
whose absence skips the module: socat.
"""

import json
import socket
import subprocess
import unittest

from shell_harness import BASH, FACTS, REPO, ShellCase, needs_tool

SCRIPT = REPO / "nix" / "guest" / "herdr-agent-lease.sh"
AGENT_SOCK = FACTS["agent_auth_sock"]
REGISTRATION = {
    "id": "yolobox:ssh-agent",
    "method": "server.ssh_agent.register",
    "params": {"socket_path": AGENT_SOCK},
}
OK = {"id": "yolobox:ssh-agent", "result": {"type": "ok"}}
REFUSED = {
    "id": "yolobox:ssh-agent",
    "error": {"code": "invalid_ssh_agent", "message": "no socket at that path"},
}


def line(message):
    return (json.dumps(message, separators=(",", ":")) + "\n").encode()


@needs_tool("socat")
class HerdrAgentLease(ShellCase):
    def setUp(self):
        super().setUp()
        self.api_sock = str(self.tmp / "herdr.sock")
        self.env.update(HERDR_API_SOCK=self.api_sock, AGENT_AUTH_SOCK=AGENT_SOCK)

    def herdr(self):
        """A listening fake herdr API socket."""
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        server.settimeout(self.timeout)
        server.bind(self.api_sock)
        server.listen(1)
        return server

    def start(self):
        """The script, running; killed at the end of the test if it still is."""
        proc = subprocess.Popen(
            [*BASH, str(SCRIPT)],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc

    def accept(self, server):
        """The script's connection, as a file of the bytes it sends, and its
        socket to answer on."""
        conn, _ = server.accept()
        self.addCleanup(conn.close)
        conn.settimeout(self.timeout)
        return conn, conn.makefile("rb")

    def finish(self, proc):
        out, err = proc.communicate(timeout=self.timeout)
        self.assertEqual(out, "")
        return proc.returncode, err

    def test_it_registers_the_agent_socket_and_says_nothing_more_until_herdr_hangs_up(
        self,
    ):
        server = self.herdr()
        proc = self.start()
        conn, sent = self.accept(server)
        self.assertEqual(json.loads(sent.readline()), REGISTRATION)
        conn.sendall(line(OK))
        # A write after the request ends the lease. Once herdr has closed its
        # side, whatever the script wrote is all that is left to read.
        conn.shutdown(socket.SHUT_WR)
        self.assertEqual(sent.read(), b"")
        status, err = self.finish(proc)
        self.assertNotEqual(status, 0)
        self.assertIn("herdr ended the SSH agent lease", err)

    def test_it_exits_naming_the_reply_when_herdr_refuses_the_socket(self):
        server = self.herdr()
        proc = self.start()
        conn, sent = self.accept(server)
        sent.readline()
        conn.sendall(line(REFUSED))
        status, err = self.finish(proc)
        self.assertNotEqual(status, 0)
        self.assertIn("invalid_ssh_agent", err)

    def test_it_exits_when_herdr_closes_the_connection_before_replying(self):
        server = self.herdr()
        proc = self.start()
        conn, sent = self.accept(server)
        sent.readline()
        # Not close(): `sent` still holds the socket open.
        conn.shutdown(socket.SHUT_RDWR)
        status, err = self.finish(proc)
        self.assertNotEqual(status, 0)
        self.assertIn("before replying", err)

    def test_it_exits_when_herdr_has_no_api_socket(self):
        done = self.run_bash(SCRIPT)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn(self.api_sock, done.stderr)


if __name__ == "__main__":
    unittest.main()
