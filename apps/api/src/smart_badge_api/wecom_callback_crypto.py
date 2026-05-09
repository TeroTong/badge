from __future__ import annotations

import base64
import hashlib
import struct
import xml.etree.ElementTree as ET

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class WecomCallbackCryptoError(RuntimeError):
    pass


def parse_xml_flat_texts(xml_text: str) -> dict[str, str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise WecomCallbackCryptoError("invalid xml payload") from exc
    result: dict[str, str] = {}

    def walk(node: ET.Element, path: str) -> None:
        text = (node.text or "").strip()
        if text:
            result[node.tag] = text
            result[path] = text
        for child in list(node):
            walk(child, f"{path}/{child.tag}")

    walk(root, root.tag)
    return result


def parse_xml_texts(xml_text: str) -> dict[str, str]:
    return parse_xml_flat_texts(xml_text)


def extract_encrypt_value(raw_payload: str) -> str:
    text = str(raw_payload or "").strip()
    if not text:
        raise WecomCallbackCryptoError("empty encrypted payload")
    if not text.startswith("<"):
        return text
    encrypted = parse_xml_texts(text).get("Encrypt", "").strip()
    if not encrypted:
        raise WecomCallbackCryptoError("missing Encrypt field")
    return encrypted


def verify_callback_signature(
    *,
    token: str,
    msg_signature: str,
    timestamp: str,
    nonce: str,
    encrypted: str,
) -> None:
    expected = hashlib.sha1("".join(sorted([token, timestamp, nonce, encrypted])).encode("utf-8")).hexdigest()
    if expected != str(msg_signature or "").strip():
        raise WecomCallbackCryptoError("invalid callback signature")


def _decode_aes_key(aes_key: str) -> bytes:
    clean = str(aes_key or "").strip()
    if len(clean) != 43:
        raise WecomCallbackCryptoError("invalid EncodingAESKey length")
    try:
        key = base64.b64decode(f"{clean}=")
    except Exception as exc:
        raise WecomCallbackCryptoError("invalid EncodingAESKey") from exc
    if len(key) != 32:
        raise WecomCallbackCryptoError("invalid EncodingAESKey bytes")
    return key


def _remove_pkcs7_padding(payload: bytes) -> bytes:
    if not payload:
        raise WecomCallbackCryptoError("empty decrypted payload")
    pad = payload[-1]
    if pad < 1 or pad > 32:
        raise WecomCallbackCryptoError("invalid decrypted padding")
    return payload[:-pad]


def decrypt_callback_payload(
    *,
    token: str,
    aes_key: str,
    corp_id: str,
    msg_signature: str,
    timestamp: str,
    nonce: str,
    payload: str,
) -> str:
    encrypted = extract_encrypt_value(payload)
    verify_callback_signature(
        token=token,
        msg_signature=msg_signature,
        timestamp=timestamp,
        nonce=nonce,
        encrypted=encrypted,
    )
    key = _decode_aes_key(aes_key)
    try:
        encrypted_bytes = base64.b64decode(encrypted)
    except Exception as exc:
        raise WecomCallbackCryptoError("invalid encrypted payload") from exc

    decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    try:
        plain = decryptor.update(encrypted_bytes) + decryptor.finalize()
    except Exception as exc:
        raise WecomCallbackCryptoError("decrypt callback payload failed") from exc
    plain = _remove_pkcs7_padding(plain)
    if len(plain) < 20:
        raise WecomCallbackCryptoError("decrypted payload too short")

    msg_len = struct.unpack(">I", plain[16:20])[0]
    msg_start = 20
    msg_end = msg_start + msg_len
    if msg_end > len(plain):
        raise WecomCallbackCryptoError("invalid decrypted message length")
    message = plain[msg_start:msg_end].decode("utf-8")
    actual_corp_id = plain[msg_end:].decode("utf-8")
    expected_corp_id = str(corp_id or "").strip()
    if expected_corp_id and actual_corp_id != expected_corp_id:
        raise WecomCallbackCryptoError("callback corp id mismatch")
    return message
