"""Android 微信自动化：UI 与用户导出数据是独立的来源。"""
from .archive import ExportArchive, GroupMemberWatcher, MomentObserver
from .listener import Listener
from .media import MediaDownloader
from .controller import Chat, WeChat, capabilities
from .errors import WeChatError
from .rooted import AndroidDatabase, AdbRootSnapshotter, RootSnapshotConfig, CipherProfile
from .moments import MomentReference, MomentOperations
from .message_formats import parse_message, parse_payload, ParsedMessage, ParsedPayload
from .recall import RecallGuard, RecallObservation
from .keycapture import CaptureConfig, FridaKeyCapture
from .models import ActionResult, DeviceIdentity, Message, UIObservation
from .profile import Profile, ProfileRegistry, Target
from .safety import OperationJournal, SafetyPolicy

__version__ = "0.1.0"
__all__ = ["WeChat", "Chat", "Message", "UIObservation", "ActionResult", "DeviceIdentity",
           "Profile", "ProfileRegistry", "Target", "SafetyPolicy", "OperationJournal",
           "WeChatError", "capabilities", "ExportArchive", "GroupMemberWatcher",
           "MomentObserver", "Listener", "MediaDownloader", "AndroidDatabase",
           "AdbRootSnapshotter", "RootSnapshotConfig", "CipherProfile", "MomentReference", "MomentOperations",
           "parse_message", "parse_payload", "ParsedMessage", "ParsedPayload", "RecallGuard",
           "RecallObservation", "CaptureConfig", "FridaKeyCapture"]
