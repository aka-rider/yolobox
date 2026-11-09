"""The ssh-agent protocol and the disk passphrase derived from the 1Password key."""

from __future__ import annotations

import functools
import hashlib
import hmac
import socket
import struct
from typing import Callable, List, Optional, Sequence, Tuple, TypeVar

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
ED25519_KEY_LENGTH = 32
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


def ed25519_public_key(blob: bytes) -> bytes:
    """The 32 raw key bytes of an ssh-ed25519 public key blob."""
    _, pos = read_ssh_string(blob, 0)
    key, _ = read_ssh_string(blob, pos)
    if len(key) != ED25519_KEY_LENGTH:
        raise YoError(
            "the %s key's public half is %d bytes, not %d"
            % (LUKS_KEY_ITEM, len(key), ED25519_KEY_LENGTH)
        )
    return key


# RFC 8032 Ed25519, verification only, in the stdlib: the signature the agent
# returns becomes the one LUKS keyslot, so before it does, it is checked
# against the listed key's public half. Points are extended coordinates
# (X, Y, Z, T) with x = X/Z, y = Y/Z, xy = T/Z.
ED25519_P = 2**255 - 19
ED25519_ORDER = 2**252 + 27742317777372353535851937790883648493
ED25519_D = (-121665 * pow(121666, -1, ED25519_P)) % ED25519_P
Point = Tuple[int, int, int, int]


def ed25519_decode_point(encoded: bytes) -> Optional[Point]:
    """The point a 32-byte little-endian y with the sign of x in its top bit
    names, or None when no point on the curve has that encoding."""
    y = int.from_bytes(encoded, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= ED25519_P:
        return None
    y2 = y * y % ED25519_P
    u = (y2 - 1) % ED25519_P
    v = (ED25519_D * y2 + 1) % ED25519_P
    x = u * pow(v, 3, ED25519_P) % ED25519_P
    x = x * pow(u * pow(v, 7, ED25519_P), (ED25519_P - 5) // 8, ED25519_P)
    x %= ED25519_P
    vx2 = v * x * x % ED25519_P
    if vx2 == (-u) % ED25519_P:
        x = x * pow(2, (ED25519_P - 1) // 4, ED25519_P) % ED25519_P
    elif vx2 != u:
        return None
    if x == 0 and sign == 1:
        return None
    if x & 1 != sign:
        x = ED25519_P - x
    return (x, y, 1, x * y % ED25519_P)


def ed25519_point_add(p: Point, q: Point) -> Point:
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % ED25519_P
    b = (y1 + x1) * (y2 + x2) % ED25519_P
    c = 2 * t1 * t2 * ED25519_D % ED25519_P
    d = 2 * z1 * z2 % ED25519_P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % ED25519_P, g * h % ED25519_P, f * g % ED25519_P, e * h % ED25519_P)


def ed25519_point_mul(scalar: int, point: Point) -> Point:
    result: Point = (0, 1, 1, 0)
    while scalar:
        if scalar & 1:
            result = ed25519_point_add(result, point)
        point = ed25519_point_add(point, point)
        scalar >>= 1
    return result


def ed25519_points_equal(p: Point, q: Point) -> bool:
    x1, y1, z1, _ = p
    x2, y2, z2, _ = q
    return (x1 * z2 - x2 * z1) % ED25519_P == 0 and (y1 * z2 - y2 * z1) % ED25519_P == 0


# The base point: y = 4/5, x the even root (RFC 8032, section 5.1).
ED25519_BASE = ed25519_decode_point(
    (4 * pow(5, -1, ED25519_P) % ED25519_P).to_bytes(32, "little")
)
assert ED25519_BASE is not None


def ed25519_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Whether `signature` (64 bytes) is `public_key`'s Ed25519 signature over
    `message`, as RFC 8032 section 5.1.7 checks it."""
    if len(public_key) != ED25519_KEY_LENGTH:
        return False
    if len(signature) != ED25519_SIGNATURE_LENGTH:
        return False
    a = ed25519_decode_point(public_key)
    r = ed25519_decode_point(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if a is None or r is None or s >= ED25519_ORDER:
        return False
    digest = hashlib.sha512(signature[:32] + public_key + message).digest()
    k = int.from_bytes(digest, "little") % ED25519_ORDER
    return ed25519_points_equal(
        ed25519_point_mul(s, ED25519_BASE),
        ed25519_point_add(r, ed25519_point_mul(k, a)),
    )


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
        # A protocol violation, not a socket that failed: named as the agent's.
        raise YoError(
            "the ssh agent announced a reply of %d bytes, which no agent message "
            "has" % length
        )
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
    except (FileNotFoundError, ConnectionRefusedError) as failure:
        raise YoError(
            "1Password is not running on this host: nothing serves its SSH agent "
            "at %s (%s), and it holds the %s key that unlocks the box — start "
            "1Password with its SSH agent turned on, then run the command again"
            % (sock_path, ui.os_error_reason(failure), LUKS_KEY_ITEM)
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


# The passphrase lives as a mutable bytearray so that the command that derived
# it can zero it when it ends; a bytes object would stay in memory until the
# allocator reused it. Every derivation is kept here until forget_passphrases().
Passphrase = bytearray
live_passphrases: List[bytearray] = []

Command = TypeVar("Command", bound=Callable[..., None])


def forget_passphrases() -> None:
    for passphrase in live_passphrases:
        passphrase[:] = bytes(len(passphrase))
    live_passphrases.clear()


def clearing_passphrases(command: Command) -> Command:
    """The command boundary: every passphrase derived while `command` runs is
    zeroed when it returns or raises."""

    @functools.wraps(command)
    def run(*args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            return command(*args, **kwargs)
        finally:
            forget_passphrases()

    return run  # type: ignore[return-value]


class PassphraseSource:
    """The disk passphrase, derived from 1Password the first time it is needed
    and then kept for the command (zeroed by clearing_passphrases)."""

    def __init__(self) -> None:
        self.derived: Optional[Passphrase] = None

    def __call__(self) -> Passphrase:
        if self.derived is None:
            self.derived = luks_passphrase()
        return self.derived

    def require_key(self) -> None:
        """Refuse unless the passphrase can be derived when an initrd asks:
        1Password's agent answers and lists the one LUKS_KEY_ITEM key. A key
        listing, never a signature, so nothing is approved and nothing prompts."""
        if self.derived is None:
            luks_key(state.driver().op_sock())


def luks_key(sock_path: str) -> bytes:
    """The LUKS_KEY_ITEM key's blob, as 1Password's agent at `sock_path` lists it."""
    return pick_luks_key(ssh_agent_identities(sock_path))


def luks_passphrase() -> Passphrase:
    sock_path = state.driver().op_sock()
    blob = luks_key(sock_path)
    ui.err(
        "yo: 1Password: approve the %s key (Touch ID); it derives %s's disk passphrase"
        % (LUKS_KEY_ITEM, state.box.name)
    )
    signature = ssh_agent_sign(sock_path, blob, LUKS_SIGN_DATA)
    # The signature is the disk's only keyslot: one the listed key did not
    # make (an agent that signed with another key, or mangled the reply)
    # would encrypt a fresh disk under a passphrase nothing can derive again.
    if not ed25519_verify(ed25519_public_key(blob), LUKS_SIGN_DATA, signature):
        raise YoError(
            "the signature the ssh agent returned is not the %s key's over the "
            "fixed data; the agent signed with another key or corrupted the "
            "reply, and the disk passphrase is not derived from it" % LUKS_KEY_ITEM
        )
    passphrase = bytearray(derive_luks_passphrase(signature))
    live_passphrases.append(passphrase)
    return passphrase
