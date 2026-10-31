"""原生提及/转发流程的合成状态机；没有真实微信 UI 或设备证据。"""

from __future__ import annotations

import copy
import sqlite3
from dataclasses import replace
from xml.sax.saxutils import quoteattr

import pytest

from fakes import FakeAdapter, fixture_profile
from wechatauto_android.controller import WeChat
from wechatauto_android.errors import (
    AmbiguousTarget,
    DuplicateOperation,
    IdentityMismatch,
    OutcomeUnknown,
    PermissionDenied,
    SelectorNotFound,
    UnsupportedFeature,
    ValidationError,
)
from wechatauto_android.interactions import (
    EXTENSION_ACTIONS,
    forward_message,
    send_at,
)
from wechatauto_android.profile import Target
from wechatauto_android.safety import SafetyPolicy
from wechatauto_android.ui import Hierarchy

PACKAGE = "com.tencent.mm"
GROUP = Target("group-fixture@chatroom", "Synthetic Group", "group")
SOURCE = Target("wxid_source", "Synthetic Source")
DESTINATION = Target("wxid_destination", "Synthetic Destination")
MEMBER_ID = "wxid_member"
MEMBER_NAME = "成员 👩\u200d💻"
BODY = "正文 e\u0301 "
ORIGINAL = "Unique original 内容"


def interaction_profile():
    original = fixture_profile()
    roles = copy.deepcopy(original.roles)
    for names in EXTENSION_ACTIONS.values():
        for name in names:
            roles.setdefault(name, {"resource_id": PACKAGE + ":id/synthetic_" + name})
    for name in ("mention.member_identity", "mention.selected_identity"):
        roles[name]["text"] = "{member}"
    roles["mention.result"].update(text="{name}", description="member:{member}")
    for name in ("forward.recipient_identity", "forward.destination"):
        roles[name]["text"] = "{chat_id}"
    roles["forward.result"].update(text="{title}", description="recipient:{chat_id}")
    roles["forward.preview"]["text"] = "{text}"
    roles["message.text"]["text"] = "{text}"
    return replace(original, roles=roles, group_identity_supported=True)


class InteractionAdapter(FakeAdapter):
    """所有身份、原生 token 和页面都只是测试夹具，不代表微信实际提供这些节点。"""

    def __init__(self, source=GROUP):
        super().__init__()
        self.chat_id, self.title = source.chat_id, source.title
        self.original = ORIGINAL
        self.member_id, self.member_name = MEMBER_ID, MEMBER_NAME
        self.destination_id, self.destination_name = DESTINATION.chat_id, DESTINATION.title
        self.mention_search = self.forward_search = ""
        self.selected_native = None
        self.separator = "\u2005"
        self.duplicate_mention = self.duplicate_forward = self.duplicate_message = False
        self.drop_native = self.corrupt_draft = False
        self.drop_native_after_info = False
        self.corrupt_after_info = False
        self.wrong_destination_after_select = False
        self.wrong_preview = False
        self.false_selection = self.false_trigger = self.false_long_click = False
        self.fake_native_text_only = False
        self.native_chosen = False

    def _items(self):
        if self.state == "mention_picker":
            result = [
                ("mention.picker", "选择提醒的人"),
                ("mention.search", self.mention_search),
                ("mention.member_identity", self.member_id),
                ("mention.result", self.member_name),
            ]
            if self.duplicate_mention:
                result.append(("mention.result", self.member_name))
            return result
        if self.state == "forward_picker":
            result = [
                ("forward.picker", "选择一个接收者"),
                ("forward.search", self.forward_search),
                ("forward.recipient_identity", self.destination_id),
                ("forward.result", self.destination_name),
            ]
            if self.duplicate_forward:
                result.append(("forward.result", self.destination_name))
            return result
        if self.state == "forward_confirm":
            return [
                (
                    "forward.destination",
                    "wrong" if self.wrong_destination_after_select else self.destination_id,
                ),
                ("forward.preview", "Wrong source" if self.wrong_preview else self.original),
                ("forward.send", "发送"),
            ]
        result = super()._items()
        if self.state == "chat":
            if self.duplicate_message:
                result.append(("message.text", self.original))
            if self.selected_native is not None:
                result.append(("mention.selected_identity", self.selected_native))
        if self.state == "message_menu":
            result.append(("message.forward", "转发"))
        return result

    def hierarchy(self):
        nodes = []
        for index, (role, text) in enumerate(self._items()):
            attrs = {
                "text": text,
                "resource-id": PACKAGE + ":id/synthetic_" + role,
                "package": PACKAGE,
                "bounds": f"[0,{index * 20}][200,{index * 20 + 20}]",
                "enabled": "true",
                "clickable": "true",
            }
            if role == "mention.result":
                attrs["content-desc"] = "member:" + self.member_id
            if role == "forward.result":
                attrs["content-desc"] = "recipient:" + self.destination_id
            nodes.append(
                "<node "
                + " ".join(f"{key}={quoteattr(value)}" for key, value in attrs.items())
                + "/>"
            )
        return Hierarchy.parse("<hierarchy>" + "".join(nodes) + "</hierarchy>")

    def set_text(self, selector, text):
        role = self._role(selector)
        if role == "chat.input" and text.endswith("@"):
            assert not self.native_chosen, "set_text after native mention would destroy the span"
            self.calls.append(("set_text", role, text))
            self.draft = text
            if self.false_trigger:
                return False
            if not self.fake_native_text_only:
                self.state = "mention_picker"
            return None
        if role in {"mention.search", "forward.search"}:
            self.calls.append(("set_text", role, text))
            setattr(self, "mention_search" if role == "mention.search" else "forward_search", text)
            return None
        return super().set_text(selector, text)

    def click(self, selector, *, expected_fingerprint=None):
        role = self._role(selector, expected_fingerprint)
        if role in {"mention.result", "message.forward", "forward.result", "forward.send"}:
            self.calls.append(("click", role))
            if role in {"mention.result", "forward.result"} and self.false_selection:
                return False
            if role == "mention.result":
                self.native_chosen = True
                self.draft += self.member_name + self.separator
                if self.corrupt_draft:
                    self.draft += "UNAUTHORIZED"
                self.selected_native = None if self.drop_native else self.member_id
                self.state = "chat"
            elif role == "message.forward":
                self.state = "forward_picker"
            elif role == "forward.result":
                self.state = "forward_confirm"
            else:
                self.commits.append(role)
                if self.error_commit:
                    raise TimeoutError("synthetic uncertain forward")
                if self.false_commit:
                    return False
            return None
        return super().click(selector, expected_fingerprint=expected_fingerprint)

    def long_click(self, selector, *, expected_fingerprint=None):
        if self.false_long_click:
            return False
        return super().long_click(selector, expected_fingerprint=expected_fingerprint)

    def press_back(self):
        if self.state == "info" and self.native_chosen and self.drop_native_after_info:
            self.selected_native = None
        if self.state == "info" and self.native_chosen and self.corrupt_after_info:
            self.draft += "changed after identity check"
        return super().press_back()


def client(
    tmp_path, adapter=None, *, execute=True, profile=None, allowed=None, account_verifier=None
):
    if adapter is None:
        adapter = InteractionAdapter()
    policy = SafetyPolicy(
        "SERIAL",
        "wxid_self",
        execute=execute,
        allowed_actions={"send_at", "forward"},
        allowed_targets=allowed
        if allowed is not None
        else {GROUP.chat_id, SOURCE.chat_id, DESTINATION.chat_id, MEMBER_ID},
    )
    return WeChat(
        adapter,
        profile or interaction_profile(),
        policy,
        tmp_path / "ops.sqlite",
        transition_timeout=0.001,
        account_verifier=account_verifier or (lambda: "wxid_self"),
    )


def mention(api, operation_id="mention-test-001"):
    return send_at(api, GROUP, BODY, MEMBER_ID, MEMBER_NAME, operation_id)


def forward(api, operation_id="forward-test-001"):
    return forward_message(api, SOURCE, DESTINATION, ORIGINAL, operation_id)


def test_action_contracts_are_registered():
    from wechatauto_android.controller import IMPLEMENTED_ACTIONS

    for action, roles in EXTENSION_ACTIONS.items():
        assert IMPLEMENTED_ACTIONS[action] == roles


@pytest.mark.parametrize("separator", [" ", "\u2005"])
def test_native_mention_is_selected_not_literal_and_body_precedes_it(tmp_path, separator):
    adapter = InteractionAdapter()
    adapter.separator = separator
    api = client(tmp_path, adapter)
    result = mention(api)
    assert result.status == "submitted" and not result.success
    assert adapter.commits == ["chat.send"]
    assert adapter.draft == BODY + "@" + MEMBER_NAME + separator
    assert adapter.selected_native == MEMBER_ID
    assert adapter.native_chosen is True
    assert [
        (entry[1], entry[2])
        for entry in adapter.calls
        if entry[0] == "set_text" and entry[1] in {"chat.input", "mention.search"}
    ] == [("chat.input", BODY + "@"), ("mention.search", MEMBER_NAME)]
    assert result.assurance.startswith("independent_account_proof")


def test_dry_run_both_actions_never_touch_device_or_create_journal(tmp_path):
    class NoDevice:
        def __getattr__(self, name):
            raise AssertionError("dry-run touched device")

    api = client(tmp_path, NoDevice(), execute=False)
    assert mention(api).status == "dry_run"
    assert forward(api).status == "dry_run"
    assert not (tmp_path / "ops.sqlite").exists()


@pytest.mark.parametrize("allowed", [{GROUP.chat_id}, {MEMBER_ID}, set()])
def test_group_and_pinged_member_require_independent_allowlisting(tmp_path, allowed):
    adapter = InteractionAdapter()
    with pytest.raises(PermissionDenied):
        mention(client(tmp_path, adapter, allowed=allowed))
    assert adapter.calls == [] and not (tmp_path / "ops.sqlite").exists()


@pytest.mark.parametrize("member", ["all", "@all", "notify@all"])
def test_mention_all_is_not_a_single_member_action(tmp_path, member):
    with pytest.raises(UnsupportedFeature):
        send_at(client(tmp_path), GROUP, BODY, member, "所有人", "mention-all-001")


def test_mentions_require_provable_group_not_contact(tmp_path):
    with pytest.raises(ValidationError):
        send_at(client(tmp_path), SOURCE, BODY, MEMBER_ID, MEMBER_NAME, "mention-test-001")
    with pytest.raises(UnsupportedFeature):
        mention(
            client(tmp_path, profile=replace(interaction_profile(), group_identity_supported=False))
        )


@pytest.mark.parametrize(
    "role", ["mention.result", "mention.member_identity", "mention.selected_identity"]
)
def test_mention_roles_cannot_replace_member_id_with_name(tmp_path, role):
    profile = interaction_profile()
    roles = copy.deepcopy(profile.roles)
    roles[role] = {"text": "{name}"}
    with pytest.raises(PermissionDenied):
        mention(client(tmp_path, profile=replace(profile, roles=roles)))


@pytest.mark.parametrize(
    "role",
    [
        "mention.picker",
        "mention.member_identity",
        "mention.selected_identity",
        "forward.destination",
        "forward.preview",
    ],
)
def test_missing_critical_roles_are_explicitly_unsupported(tmp_path, role):
    profile = interaction_profile()
    roles = copy.deepcopy(profile.roles)
    del roles[role]
    api = client(tmp_path, execute=False, profile=replace(profile, roles=roles))
    with pytest.raises(UnsupportedFeature):
        (mention if role.startswith("mention") else forward)(api)


@pytest.mark.parametrize(
    "mutation,error",
    [
        ("duplicate_mention", AmbiguousTarget),
        ("drop_native", SelectorNotFound),
        ("corrupt_draft", IdentityMismatch),
        ("corrupt_after_info", IdentityMismatch),
        ("drop_native_after_info", SelectorNotFound),
        ("false_selection", OutcomeUnknown),
        ("false_trigger", OutcomeUnknown),
        ("fake_native_text_only", SelectorNotFound),
    ],
)
def test_native_mention_negative_paths_never_send(tmp_path, mutation, error):
    adapter = InteractionAdapter()
    setattr(adapter, mutation, True)
    with pytest.raises(error):
        mention(client(tmp_path, adapter))
    assert adapter.commits == []


def test_member_picker_different_stable_id_is_not_same_display_name(tmp_path):
    adapter = InteractionAdapter()
    adapter.member_id = "wxid_impostor"
    with pytest.raises(SelectorNotFound):
        mention(client(tmp_path, adapter))
    assert not adapter.native_chosen and not adapter.commits


def test_unknown_native_spacing_fails_closed(tmp_path):
    adapter = InteractionAdapter()
    adapter.separator = "\n"
    with pytest.raises(IdentityMismatch):
        mention(client(tmp_path, adapter))
    assert not adapter.commits


def test_forward_has_source_and_destination_proof_and_single_commit(tmp_path):
    adapter = InteractionAdapter(SOURCE)
    result = forward(client(tmp_path, adapter))
    assert result.status == "submitted" and result.target == SOURCE.chat_id
    assert adapter.commits == ["forward.send"]
    assert adapter.forward_search == DESTINATION.title
    with sqlite3.connect(tmp_path / "ops.sqlite") as connection:
        row = connection.execute("SELECT action,target,status FROM operations").fetchone()
    assert row == ("forward", SOURCE.chat_id, "submitted")


@pytest.mark.parametrize("allowed", [{SOURCE.chat_id}, {DESTINATION.chat_id}, set()])
def test_forward_requires_both_source_and_destination_allowlists(tmp_path, allowed):
    adapter = InteractionAdapter(SOURCE)
    with pytest.raises(PermissionDenied):
        forward(client(tmp_path, adapter, allowed=allowed))
    assert not adapter.calls and not (tmp_path / "ops.sqlite").exists()


@pytest.mark.parametrize(
    "mutation,error",
    [
        ("duplicate_message", AmbiguousTarget),
        ("duplicate_forward", AmbiguousTarget),
        ("wrong_destination_after_select", SelectorNotFound),
        ("wrong_preview", SelectorNotFound),
        ("false_selection", OutcomeUnknown),
        ("false_long_click", OutcomeUnknown),
    ],
)
def test_forward_negative_paths_never_commit(tmp_path, mutation, error):
    adapter = InteractionAdapter(SOURCE)
    setattr(adapter, mutation, True)
    with pytest.raises(error):
        forward(client(tmp_path, adapter))
    assert not adapter.commits


def test_same_recipient_name_different_id_fails(tmp_path):
    adapter = InteractionAdapter(SOURCE)
    adapter.destination_id = "wxid_impostor"
    with pytest.raises(SelectorNotFound):
        forward(client(tmp_path, adapter))
    assert not adapter.commits


@pytest.mark.parametrize(
    "role",
    ["forward.result", "forward.recipient_identity", "forward.destination", "forward.preview"],
)
def test_forward_confirmation_requires_exact_identity_templates(tmp_path, role):
    profile = interaction_profile()
    roles = copy.deepcopy(profile.roles)
    roles[role] = {"text": "ambiguous display label"}
    with pytest.raises(PermissionDenied):
        forward(client(tmp_path, profile=replace(profile, roles=roles)))


@pytest.mark.parametrize("action", ["mention", "forward"])
@pytest.mark.parametrize("failure", ["false_commit", "error_commit"])
def test_uncertain_final_gesture_is_durable_and_never_replayed(tmp_path, action, failure):
    adapter = InteractionAdapter(GROUP if action == "mention" else SOURCE)
    setattr(adapter, failure, True)
    api = client(tmp_path, adapter)
    operation = mention if action == "mention" else forward
    with pytest.raises(OutcomeUnknown):
        operation(api)
    assert len(adapter.commits) == 1
    with sqlite3.connect(tmp_path / "ops.sqlite") as connection:
        assert connection.execute("SELECT status FROM operations").fetchone() == ("unknown",)
    with pytest.raises(DuplicateOperation):
        operation(api)
    assert len(adapter.commits) == 1


def test_final_account_change_blocks_forward(tmp_path):
    adapter = InteractionAdapter(SOURCE)
    with pytest.raises(IdentityMismatch):
        forward(client(tmp_path, adapter, account_verifier=lambda: "another-account"))
    assert not adapter.commits


def test_forward_group_destination_needs_identity_support(tmp_path):
    profile = replace(interaction_profile(), group_identity_supported=False)
    with pytest.raises(UnsupportedFeature):
        forward_message(
            client(tmp_path, profile=profile), SOURCE, GROUP, ORIGINAL, "forward-group-001"
        )


@pytest.mark.parametrize("body", ["", "\x00", "x" * 5001])
def test_invalid_content_fails_before_navigation(tmp_path, body):
    adapter = InteractionAdapter()
    with pytest.raises(ValidationError):
        send_at(client(tmp_path, adapter), GROUP, body, MEMBER_ID, MEMBER_NAME, "invalid-001")
    assert not adapter.calls


def test_public_facades_route_to_same_bounded_workflows(tmp_path):
    api = client(tmp_path, execute=False)
    assert (
        api.send_at(
            GROUP,
            BODY,
            member_id=MEMBER_ID,
            member_name=MEMBER_NAME,
            operation_id="facade-mention-001",
        ).action
        == "send_at"
    )
    assert (
        api.forward_message(SOURCE, DESTINATION, ORIGINAL, operation_id="facade-forward-001").action
        == "forward"
    )
    assert not (tmp_path / "ops.sqlite").exists()


def test_string_nicknames_are_not_stable_target_objects(tmp_path):
    api = client(tmp_path, execute=False)
    with pytest.raises(ValidationError):
        send_at(api, "Synthetic Group", BODY, MEMBER_ID, MEMBER_NAME, "invalid-target-001")
    with pytest.raises(ValidationError):
        forward_message(api, SOURCE, "Synthetic Destination", ORIGINAL, "invalid-target-002")
