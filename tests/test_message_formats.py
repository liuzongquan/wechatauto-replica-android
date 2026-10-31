"""Synthetic offline payloads only: not production WeChat format qualification."""
from dataclasses import replace

import pytest

from wechatauto_android.errors import ValidationError
from wechatauto_android.message_formats import (
    MAX_PAYLOAD_BYTES, MAX_XML_DEPTH, MAX_XML_NODES, native_server_id,
    parse_message, parse_payload, snapshot_message,
)
from wechatauto_android.models import Message


def message(content="你好🙂\n" + "long plain text " * 80, **extra):
    return Message("local-1", "account-a", "group@chatroom", 1, "native-sender", content, **extra)


def app(code, body="", title="Example"):
    return f"<msg><appmsg><type>{code}</type><title>{title}</title>{body}</appmsg></msg>"


def revoke(identifier="123", session="group@chatroom"):
    return ('<sysmsg type="revokemsg"><revokemsg>'
            f'<session>{session}</session><msgid>456</msgid><newmsgid>{identifier}</newmsgid>'
            '<replacemsg><![CDATA["A"撤回了一条消息]]></replacemsg>'
            '<revoketime>1700000000</revoketime></revokemsg></sysmsg>')


def test_text_preserves_whitespace_unicode_and_non_cjk_long_text():
    item = message()
    result = parse_message(item)
    assert result.kind == "text" and result.status == "parsed"
    assert result.fields["text"] == item.content == result.raw_payload
    assert result.source == item
    assert parse_payload("   ").fields["text"] == "   "
    assert parse_payload("", native_type=1).status == "parsed"
    # A text message that happens to contain XML must not become a recall event.
    assert parse_payload(revoke(), native_type=1).kind == "text"


def test_source_and_provenance_are_detached_without_identity_inference():
    source = message(app(57, '<refermsg><svrid>123</svrid><displayname>Alice</displayname>'
                         '<content>quoted</content></refermsg>'), type="quote",
                     metadata={"native_type": 49, "source_id": "fixture", "nested": ["original"]})
    result = parse_message(source)
    source.metadata["nested"].append("mutated")
    assert result.source.metadata["nested"] == ["original"]
    assert result.source.origin == "user_export"
    assert result.fields["quote"]["server_id"] == "123"
    assert result.fields["quote"]["display_name"] == "Alice"
    assert "from_user" not in result.fields["quote"]
    modified = result.fields
    modified["quote"]["server_id"] = "999"
    assert result.fields["quote"]["server_id"] == "123"
    assert "quoted" not in repr(result)


@pytest.mark.parametrize("code,expected", [(5, "link"), (6, "file"), (19, "forwarded"),
    (33, "mini_program"), (36, "mini_program"), (2000, "transfer"), (2001, "red_packet")])
def test_supported_app_envelopes_are_metadata_not_actions(code, expected):
    raw = app(code, '<des>说明</des><url>file:///not-opened/secret</url>')
    result = parse_payload(raw, native_type=49)
    assert (result.status, result.kind) == ("parsed", expected)
    assert result.fields["description"] == "说明"
    assert result.fields["url"] == "file:///not-opened/secret"
    assert result.raw_payload == raw


def test_quote_native_fields_and_nested_content_preserved_not_recursively_parsed():
    quoted = '<refermsg><type>1</type><svrid>00123</svrid><fromusr>wxid_observed</fromusr>'
    quoted += '<chatusr>group@chatroom</chatusr><displayname>Other Name</displayname>'
    quoted += '<content><![CDATA[<sysmsg type="revokemsg"/>]]></content></refermsg>'
    result = parse_payload(app(57, quoted), native_type=(57 << 32) | 49)
    assert result.kind == "quote"
    assert result.native_type == (57 << 32) | 49
    assert result.fields["quote"]["from_user"] == "wxid_observed"
    assert result.fields["quote"]["content"] == '<sysmsg type="revokemsg"/>'
    assert result.fields["quote"]["server_id"] == "123"
    assert parse_payload(app(57, ""), native_type=49).status == "invalid"


@pytest.mark.parametrize("name", ["../private", "/tmp/private", "C:\\private", "..", ".",
                                   "name:stream", "line&#10;break"])
def test_attachment_path_names_never_become_local_paths(name):
    result = parse_payload(app(6, '<appattach><totallen>12</totallen><fileext>txt</fileext>'
                                '<attachid>opaque</attachid></appattach>', name))
    assert result.fields["file_name"] is None
    assert "unsafe_attachment_name" in result.warnings
    assert "media_path" not in result.fields
    assert result.fields["attachment"]["attachid"] == "opaque"


def test_safe_filename_is_only_metadata_and_unknown_type_is_lossless():
    result = parse_payload(app(6, title="报告.txt"))
    assert result.fields["file_name"] == "报告.txt"
    raw = app(98765, '<unknown secret="kept"/>')
    result = parse_payload(raw, native_type=49)
    assert result.status == "unsupported" and result.raw_payload == raw
    assert result.fields["app_type"] == 98765
    raw = '<future><opaque>all data</opaque></future>'
    assert parse_payload(raw).status == "unsupported"
    assert parse_payload(raw).raw_payload == raw
    assert parse_payload("opaque", native_type=0x12345631).kind == "unknown"


@pytest.mark.parametrize("tag,kind", [("img", "image"), ("voicemsg", "voice"),
    ("videomsg", "video"), ("emoji", "emoji"), ("location", "location"), ("voipmsg", "call")])
def test_media_envelopes_metadata_only(tag, kind):
    raw = f'<msg><{tag} path="../../never-opened" url="https://invalid.test/"/></msg>'
    result = parse_payload(raw)
    assert result.status == "parsed" and result.kind == kind
    assert result.fields["attributes"]["path"] == "../../never-opened"
    assert "media_path" not in result.fields


def test_native_recall_server_id_not_local_msgid():
    result = parse_payload(revoke(), native_type=10002)
    assert result.kind == "recall" and result.status == "parsed"
    assert result.fields["server_id"] == "123"
    assert result.fields["local_id_hint"] == "456"
    assert result.fields["session"] == "group@chatroom"
    assert result.fields["replacement_text"] == '"A"撤回了一条消息'
    assert result.fields["revoked_at"] == "1700000000"
    no_server = revoke().replace("<newmsgid>123</newmsgid>", "")
    result = parse_payload(no_server, native_type=10002)
    assert result.status == "unsupported" and result.fields["server_id"] is None
    assert result.raw_payload == no_server


@pytest.mark.parametrize("identifier", ["-1", "0", "name", "1.2", "1e3", "18446744073709551616"])
def test_invalid_recall_ids_are_unknown(identifier):
    result = parse_payload(revoke(identifier), native_type=10002)
    assert result.kind == "recall" and result.status == "unsupported"
    assert result.fields["server_id"] is None


@pytest.mark.parametrize("value,expected", [(1, "1"), ("001", "1"), (2**64 - 1, str(2**64 - 1)),
    (0, None), (True, None), (1.0, None), (-1, None), (" 1 ", None), ("١", None),
    ("01" * 100, None), (None, None)])
def test_strict_native_identity(value, expected):
    assert native_server_id(value) == expected


@pytest.mark.parametrize("raw", [
    '<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///etc/passwd">]><x>&secret;</x>',
    '<!DOCTYPE x [<!ENTITY a "expanded">]><x>&a;</x>',
    '<!DOCTYPE x SYSTEM "https://invalid.test/schema"><x/>',
])
def test_xml_dtd_entities_external_references_forbidden(raw):
    result = parse_payload(raw)
    assert result.status == "unsafe" and result.raw_payload == raw
    assert "secret" not in repr(result)


@pytest.mark.parametrize("raw", ["<msg>", "<msg></other>", "<", "<msg>&missing;</msg>",
    app(5).replace("<type>5</type>", "<type>5</type><type>6</type>"),
    app(5).replace("<title>Example</title>", "<title><nested/></title>"),
    revoke().replace("<newmsgid>123</newmsgid>", "<newmsgid>123</newmsgid><newmsgid>999</newmsgid>"),
    '<msg><img/><voicemsg/></msg>',
])
def test_malformed_ambiguous_xml_no_partial_parse(raw):
    result = parse_payload(raw)
    assert result.status == "invalid"
    assert result.raw_payload == raw
    assert not result.fields


def test_size_depth_node_limits_and_invalid_binary_no_decompression():
    assert parse_payload("x" * (MAX_PAYLOAD_BYTES + 1)).status == "too_large"
    assert parse_payload("中" * (MAX_PAYLOAD_BYTES // 3 + 1)).status == "too_large"
    deep = "<x>" * (MAX_XML_DEPTH + 1) + "</x>" * (MAX_XML_DEPTH + 1)
    assert parse_payload(deep).status == "invalid"
    wide = "<x>" + "<y/>" * MAX_XML_NODES + "</x>"
    assert parse_payload(wide).status == "invalid"
    for raw in (b"\x28\xb5\x2f\xfd\x80", b"\xff", b"container\x00text", "\ud800"):
        result = parse_payload(raw)
        assert result.status == "unsupported" and result.raw_payload == raw
    assert parse_payload("文本".encode(), native_type=1).fields["text"] == "文本"


@pytest.mark.parametrize("native", [True, "49", -1, 2**64])
def test_bad_type_codes_do_not_guess(native):
    assert parse_payload(app(5), native_type=native).status == "invalid"


def test_invalid_python_input_and_message_contract():
    with pytest.raises(ValidationError):
        parse_payload(None)
    with pytest.raises(ValidationError):
        parse_message({})
    for changes in ({"account_id": ""}, {"sequence": True}, {"content": "a\x00b"},
                    {"timestamp": 7}, {"metadata": {"nan": float("nan")}},
                    {"metadata": {"blob": b"x"}}, {"metadata": {1: "key"}}):
        with pytest.raises(ValidationError):
            snapshot_message(replace(message(), **changes))
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(ValidationError):
        snapshot_message(replace(message(), metadata={"cycle": cyclic}))


@pytest.mark.parametrize("payload,code", [(revoke(), 3), (app(5), 34),
                                          ('<msg><img/></msg>', 43), (revoke(), 99999)])
def test_native_type_conflicts_do_not_fabricate_classification(payload, code):
    result = parse_payload(payload, native_type=code)
    assert result.status == "unsupported" and result.reason == "native_type_envelope_mismatch"
    assert result.raw_payload == payload


def test_utf8_bom_and_contact_card_observed_username():
    assert parse_payload(b"\xef\xbb\xbf" + app(5).encode()).kind == "link"
    raw = '<msg username="explicit-native-id" nickname="Not an identity"/>'
    result = parse_payload(raw, native_type=42)
    assert result.status == "parsed" and result.kind == "contact_card"
    assert result.fields["attributes"]["username"] == "explicit-native-id"
    assert parse_payload('<msg nickname="Name only"/>', native_type=42).status == "unsupported"


@pytest.mark.parametrize("body", ["", "<type/>", "<type>NaN</type>", "<type>-1</type>"])
def test_app_type_required(body):
    assert parse_payload(f'<appmsg>{body}</appmsg>').status == "invalid"


def test_record_structural_limits_and_invalid_unicode(monkeypatch):
    import wechatauto_android.message_formats as module
    for metadata in ({"unicode": "\ud800"}, {"wide": [0] * (MAX_XML_NODES + 1)},
                     {str(i): 0 for i in range(MAX_XML_NODES + 1)}):
        with pytest.raises(ValidationError):
            snapshot_message(replace(message(), metadata=metadata))
    with pytest.raises(ValidationError):
        snapshot_message(replace(message(), content="x" * (MAX_PAYLOAD_BYTES + 1)))
    with pytest.raises(ValidationError):
        snapshot_message(replace(message(), metadata=[]))
    monkeypatch.setattr(module, "MAX_RECORD_BYTES", 100)
    with pytest.raises(ValidationError):
        snapshot_message(replace(message("a"), metadata={"large": "x" * 101}))
    with pytest.raises(ValidationError):
        snapshot_message(replace(message("a"), metadata={"unicode": "中" * 99}))
    with pytest.raises(ValidationError):
        snapshot_message(message("a"))
