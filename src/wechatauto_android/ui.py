"""受限、精确的 Android UI 树解析；不从昵称推断微信身份。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, fields
from types import MappingProxyType
from typing import Mapping

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from .errors import AmbiguousTarget, SelectorNotFound, ValidationError

_MAX_BYTES = 2 * 1024 * 1024
_MAX_NODES = 20_000
_MAX_DEPTH = 128
_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]\Z")
_ATTRS = {
    "text": "text",
    "resource_id": "resource-id",
    "description": "content-desc",
    "class_name": "class",
    "package": "package",
    "enabled": "enabled",
    "clickable": "clickable",
}


@dataclass(frozen=True)
class Selector:
    text: str | None = None
    resource_id: str | None = None
    description: str | None = None
    class_name: str | None = None
    package: str | None = None
    enabled: bool | None = None
    clickable: bool | None = None

    def __post_init__(self) -> None:
        identity = (self.text, self.resource_id, self.description, self.class_name, self.package)
        if not any(isinstance(value, str) and value for value in identity):
            raise ValidationError("selector requires a nonempty exact identity condition")
        for name, value in self.to_dict().items():
            if name in {"enabled", "clickable"}:
                if type(value) is not bool:
                    raise ValidationError(f"selector {name} must be boolean")
            elif not isinstance(value, str) or len(value) > 16384 or "\x00" in value:
                raise ValidationError(f"selector {name} must be bounded text without NUL")
            else:
                try:
                    value.encode("utf-8")
                except UnicodeError as exc:
                    raise ValidationError("selector must contain valid Unicode") from exc

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Selector:
        if not isinstance(value, Mapping):
            raise ValidationError("selector must be an object")
        if set(value) - {item.name for item in fields(cls)}:
            raise ValidationError("unknown selector field")
        try:
            return cls(**value)
        except TypeError as exc:
            raise ValidationError("invalid selector") from exc

    def to_dict(self) -> dict:
        return {key: value for key, value in asdict(self).items() if value is not None}

    def matches(self, node: Node) -> bool:
        for key, value in self.to_dict().items():
            expected = str(value).lower() if type(value) is bool else value
            if node.attrs.get(_ATTRS[key]) != expected:
                return False
        return True


@dataclass(frozen=True)
class Node:
    attrs: Mapping[str, str]
    bounds: tuple[int, int, int, int]
    children: tuple[Node, ...] = ()

    @property
    def text(self) -> str:
        return self.attrs.get("text", "")

    @property
    def description(self) -> str:
        return self.attrs.get("content-desc", "")

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return (left + right) // 2, (top + bottom) // 2


@dataclass(frozen=True)
class Hierarchy:
    nodes: tuple[Node, ...]
    fingerprint: str

    @classmethod
    def parse(cls, xml: str) -> Hierarchy:
        if not isinstance(xml, str):
            raise ValidationError("hierarchy must be UTF-8 XML text")
        try:
            raw = xml.encode("utf-8")
        except UnicodeError as exc:
            raise ValidationError("invalid Unicode in hierarchy") from exc
        if not raw or len(raw) > _MAX_BYTES:
            raise ValidationError("hierarchy is empty or exceeds 2 MiB")
        try:
            root = ElementTree.fromstring(
                raw, forbid_dtd=True, forbid_entities=True, forbid_external=True
            )
        except (ElementTree.ParseError, DefusedXmlException) as exc:
            raise ValidationError("malformed or unsafe hierarchy XML") from exc
        if root.tag != "hierarchy":
            raise ValidationError("expected Android hierarchy root")
        pending = [(root, 0)]
        flat = []
        while pending:
            item, depth = pending.pop()
            if depth > _MAX_DEPTH:
                raise ValidationError("hierarchy exceeds maximum depth")
            if item is not root:
                if item.tag != "node":
                    raise ValidationError("unexpected hierarchy element")
                flat.append(item)
                if len(flat) > _MAX_NODES:
                    raise ValidationError("hierarchy exceeds maximum node count")
            pending.extend((child, depth + 1) for child in reversed(list(item)))
        built = {}
        for item in reversed(flat):
            attrs = dict(item.attrib)
            for key in ("enabled", "clickable", "visible-to-user", "long-clickable", "scrollable"):
                if key in attrs and attrs[key] not in {"true", "false"}:
                    raise ValidationError(f"invalid boolean hierarchy attribute: {key}")
            value = attrs.get("bounds", "[0,0][0,0]")
            matched = _BOUNDS.fullmatch(value)
            if not matched:
                raise ValidationError("malformed node bounds")
            try:
                bounds = tuple(int(part) for part in matched.groups())
            except ValueError as exc:
                raise ValidationError("invalid numeric node bounds") from exc
            if any(abs(part) > 1_000_000 for part in bounds):
                raise ValidationError("unreasonable node bounds")
            if bounds[2] < bounds[0] or bounds[3] < bounds[1]:
                raise ValidationError("inverted node bounds")
            built[id(item)] = Node(
                MappingProxyType(attrs), bounds, tuple(built[id(child)] for child in item)
            )
        return cls(tuple(built[id(item)] for item in flat), hashlib.sha256(raw).hexdigest())

    def find_all(self, selector: Selector) -> list[Node]:
        if not isinstance(selector, Selector):
            raise ValidationError("expected Selector")
        return [node for node in self.nodes if selector.matches(node)]

    def one(self, selector: Selector) -> Node:
        nodes = self.find_all(selector)
        if not nodes:
            raise SelectorNotFound("exact selector matched no nodes")
        if len(nodes) != 1:
            raise AmbiguousTarget(f"exact selector matched {len(nodes)} nodes")
        return nodes[0]
