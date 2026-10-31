# Python UI 工作流与实际边界

以下为**已实现、有合成页面测试的有界工作流**，不是已通过真实微信版本测试的功能列表。所有真实操作仍需精确微信/SDK profile、稳定身份选择器、helper setup 许可，以及账号/对象/动作授权。缺少真实界面证据时 fail closed，不把示例资源 ID 用于设备。

## 默认安全预演

```python
from wechatauto_android import WeChat
from wechatauto_android.profile import Profile, Target
from wechatauto_android.safety import SafetyPolicy

profile = Profile.load("private/device-profile.json")
policy = SafetyPolicy(serial="YOUR_SERIAL", account_id="YOUR_ACCOUNT", execute=False)
client = WeChat(None, profile, policy, journal_path="private/actions.sqlite")
target = Target("YOUR_APPROVED_CHAT_ID", "EXACT_VISIBLE_CHAT_TITLE")
plan = client.send_text(target, "预演内容", operation_id="preview-operation-0001")
assert plan.status == "dry_run"  # 前提是 profile 所需角色完整；否则明确报错
```

上述客户端没有设备 adapter，不连接手机、不导航、不写操作日志、不发送。缺失角色仍可能让预演失败；这是配置尚不足，不是应跳过的测试。真实执行时使用显式 `Uiautomator2Adapter(serial, allow_device_setup=True)`，policy 逐项列出 `allowed_actions` 与 `allowed_targets` 并设置 `execute=True`；**只在本次动作已经得到授权时这样做**。严格提交前账号证明/可选较弱独占假设见 [安全文档](SECURITY.md)。

## 单一目标操作

每次执行必须有独立操作 ID，未知结果不重放；所有当前写路径返回 `submitted` 也不表示送达或完整业务成功。

| 方法 | 实际范围 | 必须保留的限制 |
|---|---|---|
| `send_text(target, text, operation_id=...)` | 精确聊天/输入回读后发送 | 中文/emoji 不模糊归一化；标题不是稳定账号 ID |
| `reply(target, original_text, text, operation_id=...)` | 当前可见唯一正文、引用预览、回读后发送 | 不跨全部历史搜索；重复相同正文拒绝 |
| `recall(target, original_text, operation_id=...)` | 唯一自己消息，撤回菜单项作为最终提交 | 不承诺撤回时效、服务端结果或恢复未见内容 |
| `voice_call(target, operation_id=..., video=False)` | 语音/视频选项作为最终提交 | 不是完整通话生命周期；限事先同意的测试对象 |
| `poke(target, operation_id=...)` | 私聊目标头像的**两次有意点击** | 不是 SDK 自动重试；群成员拍一拍未实现；中途不确定不得再双击 |
| `send_file(target, path, operation_id=...)` | 哈希绑定附件+受限上传目录+文件预览 | <=100 MiB；目标目录须预先存在；真实 picker 可能不可用 |
| `send_image(target, path, operation_id=...)` | 同上，限定图片扩展名 | 不证明微信原图质量、服务器内容或远端接收 |
| `rename_remark(target, new_remark, operation_id=...)` | 明确联系人 ID→备注编辑→回读→保存 | 不支持空备注/改群名；不等同持久化回读成功 |

`Profile.identity_route="contact_profile"` 为联系人提供“聊天信息→联系人资料→稳定 ID”的替代路线，必须配置 `contact.entry/contact.identity/chat.info_marker`。`direct` 路线使用 `chat.identity`；两者都不能把显示名当作稳定 ID。账号检查完成后还需明确回到 `home.chats`。

## 原生 @ 单个成员

签名：`client.send_at(group_target, text, member_id=..., member_name=..., operation_id=...)`。

- `group_target.kind` 必须为 `group`，profile 必须能够证明稳定群 ID。
- 群 ID 与成员 ID 都要在 `allowed_targets`，动作是 `send_at`；昵称只用于搜索/可见文本，不能代替成员 ID。
- 先输入正文和 `@`，进入原生成员选择器，核对成员身份；选择后需读取原生提及的身份标记与完整输入。
- 选择原生 token 后不再 `set_text`，避免把 mention span 退化成普通文字。
- 只支持单成员，`@all/@所有人`、多成员批量与其他 UI 变体均不隐式支持。若真实界面不暴露所需 native token 身份标记，则该版本不能执行；合成测试不是绕过理由。

## 单条、单收件人转发

签名：`client.forward_message(source_target, destination_target, original_text, operation_id=...)`。

- 来源会话和目标会话都必须明确 allowlist 动作 `forward`。
- 只选当前可见、正文唯一的源消息；不冒充原生全历史 ID 搜索或所有媒体类型支持。
- 转发选择器须读取稳定目标 ID，提交前同时核对目标 ID 与源消息预览。
- **仅单收件人**，没有原项目批量分块、标签转发、附言等完整语义。
- journal/result 的 `target` 是**来源会话**；目标会话包含在 payload 摘要中，审计解释时不能把这个字段误当成收件人。

## 可见朋友圈观察、点赞/取消点赞、评论

`client.Moment.observe()` 会读取账号并导航到朋友圈，返回当前屏幕的 `MomentReference` 列表。它不滚动完整历史；`author_display/content_visible/time_visible` 都是可见值，**不是作者 wxid 或原生 SNS ID**。

1. `MomentReference.id` 绑定账号、设备、profile、版本、整页快照和行内容摘要。重复不可区分的帖子拒绝产生可操作选择。
2. 观察后由操作人选择并授权一条确切引用，再让对应写 policy 的 `allowed_targets` 包含该 `ref.id`，以及明确动作 `moment_like`、`moment_unlike` 或 `moment_comment`。不要把全部观察结果自动加入授权范围。
3. `client.Moment.like(ref, operation_id=..., unlike=False)` / `unlike=True` 按指定菜单项执行，不盲目切换状态；无法识别菜单状态时失败。
4. `client.Moment.comment(ref, text, operation_id=...)` 精确回读评论后提交。
5. 引用快照过期/页面变化必须重新观察并重新核对授权，不能因为显示名或正文相同而沿用旧许可。

未实现对特定评论的回复、SNS 原生 ID 查询、朋友圈媒体解密或发朋友圈。观察与点击仍非原子，需严格当前账号证明或明确接受独占设备弱假设；全量业务验收依然暂缓。

## 失败之后

提交前失败可能留下搜索页面、未发送草稿、选中的成员或附件；程序不盲目清理或重试。提交后失败记作结果未知，先人工核查实际状态。UI 自动化没有事务回滚，不能把一次 Python 异常解释为“手机没有任何变化”。
