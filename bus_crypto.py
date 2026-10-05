"""End-to-end encryption for Agent Console traffic between Cameron's PCs (stdlib only).

Both PCs hold the same SSH private key file. `python notify.py console key` (run as Cameron, so the service never
reads ~/.ssh) hashes that file's base64 body (it does not need the passphrase: the encrypted key file itself is the
shared secret) into a 32-byte master key and stores it as console.key in config.json, plus a short key id to compare
between PCs. The planner relay then carries only ciphertext it cannot read or forge.

Construction (encrypt-then-MAC from standard primitives, no hand-made cipher core):
  master = HKDF-SHA256(salt="agent-bus console v1", ikm=decoded key file)
  k_enc = HMAC(master, "enc"), k_mac = HMAC(master, "mac")
  keystream block i = HMAC-SHA256(k_enc, nonce || i)       (HMAC as a PRF in counter mode)
  tag = HMAC-SHA256(k_mac, "v1" || len(aad) || aad || nonce || ciphertext)
Token = base64url(0x01 || nonce(16) || ciphertext || tag(32)). aad binds sender and recipient machine.
"""
import base64
import hashlib
import hmac
import os
import re
import struct

VERSION = 1
SALT = b"agent-bus console v1"
PEM_RE = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", re.S)


class CryptoError(ValueError):
    pass


def hkdf(ikm, salt, info, length=32):
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def key_material(text):
    """The decoded body of a PEM / OpenSSH private key file (line endings and wrapping do not matter)."""
    match = PEM_RE.search(text)
    if not match or "PRIVATE KEY" not in match.group(1):
        raise CryptoError("that file is not a private key (expected -----BEGIN ... PRIVATE KEY-----)")
    body = "".join(line.strip() for line in match.group(2).splitlines() if line.strip() and ":" not in line)
    try:
        raw = base64.b64decode(body, validate=True)
    except ValueError:
        raise CryptoError("the private key body is not valid base64") from None
    if len(raw) < 32:
        raise CryptoError("the private key is too short")
    return raw


def derive(path):
    """(master key, key id) from a private key file."""
    with open(path, encoding="utf-8", errors="strict") as handle:
        raw = key_material(handle.read())
    master = hkdf(raw, SALT, b"master")
    return master, key_id(master)


def key_id(master):
    return hmac.new(master, b"key-id", hashlib.sha256).hexdigest()[:12]


def encode_key(master):
    return base64.urlsafe_b64encode(master).decode("ascii").rstrip("=")


def decode_key(text):
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", text):
        raise CryptoError("console.key must be 43 base64url characters (run: python notify.py console key)")
    return base64.urlsafe_b64decode(text + "=")


def _keys(master):
    return hmac.new(master, b"enc", hashlib.sha256).digest(), hmac.new(master, b"mac", hashlib.sha256).digest()


def _stream(k_enc, nonce, length):
    blocks = (hmac.new(k_enc, nonce + struct.pack(">Q", i), hashlib.sha256).digest() for i in range((length + 31) // 32))
    return b"".join(blocks)[:length]


def _tag(k_mac, aad, nonce, ciphertext):
    return hmac.new(k_mac, b"v1" + struct.pack(">I", len(aad)) + aad + nonce + ciphertext, hashlib.sha256).digest()


def seal(master, plaintext, aad=b""):
    k_enc, k_mac = _keys(master)
    nonce = os.urandom(16)
    stream = _stream(k_enc, nonce, len(plaintext))
    ciphertext = bytes(a ^ b for a, b in zip(plaintext, stream))
    token = bytes([VERSION]) + nonce + ciphertext + _tag(k_mac, aad, nonce, ciphertext)
    return base64.urlsafe_b64encode(token).decode("ascii").rstrip("=")


def open_(master, token, aad=b""):
    """Plaintext of a sealed token; CryptoError when it was tampered with, is for someone else, or the key differs."""
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, TypeError):
        raise CryptoError("not a sealed token") from None
    if len(raw) < 1 + 16 + 32 or raw[0] != VERSION:
        raise CryptoError("not a sealed token")
    nonce, ciphertext, tag = raw[1:17], raw[17:-32], raw[-32:]
    k_enc, k_mac = _keys(master)
    if not hmac.compare_digest(tag, _tag(k_mac, aad, nonce, ciphertext)):
        raise CryptoError("authentication failed (different key, wrong recipient, or tampered)")
    stream = _stream(k_enc, nonce, len(ciphertext))
    return bytes(a ^ b for a, b in zip(ciphertext, stream))
