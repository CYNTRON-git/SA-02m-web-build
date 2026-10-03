"""AES-GCM-128 suite 0 (SPODES table 2.2).

Security control 0x30 is authenticated encryption. The nonce is the
8-byte system title plus the 4-byte invocation counter. Additional
authenticated data is the security-control byte plus the authentication
key. The authentication tag on the wire is 12 bytes.

``cryptography`` only accepts a 16-byte GCM tag, so a 12-byte tag is
checked by recovering the plaintext with the counter keystream and
re-sealing. A wrong key or a flipped invocation counter fails that check.
"""

from __future__ import annotations

import hmac

from sa02m_spodes.axdr import dec_len, enc_len

SC_AUTH = 0x10
SC_ENC = 0x20
SC_BOTH = 0x30
TAG_LEN = 12

# Service-specific global ciphering tags (IEC 62056-5-3).
_GLO = {
    0xC0: 0xC8,  # get
    0xC1: 0xC9,  # set
    0xC3: 0xCB,  # action
    0xC4: 0xCC,
    0xC5: 0xCD,
    0xC7: 0xCF,
}
_UNGLO = {v: k for k, v in _GLO.items()}


class SecurityError(Exception):
    pass


class Secret:
    """A password or key whose str/repr never shows the value."""

    __slots__ = ("_raw",)

    def __init__(self, raw):
        self._raw = raw

    def reveal(self):
        return self._raw

    def __repr__(self) -> str:
        return "<redacted>"

    def __str__(self) -> str:
        return "<redacted>"


def gcm_seal(key: bytes, nonce: bytes, aad: bytes, plaintext: bytes):
    """AES-GCM. Returns ``(ciphertext, tag16)``. The NIST check uses this."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    enc = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    if aad:
        enc.authenticate_additional_data(aad)
    ct = enc.update(plaintext) + enc.finalize()
    return ct, enc.tag


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def protect(apdu: bytes, *, key: bytes, auth_key: bytes, system_title: bytes,
            ic: int, sc: int = SC_BOTH) -> bytes:
    """One suite-0 frame: SC || IC || ciphertext || tag12 (for SC 0x30)."""
    if len(key) != 16 or len(system_title) != 8:
        raise SecurityError("key")
    nonce = bytes(system_title) + int(ic).to_bytes(4, "big")
    aad = bytes((sc & 0xFF,)) + bytes(auth_key) if sc & SC_AUTH else b""
    ct, tag = gcm_seal(bytes(key), nonce, aad, apdu)
    tag12 = tag[:TAG_LEN]
    if sc & SC_ENC and sc & SC_AUTH:
        body = ct + tag12
    elif sc & SC_AUTH:
        body = apdu + tag12
    elif sc & SC_ENC:
        body = ct
    else:
        body = apdu
    return bytes((sc & 0xFF,)) + int(ic).to_bytes(4, "big") + body


def unprotect(frame: bytes, *, key: bytes, auth_key: bytes,
              system_title: bytes) -> bytes:
    """Recover the APDU. A bad tag, key or invocation counter raises."""
    if len(frame) < 5 or len(key) != 16 or len(system_title) != 8:
        raise SecurityError("short")
    sc = frame[0]
    rest = frame[5:]
    nonce = bytes(system_title) + frame[1:5]
    aad = bytes((sc,)) + bytes(auth_key) if sc & SC_AUTH else b""
    if sc & SC_ENC and sc & SC_AUTH:
        if len(rest) < TAG_LEN:
            raise SecurityError("tag")
        ct, tag12 = rest[:-TAG_LEN], rest[-TAG_LEN:]
        ks, _tag = gcm_seal(bytes(key), nonce, aad, bytes(len(ct)))
        plain = _xor(ct, ks)
        ct2, tag = gcm_seal(bytes(key), nonce, aad, plain)
        if ct2 != ct or not hmac.compare_digest(tag[:TAG_LEN], tag12):
            raise SecurityError("tag")
        return plain
    if sc & SC_AUTH:
        if len(rest) < TAG_LEN:
            raise SecurityError("tag")
        plain, tag12 = rest[:-TAG_LEN], rest[-TAG_LEN:]
        _ct, tag = gcm_seal(bytes(key), nonce, aad, plain)
        if not hmac.compare_digest(tag[:TAG_LEN], tag12):
            raise SecurityError("tag")
        return plain
    if sc & SC_ENC:
        ks, _tag = gcm_seal(bytes(key), nonce, b"", bytes(len(rest)))
        return _xor(rest, ks)
    raise SecurityError("sc")


class SecurityContext:
    """Invocation counter and glo-ciphering around one xDLMS APDU."""

    def __init__(self, encryption_key: bytes, authentication_key: bytes,
                 system_title: bytes, invocation_counter: int = 0):
        if (len(encryption_key) != 16 or len(authentication_key) != 16
                or len(system_title) != 8):
            raise SecurityError("key")
        self.ek = bytes(encryption_key)
        self.ak = bytes(authentication_key)
        self.title = bytes(system_title)
        self.ic = int(invocation_counter) & 0xFFFFFFFF

    def protect_apdu(self, apdu: bytes) -> bytes:
        glo = _GLO.get(apdu[0] if apdu else 0)
        if glo is None:
            raise SecurityError("tag")
        self.ic = (self.ic + 1) & 0xFFFFFFFF
        inner = protect(
            apdu, key=self.ek, auth_key=self.ak, system_title=self.title,
            ic=self.ic, sc=SC_BOTH)
        return bytes((glo,)) + enc_len(len(inner)) + inner

    def unprotect_apdu(self, apdu: bytes) -> bytes:
        if not apdu:
            raise SecurityError("short")
        plain_tag = _UNGLO.get(apdu[0])
        if plain_tag is None:
            # A ciphered association must not accept a clear xDLMS APDU.
            raise SecurityError("clear")
        ln, rest = dec_len(apdu[1:])
        if len(rest) < ln:
            raise SecurityError("short")
        plain = unprotect(
            rest[:ln], key=self.ek, auth_key=self.ak, system_title=self.title)
        if not plain or plain[0] != plain_tag:
            raise SecurityError("tag")
        return plain
