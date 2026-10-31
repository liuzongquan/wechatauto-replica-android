# Android WeChat 本地数据库密钥方案：开源证据比较

核查时间：2026-10-31 UTC。范围：用户本人已授权的设备/账号，允许 root，目标 Android 11/API 30+、WeChat 8.0+。本次仅通过公开仓库与官方网页阅读源代码/文档；**未下载或执行第三方项目，未连接真机，未捕获任何账号密钥**。以下是方案选型，不是“已完成全版本兼容”的证明。

## 结论（可直接用于架构决策）

1. **主方向：显式授权、限定数据库路径的运行时 WCDB 参数观察，然后对同一账号快照实际开库验证。** 本次检索中，WeKit 提供了最明确的近期维护、版本分层和真实 hook 代码证据，适合作为架构参考；不应因此把完整增强模块引入产品，也不应直接复制 GPL 代码到不兼容许可证项目。
2. **IMEI/device ID + UIN 只能是受限候选生成器。** Android 10 起设备标识访问受到限制；不能把当前硬件 IMEI 或某个常量当成所有 Android 11+/微信 8.x 的确定解密算法。候选必须绑定用户选择的 Android user、账号目录、数据库快照并验证。
3. **没有找到足以证明“所有 Android 11+ × 所有微信 8.0+”的开源实测矩阵。** 文档宣称支持、代码能编译、合成数据库测试通过、真机运行四者必须分别陈述。三个以上有可检查实现的候选如下，其中两项只能作为历史/风险对照，不能冒充同等级“可靠方案”。
4. 本仓库已独立实现 legacy WCDB 七参数观察器及现代 WCDB `core.Database.setCipherKey` 的两个**受限实验性 opt-in profile**，签名或身份不匹配时明确失败。它们已通过离线接口与合成加密库测试，但均未获真机验证；不能把这些已知签名等同于全部微信 8.x。

## 候选比较

### A. Ujhhgtg/WeKit — 近期维护的运行时观察参考

- [仓库](https://github.com/Ujhhgtg/WeKit)；核查时将 `master` 固定为 [`650febaa3717eddd39f5f29a4dfa6dfd37875cb1`](https://github.com/Ujhhgtg/WeKit/commit/650febaa3717eddd39f5f29a4dfa6dfd37875cb1)，提交时间 2026-09-12；仓库元数据 `pushed_at` 为 2026-10-26。**push 时间不等于默认分支提交时间，也不等于本功能最后测试时间。** 其 README 指向的原始 `cwuom/WeKit` 仓库当前已 archived，不能混为同一维护状态。
- [固定提交的版本说明](https://github.com/Ujhhgtg/WeKit/blob/650febaa3717eddd39f5f29a4dfa6dfd37875cb1/docs/getting-started.md)：Android >=9/SDK28，ARM64；国内微信 <8.0.65 不支持，8.0.65–68 维护，8.0.69–76 支持，8.0.77–78 初步适配；Play 8.0.68–69 维护。其“维护”明确不再每次更新主动测试；“初步适配”明确未充分测试。
- [在线版本说明](https://docs.wekit.ujhhgtg.dev/getting-started) 核查时已把初步适配扩到 8.0.77–80，加载要求亦与上述固定提交不同。**在线站点和固定提交不是同一快照，不能把在线最新版声明套在固定源代码上。** 两者均不是数据库密钥功能专属的 Android/ROM/微信配对测试报告。
- [实际代码 `WeDatabaseApi.kt`](https://github.com/Ujhhgtg/WeKit/blob/650febaa3717eddd39f5f29a4dfa6dfd37875cb1/app/src/main/java/dev/ujhhgtg/wekit/features/api/core/WeDatabaseApi.kt)：仅在 verbose log 开启时 hook `com.tencent.wcdb.database.SQLiteDatabase.openDatabase` 的七参数重载，观察路径、`byte[]` password，以及 HMAC/KDF/page-size 参数；不是只靠 README 的方案。[官方 FAQ](https://docs.wekit.ujhhgtg.dev/faq) 对应日志说明和 SQLCipher compatibility 1 导出流程。
- 局限：日志会暴露明文密钥，完整模块功能面很大；所检查 hook 不做账号目录过滤、快照绑定验证或 WAL 一致性处理。不能直接沿用这些生产安全边界。
- [LICENSE](https://github.com/Ujhhgtg/WeKit/blob/650febaa3717eddd39f5f29a4dfa6dfd37875cb1/LICENSE)：GPL v3 文本（GitHub SPDX 为 GPL-3.0）。复制/派生并分发时须遵守相应 copyleft、许可证/声明与对应源码要求。当前建议只参考设计与公开 API，独立实现并保留研究来源；不是声称已经复用了其代码。

### B. adamyi/AndroidWechatSQLiteDecrypt — 可复核的历史最小 Xposed 实现

- [固定提交](https://github.com/adamyi/AndroidWechatSQLiteDecrypt/commit/4d48105425246fd6592e1b3f0f10b518367b6089)：`4d48105425246fd6592e1b3f0f10b518367b6089`，2017-12-02；默认分支未显示新的实现维护证据。
- [测试声明](https://github.com/adamyi/AndroidWechatSQLiteDecrypt/blob/4d48105425246fd6592e1b3f0f10b518367b6089/AndroidHook/README.md) 精确为 **Android 7.0 + Xposed 88.2 + WeChat 6.5.16**，不满足本任务版本目标。
- [实际 `XModule.java`](https://github.com/adamyi/AndroidWechatSQLiteDecrypt/blob/4d48105425246fd6592e1b3f0f10b518367b6089/AndroidHook/app/src/main/java/am/yiad/wxsqlpasswordhook/XModule.java) 过滤 `com.tencent.mm`，观察同一 WCDB 七参数 `openDatabase` 并将路径、密钥/hex 和 cipherSpec 写入 Logcat。[解密脚本](https://github.com/adamyi/AndroidWechatSQLiteDecrypt/blob/4d48105425246fd6592e1b3f0f10b518367b6089/decrypt/decrypt.sh) 使用 Tencent SQLCipher fork 与参数后导出；未见快照/WAL/账户绑定校验；脚本提示输入 kdfIter 但实际默认 PRAGMA 写死 4000，故不宜直接拿来当参数通用实现。
- [BSD-2-Clause LICENSE](https://github.com/adamyi/AndroidWechatSQLiteDecrypt/blob/4d48105425246fd6592e1b3f0f10b518367b6089/LICENSE)：源代码分发保留版权、条件和免责声明；二进制分发在文档/材料中保留。可作为许可相对清晰的历史参考，但仍需审查其第三方子模块/反编译材料，不建议整仓复用。

### C. XxxxX114/WeChatDumpTool-Android — 近期离线派生/验证实现，兼容性证据不足

- [固定提交](https://github.com/XxxxX114/WeChatDumpTool-Android/commit/2d337dd78f9bf04eb73b15d1a543ae6ee10e170d)：`2d337dd78f9bf04eb73b15d1a543ae6ee10e170d`，2026-10-05，仓库亦在当天创建；维护历史短。
- [README](https://github.com/XxxxX114/WeChatDumpTool-Android/blob/2d337dd78f9bf04eb73b15d1a543ae6ee10e170d/README.md) 声称真机大库调通和提供合成自检，但**未给出精确 Android/ROM/WeChat 版本配对**；Python 版本声明不能替代 Android 兼容性证据。
- [实际 `wxdump_android.py`](https://github.com/XxxxX114/WeChatDumpTool-Android/blob/2d337dd78f9bf04eb73b15d1a543ae6ee10e170d/wxdump_android.py) 从 prefs 搜集 UIN 与设备标识候选（包括符号变化和十六进制编码），用历史 MD5 派生模式产出候选；调用 SQLCipher compatibility 1 并读 `sqlite_master` 判断可开库，随后 `sqlcipher_export`。有实际验证，不只是看似正确的字符串。
- [实际 `wxpull.sh`](https://github.com/XxxxX114/WeChatDumpTool-Android/blob/2d337dd78f9bf04eb73b15d1a543ae6ee10e170d/wxpull.sh) 复制 DB/WAL/SHM，并默认 force-stop；但默认选择最大数据库、多账号不精确、共享存储中转及 `chmod -R a+rX`、明文日志/key.json 和可选暴力枚举均不应照搬。一次停止也不等于已证明所有写进程静止或快照一致。
- [MIT LICENSE](https://github.com/XxxxX114/WeChatDumpTool-Android/blob/2d337dd78f9bf04eb73b15d1a543ae6ee10e170d/LICENSE)：实有许可证文件，复制 substantial portions 时保留版权与许可声明。仍需审查依赖与来源，当前只把候选生成/数据库验证分离的思想作为对照。

### D. linuxwff789/wxbackup — 现代 WCDB API 路线提示，不宜直接采用

- [固定提交](https://github.com/linuxwff789/wxbackup/commit/bfc04e3d01d31fff7083e61d6487cc8246faff96)：`bfc04e3d01d31fff7083e61d6487cc8246faff96`，2026-09-17；该次提交是定时备份权限修复，不证明 key hook 当日通过。
- [README](https://github.com/linuxwff789/wxbackup/blob/bfc04e3d01d31fff7083e61d6487cc8246faff96/README.md) 面向微信 8.0.74、最低 Android 8/推荐 Android 12+、Magisk/LSPosed。是版本要求/适配宣称，不是精确设备配对测试报告。
- [实际 `KeyCaptureHook.kt`](https://github.com/linuxwff789/wxbackup/blob/bfc04e3d01d31fff7083e61d6487cc8246faff96/xposed/src/main/java/com/nous/wxhook/xposed/hook/KeyCaptureHook.kt) hook `com.tencent.wcdb.core.Database.setCipherKey` 所有同名方法，读取 key/pageSize/version。它采用全进程 `keyCaptured` first-key-wins，未绑定数据库路径；既写宿主 filesDir 又经 ContentProvider 传递，并把原始 key 写日志。README 说 `/data/local/tmp/.wechat_key`，实际代码写 `ctx.filesDir`，说明必须核对实现而不是照 README 集成。加载 native library，但所检查入口没有调用所声明的 `nativeInstallHooks()`；不能据此声称已验证 native fallback。
- README 的 MIT badge/link 与仓库事实矛盾：本次默认分支根目录无 LICENSE，`LICENSE` fetch 404，GitHub license 元数据为空。因此**不能把它标成已核验 MIT 可复用项目**；只可据公开 API 文档独立实现类似思路。

## 排除/辅助参考

- [ppwwyyxx/wechat-dump](https://github.com/ppwwyyxx/wechat-dump/blob/be3009fbd24a26e4c9f4dfbd498f6e56ef4831e6/README.md)，`be3009fbd24a26e4c9f4dfbd498f6e56ef4831e6`（2026-02-23），GPL3：当前 README 明确要求用户另行解密，项目不提供解密指引。其 2025-01-01“最新微信验证”是消息解析/渲染声明，**不能用作当前密钥获取支持证据**。
- [zoome22/android-wechat-decrypt](https://github.com/zoome22/android-wechat-decrypt/tree/c4ad34bc96d78b9518a99b9232ce83996edb3678)，`c4ad34bc96d78b9518a99b9232ce83996edb3678`（2025-12-26）：README 是 IMEI/UIN 派生与手工页解密；根目录无 LICENSE、未见精确版本测试说明，不优先选为复用依赖。
- [TANGandXUE/wcdb-key-tool](https://github.com/TANGandXUE/wcdb-key-tool) 和 [dylan121322/wxkey-hook](https://github.com/dylan121322/wxkey-hook) 当前 README 均针对桌面平台（后者 macOS），不是 Android 实现。不可因为名称有 WCDB/微信密钥就套用其 raw-key/HMAC/page-size 常量。

## 官方证据与实现约束

- [Tencent WCDB 源码](https://github.com/Tencent/wcdb/blob/17ebdcbb0a6d610432ea0106718a40676506b0a3/src/java/main/src/main/java/com/tencent/wcdb/core/Database.java)：固定 `17ebdcbb0a6d610432ea0106718a40676506b0a3`（2026-10-08）确有现代 `setCipherKey(byte[], int, CipherVersion)`、简化重载与 `getPath()`；可以研究**按实例路径绑定**的后续 adapter。官方库存在此 API 不证明某个微信 APK 使用它。[LICENSE](https://github.com/Tencent/wcdb/blob/17ebdcbb0a6d610432ea0106718a40676506b0a3/LICENSE) 为 BSD-3-Clause 并明确列出第三方不同许可证，实际集成需要一并遵守。
- [Android 官方标识符限制](https://developer.android.com/about/versions/10/privacy/changes#non-resettable-device-ids)：API29 开始对不可重置标识符加权限限制。这只能支持“硬件 IMEI 不可靠”的判断，**不能证明微信所有新版一定使用某个固定 fallback**。
- [SQLCipher 官方 API](https://www.zetetic.net/sqlcipher/sqlcipher-api/)：设置 key 不会立即验证；需实际读 schema。所有 cipher 参数须在首次数据读前正确配置。`cipher_integrity_check` 依赖 HMAC 开启；compatibility1/HMACoff 的库不能冒称 HMAC 认证成功。应报告“给定参数可读+SQLite结构检查通过”，与密码学认证区分。
- [SQLite WAL 官方文档](https://www.sqlite.org/wal.html)：WAL 属持久数据库状态，离开 WAL 复制主库可能丢失已提交数据。应在授权只读快照流程中保留/验证同一时刻的 DB+WAL（SHM 按所用引擎处理），只在隔离工作副本合并/导出，不删除源 WAL，不对源库 `rekey`/`cipher_migrate`/写入。
- [Frida 17 官方迁移说明](https://frida.re/news/2025/05/17/frida-17-0-0-released/)：GumJS 不再内置 Java bridge；frida-tools 的 REPL 特别打包 bridge，不代表 Python `create_script` 的裸脚本天然有 Java。当前独立适配器固定 16.7.19 只解决已知打包差异，**不是“16.7.19 对所有新 Android 最稳定”的证明**；需真机匹配 host/server/ABI/API 后测试，后续可显式打包新版 bridge。

## 建议保留的产品边界与验收证据

- 候选状态至少区分：未捕获、签名不支持、候选已观察、账号/路径匹配、同一快照可开库、结构校验通过；捕获不等于解密成功。
- 仅用户指定设备、Android user、包名、账号目录、`EnMicroMsg.db`；禁止按最大 DB、首个 key 或全进程全部秘密猜测成功；不上传到服务端。
- 密钥与明文只保留在权限收紧的临时本地存储/内存中，日志仅输出不含秘密的结果；不使用共享存储、Logcat、命令行参数打印密钥；过期/换号清理。
- attach-only 可能错过已发生的 open；应有超时与未捕获结果，不伪报成功。没有设备测试前不为强制停机、注入启动、native扫描等扩大默认行为。
- 每个成功组合记录 Android API/ROM/ABI、WeChat versionName+versionCode+APK来源、adapter版本/commit、hook签名、实际 cipher参数（不含 key）、DB/WAL快照处理和验证结论。现阶段把整个目标范围标为**待真机矩阵验证**。

本报告未按 stars 排名，也未声称绝对最可靠。当前选择依据是可读源码、明确维护状态、版本声明、许可证可核验性、数据边界和可验证失败模式。


## 本仓库后续实现状态（与上述研究证据区分）

研究完成后，本仓库独立增加 `wcdb2-setcipher3-experimental-v1`，按官方三参数 API 与实例 `getPath()` 限定路径。它与 legacy observer 一样只有离线合约/加密 fixture 证据，没有真机版本通过记录；并未复制 wxbackup/WeKit 代码。当前精确 API、二进制口令限制和授权边界以 [数据库密钥说明](DATABASE_KEYS.md) 为准。
