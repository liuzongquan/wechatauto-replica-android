"""Bounded, offline decoding of explicitly supplied WeChat message payloads.

Independent implementation, not a live WeChat reader. Unknown/compressed/binary
formats remain raw; no files, URLs, quoted identities or attachments are resolved.
``parsed`` means only that a supported envelope was parsed, not source authenticity.
"""
from __future__ import annotations

import copy
import io
import json
import re
from dataclasses import dataclass, field
from typing import Any

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from .errors import ValidationError
from .models import Message

MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_XML_DEPTH = 32
MAX_XML_NODES = 4096
MAX_RECORD_BYTES = 4 * MAX_PAYLOAD_BYTES

_NATIVE_KINDS = {
    1: "text", 3: "image", 34: "voice", 42: "contact_card", 43: "video",
    47: "emoji", 48: "location", 49: "app", 50: "call", 62: "video",
    10000: "system", 10002: "recall", 11000: "emoji",
}
_DECLARED_TYPES = {
    "text": 1, "image": 3, "voice": 34, "contact_card": 42, "video": 43,
    "emoji": 47, "location": 48, "app": 49, "file": 49, "link": 49,
    "quote": 49, "call": 50, "system": 10000, "recall": 10002,
}
_APP_KINDS = {5: "link", 6: "file", 19: "forwarded", 33: "mini_program",
              36: "mini_program", 57: "quote", 2000: "transfer", 2001: "red_packet"}
_DECIMAL = re.compile(r"[0-9]{1,20}\Z")


@dataclass(frozen=True)
class ParsedPayload:
    status: str
    kind: str
    raw_payload: str | bytes = field(repr=False)
    native_type: int | None = None
    fields: dict[str, Any] = field(default_factory=dict, repr=False)
    reason: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedMessage:
    source: Message = field(repr=False)
    payload: ParsedPayload

    @property
    def status(self) -> str:
        return self.payload.status

    @property
    def kind(self) -> str:
        return self.payload.kind

    @property
    def raw_payload(self) -> str | bytes:
        return self.payload.raw_payload

    @property
    def fields(self) -> dict[str, Any]:
        return copy.deepcopy(self.payload.fields)


def native_server_id(value: Any) -> str | None:
    """Canonical native unsigned server ID; never a local ID, nickname or hash."""
    if type(value) is int:
        return str(value) if 0 < value < 2**64 else None
    if isinstance(value, str) and _DECIMAL.fullmatch(value):
        number = int(value)
        return str(number) if 0 < number < 2**64 else None
    return None


def _bounded_json(value: Any) -> None:
    pending = [(value, 0)]
    count = 0
    total_text = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > MAX_XML_DEPTH or count > MAX_XML_NODES:
            raise ValidationError("message metadata exceeds structural limits")
        if isinstance(item, str):
            if len(item) > MAX_RECORD_BYTES:
                raise ValidationError("message exceeds record size limit")
            try:
                total_text += len(item.encode("utf-8"))
            except UnicodeError:
                raise ValidationError("message contains invalid Unicode") from None
            if total_text > MAX_RECORD_BYTES:
                raise ValidationError("message exceeds record size limit")
        elif type(item) is dict:
            if len(item) > MAX_XML_NODES:
                raise ValidationError("message metadata exceeds structural limits")
            if any(not isinstance(key, str) for key in item):
                raise ValidationError("message metadata keys must be strings")
            pending.extend((part, depth + 1) for pair in item.items() for part in pair)
        elif type(item) is list:
            if len(item) > MAX_XML_NODES:
                raise ValidationError("message metadata exceeds structural limits")
            pending.extend((part, depth + 1) for part in item)
        elif item is not None and type(item) not in (bool, int, float):
            raise ValidationError("message metadata must contain JSON values")


def snapshot_message(message: Message) -> Message:
    """Validate a bounded JSON-compatible Message and detach all mutable metadata."""
    if not isinstance(message, Message):
        raise ValidationError("Message instance required")
    for name in ("id", "account_id", "chat_id", "sender_id", "type", "origin"):
        value = getattr(message, name)
        if (not isinstance(value, str) or (not value and name != "sender_id")
                or len(value) > 1024 or "\x00" in value):
            raise ValidationError(f"invalid message {name}")
    if type(message.sequence) is not int or not 0 <= message.sequence < 2**63:
        raise ValidationError("invalid message sequence")
    if not isinstance(message.content, str) or "\x00" in message.content:
        raise ValidationError("message content must be text without NUL")
    if message.timestamp is not None and (
        not isinstance(message.timestamp, str) or len(message.timestamp) > 256
        or "\x00" in message.timestamp
    ):
        raise ValidationError("invalid message timestamp")
    if type(message.metadata) is not dict:
        raise ValidationError("message metadata must be an object")
    # Walk before asdict/deepcopy/json serialization to reject cycles and giant trees.
    _bounded_json(message.metadata)
    try:
        if (len(message.content) > MAX_PAYLOAD_BYTES
                or len(message.content.encode("utf-8")) > MAX_PAYLOAD_BYTES):
            raise ValidationError("message payload exceeds size limit")
        encoded = json.dumps(message.to_dict(), ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_RECORD_BYTES:
            raise ValidationError("message exceeds record size limit")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise ValidationError("message must contain bounded finite JSON data") from None
    return Message(**json.loads(encoded))


def _kind(native_type: int | None) -> str:
    if native_type is None:
        return "unknown"
    # Only the documented high-32-bit envelope is recognized. Do not guess from
    # the low byte of arbitrary future Android codes.
    return _NATIVE_KINDS.get(native_type, _NATIVE_KINDS.get(native_type & 0xFFFFFFFF,
                                                        "unknown"))


def _xml(text: str):
    depth = count = 0
    root = None
    for event, element in ElementTree.iterparse(
        io.StringIO(text), events=("start", "end"), forbid_dtd=True,
        forbid_entities=True, forbid_external=True,
    ):
        if event == "start":
            depth += 1
            count += 1
            if depth > MAX_XML_DEPTH or count > MAX_XML_NODES:
                raise ValidationError("xml_structure_limit")
            if root is None:
                root = element
        else:
            depth -= 1
    return root


def _child(parent, name: str):
    children = parent.findall(name)
    if len(children) > 1:
        raise ValidationError("duplicate_xml_field")
    return children[0] if children else None


def _value(parent, name: str) -> str | None:
    node = _child(parent, name)
    if node is None:
        return None
    if len(node):
        raise ValidationError("non_scalar_xml_field")
    return node.text or ""


def _file_name(title: str | None) -> str | None:
    if (not title or len(title) > 255 or title in {".", ".."}
            or any(char in title for char in '/\\:')
            or any(ord(char) < 32 or ord(char) == 127 for char in title)):
        return None
    return title


def _app_fields(app) -> tuple[str, dict[str, Any], tuple[str, ...], str]:
    type_text = _value(app, "type")
    if not type_text or not re.fullmatch(r"[0-9]{1,10}", type_text.strip()):
        raise ValidationError("invalid_app_type")
    code = int(type_text)
    fields = {"app_type": code}
    for source, target in (("title", "title"), ("des", "description"), ("url", "url")):
        value = _value(app, source)
        if value is not None:
            fields[target] = value
    kind = _APP_KINDS.get(code, "app")
    warnings = ()
    if code == 6:
        name = _file_name(fields.get("title"))
        fields["file_name"] = name
        if fields.get("title") and name is None:
            warnings = ("unsafe_attachment_name",)
        attachment = _child(app, "appattach")
        if attachment is not None:
            fields["attachment"] = {key: value for key in ("totallen", "fileext", "attachid")
                                    if (value := _value(attachment, key)) is not None}
        # File names/attachment IDs are metadata only. Never return a resolved path.
    if code == 57:
        quote = _child(app, "refermsg")
        if quote is None:
            raise ValidationError("missing_quote_reference")
        fields["quote"] = {target: value for source, target in (
            ("type", "native_type"), ("fromusr", "from_user"), ("chatusr", "chat_user"),
            ("displayname", "display_name"), ("content", "content"), ("svrid", "raw_server_id"),
        ) if (value := _value(quote, source)) is not None}
        raw_id = fields["quote"].get("raw_server_id")
        fields["quote"]["server_id"] = native_server_id(raw_id.strip()) if raw_id else None
        # A display name is never promoted to from_user. Embedded quoted XML stays raw.
    return kind, fields, warnings, "parsed" if code in _APP_KINDS else "unsupported"


def parse_payload(payload: str | bytes, *, native_type: int | None = None) -> ParsedPayload:
    """Parse supported UTF-8 text/XML, retaining byte-for-byte input on every outcome.

    Status: parsed, unsupported, invalid, unsafe, or too_large. XML DTDs/entities,
    oversized/deep documents and ambiguous duplicate fields are rejected. This
    deliberately does not decompress or guess binary container/voice formats.
    """
    if not isinstance(payload, (str, bytes)):
        raise ValidationError("payload must be text or bytes")
    if native_type is not None and (type(native_type) is not int or not 0 <= native_type < 2**64):
        return ParsedPayload("invalid", "unknown", payload, reason="invalid_native_type")
    kind = _kind(native_type)

    def result(status, *, kind=kind, fields=None, reason=None, warnings=()):
        return ParsedPayload(status, kind, payload, native_type, fields or {}, reason, warnings)

    try:
        if len(payload) > MAX_PAYLOAD_BYTES:
            return result("too_large", reason="payload_size_limit")
        raw = payload.encode("utf-8") if isinstance(payload, str) else payload
        if len(raw) > MAX_PAYLOAD_BYTES:
            return result("too_large", reason="payload_size_limit")
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError:
        return result("unsupported", reason="non_utf8_payload")
    if "\x00" in text:
        return result("unsupported", reason="binary_or_nul_payload")
    if kind == "text":
        return result("parsed", fields={"text": text})
    if not text.lstrip("\ufeff \r\n\t").startswith("<"):
        if native_type is None:
            return result("parsed", kind="text", fields={"text": text})
        return result("unsupported", reason="unsupported_non_xml_payload")
    try:
        root = _xml(text)
    except DefusedXmlException:
        return result("unsafe", reason="forbidden_xml_declaration")
    except (ElementTree.ParseError, ValueError):
        return result("invalid", reason="malformed_or_oversized_xml")
    try:
        if root.tag == "sysmsg" and root.get("type") == "revokemsg":
            if native_type is not None and kind not in {"recall", "system"}:
                return result("unsupported", reason="native_type_envelope_mismatch")
            revoke = _child(root, "revokemsg")
            if revoke is None:
                return result("invalid", kind="recall", reason="missing_revoke_body")
            fields = {target: value for source, target in (
                ("newmsgid", "raw_server_id"), ("session", "session"),
                ("replacemsg", "replacement_text"), ("revoketime", "revoked_at"),
                ("msgid", "local_id_hint"),
            ) if (value := _value(revoke, source)) is not None}
            raw_id = fields.get("raw_server_id")
            fields["server_id"] = native_server_id(raw_id.strip()) if raw_id else None
            return result("parsed" if fields["server_id"] else "unsupported", kind="recall",
                          fields=fields, reason=None if fields["server_id"] else "missing_native_server_id")
        app = root if root.tag == "appmsg" else (_child(root, "appmsg") if root.tag == "msg" else None)
        if app is not None:
            if native_type is not None and kind != "app":
                return result("unsupported", reason="native_type_envelope_mismatch")
            app_kind, fields, warnings, status = _app_fields(app)
            return result(status, kind=app_kind, fields=fields, warnings=warnings,
                          reason=None if status == "parsed" else "unknown_app_type")
        # Supported metadata envelopes; these never establish media availability.
        envelope = {"img": "image", "voicemsg": "voice", "videomsg": "video",
                    "emoji": "emoji", "location": "location", "voipmsg": "call"}
        node = root if root.tag in envelope else None
        if root.tag == "msg":
            nodes = [child for child in root if child.tag in envelope]
            if len(nodes) > 1:
                raise ValidationError("ambiguous_message_envelope")
            node = nodes[0] if nodes else None
        if node is not None:
            if native_type is not None and kind != envelope[node.tag]:
                return result("unsupported", reason="native_type_envelope_mismatch")
            return result("parsed", kind=envelope[node.tag], fields={"attributes": dict(node.attrib)})
        if kind == "contact_card" and root.tag == "msg" and root.get("username"):
            return result("parsed", fields={"attributes": dict(root.attrib)})
        return result("unsupported", reason="unknown_xml_envelope")
    except ValidationError:
        return result("invalid", reason="ambiguous_or_invalid_xml_fields")


def parse_message(message: Message) -> ParsedMessage:
    """Decode message.content; preserve its original record and provenance separately."""
    source = snapshot_message(message)
    native_type = source.metadata.get("native_type", _DECLARED_TYPES.get(source.type))
    return ParsedMessage(source, parse_payload(source.content, native_type=native_type))
