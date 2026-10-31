# Android 微信数据库解密密钥：获取、验证与应用

## 明确对象与当前结论

这里处理用户明确指定的**本地微信数据库解密密钥/口令**，不是网页登录 token、登录 session、支付密钥或用于冒用账号的凭据。只允许在已授权账号/设备范围内用于只读数据库副本。

已有 `rooted.py` 实现：受限 ADB/root 静止快照、显式 key provider、SQLCipher 配置、完整性/表结构/自身账号字段检查，以及内存中绑定数据库包指纹的 `BoundDatabaseKey`。另有 `keycapture.py` 的显式 opt-in 实验 WCDB Java hook（默认 attach；可选 spawn 另需更高范围许可），详见下文。**没有已验证的真实微信 8.x 设备密钥获取结果**；“用户允许 root”不代表当前环境已连接设备或知道该版本的密钥。实验运行时捕获必须单独公开精确版本/方法与证据，不能把历史派生公式或库中一个候选值称为已获取现代微信密钥。

## 支持的密钥来源

### 开源方案选型依据

已检查开源社区的实际实现、近期维护、精确版本声明与许可证，而不是按 stars 选择“最靠谱”：

- **WeKit**：有近期维护和可阅读 WCDB hook 实现，作为运行时观察的架构参考；固定源码声明支持从微信 8.0.65 起的部分版本、较新版本仅初步适配，不能覆盖本项目整个 8.0+ 目标。其 GPL-3.0 代码没有复制进本项目。[固定源码与版本说明](https://github.com/Ujhhgtg/WeKit/blob/650febaa3717eddd39f5f29a4dfa6dfd37875cb1/docs/getting-started.md)
- **AndroidWechatSQLiteDecrypt**：有明确的 Android7/微信6.5.16 历史测试证据，不能拿来证明 Android11+/微信8.x；仅作历史机制参考。[固定测试声明](https://github.com/adamyi/AndroidWechatSQLiteDecrypt/blob/4d48105425246fd6592e1b3f0f10b518367b6089/AndroidHook/README.md)
- **WeChatDumpTool-Android**：候选派生后实际开库验证值得参考，但精确现代版本矩阵不足，默认强停/最大库选择/明文 key 输出等边界不采用。[固定实现](https://github.com/XxxxX114/WeChatDumpTool-Android/blob/2d337dd78f9bf04eb73b15d1a543ae6ee10e170d/wxdump_android.py)
- **wxbackup**：提供现代 WCDB `core.Database.setCipherKey` 路线提示，但 first-key-wins/密钥日志不采用，README 的 MIT 声明未能由 LICENSE 文件核验，因此不直接复用其代码。[固定实现](https://github.com/linuxwff789/wxbackup/blob/bfc04e3d01d31fff7083e61d6487cc8246faff96/xposed/src/main/java/com/nous/wxhook/xposed/hook/KeyCaptureHook.kt)

当前选择是**按官方 API 独立实现受限观察 + 绑定快照实际验证**，而不是引入功能面更大的第三方模块。完整证据、在线文档与固定提交版本差异、维护/许可证边界见 [开源方案比较](KEY_SCHEME_COMPARISON.md)。没有找到能证明所有 Android11+ × 微信8.0+ 的实测矩阵，也不宣称任何项目绝对最可靠。

| 来源 | 当前含义 | 关键限制 |
|---|---|---|
| `key_provider` 返回 `str` 或 `bytes` | 操作人以本地交互或受控内存提供已有授权口令 | 工具不会替操作者证明口令来源；必须实际解密并核验 |
| `BoundDatabaseKey` | 已成功验证后仅在内存保留的绑定对象 | 仅接受同账号、同数据库/侧文件指纹、同微信版本、同 cipher profile |
| `LegacyKeyCandidate` | 使用明确提供的设备标识与 UIN 生成**历史算法候选** | 不自动读取标识、不保证适用任何现代版本；只能经真实数据库验证后成为已验证密钥 |
| `FridaKeyCapture` | 附着现有获准 USB Frida runtime，显式选择 WCDB 七参数 openDatabase 或 WCDB2 三参数 setCipherKey | 实验性，无真机验证；需单独 attach 同意与未验证 profile 风险确认 |
| 自动获取全部现代 8.x 密钥 | **未完成/未验证** | 缺少真实设备、确切版本与可证实的捕获/验证证据 |

`LegacyKeyCandidate` 不是通用解决方案。它实现历史 `MD5(device_id + uin)` 的前七位候选，但设备标识来源、Android 隐私限制、微信加密实现和数据库版本均可能不同。`CipherProfile.legacy_android()` 同样只是显式历史参数集合，不是“8.0+ 默认正确配置”。不要在文档/产品 UI 中把两者配起来后宣称通吃所有版本。

## 1. 获准的静止 Root 快照

前提：已 root 设备、已获批准的 `su` 权限、Android API30+、微信精确版本、明确 Android user、明确 32 位小写十六进制账号目录、明确账号 ID。工具**不**自动 root、刷机、提权、强停/冻结微信、替用户处理 root 授权或扫描其他账号。

UI 测试要求微信在前台，Root 一致快照则要求微信所有相关进程已经静止/退出；二者是不同阶段，不应同时执行。操作人自行在授权范围内准备静止状态并确认；代码发现 `com.tencent.mm` 或其子进程运行时拒绝捕获。

```python
from wechatauto_android.rooted import AdbRootSnapshotter, RootSnapshotConfig

# 值均由操作人提供；输出父目录须已存在且为本地私有目录。
config = RootSnapshotConfig(
    serial=authorized_serial,
    android_user=authorized_android_user,
    account_dir=authorized_account_directory_hash,
    account_id=authorized_account_id,
    expected_wechat_version=observed_exact_wechat_version,
    consent_root_read=True,       # 仅在本次设备/账号读取已获授权时设置
    confirm_quiesced=True,        # 操作人已准备静止状态；不是强停授权
)
snapshot = AdbRootSnapshotter(config).capture("/private/new-snapshot-directory")
```

仅固定读取该账号目录中的 `EnMicroMsg.db`、`SnsMicroMsg.db` 及存在的 `-wal/-shm/-journal` 配套文件。拒绝符号链接/越界路径、非空回滚 journal、缺主库、超限文件或复制前后哈希变化；不覆盖已有输出。快照文件权限 0600，目录 0700。清单含序列号、账号断言和路径标识，也是私有数据。

两次进程检查+文件哈希相同仅是“操作人静止确认”的补充证据，**不是原子在线备份保证**。账号在捕获阶段尚未验证（manifest 明确记录 false），需在数据库打开后核验自身账号。`SnsMicroMsg.db` 被保存不代表实现朋友圈 schema 解析。

## 2. 使用口令与精确 CipherProfile

数据库解密需要带 SQLCipher 能力的可选驱动；在受支持 Linux x86_64 主机安装 `pip install -e ".[root]"`，其他主机限制见 [部署说明](DEPLOYMENT.md#主机安装)。不要使用普通 `sqlite3` 冒充加密支持。已解密的、结构合格的 SQLite 副本可只读打开，但不能把规范化 JSON 档案或任意 SQLite schema 当作微信数据库。

```python
import getpass
import json
from pathlib import Path
from wechatauto_android.rooted import AndroidDatabase, CipherProfile

# 参数文件只存非秘密的 cipher 设置，来自针对该实际数据库的已核验记录。
# 不在这里提供一个“所有 8.x 通用”的猜测配置。
cipher = CipherProfile(**json.loads(Path("private/cipher-profile.json").read_text()))

with AndroidDatabase(
    snapshot.database_path,
    account_id=authorized_account_id,
    expected_wechat_version=observed_exact_wechat_version,
    key_provider=lambda: getpass.getpass("数据库口令（不回显）: "),
    cipher_profile=cipher,
) as database:
    capabilities = database.capabilities
    messages = database.get_messages(authorized_chat_id, limit=20)
    bound_key = database.validated_key  # 保留在当前受控进程内，不打印/序列化
```

不要将口令写在 CLI 参数、环境变量示例、shell history、源码或日志中。BoundDatabaseKey/候选对象禁用 pickle 等普通序列化，仍不应通过其他反射/调试方式读取并外传其内部内容。`getpass` 只隐藏终端输入，不保证进程内存安全；Python 字符串/对象也不能承诺可靠擦除内存。`repr` 被脱敏并不允许把对象 `asdict()`、pickle 或任意调试转储上传外部。

## 3. 成为“验证成功”的条件

1. 明确而匹配的 SQLCipher 参数：compatibility、page size、KDF iteration/algorithm、HMAC 开关/algorithm。HMAC 关闭的历史配置只能声称可读/结构一致，不能称为通过了密码学消息认证。
2. 成功读取数据库并通过 `PRAGMA integrity_check`；加密驱动确有 `cipher_version`。
3. 存在当前已知 EnMicroMsg 的必需 `message`/`userinfo` 字段，拒绝未知结构而不是返回空消息。
4. `userinfo(id=2)` 字段恰好等于操作人声明的账号 ID；这只是 schema 字段一致，不是对文件来源或真实已登录设备账号的认证，不自动切换到别的账号。
5. 验证期间数据库/关键侧文件指纹未变化；指纹包含主库、WAL、回滚 journal，不包含可重建的 SHM。只读连接使用 WAL 感知方式，不用 `immutable=1` 忽略 WAL。
6. 成功后才生成绑定对象。任一账号/数据库指纹/微信版本/cipher profile 不同都拒绝复用。

本地 `AndroidDatabase` 接收的微信版本是操作人断言，数据库文件本身不能证明安装版本；能力输出把 `wechat_version_device_verified` 和 `authenticated_device_account_verified` 设为 false，仅报告 `schema_self_id_matches=true`。应将捕获时设备版本证据与本地验证记录一起保存在私有目录。

`mode=ro`/`query_only` 阻止写数据库内容，但 SQLite 打开 WAL 副本时仍可能创建/更新本地 `-shm`；不能据此保证任意输入目录字节级不变。保留原始加密证据，直接使用 `AndroidDatabase` 时在专用私有工作副本中验证。容器只读挂载不满足引擎所需侧文件时应报错，不得因此删除 WAL 或使用 `immutable=1` 丢弃已提交数据。

## 4. 能读什么、不能读什么

当前已知 schema 可读取消息、按消息聚合会话；当相应字段存在时读取联系人/群/成员。返回 `origin=operator_supplied_android_snapshot`（数据来自调用者提供的本地文件，不认证为设备来源），消息保留原始类型、server ID、方向、时间及数据库指纹。未知群发送者不凭空猜测。没有支持的二进制消息解码器时明确报错。

不支持完整媒体密钥/图片解密、SILK 提取、朋友圈 schema、连续实时数据库同步、所有 8.x schema。固定快照的数据源身份含指纹；下一份快照不是原来的持续实时来源，不能把切换快照误称为不丢事件的 live listener。

## 5. 实验性运行时观察：两个显式 WCDB profile

`FridaKeyCapture` 是独立编写的、**未真机验证**的候选观察器，不是一个覆盖全社区方案/全部版本的现成破解器。可显式选择以下精确签名；没有对应类/签名、类尚不可用、密码格式不同、cipher profile 不符或没有相应正常事件时，明确失败/超时，不自动切换 profile、扫描任意内存或附着其他进程。官方 API 存在并不证明每个微信 APK 使用它。

| hook_profile | 观察点与路径约束 | 证据 |
|---|---|---|
| `wcdb-java-open7-experimental-v1`（默认） | `com.tencent.wcdb.database.SQLiteDatabase.openDatabase` 七参数重载；匹配传入数据库路径 | [官方 WCDB API](https://tencent.github.io/wcdb/references/android/reference/com/tencent/wcdb/database/SQLiteDatabase.html) |
| `wcdb2-setcipher3-experimental-v1` | `com.tencent.wcdb.core.Database.setCipherKey(byte[], int, Database.CipherVersion)`；用该实例的 `getPath()` 匹配显式账号数据库 | [官方固定源码](https://github.com/Tencent/wcdb/blob/17ebdcbb0a6d610432ea0106718a40676506b0a3/src/java/main/src/main/java/com/tencent/wcdb/core/Database.java) |

前置条件：

1. 操作人已在明确获准的设备上配置可信、版本匹配的 USB Frida runtime；主机安装 `[keycapture]`。程序不下载/安装/启动设备端 server，不开放网络端口。
2. **单独取得进程 attach 同意**。root 读取许可不能替代这一许可；运行时挂钩可能改变时序、造成不稳定。
3. 已有该设备/账号/精确版本的授权加密 `RootSnapshot`；manifest/实际文件集与哈希匹配。明文 fixture 不能作为成功捕获证据。
4. 明确指定所有 cipher 参数，`compatibility/page_size/kdf_iterations/use_hmac/kdf_algorithm/hmac_algorithm` 不留自动猜测。历史 WCDB 有些字段不可观测，不会据此推断默认算法；最终仍需真实解密验证。
5. 默认 `mode="attach"` 只附着已存在的 `com.tencent.mm` 进程，用户在另一个明确获准的测试阶段自行触发正常数据库打开/密钥配置事件；默认模式不重启、登录、切账号或点击来制造事件。快照阶段要求静止，观察阶段需要进程运行，**两阶段不能并行取在线副本**。

```python
from wechatauto_android.keycapture import CaptureConfig, FridaKeyCapture

# 只有操作者已明确同意本次 attach 与实验风险，才设置以下两个 True。
capture_config = CaptureConfig(
    serial=authorized_serial,
    android_user=authorized_android_user,
    account_dir=authorized_account_directory_hash,
    account_id=authorized_account_id,
    expected_wechat_version=observed_exact_wechat_version,
    consent_attach=True,
    allow_unverified_profile=True,
    timeout_seconds=30,
    max_candidates=8,
    mode="attach",  # 不包含启动/强停/系统进程 instrumentation 授权
    hook_profile="wcdb-java-open7-experimental-v1",  # 或明确选择上表 WCDB2 profile
)
verified_key = FridaKeyCapture(capture_config).capture(snapshot, cipher)
# 仅在同一受控进程内用于同一 snapshot/cipher；不要打印、记录或返回密钥值。
```

候选密码通过受信 USB runtime 的进程间通道暂存于主机内存，不能声称它从不离开设备。1–40 字节密码按原始字节保留，支持 NUL/非 UTF-8；更长的观察值只能以无 NUL 的无损 UTF-8 文本应用，无法满足时明确拒绝。底层二进制口令使用 `PRAGMA hexkey` 解码后进入 KDF，不当作跳过 KDF 的 raw-AES `x'...'` 密钥。固定 SQLCipher4.6.1 的 hexkey 路径只有 40 字节缓冲区，因此不能直接提交更长二进制值以免静默截断。[官方实现](https://github.com/sqlcipher/sqlcipher/blob/v4.6.1/src/pragma.c) 候选不会暴露给正常调用者；通过指定快照的 SQLCipher、完整性、schema、账号字段检查后才返回 `BoundDatabaseKey`，`source=frida_observed_candidate_verified` 表示该候选通过此数据库验证，**不是对手机账号来源的密码学认证**。

观察有明确时限/候选数量上限，不自动再次 attach。超时不代表数据库没有密钥；可能只是没有触发匹配重载/密钥配置事件。清理/卸载 hook/detach 结果不明时返回 `OutcomeUnknown`，先人工检查 runtime，不把候选已验证视为整体成功。日志处理禁止回显 hook payload/错误堆栈；内存擦除仅尽力而为，Python/Frida 的不可变副本无法保证被清零。

主机固定 `frida==16.7.19` 是为了这个实现依赖的 Java bridge 行为，并非声称它是最新版本。升级需显式引入/验证对应桥接方式，不能无测试替换版本。[Frida Java bridge 文档](https://frida.re/docs/bridges/)

候选验证会把限定的**加密** DB/侧文件复制到 0700 私有临时目录（文件 0600），在工作副本上验证，避免 SQLite 的 SHM 行为改变原始 snapshot/manifest。此处不把口令或解密明文写入临时目录；结束后清理工作副本，但不承诺系统缓存/内存的可靠擦除。

### 可选 spawn：不是只读 attach 的别名

为观察启动期数据库事件，存在实验性 `mode="spawn"`。**本次没有在任何设备上运行过 spawn；root 许可本身不足以启用它。** 除已有 attach 同意和未验证 profile 风险确认外，还必须逐项独立取得并设置：

- `consent_package_stop_relaunch=True`：允许确切 `com.tencent.mm` 包的强停/重新启动副作用。
- `consent_system_instrumentation=True`：允许 Frida 的 zygote/system_server instrumentation，范围明显大于仅读取微信数据库。

该模式仅支持 Android user 0；会核对指定包、精确版本、数据目录和未运行状态。检查与调用不是原子的，而且 **Frida spawn 路径仍会无条件 force-stop 该包后启动**，不能因检查时空闲就称为无强停。未取得上述许可时保持 attach，不从 root/helper setup 许可推定，也不通过其他方式绕过。

程序在新进程暂停阶段加载 hook，再恢复进程等候 Java 初始化/对应事件；不能保证一定赶上密钥设置。初始化失败时尝试恢复自己启动的暂停进程并卸载/分离观察器，而非擅自杀进程，因此失败后微信也可能仍运行。清理不确定应人工检查，不能循环 spawn。`operation_notice()` 可在接触 runtime 前显示影响；成功后的 `last_capture` 只记录模式/钩子和启动/恢复元数据，不含密钥，不是设备认证或真机验收证书。

## 6. 错误诊断，不泄露秘密

| 现象/异常 | 可能原因 | 合理下一步 |
|---|---|---|
| `PermissionDenied` / 设备不可读 | 未获 su、微信仍运行、缺明确范围/静止确认 | 操作人核对授权与状态；不自动 root/强停 |
| `StaleSnapshot` | DB/WAL 变化、回滚 journal、复制不一致 | 重新建立获准的静止状态，再捕获新目录 |
| `DatabaseOpenError` | 错口令、错 cipher 参数、损坏/混合 WAL、非 SQLCipher 格式 | 核对来源/版本/完整 bundle 和参数；单凭错误不能断定“密钥错” |
| `UnsupportedSchema` | 加密可能已成功，但表/字段/内容类型不受支持 | 为该版本新增独立解析器与真实脱敏 fixture，不把结果当空库 |
| `IdentityMismatch` | 账号错误或绑定对象来自别的数据库/版本/参数 | 停止，核对账号与快照来源，禁止自动选另一个账号 |

公开报告只给异常类型、版本、参数名/非秘密值、脱敏 schema 结构和数据完整性结论；不提供密钥、device_id/UIN、原始 DB/WAL/SHM、账号目录、数据库正文或包含敏感值的运行时 hook 日志。

## 测试界限

`tests/test_rooted.py` 使用合成明文/真实 SQLCipher 加密 fixtures 和假 ADB 输出验证错误口令、账号/指纹绑定、WAL/快照边界、未知 schema 与解密结果。它不是对真实微信设备的密钥捕获、数据解密或全版本兼容验证。真实端到端状态仍为 **live-pending**。
