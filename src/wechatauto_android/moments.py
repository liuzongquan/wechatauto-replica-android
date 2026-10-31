"""朋友圈当前可见帖子操作：快照作用域引用，不冒充 native SNS ID 或作者 wxid。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from .errors import (
    AmbiguousTarget, IdentityMismatch, OutcomeUnknown, SelectorNotFound,
    StaleSnapshot, UnsupportedFeature, ValidationError, PermissionDenied,
)
from .profile import Target
from .safety import device_lock
from .ui import Hierarchy, Node, Selector

MOMENT_CONTEXT_ROLES = ("home.discover", "discover.moments", "moments.feed", "moments.author",
                        "moments.content", "moments.timestamp", "moments.menu")
MOMENT_ACTIONS = {
    "moment_like": ("moments.like",),
    "moment_unlike": ("moments.unlike",),
    "moment_comment": ("moments.comment", "moments.comment_input", "moments.comment_send"),
}


def _digest(parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class MomentReference:
    id: str
    account_id: str
    device_serial: str
    profile_name: str
    wechat_version: str
    snapshot_fingerprint: str
    row_signature: str
    author_display: str
    content_visible: str
    time_visible: str
    assurance: str
    origin: str = "android_visible_moment_observation"

    def to_dict(self):
        return asdict(self)

    def expected_id(self):
        return "moment-observation:" + _digest([
            self.account_id, self.device_serial, self.profile_name, self.wechat_version,
            self.snapshot_fingerprint, self.row_signature,
        ])


class MomentOperations:
    def __init__(self, client: Any):
        self.client = client

    def _require(self):
        self.client.profile.require(*MOMENT_CONTEXT_ROLES)
        if "moments_rows" not in self.client.profile.collections:
            raise UnsupportedFeature("No calibrated visible Moment row-container selector")

    def _ensure_home(self):
        if not self.client.profile.calibrated:
            raise PermissionDenied("Moment navigation requires an operator-calibrated profile")
        snapshot = self.client._snapshot()
        feed = snapshot.find_all(self.client._selector("moments.feed"))
        if feed:
            snapshot.one(self.client._selector("moments.feed"))
            self.client._back("home.marker")
        else:
            snapshot.one(self.client._selector("home.marker"))

    def _open_feed(self):
        self.client._click("home.discover")
        self.client._click("discover.moments")
        self.client._wait("moments.feed")

    @staticmethod
    def _descendants(node: Node) -> list[Node]:
        result = []
        stack = list(reversed(node.children))
        while stack:
            item = stack.pop()
            result.append(item)
            stack.extend(reversed(item.children))
        return result

    def _one_in(self, row: Node, role: str) -> Node:
        matches = [node for node in self._descendants(row)
                   if self.client._selector(role).matches(node)]
        if not matches:
            raise SelectorNotFound(f"Moment row lacks {role}")
        if len(matches) != 1:
            raise AmbiguousTarget(f"Moment row has ambiguous {role}")
        return matches[0]

    def _rows(self, snapshot: Hierarchy):
        selector_values = dict(self.client.profile.collections["moments_rows"])
        selector_values.setdefault("package", self.client.profile.package)
        rows = snapshot.find_all(Selector.from_dict(selector_values))
        records = []
        signatures = set()
        for row in rows:
            author = self._one_in(row, "moments.author")
            content = self._one_in(row, "moments.content")
            timestamp = self._one_in(row, "moments.timestamp")
            menu = self._one_in(row, "moments.menu")
            texts = [node.text or node.description for node in (author, content, timestamp)]
            if not texts[0] or not texts[2]:
                raise ValidationError("Visible Moment requires author and time labels")
            signature = _digest(texts)
            if signature in signatures:
                raise AmbiguousTarget("Indistinguishable visible Moment posts; refuse to choose a row")
            signatures.add(signature)
            records.append((signature, texts, menu))
        return records

    def observe(self) -> list[MomentReference]:
        """仅当前可见帖子；返回显示名而非推测的 wxid，不滚动历史。"""
        self._require()
        client = self.client
        with device_lock(client.policy.serial, client.policy.lock_timeout):
            self._ensure_home()
            client._verify_account()
            self._open_feed()
            snapshot = client._snapshot()
            assurance = client._commit_account_proof(snapshot)
            references = []
            for signature, texts, _ in self._rows(snapshot):
                kwargs = dict(account_id=client.policy.account_id, device_serial=client.policy.serial,
                              profile_name=client.profile.name, wechat_version=client.profile.version,
                              snapshot_fingerprint=snapshot.fingerprint, row_signature=signature,
                              author_display=texts[0], content_visible=texts[1], time_visible=texts[2],
                              assurance=assurance)
                reference = MomentReference(id="", **kwargs)
                references.append(MomentReference(id=reference.expected_id(), **kwargs))
            return references

    def _validate_reference(self, reference: MomentReference):
        if not isinstance(reference, MomentReference):
            raise ValidationError("Moment action requires an observed MomentReference")
        client = self.client
        if (reference.account_id, reference.device_serial, reference.profile_name,
                reference.wechat_version) != (client.policy.account_id, client.policy.serial,
                client.profile.name, client.profile.version):
            raise IdentityMismatch("Moment observation belongs to another device/account/profile")
        if reference.id != reference.expected_id() or reference.row_signature != _digest([
                reference.author_display, reference.content_visible, reference.time_visible]):
            raise ValidationError("Moment observation identity was changed")
        if reference.origin != "android_visible_moment_observation":
            raise ValidationError("Moment reference has an unsupported origin")

    def _resolve(self, reference: MomentReference, snapshot: Hierarchy, *, initial=False) -> Node:
        if initial and snapshot.fingerprint != reference.snapshot_fingerprint:
            raise StaleSnapshot("Moment feed changed; observe again and obtain new per-reference authorization")
        rows = [row for row in self._rows(snapshot) if row[0] == reference.row_signature]
        if len(rows) != 1:
            raise StaleSnapshot("Referenced Moment is absent or no longer uniquely observable")
        return rows[0][2]

    def _menu(self, reference: MomentReference):
        snapshot = self.client._snapshot()
        node = self._resolve(reference, snapshot, initial=True)
        result = self.client.adapter.click_node(node, expected_fingerprint=snapshot.fingerprint)
        if result is False:
            raise OutcomeUnknown("Moment menu navigation was not acknowledged")

    def like(self, reference: MomentReference, *, operation_id: str, unlike: bool = False):
        self._require()
        self._validate_reference(reference)
        action = "moment_unlike" if unlike else "moment_like"
        role = "moments.unlike" if unlike else "moments.like"
        def prepare():
            self._menu(reference)
            snapshot = self.client._wait(role)
            self._resolve(reference, snapshot)
            return self.client._selector(role), snapshot
        return self.client._run(action, Target(reference.id, reference.author_display), operation_id,
            {"observation_id": reference.id, "unlike": unlike}, prepare,
            context_roles=MOMENT_CONTEXT_ROLES, navigate=self._open_feed,
            before_account=self._ensure_home)

    def comment(self, reference: MomentReference, text: str, *, operation_id: str):
        self._require()
        self._validate_reference(reference)
        self.client._validate_text(text)
        def prepare():
            self._menu(reference)
            self.client._click("moments.comment")
            self.client._text("moments.comment_input", text)
            snapshot = self.client._wait("moments.comment_send")
            self._resolve(reference, snapshot)
            if snapshot.one(self.client._selector("moments.comment_input")).text != text:
                raise IdentityMismatch("Comment text changed before commit")
            return self.client._selector("moments.comment_send"), snapshot
        return self.client._run("moment_comment", Target(reference.id, reference.author_display),
            operation_id, {"observation_id": reference.id, "text": text}, prepare,
            context_roles=MOMENT_CONTEXT_ROLES, navigate=self._open_feed,
            before_account=self._ensure_home)
