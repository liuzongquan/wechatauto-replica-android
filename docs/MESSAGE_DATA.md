# 消息载荷解析与已观测撤回镜像

这两个模块只处理**调用者明确传入的数据**，没有设备连接、网页抓取、媒体下载或后台数据库轮询。解析元数据不等于取得消息原始附件；返回已缓存的原消息不等于破解微信撤回或恢复未观测历史。

## 载荷解析

```python
from wechatauto_android.message_formats import parse_payload, parse_message

parsed = parse_payload('<msg><appmsg><type>5</type><title>合成链接</title>'
                       '<url>https://example.invalid/</url></appmsg></msg>', native_type=49)
assert parsed.raw_payload.startswith('<msg>')  # 原输入保留，不访问 URL
```

- `parse_payload(str_or_bytes, native_type=None)` 返回 `ParsedPayload`：`status`、`kind`、`raw_payload`、`fields`、`reason`、`warnings`。
- `parse_message(message)` 返回 `ParsedMessage`，其中 `source` 是保留所有字段、origin 和 metadata 的独立副本，`payload` 是解析结果；不会把档案来源改成真机来源。
- 状态为 `parsed / unsupported / invalid / unsafe / too_large`。未知输入保留，不猜测解码成功；非法 Python 参数仍可能抛 `ValidationError`。
- 支持严格 UTF-8 文本（允许标准 BOM），以及已知 XML 的明确 username 联系人名片、链接、文件、转发集合、小程序、引用、转账/红包**信封元数据**、图片/语音/视频/表情/位置/通话元数据和撤回事件。识别转账/红包不包含任何支付或领取动作。
- 不解压 zstd、猜二进制容器、解码语音或下载任何 XML URL。未知类型/二进制保留 `raw_payload` 并明确不支持。
- 限制：单载荷 1 MiB、XML 深度 32/节点 4096、完整 JSON 消息记录 4 MiB；禁止 DTD/entity，重复歧义字段和结构错误不静默选一个。

解析状态通过只证明本条数据可按当前规则处理，不证明真实微信全类型兼容。原始载荷和解析后的 URL/账号/正文仍可能是私人数据，不能默认记录或发布。

## RecallGuard：只返回之前已经观察的消息

```python
from wechatauto_android.models import Message
from wechatauto_android.recall import RecallGuard

original = Message("local-1", "demo-account", "demo-chat", 1, "demo-contact",
                   "合成示例", metadata={"native_msg_svr_id": "12345"})
event = Message("local-2", "demo-account", "demo-chat", 2, "demo-contact",
                '<sysmsg type="revokemsg"><revokemsg><newmsgid>12345</newmsgid>'
                '<session>demo-chat</session></revokemsg></sysmsg>', type="recall")
with RecallGuard("demo-account") as guard:  # 默认仅内存，不读取设备或创建文件
    guard.observe(original)
    result = guard.observe(event)
    assert result.status == "recovered"
    assert result.original.content == "合成示例"
```

`recovered` 的含义仅为“返回调用者**先前明确提供**的原始记录”。匹配要求同一个绑定账号、完全相同的聊天 ID、以及原记录 `metadata.native_msg_svr_id` 对应撤回 XML 的 `<newmsgid>`。本地 `<msgid>`、昵称、正文、时间戳均不是替代证据。带 `<session>` 的撤回记录必须同样匹配聊天。

没有观察过原消息、账号/聊天不一致、原身份冲突或原生 server ID 缺失时不恢复；先收到撤回再迟到原消息也不会追溯篡改第一次未知结论。重复事件保持第一次持久结果，标记 `duplicate=True`。

API：`observe(message)`、可手动接入可信 listener 的 `on_msg(message, listener)`、`events(limit=100)`、`close()`/context manager。它不自动注册监听，不扫描数据库的原地撤回改写，不回填全部历史，也不备份附件或恢复已删除媒体。原项目 RecallGuard 的这些更宽语义尚未实现。

## 可选持久化的隐私成本

`RecallGuard(account_id, path="private/recall.sqlite")` 仅在显式指定时写文件：父目录必须已经存在且为当前所有者私有（POSIX 0700），数据库为 0600，拒绝共享目录、符号链接、硬链接、异常侧文件或其他账号库。

**文件保存完整传入消息、metadata、原消息副本和撤回事件，明文、未加密、无自动过期。** 调用者负责范围控制、关闭后删除及保留策略；SQLite `secure_delete` 不是备份/SSD/系统缓存的可靠擦除保证。不要提交、上传或自动同步该文件。仅想验证逻辑时保持默认内存模式。
