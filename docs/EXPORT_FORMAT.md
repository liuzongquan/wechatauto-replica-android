# 用户提供导出格式（不是 Android 微信数据库）

本模块只处理用户已有、授权的规范化 JSON。它**不**读取 `EnMicroMsg.db`、微信私有目录、密钥或手机账号；不替代真实 Android 历史/媒体提取。数据库解密、备份转换及导出工具不在实现范围内。

```json
{
  "schema_version": 1,
  "account_id": "local-account-id",
  "export_id": "my-authorized-dataset-1",
  "origin": "user_export",
  "messages": [
    {
      "id": "export-message-id-1",
      "chat_id": "wxid_contact_from_your_export",
      "sequence": 1,
      "sender_id": "wxid_sender_from_your_export",
      "content": "你好🙂",
      "type": "text",
      "timestamp": null,
      "metadata": {}
    }
  ],
  "contacts": [{"id": "wxid_contact_from_your_export", "nickname": "联系人"}],
  "groups": [{"id": "group_from_export@chatroom", "members": ["wxid_member"]}],
  "labels": [{"id": "label-1", "name": "测试", "members": ["wxid_member"]}],
  "moments": [{"id": "post-from-export", "content": "用户导出的朋友圈内容"}]
}
```

- 文件最大 64 MiB，最多 500,000 条消息。重复 JSON 键、非有限数值、非整数序号、未知消息字段及账号混用会失败。
- 消息身份是 `(account_id, chat_id, id)`，不是 id 单独；同序号以 id 的 Unicode 字典序稳定排序。完全一致重复可合并，冲突重复拒绝。
- `sequence` 必须由导出提供（0 至 2^63−1），不会伪造时间、消息 ID、账号 ID 或 sender_id。`timestamp` 可省略/null，非 null 时原样保留字符串，不保证时间格式。
- 所有来源是 `user_export`；伪装成设备读取的 origin 会被拒绝。联系人等记录及群成员访问结果也保留该来源。
- 文件路径和 `export_id` 共同绑定持久化游标。`export_id` 可省略，文件路径仍提供来源命名空间。内存构造必须提供 `export_id` 或显式 `source_id`。
- 联系人、群、标签、朋友圈仅来自导出；这些查询不等同于实时手机读取，`wxid_*` 字符串不会使任意 UI 昵称变成已核验微信身份。

## 读取和重新导出

```python
from wechatauto_android.archive import ExportArchive
archive = ExportArchive.open("authorized.json", account_id="local-account-id")
latest = archive.get_messages("wxid_contact_from_your_export", limit=20)
archive.export_history("history.json", users=["wxid_contact_from_your_export"])
archive.export_history("history.sqlite", fmt="sqlite")
```

输出是**当前档案的再次导出**。不会覆盖已存在文件/源档案，拒绝符号链接路径。输出目录须已存在；JSON、SQLite 文件权限为 0600。SQLite 的 `messages` 表保存 `(account_id, chat_id, id, sequence, data)`，不是微信原生 schema。

## 导出文件追加监听

```python
from wechatauto_android.listener import Listener
listener = Listener(archive, "local-account-id", watermark_file="checkpoint.json")
listener.add_listener("wxid_contact_from_your_export", lambda message, owner: print(message.content))
listener.start()
# ...
listener.stop(timeout=5)
```

每次轮询刷新导出文件（外部写入者应原子替换）。只允许消息追加；不能删除/修改既有消息，也不能在既有 `(sequence,id)` 游标之前插入新消息。违反时停止并暴露错误，不静默丢数据。群成员等非消息记录可更新，由 `GroupMemberWatcher` 比较。

监听器要求来源支持 `get_messages_after(chat_id, after=(sequence,id), limit=N)`、固定 `account_id`/`source_id`。仅 `since_seq` 接口会丢失跨分页同序号消息，因此不接受。批次整体验证账号、会话、顺序后再回调。开始时无游标会投递当前档案全部消息，分多批执行；**不是只监听启动后的消息**。

回调失败不推进该消息游标；前面已成功的其他回调可能重复。检查点写盘失败也回滚内存游标。语义为**至少一次尝试**而非恰好一次；如回调包含外部副作用，必须自行持久化去重。后台出错即停止，查看 `listener.last_error`；没有静默无限重试。`stop(timeout=...)` 无法强杀用户阻塞回调，超时抛出 `LockTimeout`。停止请求和移除回调在每条消息及回调之间生效；已执行但未完成所有回调的当前消息不会确认，下次启动可能重投。不要运行多个进程写同一检查点文件。

## 本地媒体

`MediaDownloader` 从用户指定 source_root 复制已有导出媒体到另一个 output_root；不会下载微信服务器/手机内容。两个目录须存在、不含符号链接且互不嵌套。当前安全文件描述符实现要求 POSIX（Linux/macOS）；不支持 Windows 本地调用，可用 Docker/Linux 主机。

```python
from wechatauto_android.media import MediaDownloader
media = MediaDownloader("authorized-media", "copied-media")
media.download("images/example.png", destination="images/copy.png", sha256="<64 hex digits>")
```

路径必须相对 POSIX；拒绝 `..`、绝对路径、反斜杠、NUL、符号链接、特殊文件、覆盖；默认单文件限制 512 MiB，可显式设置 `max_bytes`。复制后校验 SHA-256，再以原子不覆盖方式发布。`download_message` 还校验显式 `account_id`/`chat_id` 与消息一致，以及 `metadata.media_path`/可选 `metadata.sha256`。
