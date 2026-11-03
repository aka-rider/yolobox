"""The ssh-agent protocol and the disk passphrase derived from the 1Password key."""

from __future__ import annotations

import hashlib
import hmac
import os
import socket
import struct
from typing import List, Sequence, Tuple

from . import state, ui
from .config import LUKS_KEY_ITEM
from .ui import YoError


# Changing LUKS_SIGN_DATA or LUKS_KDF_INFO changes every existing disk's passphrase.
LUKS_SIGN_DATA = b"yolobox luks v1"
LUKS_KDF_INFO = b"yolobox luks passphrase v1"
# SSH agent protocol message types (draft-miller-ssh-agent).
SSH_AGENT_FAILURE = 5
# How long a connect to the agent socket may take; a refusal is immediate.
AGENT_CONNECT_TIMEOUT = 10
# How long a sign request may wait for the human to approve 1Password's prompt.
AGENT_REPLY_TIMEOUT = 300
# Larger than any reply the agent protocol has a reason to send here.
AGENT_MAX_REPLY_BYTES = 1 << 20
ED25519_SIGNATURE_LENGTH = 64
SSH_AGENT_REQUEST_IDENTITIES = 11
SSH_AGENT_IDENTITIES_ANSWER = 12
SSH_AGENT_SIGN_REQUEST = 13
SSH_AGENT_SIGN_RESPONSE = 14


def ssh_string(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def read_ssh_string(buf: bytes, pos: int) -> Tuple[bytes, int]:
    if pos + 4 > len(buf):
        raise YoError("the ssh agent sent a truncated message")
    (length,) = struct.unpack(">I", buf[pos : pos + 4])
    end = pos + 4 + length
    if end > len(buf):
        raise YoError("the ssh agent sent a truncated message")
    return buf[pos + 4 : end], end


def parse_agent_identities(body: bytes) -> List[Tuple[bytes, str]]:
    if len(body) < 4:
        raise YoError("the ssh agent sent a truncated message")
    (count,) = struct.unpack(">I", body[:4])
    pos = 4
    identities = []
    for _ in range(count):
        blob, pos = read_ssh_string(body, pos)
        comment, pos = read_ssh_string(body, pos)
        identities.append((blob, comment.decode("utf-8", "replace")))
    return identities


def parse_agent_signature(body: bytes) -> bytes:
    signature, _ = read_ssh_string(body, 0)
    algorithm, pos = read_ssh_string(signature, 0)
    raw, _ = read_ssh_string(signature, pos)
    if algorithm != b"ssh-ed25519":
        raise YoError(
            "the %s key signed with %s; it must be an Ed25519 key"
            % (LUKS_KEY_ITEM, algorithm.decode("utf-8", "replace"))
        )
    if len(raw) != ED25519_SIGNATURE_LENGTH:
        raise YoError(
            "the ssh agent returned a %d-byte Ed25519 signature, not %d"
            % (len(raw), ED25519_SIGNATURE_LENGTH)
        )
    return raw


def key_type(blob: bytes) -> str:
    kind, _ = read_ssh_string(blob, 0)
    return kind.decode("utf-8", "replace")


def derive_luks_passphrase(raw_signature: bytes) -> bytes:
    return (
        hmac.new(raw_signature, LUKS_KDF_INFO, hashlib.sha256)
        .hexdigest()
        .encode("ascii")
    )


def unique_key_titled(identities: Sequence[Tuple[bytes, str]], title: str) -> bytes:
    """The one agent key whose comment is `title`; none or several is an error."""
    blobs = [blob for blob, comment in identities if comment == title]
    if not blobs:
        raise YoError(
            "1Password's ssh agent has no key titled %s — create an SSH Key item "
            "(Ed25519) with exactly that title in 1Password" % title
        )
    if len(blobs) > 1:
        raise YoError(
            "1Password's ssh agent offers %d keys titled %s — keep exactly one"
            % (len(blobs), title)
        )
    return blobs[0]


def pick_luks_key(identities: Sequence[Tuple[bytes, str]]) -> bytes:
    blob = unique_key_titled(identities, LUKS_KEY_ITEM)
    if key_type(blob) != "ssh-ed25519":
        raise YoError(
            "the %s item in 1Password must be an Ed25519 key, not %s"
            % (LUKS_KEY_ITEM, key_type(blob))
        )
    return blob


class AgentConnectionClosed(OSError):
    """The agent hung up before it finished its reply."""


def recv_exact(conn: socket.socket, count: int) -> bytes:
    chunks = []
    while count > 0:
        chunk = conn.recv(count)
        if not chunk:
            raise AgentConnectionClosed("connection closed mid-message")
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


def read_agent_reply(conn: socket.socket) -> bytes:
    (length,) = struct.unpack(">I", recv_exact(conn, 4))
    if length == 0 or length > AGENT_MAX_REPLY_BYTES:
        raise OSError("implausible reply length %d" % length)
    return recv_exact(conn, length)


class AgentHungUp(YoError):
    """The agent took the request, then closed or reset the connection before replying."""


# What a sign request tells the user to do after the agent hung up or refused:
# the same yo command is running (up, bootstrap or disk-grow), so it is not named.
REAPPROVE_HINT = "approve the prompt in 1Password and run the same yo command again"


def ssh_agent_call(sock_path: str, payload: bytes) -> bytes:
    """One request and its reply. AgentHungUp when the agent drops a request it took."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(AGENT_CONNECT_TIMEOUT)
            conn.connect(sock_path)
            try:
                conn.sendall(ssh_string(payload))
                # A sign request is answered only after the human approves
                # 1Password's prompt, so the reply gets minutes, not seconds.
                conn.settimeout(AGENT_REPLY_TIMEOUT)
                return read_agent_reply(conn)
            except socket.timeout:
                raise YoError(
                    "1Password's SSH agent at %s did not answer within %d s — "
                    "approve its prompt" % (sock_path, AGENT_REPLY_TIMEOUT)
                )
            except (
                AgentConnectionClosed,
                ConnectionResetError,
                BrokenPipeError,
            ) as failure:
                raise AgentHungUp(
                    "1Password's SSH agent at %s hung up before it answered: %s"
                    % (sock_path, failure)
                )
    except OSError as failure:
        raise YoError(
            "1Password's SSH agent at %s is not answering: %s" % (sock_path, failure)
        )


def ssh_agent_identities(sock_path: str) -> List[Tuple[bytes, str]]:
    reply = ssh_agent_call(sock_path, bytes([SSH_AGENT_REQUEST_IDENTITIES]))
    if reply[0] != SSH_AGENT_IDENTITIES_ANSWER:
        raise YoError(
            "the ssh agent answered message type %d to a key listing" % reply[0]
        )
    return parse_agent_identities(reply[1:])


def ssh_agent_sign(sock_path: str, blob: bytes, data: bytes) -> bytes:
    try:
        reply = ssh_agent_call(
            sock_path,
            bytes([SSH_AGENT_SIGN_REQUEST])
            + ssh_string(blob)
            + ssh_string(data)
            + b"\0\0\0\0",
        )
    except AgentHungUp as failure:
        # 1Password drops the connection of a sign request whose approval
        # prompt is declined or left unanswered, though it answered the key
        # listing a moment before.
        raise YoError(
            "%s — the approval prompt was probably declined or timed out; %s"
            % (failure.message, REAPPROVE_HINT)
        )
    if reply[0] == SSH_AGENT_FAILURE:
        raise YoError(
            "1Password declined to sign with the %s key — %s"
            % (LUKS_KEY_ITEM, REAPPROVE_HINT)
        )
    if reply[0] != SSH_AGENT_SIGN_RESPONSE:
        raise YoError(
            "the ssh agent answered message type %d to a sign request" % reply[0]
        )
    return parse_agent_signature(reply[1:])


def luks_passphrase() -> bytes:
    sock_path = state.driver().op_sock()
    if not os.path.exists(sock_path):
        raise YoError(
            "1Password is not running on this host (no socket at %s); it holds the %s key that unlocks the box"
            % (sock_path, LUKS_KEY_ITEM)
        )
    blob = pick_luks_key(ssh_agent_identities(sock_path))
    ui.err(
        "yo: 1Password: approve the %s key (Touch ID) to unlock %s's disk"
        % (LUKS_KEY_ITEM, state.box.name)
    )
    return derive_luks_passphrase(ssh_agent_sign(sock_path, blob, LUKS_SIGN_DATA))
