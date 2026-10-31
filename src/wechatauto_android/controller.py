"""有限状态的 Android UI 操作；未知页面/版本/身份一律中止。"""
from __future__ import annotations

import hashlib
import time
import tempfile
import os
import stat
from pathlib import Path
from typing import Any, Callable

from .errors import (
    IdentityMismatch, OutcomeUnknown, PermissionDenied, SelectorNotFound,
    UnsupportedFeature, ValidationError,
)
from .models import ActionResult, UIObservation
from .interactions import EXTENSION_ACTIONS, send_at as _send_at, forward_message as _forward_message
from .moments import MOMENT_ACTIONS, MOMENT_CONTEXT_ROLES, MomentOperations
from .profile import Profile, Target
from .safety import OperationJournal, SafetyPolicy, device_lock, validate_operation_id
from .ui import Hierarchy, Selector

ACCOUNT_ROLES = ("home.marker", "home.me", "home.chats", "account.entry", "account.identity")
CHAT_ROLES = ("home.search", "search.input", "search.result", "chat.title", "chat.info", "chat.identity")

# 这是代码实现状态，不是设备验证或跨 8.x 版本保证。
IMPLEMENTED_ACTIONS = {
    "send_text": ("chat.input", "chat.send"),
    "reply": ("message.quote", "chat.quote_preview", "chat.input", "chat.send"),
    "recall": ("message.self_text", "message.recall"),
    "voice_call": ("chat.more", "chat.call", "call.voice"),
    "video_call": ("chat.more", "chat.call", "call.video"),
    "poke": ("chat.target_avatar",),
    "rename_remark": ("contact.entry", "contact.identity", "contact.more", "contact.remark", "remark.input", "remark.save"),
    "send_file": ("chat.more", "attachment.file", "attachment.browse", "attachment.filename", "attachment.preview_name", "attachment.target", "attachment.send"),
    "send_image": ("chat.more", "attachment.image", "attachment.filename", "attachment.preview_name", "attachment.target", "attachment.send"),
}
IMPLEMENTED_ACTIONS.update(MOMENT_ACTIONS)
IMPLEMENTED_ACTIONS.update(EXTENSION_ACTIONS)

UNAVAILABLE = {
    "all_chat_listener": "Visible UI does not expose complete history or durable message sequence IDs",
    "voice_forward": "Android voice retransmission workflow is not validated",
    "labels": "Contact label CRUD/member workflows require device UI calibration and implementation",
    "bulk_send": "Label/bulk dispatch is not implemented; use separately approved per-target calls",
    "group_friend": "Group-member stable identity/verification dialog workflows not yet implemented",
    "moments_reply": "Replies to particular comments require another verified comment observation; not implemented",
    "moments_post": "Deliberately absent in pinned upstream too",
    "subwindows": "Android does not provide Windows independent chat windows",
    "encrypted_media": "No app-private cache/key extraction; use authorized exported media only",
}

class WeChat:
    def __init__(self, adapter: Any, profile: Profile, policy: SafetyPolicy,
                 journal_path: str | Path = "private/operations.sqlite", *,
                 transition_timeout: float = 3.0,
                 account_verifier: Callable[[], str] | None = None):
        if not 0 < transition_timeout <= 30:
            raise ValidationError("transition_timeout must be in (0,30]")
        self.adapter = adapter
        self.profile = profile
        self.policy = policy
        self.journal_path = Path(journal_path)
        self.transition_timeout = transition_timeout
        self.account_verifier = account_verifier

    def _selector(self, role: str, target: Target | None = None, **values: str) -> Selector:
        parameters = {"account_id": self.policy.account_id}
        if target:
            parameters.update(chat_id=target.chat_id, title=target.title)
        parameters.update(values)
        return self.profile.selector(role, **parameters)

    def _snapshot(self) -> Hierarchy:
        identity = self.adapter.identity()
        self.policy.verify_device(identity, self.profile.package, self.profile.version)
        if self.profile.calibrated and identity.sdk not in self.profile.android_sdks:
            raise IdentityMismatch("Android SDK is not in this calibrated profile")
        return self.adapter.hierarchy()

    def _wait(self, role: str, target: Target | None = None, **values: str) -> Hierarchy:
        selector = self._selector(role, target, **values)
        deadline = time.monotonic() + self.transition_timeout
        while True:
            snapshot = self._snapshot()
            try:
                snapshot.one(selector)
                return snapshot
            except SelectorNotFound:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))

    def _click(self, role: str, target: Target | None = None, **values: str) -> None:
        snapshot = self._wait(role, target, **values)
        if target is not None and role in {"chat.more", "chat.info"}:
            snapshot.one(self._selector("chat.title", target))
        if self.adapter.click(self._selector(role, target, **values), expected_fingerprint=snapshot.fingerprint) is False:
            raise OutcomeUnknown("Navigation click returned false; stop without further steps")

    def _text(self, role: str, text: str, target: Target | None = None) -> None:
        self._wait(role, target)
        result = self.adapter.set_text(self._selector(role, target), text)
        if result is False:
            raise OutcomeUnknown("Text entry RPC returned false; input state is not trusted")
        current = self._wait(role, target).one(self._selector(role, target))
        if current.text != text:
            raise IdentityMismatch("Input readback differs from the exact requested Unicode text")

    def _back(self, role: str, target: Target | None = None) -> None:
        self._snapshot()
        if self.adapter.press_back() is False:
            raise OutcomeUnknown("Back key returned false; stop without further steps")
        self._wait(role, target)

    def _verify_account(self) -> dict[str, str]:
        if not self.profile.calibrated:
            raise PermissionDenied("Account navigation requires an operator-calibrated profile")
        self.profile.require(*ACCOUNT_ROLES)
        self._wait("home.marker")
        self._click("home.me")
        self._click("account.entry")
        self._wait("account.identity")
        # identity selector must actually include the expected account, not a constant label.
        self._require_identity_template("account.identity", "account_id")
        self._back("account.entry")
        self._click("home.chats")
        self._wait("home.marker")
        return {"account_id": self.policy.account_id, "serial": self.policy.serial,
                "version": self.profile.version}

    def verify_account(self) -> dict[str, str]:
        """只读账号信息但会导航；调用方应确保从微信首页开始。"""
        with device_lock(self.policy.serial, self.policy.lock_timeout):
            return self._verify_account()

    def _require_identity_template(self, role: str, parameter: str) -> None:
        self.profile.require(role)
        selector = self.profile.roles[role]
        if not any("{" + parameter + "}" in str(selector.get(field, ""))
                   for field in ("text", "description")):
            raise PermissionDenied(f"{role} must prove the exact {{{parameter}}}, not a display name")

    def _chat_roles(self, target: Target) -> tuple[str, ...]:
        if target.kind == "contact" and self.profile.identity_route == "contact_profile":
            return tuple(role for role in CHAT_ROLES if role != "chat.identity") + (
                "contact.entry", "contact.identity", "chat.info_marker")
        return CHAT_ROLES

    def _identity_role(self, target: Target) -> str:
        return ("contact.identity" if target.kind == "contact" and
                self.profile.identity_route == "contact_profile" else "chat.identity")

    def _open_chat(self, target: Target) -> None:
        self.profile.require(*self._chat_roles(target))
        self._require_identity_template(self._identity_role(target), "chat_id")
        self._require_identity_template("chat.title", "title")
        if target.kind == "group" and not self.profile.group_identity_supported:
            raise UnsupportedFeature("Group chatroom ID is not exposed by this profile; title is not identity")
        self._click("home.search")
        self._text("search.input", target.title)
        self._click("search.result", target)
        self._verify_target(target)

    def _verify_target(self, target: Target) -> None:
        self._wait("chat.title", target)
        self._click("chat.info", target)
        if self._identity_role(target) == "contact.identity":
            self._click("contact.entry", target)
            self._wait("contact.identity", target)
            self._back("chat.info_marker", target)
        else:
            self._wait("chat.identity", target)
        self._back("chat.title", target)

    def _validate_text(self, text: str) -> None:
        if not isinstance(text, str) or not text.strip() or len(text) > self.policy.max_text_length:
            raise ValidationError("Message text is empty or exceeds the configured bound")
        try:
            text.encode("utf-8")
        except UnicodeError as exc:
            raise ValidationError("Text contains invalid Unicode") from exc
        if any(ord(c) < 32 and c not in "\n\t" for c in text) or "\x7f" in text:
            raise ValidationError("Unsupported control character in text")

    def _run(self, action: str, target: Target, operation_id: str, payload: dict[str, Any],
             prepare: Callable[[], tuple[Selector, Hierarchy]],
             verify: Callable[[], bool] | None = None,
             commit: Callable[[Selector, Hierarchy], Any] | None = None,
             context_roles: tuple[str, ...] | None = None,
             navigate: Callable[[], None] | None = None,
             before_account: Callable[[], None] | None = None) -> ActionResult:
        validate_operation_id(operation_id)
        if action not in IMPLEMENTED_ACTIONS:
            raise UnsupportedFeature(UNAVAILABLE.get(action, action))
        self.profile.require(*ACCOUNT_ROLES, *(context_roles if context_roles is not None else self._chat_roles(target)), *IMPLEMENTED_ACTIONS[action])
        self._require_identity_template("account.identity", "account_id")
        if navigate is None:
            self._require_identity_template(self._identity_role(target), "chat_id")
            self._require_identity_template("chat.title", "title")
        if target.kind == "group" and not self.profile.group_identity_supported:
            raise UnsupportedFeature("No stable group identity in this profile")
        if not self.policy.execute:
            return ActionResult(operation_id, action, target.chat_id, "dry_run",
                                "Planned only: no device connection, navigation, file transfer or send",
                                {"profile": self.profile.name, "calibrated": self.profile.calibrated,
                                 "payload_sha256": hashlib.sha256(repr(payload).encode()).hexdigest()})
        self.policy.authorize(action, target.chat_id)
        if not self.profile.calibrated:
            raise PermissionDenied("Profile is uncalibrated; live writes are disabled")
        if ("commit.account_identity" not in self.profile.roles and self.account_verifier is None
                and not self.policy.assume_exclusive_device):
            raise PermissionDenied("Strict mode requires fresh commit account proof; UI-only prior proof needs explicit assume_exclusive_device risk acknowledgment")
        with device_lock(self.policy.serial, self.policy.lock_timeout):
            journal = OperationJournal(self.journal_path)
            journal.reserve(operation_id, self.policy, action, target.chat_id, payload)
            committing = False
            try:
                if before_account is not None:
                    before_account()
                self._verify_account()
                if navigate is None:
                    self._open_chat(target)
                else:
                    navigate()
                selector, snapshot = prepare()
                assurance = self._commit_account_proof(snapshot)
                # 先落盘，再进行一次有界提交动作；双击是两次有意点击，不是失败重试。
                journal.mark(operation_id, "committing")
                committing = True
                result = (commit(selector, snapshot) if commit is not None else
                          self.adapter.click(selector, expected_fingerprint=snapshot.fingerprint))
                if result is False:
                    raise OutcomeUnknown("Commit returned false; never retry automatically")
                journal.mark(operation_id, "submitted")
                if verify is not None and verify():
                    journal.mark(operation_id, "verified")
                    return ActionResult(operation_id, action, target.chat_id, "verified",
                                        "Visible postcondition observed; not proof of server delivery",
                                        assurance=assurance)
                return ActionResult(operation_id, action, target.chat_id, "submitted",
                                    "One bounded commit attempt issued; delivery/effect not independently verified",
                                    assurance=assurance)
            except BaseException as exc:
                journal.mark(operation_id, "unknown" if committing or isinstance(exc, OutcomeUnknown) else "failed_before_commit")
                if committing and not isinstance(exc, (KeyboardInterrupt, SystemExit, OutcomeUnknown)):
                    raise OutcomeUnknown("Commit may have occurred; inspect device and journal, do not retry") from exc
                raise

    def _commit_account_proof(self, snapshot: Hierarchy) -> str:
        if "commit.account_identity" in self.profile.roles:
            self._require_identity_template("commit.account_identity", "account_id")
            snapshot.one(self._selector("commit.account_identity"))
            return "fresh_ui_account_proof_recent_target_proof_not_atomic"
        if self.account_verifier is not None:
            if self.account_verifier() != self.policy.account_id:
                raise IdentityMismatch("Independent precommit account proof changed")
            return "independent_account_proof_recent_target_proof_not_atomic"
        if self.policy.assume_exclusive_device:
            return "prior_account_proof_ONLY_assumes_exclusive_device_and_no_account_switch"
        raise PermissionDenied("Fresh account proof unavailable at commit")

    def send_text(self, target: Target, text: str, *, operation_id: str) -> ActionResult:
        self._validate_text(text)
        def prepare():
            self._text("chat.input", text, target)
            self._verify_target(target)
            snapshot = self._wait("chat.send", target)
            if snapshot.one(self._selector("chat.input", target)).text != text:
                raise IdentityMismatch("Draft changed before final send")
            snapshot.one(self._selector("chat.title", target))
            return self._selector("chat.send", target), snapshot
        return self._run("send_text", target, operation_id, {"text": text}, prepare)

    def reply(self, target: Target, original_text: str, text: str, *, operation_id: str) -> ActionResult:
        self._validate_text(text)
        self._validate_text(original_text)
        self.profile.require("message.text")
        self._require_identity_template("message.text", "text")
        self._require_identity_template("chat.quote_preview", "text")
        def prepare():
            selector = self._selector("message.text", target, text=original_text)
            snapshot = self._snapshot()
            snapshot.one(self._selector("chat.title", target))
            snapshot.one(selector)  # 重复正文不猜第一条。
            if self.adapter.long_click(selector, expected_fingerprint=snapshot.fingerprint) is False:
                raise OutcomeUnknown("Long-click was not acknowledged; stop without further steps")
            self._click("message.quote", target)
            self._wait("chat.quote_preview", target, text=original_text)
            self._text("chat.input", text, target)
            self._verify_target(target)
            snapshot = self._wait("chat.send", target)
            snapshot.one(self._selector("chat.quote_preview", target, text=original_text))
            if snapshot.one(self._selector("chat.input", target)).text != text:
                raise IdentityMismatch("Reply input changed")
            return self._selector("chat.send", target), snapshot
        return self._run("reply", target, operation_id,
                         {"original_text": original_text, "text": text}, prepare)

    def recall(self, target: Target, original_text: str, *, operation_id: str) -> ActionResult:
        self._validate_text(original_text)
        self.profile.require("message.self_text")
        self._require_identity_template("message.self_text", "text")
        def prepare():
            selector = self._selector("message.self_text", target, text=original_text)
            snapshot = self._snapshot()
            snapshot.one(self._selector("chat.title", target))
            snapshot.one(selector)
            if self.adapter.long_click(selector, expected_fingerprint=snapshot.fingerprint) is False:
                raise OutcomeUnknown("Long-click was not acknowledged; stop without further steps")
            snapshot = self._wait("message.recall", target)
            return self._selector("message.recall", target), snapshot
        return self._run("recall", target, operation_id, {"original_text": original_text}, prepare)

    def voice_call(self, target: Target, *, operation_id: str, video: bool = False) -> ActionResult:
        action = "video_call" if video else "voice_call"
        def prepare():
            self._click("chat.more", target)
            self._click("chat.call", target)
            # 选择语音/视频本身会触发通话，因此它是唯一的最终提交步骤。
            role = "call.video" if video else "call.voice"
            snapshot = self._wait(role, target)
            return self._selector(role, target), snapshot
        return self._run(action, target, operation_id, {"video": video}, prepare)

    def poke(self, target: Target, *, operation_id: str) -> ActionResult:
        self._require_identity_template("chat.target_avatar", "title")
        if target.kind != "contact":
            raise UnsupportedFeature("Group pokes require a separately verified member identity")
        def prepare():
            self._verify_target(target)
            snapshot = self._wait("chat.target_avatar", target)
            snapshot.one(self._selector("chat.title", target))
            return self._selector("chat.target_avatar", target), snapshot
        def commit(selector, snapshot):
            return self.adapter.double_click(selector, expected_fingerprint=snapshot.fingerprint)
        return self._run("poke", target, operation_id, {}, prepare, commit=commit)

    def send_at(self, target: Target, text: str, *, member_id: str, member_name: str,
                operation_id: str) -> ActionResult:
        return _send_at(self, target, text, member_id, member_name, operation_id)

    def forward_message(self, source: Target, destination: Target, original_text: str, *,
                        operation_id: str) -> ActionResult:
        return _forward_message(self, source, destination, original_text, operation_id)

    def rename_remark(self, target: Target, new_remark: str, *, operation_id: str) -> ActionResult:
        self._validate_text(new_remark)
        if target.kind != "contact":
            raise UnsupportedFeature("Contact remark workflow does not rename groups")
        self._require_identity_template("contact.identity", "chat_id")
        def prepare():
            self._click("chat.info", target)
            self._click("contact.entry", target)
            self._wait("contact.identity", target)
            self._click("contact.more", target)
            self._click("contact.remark", target)
            self._text("remark.input", new_remark, target)
            snapshot = self._wait("remark.save", target)
            if snapshot.one(self._selector("remark.input", target)).text != new_remark:
                raise IdentityMismatch("Remark changed before save")
            return self._selector("remark.save", target), snapshot
        return self._run("rename_remark", target, operation_id, {"new_remark": new_remark}, prepare)

    def send_file(self, target: Target, path: str | Path, *, operation_id: str,
                  image: bool = False) -> ActionResult:
        local = Path(path).absolute()
        if any(part.is_symlink() for part in (local, *local.parents)):
            raise ValidationError("Attachment path must not contain symlinks")
        try:
            descriptor = os.open(local, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 100 * 1024 * 1024:
                    raise ValidationError("Attachment must be a regular local file <=100MiB")
                approved_bytes = handle.read(100 * 1024 * 1024 + 1)
                if len(approved_bytes) > 100 * 1024 * 1024:
                    raise ValidationError("Attachment grew beyond transfer bound")
        except OSError as exc:
            raise ValidationError("Attachment cannot be read") from exc
        if any(c in local.name for c in "\r\n\x00"):
            raise ValidationError("Invalid attachment filename")
        if image and local.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise ValidationError("Image must use a supported image extension")
        action = "send_image" if image else "send_file"
        self._require_identity_template("attachment.target", "chat_id")
        self._require_identity_template("attachment.filename", "filename")
        self._require_identity_template("attachment.preview_name", "filename")
        digest = hashlib.sha256(approved_bytes).hexdigest()
        remote_name = digest[:16] + local.suffix.lower()
        remote = "/storage/emulated/0/Download/wechatauto/" + remote_name
        def prepare():
            # 发送预先散列的不可变内容，避免导航期间源文件被替换。
            with tempfile.TemporaryDirectory(prefix="wechatauto-attachment-") as directory:
                staged = Path(directory) / remote_name
                fd = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(approved_bytes)
                self.adapter.push(staged, remote, expected_sha256=digest)
            self._click("chat.more", target)
            self._click("attachment.image" if image else "attachment.file", target)
            if not image:
                self._click("attachment.browse", target)
            self._click("attachment.filename", target, filename=remote_name)
            self._wait("attachment.preview_name", target, filename=remote_name)
            self._require_identity_template("attachment.target", "chat_id")
            snapshot = self._wait("attachment.target", target)
            return self._selector("attachment.send", target), snapshot
        return self._run(action, target, operation_id,
                         {"sha256": digest, "filename": local.name, "size": len(approved_bytes)}, prepare)

    def send_image(self, target: Target, path: str | Path, *, operation_id: str) -> ActionResult:
        return self.send_file(target, path, operation_id=operation_id, image=True)

    def read_visible(self, target: Target, collection: str = "messages") -> list[UIObservation]:
        """从已打开且经过身份检查的聊天读取可见数据；不声称完整历史。"""
        if collection not in self.profile.collections:
            raise UnsupportedFeature(f"No visible {collection} collection selector in profile")
        self.profile.require(*self._chat_roles(target))
        self._require_identity_template(self._identity_role(target), "chat_id")
        if target.kind == "group" and not self.profile.group_identity_supported:
            raise UnsupportedFeature("No stable group identity in this profile")
        with device_lock(self.policy.serial, self.policy.lock_timeout):
            # Reading also needs account proof, never leak a switched account into cached IDs.
            self._verify_account()
            self._open_chat(target)
            snapshot = self._snapshot()
            assurance = self._commit_account_proof(snapshot)
            collection_selector = dict(self.profile.collections[collection])
            collection_selector.setdefault("package", self.profile.package)
            selector = Selector.from_dict(collection_selector)
            result = []
            for index, node in enumerate(snapshot.find_all(selector)):
                content = node.text or node.description
                key = f"{self.policy.account_id}\0{target.chat_id}\0{snapshot.fingerprint}\0{index}\0{content}"
                result.append(UIObservation(hashlib.sha256(key.encode()).hexdigest(),
                                            self.policy.account_id, target.chat_id, content,
                                            collection, snapshot.fingerprint, assurance=assurance))
            return result

    @property
    def Moment(self) -> MomentOperations:
        return MomentOperations(self)

    def capabilities(self) -> dict[str, Any]:
        return capabilities(self.profile)

    def ChatWith(self, who: str, *, title: str, kind: str = "contact") -> Chat:
        return Chat(self, Target(who, title, kind))

    def ForwardMessage(self, to: Target, *, chat: Target, match: str, operation_id: str):
        return self.forward_message(chat, to, match, operation_id=operation_id)

    def RenameMember(self, who: str, new_remark: str, *, title: str, operation_id: str):
        return self.rename_remark(Target(who, title), new_remark, operation_id=operation_id)

    def SendMsg(self, msg: str, who: str, *, title: str, operation_id: str, **kwargs: Any):
        if kwargs:
            raise UnsupportedFeature("Unsupported SendMsg options (native @ requires its own picker)")
        return self.send_text(Target(who, title), msg, operation_id=operation_id)

    def __getattr__(self, name: str):
        if name in {"SendToLabel", "ForwardToLabel", "ForwardVoiceMessage",
                    "CreateLabel", "RenameLabel", "DeleteLabel", "AddLabelMembers", "RemoveLabelMembers",
                    "AddFriendFromGroup", "StartListening", "AddListenAll",
                    "GetAllSubWindow", "GetSubWindow", "ShutDown"}:
            raise UnsupportedFeature(f"{name} is not implemented for Android; consult capability matrix")
        raise AttributeError(name)

class Chat:
    def __init__(self, client: WeChat, target: Target):
        self.client, self.target = client, target

    def SendMsg(self, msg: str, *, operation_id: str):
        return self.client.send_text(self.target, msg, operation_id=operation_id)

    def SendFiles(self, filepath: str, *, operation_id: str):
        return self.client.send_file(self.target, filepath, operation_id=operation_id)

    def GetAllMessage(self):
        raise UnsupportedFeature("Full history is unavailable from visible UI; use read_visible or an authorized export")

    def GetNewMessage(self):
        raise UnsupportedFeature("UI has no durable message watermark; use an explicitly sourced export Listener")

    def read_visible(self):
        return self.client.read_visible(self.target)


def capabilities(profile: Profile | None = None) -> dict[str, Any]:
    actions = {}
    for action, roles in IMPLEMENTED_ACTIONS.items():
        identity_roles = CHAT_ROLES
        if profile and profile.identity_route == "contact_profile" and action != "send_at":
            identity_roles = tuple(role for role in CHAT_ROLES if role != "chat.identity") + ("contact.entry", "contact.identity", "chat.info_marker")
        if action in MOMENT_ACTIONS:
            identity_roles = MOMENT_CONTEXT_ROLES
        required = set(ACCOUNT_ROLES + identity_roles + roles)
        missing = sorted(required - set(profile.roles)) if profile else sorted(required)
        bindings = {"account.identity": "account_id", "commit.account_identity": "account_id"}
        if action not in MOMENT_ACTIONS:
            identity_role = "contact.identity" if profile and profile.identity_route == "contact_profile" and action != "send_at" else "chat.identity"
            bindings.update({identity_role: "chat_id", "chat.title": "title"})
        specific = {
            "reply": {"message.text": "text", "chat.quote_preview": "text"},
            "recall": {"message.self_text": "text"},
            "poke": {"chat.target_avatar": "title"},
            "rename_remark": {"contact.identity": "chat_id"},
            "send_file": {"attachment.target": "chat_id", "attachment.filename": "filename", "attachment.preview_name": "filename"},
            "send_image": {"attachment.target": "chat_id", "attachment.filename": "filename", "attachment.preview_name": "filename"},
            "send_at": {"mention.result": "member", "mention.member_identity": "member", "mention.selected_identity": "member"},
            "forward": {"message.text": "text", "forward.result": "chat_id", "forward.recipient_identity": "chat_id", "forward.destination": "chat_id", "forward.preview": "text"},
        }
        bindings.update(specific.get(action, {}))
        unbound = sorted(role for role, parameter in bindings.items()
                         if not profile or not any("{" + parameter + "}" in str(profile.roles.get(role, {}).get(field, ""))
                                                   for field in ("text", "description")))
        ready = bool(profile and profile.calibrated and not missing and not unbound)
        if action == "send_at" and profile and not profile.group_identity_supported:
            ready = False
        if action in MOMENT_ACTIONS and profile and "moments_rows" not in profile.collections:
            ready = False
        actions[action] = {"code": "implemented_bounded_workflow", "offline_tested": True,
                           "live_tested": False, "available_for_live_execution": ready,
                           "missing_profile_roles": missing, "unbound_profile_roles": unbound}
    return {"target_wechat": ">=8.0", "verified_versions": [],
            "profile": profile.name if profile else None,
            "profile_calibration_is_operator_assertion": True,
            "actions": actions, "unavailable": UNAVAILABLE,
            "android_database": {"code": "implemented_readonly_known_schema",
                "schema": "android-enmicromsg-v1", "live_tested": False,
                "supports": ["messages", "contacts", "groups", "derived_sessions"],
                "requires": ["authorized_quiescent_snapshot", "exact_cipher_profile", "verified_key_if_encrypted"],
                "not_supported": ["all_version_schema_guarantee", "live_database_polling", "moments_schema", "media_decryption"]},
            "key_acquisition": {"code": "experimental_opt_in_runtime_observation",
                "default_mode": "attach", "optional_spawn_requires_separate_stop_relaunch_and_system_instrumentation_consents": True,
                "verified_device_versions": [], "requires_separate_attach_consent": True,
                "candidate_requires_database_validation": True},
            "exports": "User-supplied normalized archive only, not Android database extraction",
            "ui_read_scope": "Visible screen only; observation IDs are snapshot-scoped, not WeChat message IDs"}
