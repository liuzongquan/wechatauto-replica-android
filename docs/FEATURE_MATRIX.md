# 与参考项目的完整语义功能矩阵

参考：[wechatauto-replica 1.2.9](https://github.com/fanyuantaier/wechatauto-replica/tree/bc2e6f2461330eb1a53996d2f2afc18416376b52)，固定 SHA `bc2e6f2461330eb1a53996d2f2afc18416376b52`。按源码语义划分 **53 组**；572 个 AST 函数/方法定义包括构造器、属性、重复定义、底层工具和遗留实现，不能当成 572 个可用业务功能。

## 结论与状态口径

**当前没有完成原项目的全部功能等价实现，也没有任何真实 Android 微信功能验收通过记录。** 本阶段真机验收已暂缓，继续云端/离线工作；`live-pending` 保持待验状态，不因暂缓改记通过。目标系统为 Android 11/API30+，微信应用为 8.0+；精确版本/SDK profile 验收前不能把目标范围当作支持范围。

- **implemented**：存在可执行实现，必须同时看“实际语义”，不等于真机能用。
- **automated-fixture**：有合成数据/模拟页面/真实 SDK 接口合约测试；只证明被测试的输入和代码路径。最终通过数量见实际测试报告。
- **live-pending**：需要确切设备、版本、profile、账号与动作/目标授权；**不是通过**。
- **unavailable**：当前未实现或无法满足所需前置证明；明确错误，不返回伪造成功。
- **desktop-only**：Windows 专属机制，不以 Android 空操作冒充等价。

四类数据不能混用：`user_export` 标准化用户档案、`android_visible_ui` 当前屏幕观测、`operator_supplied_android_snapshot` 经已知 schema 检查的本地 Android 数据库副本（不是来源认证）、合成 fixtures。尤其是 UI 观测 ID 只绑定截图/层级快照，不能充当微信原生持久消息 ID；档案监听不是实时微信全局监听。写操作默认需要当前提交账号证明；显式独占设备假设属于较弱保证，结果 `assurance` 不得丢弃。

## 逐项覆盖

测试文件索引缩写：**C**=`tests/test_controller.py`，**U**=`test_ui.py`/`test_adapter.py`，**A**=`test_archive.py`，**L**=`test_listener.py`，**M**=`test_media.py`，**S**=`test_adversarial_safety.py`，**H**=`test_hardware_smoke.py`，**R**=`test_rooted.py`，**K**=`test_keycapture.py`，**X**=`test_interactions.py`，**P**=`test_moments.py`，**D**=`test_message_formats.py`/`test_recall.py`。所有测试证据均为离线；没有一项意味着真机通过。

| ID | 参考语义 / 主要 API | Android 实现与边界 | 自动化证据 | 真机/等价状态 |
|---|---|---|---|---|
| F01 | 账号发现、自身资料：`list_accounts/get_self_info` | implemented：显式账号绑定、UI `verify_account`；不枚举所有账号 | C/U/A/H/S | live-pending；自动账号发现 unavailable |
| F02 | 密钥提取、派生、SQLCipher 解密、缓存/WAL | implemented：固定账号静止 Root 快照、SQLCipher 校验、绑定密钥、已知 EnMicroMsg schema、受限实验 WCDB hook；不是 Windows 内存提取移植 | R/K；见 [密钥说明](DATABASE_KEYS.md) | live-pending；受限 hook live-pending；通用现代全版本获取 unavailable |
| F03 | 联系人搜索、昵称/微信号映射 | implemented：档案查询及已知 Root rcontact schema；UI 写入必须单独证明稳定 ID | A/R/S | Root 快照 live-pending；非实时全版本读取 |
| F04 | 会话、未读数、摘要 | implemented：档案或已验证 Root 快照的会话聚合；未知未读数明确为 unknown | A/R | 真机完整会话/未读 live-pending；未读语义 unavailable |
| F05 | 历史/最近/增量消息 | implemented：档案分页/增量；`read_visible` 仅当前屏幕；Root 历史受 schema 限制 | A/C/U/R | live-pending；屏幕不是完整历史 |
| F06 | 消息类型/内容解析，压缩/XML/card 摘要 | implemented：受限 UTF-8/XML 类型/卡片/撤回解析，保留原始载荷/来源；未知二进制/压缩明确不支持 | D/A/R | automated-fixture；无解压/现代全类型保证 |
| F07 | 原生消息 ID、hash、分片发送者解析 | implemented：档案按 account/chat/id 隔离；UI ID 为快照作用域 | A/L/S | 原生分片/全局 hash 查找不等价 |
| F08 | JSON/SQLite 全历史导出 | implemented：导出已提供档案的全部/指定会话；不保证已取得全部手机历史 | A | 导出档案 automated-fixture；手机全量 live-pending |
| F09 | 单聊/全局监听、新会话发现 | implemented：已提供追加档案监听；非后台微信数据库/通知服务 | L/A | 真实微信全局监听 unavailable |
| F10 | listener 水位、重试、重启持久化 | implemented：来源+账号绑定，(sequence,id) 游标，回调失败不确认 | L/S | automated-fixture；回调可能重复，不是 exactly-once |
| F11 | 群列表、群名与 ID 映射 | implemented：档案查询、已知 Root chatroom schema；不允许模糊群名授权发送 | A/R/C | Root 快照 live-pending；实时全量 unavailable |
| F12 | 群成员列表、成员变化 watcher | implemented：档案成员快照差异；已知 Root schema 成员读取 | A/R/L | Root 读取 live-pending；实时完整成员 unavailable |
| F13 | 文本发送、输入回读 | implemented：有身份/输入/提交检查的有界状态机；无已标定实机 profile | C/U/S | live-pending；提交不是送达回执 |
| F14 | 文件发送 | implemented：受限共享目录上传+配置的文件选择/目标确认工作流 | C/U/S | live-pending；真实文件 picker/预览未验证 |
| F15 | 图片发送 | implemented：附件路径/后缀/内容摘要与配置的图片选择流程 | C/U/S | live-pending；没有原图质量通过声明 |
| F16 | 引用/回复特定消息 | implemented：唯一原文选择+引用预览+输入回读的有界工作流 | C/U/S | live-pending；重复正文拒绝、不猜消息 |
| F17 | 真正的群 @ 成员/@所有人 | implemented：单成员原生 picker+成员 ID/token 回读，群/成员分别授权；@所有人未实现 | X/C/U | live-pending；缺稳定 token 身份时 unavailable |
| F18 | 指定消息转发到显式多个收件人 | implemented：可见唯一正文→单收件人稳定 ID/源预览校验→提交；批量分块/附言未实现 | X/C/U | live-pending；仅单目标子集，不是完整多收件人等价 |
| F19 | 转发到标签 | unavailable；档案标签查询不能替代真实转发 | A 仅查询 | unavailable |
| F20 | 语音转发为 SILK 文件 | unavailable：原项目本来也不是原生语音转发；这里不冒充支持 | M 仅本地复制 | unavailable |
| F21 | 发起语音/视频通话 | implemented：校验账号/聊天后，语音/视频选项作为最终提交；此前仅导航 | C/U/S | live-pending；菜单/提交边界须按真实版本验证 |
| F22 | 拍一拍 | implemented：仅私聊，稳定聊天/目标头像证明后双击提交；群拍一拍未实现 | C/U/S | live-pending；没有真实微信 UI 通过证据 |
| F23 | 撤回自己消息 | implemented：自己消息唯一选择，撤回菜单项作为最终提交 | C/U/S | live-pending；撤回过期/真实菜单未验证 |
| F24 | 从群中探测/添加好友 | unavailable：未实现群成员稳定身份+申请对话框流程 | C 不支持路径 | unavailable |
| F25 | 标签列表、创建/改名/删除 | 标签读取仅来自档案；创建/修改/删除 unavailable | A 只读 | unavailable（手机 CRUD） |
| F26 | 标签成员读取/添加/移除 | 档案标签数据可读；真实成员编辑 unavailable | A 只读 | unavailable（手机编辑） |
| F27 | 给标签成员广播文字 | unavailable：不提供无人确认批量外部副作用 | C 不支持路径 | unavailable |
| F28 | 联系人改备注/清空备注 | implemented：稳定联系人 ID→资料→备注编辑→精确输入回读→保存提交；不支持空备注/群改名 | C/S | live-pending；保存尝试不代表持久化核实 |
| F29 | 标签/备注/加好友只读探测 | unavailable；`verify_account` 只检查账号，不代表这些探测已实现 | H 仅账号 | unavailable |
| F30 | 图片密钥探测、XOR/AES 解码/下载 | unavailable：本地已导出媒体复制不等于 Android 加密媒体解密 | M 仅本地路径/校验 | unavailable |
| F31 | 图片档次与触发原图下载 | unavailable；不把缩略图/截图当原图 | 无业务实现 | unavailable |
| F32 | 语音可用性、原始 SILK 提取 | unavailable：Root 数据库可读不自动建立媒体表/codec 支持 | M 仅已有文件 | unavailable |
| F33 | 视频/文件定位下载、类型分发 | implemented：`MediaDownloader` 只复制既有导出文件并校验 SHA256 | M/S | automated-fixture；手机/服务器下载 unavailable |
| F34 | 表情截图与气泡裁剪 | 适配器有整屏截图；未实现选中表情方向感知裁剪 | U 截图输出守卫 | unavailable（完整语义） |
| F35 | 朋友圈读取、过滤、增量 | implemented：档案查询及当前可见帖子观察；非 sns.db 完整过滤/增量 | A/P | live-pending；原生 SNS 数据库语义 unavailable |
| F36 | 朋友圈点赞/评论通知和未读计数 | unavailable；普通档案记录不是通知事件数据库 | 无业务实现 | unavailable |
| F37 | 朋友圈内容、评论/回复树 | 档案可保存原数据；未实现完整 XML/评论树语义 | A 仅档案 | unavailable（完整解析） |
| F38 | 进入/定位/读取可见朋友圈 | implemented：发现→朋友圈可见行，MomentReference 绑定快照/显示内容；不滚动全文历史 | P/U | live-pending；引用不是原生 SNS ID/作者 wxid |
| F39 | 朋友圈点赞/取消点赞 | implemented：逐 MomentReference 授权、过期拒绝、明确点赞/取消菜单项提交 | P/C/U | live-pending；未确认服务器状态/原生 ID |
| F40 | 朋友圈评论/回复评论 | implemented：快照引用绑定的顶层评论、精确输入回读；回复特定评论未实现 | P/C/U | live-pending；父评论回复 unavailable |
| F41 | 朋友圈缓存/本地/CDN 媒体解码下载 | unavailable；本地媒体复制不等价 | M 仅已有文件 | unavailable |
| F42 | 朋友圈缓存观察/冻结媒体 | unavailable：没有真实缓存观察/归属证据 | 无业务实现 | unavailable |
| F43 | RecallGuard 镜像与撤回检测 | implemented：只镜像明确提供的消息，以 account/chat/native server ID 匹配撤回 XML，重复事件持久化 | D/S | automated-fixture；无设备轮询/原地改写扫描/媒体备份/未见历史恢复 |
| F44 | 多账号/窗口进程绑定 | implemented：精确 serial/account/profile 与切换失效保护；不自动切账号 | C/U/A/L/S | live-pending；无 Windows 进程绑定语义 |
| F45 | 独立聊天/历史子窗口 | desktop-only；不以空对象/空列表假装完成 | C 明确错误 | unavailable（平台差异） |
| F46 | 前台/遮挡/布局/OCR 防护 | implemented：前台包/屏幕、唯一元素、边界、快照新鲜度；无 OCR 兜底 | U/C/S | live-pending；观察与点击不具原子性 |
| F47 | 诊断、生命周期、唤醒、强停 | implemented：CLI metadata、私有快照；不自动解锁/启动微信；Windows taskkill/path N/A | H/U/CLI | live-pending；强停不默认授权 |
| F48 | CLI、中英别名、新手菜单 | implemented：本项目 CLI；不保持原 12 命令/中文别名/交互菜单兼容 | CLI/CI/样例 | 原 CLI 完全兼容 unavailable |
| F49 | 配置、日志、锁、节奏、多语言 | implemented：显式 policy、摘要日志、设备锁、超时；语言由精确 profile 决定 | C/U/S | 无通用多语言/自动化节奏兼容承诺 |
| F50 | 遗留会话置顶/静音/未读/隐藏/删除 | unavailable；原项目也是未接入当前 facade 的遗留 UI 路径 | 无业务实现 | unavailable |
| F51 | 遗留各导航入口 | unavailable：账号/聊天导航不等于收藏/小程序等全部导航/业务 | C 仅账号/聊天 | unavailable |
| F52 | 遗留搜索新朋友/申请 | unavailable；不能把群成员查询或 API 名称当完整好友流程 | 无业务实现 | unavailable |
| F53 | Message/Response/Chat 类型和来源 | implemented：明确来源、快照 ID、dry_run/submitted/unknown 等状态 | A/C/U/S | 非原 wxauto 返回值/import 的 drop-in 兼容 |

## 重要语义差异

1. 原项目的 `GetAllMessage` 仅最近 50 条，`GetMessageByHash` 仅搜索最近 200 条；真正全量导出来自数据库 API。本项目不会用相同名字掩盖更少数据。
2. 原项目顶层 `wx.Listener` 是兼容抽象类，真实实现为 `db.Listener`；`GetAllSubWindow` 恒为 `[]`，`Chat.Close` 仅把主窗前置。这些不是应当照搬的功能成功证据。
3. 原项目的语音转发为导出 SILK 后“发文件”；表情 capture 为静态截图裁剪；朋友圈只读本地缓存不等于完整服务器时间线。
4. 原项目未实现朋友圈发布、支付/红包转账、创建/解散群或踢人管理、原生语音录制发送。项目没有为凑功能数字添加这些未被要求的副作用。
5. 本项目有限的相似 API（例如 `WeChat/Chat/SendMsg`）并不兼容所有原始参数、import 路径、返回值和数据库内部对象。调用前检查能力，处理 `UnsupportedFeature`，不要忽略错误。具体方法/授权对象见 [UI 工作流](UI_WORKFLOWS.md)；载荷/撤回子集见 [消息数据](MESSAGE_DATA.md)。
6. `calibrated=true` 是本地操作人声明，必须有真实环境证据；它本身不能把页面模板变成已验证微信支持。所有角色、身份标识及提交边界必须对应真实版本；缺失时该功能仍 unavailable。

## 最终验收条件

只有每一个声称支持的分组都有确切版本/SDK 的实机结果、边界/失败测试和脱敏证据，并且剩余缺口获得明确处置，才可讨论“完成全部功能测试”。目前只能报告已执行的离线测试/打包结果及上述未实现、待设备验证的范围。验收方法见 [TESTING.md](TESTING.md)。
