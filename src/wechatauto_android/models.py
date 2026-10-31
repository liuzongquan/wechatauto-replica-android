"""Android UI 观测和用户提供档案的模型；来源不能混淆。"""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from typing import Any

@dataclass(frozen=True)
class Message:
    id: str
    account_id: str
    chat_id: str
    sequence: int
    sender_id: str
    content: str
    type: str = "text"
    timestamp: str | None = None
    origin: str = "user_export"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass(frozen=True)
class DeviceIdentity:
    serial: str
    package: str
    version: str
    sdk: int = 0

@dataclass(frozen=True)
class ActionResult:
    operation_id: str
    action: str
    target: str
    status: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)
    assurance: str = "unverified"

    @property
    def success(self) -> bool:
        return self.status == "verified"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass(frozen=True)
class UIObservation:
    """仅当前屏幕观测；ID 绑定快照，不是微信 message/server ID。"""
    id: str
    account_id: str
    chat_id: str
    content: str
    kind: str
    snapshot: str
    origin: str = "android_visible_ui"
    assurance: str = "unverified"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
