"""版本固定的声明式选择器；不在未知版本上猜测坐标。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from string import Formatter
from typing import Any

from .errors import UnsupportedFeature, ValidationError
from .archive import read_json
from .ui import Selector

_ALLOWED_PARAMETERS = {"account_id", "chat_id", "title", "text", "member", "name", "filename"}

@dataclass(frozen=True)
class Target:
    chat_id: str
    title: str
    kind: str = "contact"

    def __post_init__(self) -> None:
        if not isinstance(self.chat_id, str) or not isinstance(self.title, str) or not self.chat_id.strip() or not self.title.strip():
            raise ValidationError("Target needs an explicit stable ID and exact visible title")
        if self.kind not in {"contact", "group"}:
            raise ValidationError("Unknown target kind")
        if self.chat_id.endswith("@chatroom") != (self.kind == "group"):
            raise ValidationError("Group kind and stable @chatroom ID must agree")
        if any(ord(c) < 32 for c in self.chat_id + self.title):
            raise ValidationError("Control characters are not valid in target identity")

@dataclass(frozen=True)
class Profile:
    name: str
    version: str
    roles: dict[str, dict[str, Any]]
    package: str = "com.tencent.mm"
    calibrated: bool = False
    evidence: str = ""
    group_identity_supported: bool = False
    collections: dict[str, dict[str, Any]] = field(default_factory=dict)
    android_sdks: tuple[int, ...] = ()
    identity_route: str = "direct"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not isinstance(self.version, str) or not self.name or not self.version or self.version in {"*", "unknown"}:
            raise ValidationError("Profile must pin an exact app version")
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", self.version) or tuple(map(int, self.version.split("."))) < (8, 0):
            raise ValidationError("WeChat version must be an exact numeric version >=8.0")
        object.__setattr__(self, "android_sdks", tuple(self.android_sdks))
        if any(type(sdk) is not int or sdk < 30 for sdk in self.android_sdks):
            raise ValidationError("Supported Android SDKs must be integers >=30")
        if self.calibrated and not self.android_sdks:
            raise ValidationError("Calibrated profiles must list tested exact Android SDKs")
        if self.identity_route not in {"direct", "contact_profile"}:
            raise ValidationError("identity_route must be direct or contact_profile")
        if self.package != "com.tencent.mm":
            raise ValidationError("Only the WeChat package is supported")
        if type(self.calibrated) is not bool or type(self.group_identity_supported) is not bool:
            raise ValidationError("Profile calibration flags must be booleans")
        if self.calibrated and not self.evidence.strip():
            raise ValidationError("Calibrated profile requires operator's evidence reference")
        if not isinstance(self.roles, dict) or not isinstance(self.collections, dict):
            raise ValidationError("roles and collections must be objects")
        for name, selector in self.collections.items():
            if not isinstance(name, str) or not isinstance(selector, dict):
                raise ValidationError("Invalid collection selector")
            parsed = Selector.from_dict(selector)
            if parsed.package not in (None, self.package):
                raise ValidationError("Collection must be scoped to the WeChat package")
        for role, selector in self.roles.items():
            if not isinstance(role, str) or not isinstance(selector, dict):
                raise ValidationError("Invalid selector role")
            if selector.get("package", self.package) != self.package:
                raise ValidationError("Role must be scoped to the WeChat package")
            for value in selector.values():
                if isinstance(value, str):
                    try:
                        for _, parameter, spec, conversion in Formatter().parse(value):
                            if parameter is not None and (
                                parameter not in _ALLOWED_PARAMETERS or spec or conversion
                            ):
                                raise ValidationError("Unsupported selector placeholder")
                    except ValueError as exc:
                        raise ValidationError("Malformed selector placeholder") from exc
            self.selector(role, **dict.fromkeys(_ALLOWED_PARAMETERS, "placeholder"))

    @classmethod
    def load(cls, path: str | Path) -> Profile:
        try:
            data = read_json(Path(path), max_bytes=2 * 1024 * 1024)
            schema = data.pop("schema_version", None)
            if type(schema) is not int or schema != 1:
                raise ValidationError("Expected profile schema_version 1")
            return cls(**data)
        except (OSError, ValueError, TypeError) as exc:
            raise ValidationError(f"Invalid profile: {exc}") from exc

    def selector(self, role: str, **parameters: str) -> Selector:
        if role not in self.roles:
            raise UnsupportedFeature(f"Profile {self.name!r} has no {role!r} selector")
        try:
            values = {
                key: value.format_map(parameters) if isinstance(value, str) else value
                for key, value in self.roles[role].items()
            }
        except (KeyError, ValueError) as exc:
            raise ValidationError(f"Missing selector argument for {role}") from exc
        values.setdefault("package", self.package)
        return Selector.from_dict(values)

    def require(self, *roles: str) -> None:
        missing = [role for role in roles if role not in self.roles]
        if missing:
            raise UnsupportedFeature(f"Missing calibrated selector roles: {', '.join(missing)}")


class ProfileRegistry:
    """严格版本/SDK 匹配；没有通配或最新配置自动回退。"""
    def __init__(self, profiles: list[Profile]):
        self.profiles = tuple(profiles)
        keys = [(p.package, p.version, sdk) for p in profiles for sdk in p.android_sdks]
        if len(keys) != len(set(keys)):
            raise ValidationError("Ambiguous version/SDK profile registration")

    def resolve(self, version: str, sdk: int, package: str = "com.tencent.mm") -> Profile:
        matches = [p for p in self.profiles if p.package == package and p.version == version and sdk in p.android_sdks]
        if len(matches) != 1:
            raise UnsupportedFeature("No exact WeChat version + Android SDK profile; calibration required")
        return matches[0]
