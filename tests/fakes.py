"""Pure synthetic UI state machine. NOT a captured WeChat hierarchy or emulator."""
from pathlib import Path
from xml.sax.saxutils import quoteattr

from wechatauto_android.controller import IMPLEMENTED_ACTIONS, ACCOUNT_ROLES, CHAT_ROLES
from wechatauto_android.models import DeviceIdentity
from wechatauto_android.profile import Profile
from wechatauto_android.ui import Hierarchy
from wechatauto_android.errors import StaleSnapshot

PACKAGE = "com.tencent.mm"

def fixture_profile(calibrated=True):
    roles = set(ACCOUNT_ROLES + CHAT_ROLES)
    for values in IMPLEMENTED_ACTIONS.values():
        roles.update(values)
    roles.update({"message.text", "message.self_text", "chat.info_marker"})
    selectors = {role: {"resource_id": f"{PACKAGE}:id/synthetic_{role}"} for role in roles}
    for role, value in {"account.identity": "{account_id}", "chat.identity": "{chat_id}",
                        "chat.title": "{title}", "search.result": "{title}",
                        "chat.quote_preview": "{text}", "message.text": "{text}",
                        "message.self_text": "{text}", "chat.target_avatar": "{title}", "contact.identity": "{chat_id}",
                        "attachment.target": "{chat_id}", "attachment.filename": "{filename}",
                        "attachment.preview_name": "{filename}"}.items():
        selectors[role]["text"] = value
    return Profile("SYNTHETIC-NOT-LIVE", "8.0.0", selectors, calibrated=calibrated,
                   evidence="tests/fakes.py synthetic state machine, NOT real validation",
                   android_sdks=(34,), collections={"messages": {"resource_id": PACKAGE + ":id/synthetic_message.text", "package": PACKAGE}})

class FakeAdapter:
    def __init__(self):
        self.state = "home"
        self.account = "wxid_self"
        self.chat_id = "wxid_target"
        self.title = "Exact Friend"
        self.draft = ""
        self.search = ""
        self.original = "Original message"
        self.quote = ""
        self.calls = []
        self.commits = []
        self.uploads = []
        self.filename = ""
        self.remark = ""
        self.false_commit = False
        self.error_commit = False
        self.duplicate_result = False
        self.on_account = None

    def identity(self):
        return DeviceIdentity("SERIAL", PACKAGE, "8.0.0", 34)

    def _items(self):
        pages = {
            "home": [("home.marker", "微信"), ("home.me", "我"), ("home.search", "搜索")],
            "me": [("account.entry", "个人信息"), ("home.chats", "微信")],
            "account": [("account.identity", self.account)],
            "search": [("search.input", self.search), ("search.result", self.title)],
            "chat": [("chat.title", self.title), ("chat.info", "聊天信息"),
                     ("chat.input", self.draft), ("chat.send", "发送"),
                     ("message.text", self.original), ("message.self_text", "My message"),
                     ("chat.more", "更多"), ("chat.member", self.title), ("chat.target_avatar", self.title)],
            "info": [("chat.identity", self.chat_id), ("contact.entry", self.title), ("chat.info_marker", "聊天信息")],
            "contact_profile": [("contact.identity", self.chat_id), ("contact.more", "更多")],
            "contact_menu": [("contact.remark", "设置备注和标签")],
            "remark": [("remark.input", self.remark), ("remark.save", "完成")],
            "message_menu": [("message.quote", "引用"), ("message.recall", "撤回")],
            "more": [("chat.call", "视频通话"), ("attachment.file", "文件"), ("attachment.image", "照片")],
            "call_menu": [("call.voice", "语音通话"), ("call.video", "视频通话")],
            "member": [("member.identity", self.chat_id), ("member.poke", "拍一拍")],
            "file_menu": [("attachment.browse", "手机存储")],
            "files": [("attachment.filename", self.filename)],
            "preview": [("attachment.preview_name", self.filename), ("attachment.target", self.chat_id), ("attachment.send", "发送")],
        }
        items = list(pages[self.state])
        if self.state == "chat" and self.quote:
            items.append(("chat.quote_preview", self.quote))
        if self.state == "search" and self.duplicate_result:
            items.append(("search.result", self.title))
        return items

    def hierarchy(self):
        nodes = []
        for index, (role, text) in enumerate(self._items()):
            attrs = {"text": text, "resource-id": PACKAGE + ":id/synthetic_" + role,
                     "package": PACKAGE, "bounds": f"[0,{index * 20}][200,{index * 20 + 20}]",
                     "enabled": "true", "clickable": "true"}
            nodes.append("<node " + " ".join(f"{key}={quoteattr(value)}" for key, value in attrs.items()) + "/>")
        return Hierarchy.parse("<hierarchy>" + "".join(nodes) + "</hierarchy>")

    def _role(self, selector, expected_fingerprint=None):
        h = self.hierarchy()
        if expected_fingerprint and expected_fingerprint != h.fingerprint:
            raise StaleSnapshot("synthetic UI changed")
        h.one(selector)
        return selector.resource_id.split("synthetic_")[1]

    def click(self, selector, *, expected_fingerprint=None):
        role = self._role(selector, expected_fingerprint)
        self.calls.append(("click", role))
        transitions = {"home.chats": "home", "home.me": "me", "account.entry": "account", "home.search": "search",
                       "search.result": "chat", "chat.info": "info", "message.quote": "chat",
                       "chat.more": "more", "chat.call": "call_menu", "chat.member": "member",
                       "attachment.file": "file_menu", "attachment.browse": "files",
                       "attachment.image": "files", "attachment.filename": "preview",
                       "contact.entry": "contact_profile", "contact.more": "contact_menu", "contact.remark": "remark"}
        if role in transitions:
            self.state = transitions[role]
            if role == "message.quote":
                self.quote = self.original
            if role == "account.entry" and self.on_account:
                self.on_account()
            return None
        if role in {"chat.send", "message.recall", "call.voice", "call.video", "member.poke", "attachment.send", "remark.save"}:
            self.commits.append(role)
            if self.error_commit:
                raise TimeoutError("synthetic uncertain transport")
            if self.false_commit:
                return False
            return None
        raise AssertionError("Unknown synthetic click: " + role)

    def double_click(self, selector, *, expected_fingerprint=None):
        role = self._role(selector, expected_fingerprint)
        self.calls.append(("double_click", role))
        self.commits.append(role)

    def long_click(self, selector, *, expected_fingerprint=None):
        role = self._role(selector, expected_fingerprint)
        self.calls.append(("long_click", role))
        self.state = "message_menu"

    def set_text(self, selector, text):
        role = self._role(selector)
        self.calls.append(("set_text", role))
        if role == "search.input":
            self.search = text
        elif role == "chat.input":
            self.draft = text
        elif role == "remark.input":
            self.remark = text
        else:
            raise AssertionError(role)

    def press_back(self):
        self.calls.append(("back", self.state))
        self.state = {"account": "me", "me": "home", "info": "chat", "contact_profile": "info"}[self.state]

    def push(self, local, remote, *, expected_sha256=None):
        self.filename = Path(remote).name
        self.uploads.append((Path(local).read_bytes(), remote))
