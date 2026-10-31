"""可供调用方区分的失败；不把未执行或结果不明当作成功。"""

class WeChatError(Exception):
    """Base domain error."""

class UnsupportedFeature(WeChatError):
    pass

class ValidationError(WeChatError, ValueError):
    pass

class IdentityMismatch(WeChatError):
    pass

class PermissionDenied(WeChatError):
    pass

class AmbiguousTarget(WeChatError):
    pass

class SelectorNotFound(WeChatError):
    pass

class DeviceUnavailable(WeChatError):
    pass

class StaleSnapshot(WeChatError):
    pass

class OutcomeUnknown(WeChatError):
    """The side effect may have happened; never automatically retry."""

class DuplicateOperation(WeChatError):
    pass

class LockTimeout(WeChatError):
    pass
