"""有身份约束的原生 @ 与单收件人转发；不是昵称拼接或成功占位实现。

本模块需要真机可观测的原生选择器/接收者身份证据。没有这些证据的微信版本
必须报不支持；合成测试选择器不是可部署的微信配置，也不证明跨版本兼容。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .errors import IdentityMismatch, OutcomeUnknown, UnsupportedFeature, ValidationError
from .models import ActionResult
from .profile import Target

if TYPE_CHECKING:
    from .controller import WeChat


SEND_AT_ROLES = (
    "chat.input",
    "chat.send",
    "mention.picker",
    "mention.search",
    "mention.result",
    "mention.member_identity",
    "mention.selected_identity",
)
FORWARD_ROLES = (
    "message.text",
    "message.forward",
    "forward.picker",
    "forward.search",
    "forward.result",
    "forward.recipient_identity",
    "forward.preview",
    "forward.destination",
    "forward.send",
)
EXTENSION_ACTIONS = {"send_at": SEND_AT_ROLES, "forward": FORWARD_ROLES}


def _target(value: Target) -> None:
    if not isinstance(value, Target):
        raise ValidationError("an explicit stable-ID Target is required")


def _clicked(client: WeChat, selector, snapshot) -> None:
    if client.adapter.click(selector, expected_fingerprint=snapshot.fingerprint) is False:
        raise OutcomeUnknown("selection was not acknowledged; stop without retry")


def send_at(
    client: WeChat,
    target: Target,
    text: str,
    member_id: str,
    member_name: str,
    operation_id: str,
) -> ActionResult:
    """正文在前，原生成员提及在末尾；原生 @ 证据消失时不发送。

    ``member_id`` 与群 ``target.chat_id`` 必须分别允许。只接受单个成员，不支持
    @所有人。实际原生提及末尾分隔符仅接受 U+0020/U+2005；其他形式失败。
    选择成员后不再 set_text，避免把原生 mention span 降级为普通字符串。
    """
    _target(target)
    member = Target(member_id, member_name)
    if target.kind != "group":
        raise ValidationError("native mentions require a group Target")
    if member_id in {"all", "@all", "notify@all"}:
        raise UnsupportedFeature("mention-all is not a bounded single-member operation")
    if not client.profile.group_identity_supported:
        raise UnsupportedFeature("profile cannot prove the group's stable chatroom ID")
    client._validate_text(text)
    client._validate_text(text + "@" + member_name + " ")
    client.profile.require(*SEND_AT_ROLES)
    for role in ("mention.result", "mention.member_identity", "mention.selected_identity"):
        client._require_identity_template(role, "member")
    if client.policy.execute:
        client.policy.authorize("send_at", member.chat_id)
        client.policy.authorize("send_at", target.chat_id)

    def prepare():
        # 原生 @ 选择器可能替换编辑器视图；不能用 _text() 等待旧输入框回读。
        client._wait("chat.input", target)
        if client.adapter.set_text(client._selector("chat.input", target), text + "@") is False:
            raise OutcomeUnknown("mention picker trigger was not acknowledged")
        client._wait("mention.picker", target)
        client._text("mention.search", member_name, target)
        selected = client._wait("mention.member_identity", target, member=member_id)
        selected.one(client._selector("mention.member_identity", target, member=member_id))
        result = client._selector("mention.result", target, member=member_id, name=member_name)
        selected.one(result)
        _clicked(client, result, selected)
        snapshot = client._wait("mention.selected_identity", target, member=member_id)
        expected = snapshot.one(client._selector("chat.input", target)).text
        if expected not in {text + "@" + member_name + " ", text + "@" + member_name + "\u2005"}:
            raise IdentityMismatch(
                "native mention editor differs from the authorized body and member"
            )
        snapshot.one(client._selector("mention.selected_identity", target, member=member_id))
        client._verify_target(target)
        snapshot = client._wait("chat.send", target)
        snapshot.one(client._selector("chat.title", target))
        snapshot.one(client._selector("mention.selected_identity", target, member=member_id))
        if snapshot.one(client._selector("chat.input", target)).text != expected:
            raise IdentityMismatch("mention or body changed before the final send")
        return client._selector("chat.send", target), snapshot

    return client._run(
        "send_at",
        target,
        operation_id,
        {"member_id": member_id, "member_name": member_name, "text": text},
        prepare,
    )


def forward_message(
    client: WeChat,
    source: Target,
    destination: Target,
    original_text: str,
    operation_id: str,
) -> ActionResult:
    """转发当前可见且正文唯一的一条消息；来源和单个接收者分别授权。

    这不是全文/所有类型消息搜索；重复正文无法可靠选中同一条消息，必须拒绝。
    journal/result 的 target 是来源会话；目标会话身份进入 journal 的摘要 payload。
    """
    _target(source)
    _target(destination)
    client._validate_text(original_text)
    if destination.kind == "group" and not client.profile.group_identity_supported:
        raise UnsupportedFeature("profile cannot prove destination group identity")
    client.profile.require(*FORWARD_ROLES)
    for role in ("forward.result", "forward.recipient_identity", "forward.destination"):
        client._require_identity_template(role, "chat_id")
    client._require_identity_template("forward.preview", "text")
    client._require_identity_template("message.text", "text")
    if client.policy.execute:
        client.policy.authorize("forward", source.chat_id)
        client.policy.authorize("forward", destination.chat_id)

    def prepare():
        selected = client._snapshot()
        selected.one(client._selector("chat.title", source))
        message = client._selector("message.text", source, text=original_text)
        selected.one(message)
        if client.adapter.long_click(message, expected_fingerprint=selected.fingerprint) is False:
            raise OutcomeUnknown("source-message selection was not acknowledged")
        client._click("message.forward", source)
        client._wait("forward.picker", destination)
        client._text("forward.search", destination.title, destination)
        snapshot = client._wait("forward.recipient_identity", destination)
        snapshot.one(client._selector("forward.recipient_identity", destination))
        recipient = client._selector("forward.result", destination)
        snapshot.one(recipient)
        _clicked(client, recipient, snapshot)
        snapshot = client._wait("forward.destination", destination)
        snapshot.one(client._selector("forward.destination", destination))
        snapshot.one(client._selector("forward.preview", source, text=original_text))
        final = client._selector("forward.send", destination)
        snapshot.one(final)
        return final, snapshot

    return client._run(
        "forward",
        source,
        operation_id,
        {
            "destination_id": destination.chat_id,
            "destination_title": destination.title,
            "destination_kind": destination.kind,
            "original_text": original_text,
        },
        prepare,
    )
