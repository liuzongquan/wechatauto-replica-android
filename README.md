# wechatauto-replica-android

面向 **Android 微信 8.0 及以上版本**的 Python 主机控制器：显式授权的 **ADB + uiautomator2** 设备适配器、版本绑定的选择器、默认预演与副作用保护，以及用户提供档案的查询、导出、媒体复制和增量监听工具。这里的 `8.0+` 指微信版本；“2020 年以后的 Android 系统”当前落实为 **Android 11 / API 30+** 目标，二者独立校验。

> **8.0+ 是适配目标，不是对全部 8.x/未来版本已兼容的承诺。当前不是 Windows 原项目的完整等价替代品，也没有已验证的 Android 微信版本。** 自动化测试使用合成 XML/JSON 和模拟设备；不等于真机微信验收。私有数据库、全量历史、媒体解密等能力未因同名/相似 API 而实现。用户已允许使用 root 方案，但这不代表已有设备、密钥或真机测试结果，也不授权自动 root/刷机。完整差异见 [功能矩阵](docs/FEATURE_MATRIX.md)。

**本阶段验证范围：云端开发、离线测试、打包与 CI；本地连接和真机微信验收已暂缓，不记作通过。已验证真实版本矩阵为空（`verified_versions=[]`）。**

参考 [fanyuantaier/wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica/tree/bc2e6f2461330eb1a53996d2f2afc18416376b52)，固定 revision `bc2e6f2461330eb1a53996d2f2afc18416376b52`，Apache-2.0。其目标为 Windows 微信 4.x，本项目使用 Android 适配实现，不移植进程内存扫描、DLL 修改或桌面数据库解密。

**数据库解密密钥重点**：提供受限 Root 快照、数据库/账号字段/版本/cipher 绑定验证，以及两个显式选择的实验 WCDB 运行时观察 profile；尚未真机获取任何密钥。方案取舍与使用边界见 [数据库密钥说明](docs/DATABASE_KEYS.md)。

## 1. 安装与无设备试运行

需要 Python **3.10+**。源码安装推荐虚拟环境：

```bash
git clone https://github.com/liuzongquan/wechatauto-replica-android.git
cd wechatauto-replica-android
python -m venv .venv
source .venv/bin/activate
# Windows PowerShell：.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e '.[android]'

# 以下命令不连接设备、不发送消息
wechatauto-android --help
wechatauto-android capabilities
wechatauto-android archive --file docs/examples/archive.json \
  --account demo-account messages demo-chat --limit 20
```

仅处理本地档案时安装 `python -m pip install -e .` 即可，不需要 Android 依赖。示例档案是合成数据；真实数据必须由操作者在授权范围内另行提供，不能把示例结果当作读取了手机微信。

## 2. 设备部署：先授权，再只读检查

1. 安装官方 [Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools)；按系统配置 USB 驱动/设备访问权限。不要下载来源不明的微信 APK。
2. 在自己的测试设备上开启开发者选项和 USB 调试，使用 USB 连接主机。**在设备上手动核对并接受调试授权**。项目不会自动确认授权、解锁手机或登录微信。
3. 在设备上安装并手动登录要测试的微信，保持前台、屏幕解锁。避免同时人工点击和运行其他自动化。
4. 执行 `adb devices -l`，记录唯一序列号；`unauthorized`/`offline` 不是可用设备，先由操作人解决。命令必须明确传 `--serial`，不得自动挑第一台手机。
5. 为当前微信包名、**精确版本**和界面语言配置选择器 profile。仓库里的示例不是经真机标定的通用 profile；先读 [部署与 profile 标定](docs/DEPLOYMENT.md)。
6. 仅在确认有权读取该设备后执行：

```bash
# 请替换成已获授权设备和经过人工标定的本地 profile
export ANDROID_SERIAL='YOUR_AUTHORIZED_SERIAL'
wechatauto-android profile-check /private/path/device-profile.json
wechatauto-android doctor --allow-device-setup --serial "$ANDROID_SERIAL" \
  --profile /private/path/device-profile.json
```

`doctor` 可连接设备并初始化 UI 自动化服务，可能产生设备端组件/进程；“只读”指不发送微信消息等业务副作用，不表示完全不改变设备运行状态。首次初始化的设备提示由用户自行确认。profile 不匹配或设备不可用时应停止，不要关闭安全检查来凑通。

## 3. 预演与发送边界

发送默认生成预演计划，不连接手机。仅真实执行才需要通过目标白名单、设备/账号/聊天身份、输入回读和操作编号等保护；缺失或不明确时失败，不猜测收件人。

```bash
# 预演：不加 --execute。示例不会联系任何真实收件人。
wechatauto-android send --serial "$ANDROID_SERIAL" \
  --profile /private/path/device-profile.json --account YOUR_ACCOUNT_ID \
  --chat-id YOUR_APPROVED_CHAT_ID --title 'YOUR_EXACT_CHAT_TITLE' \
  --text '仅用于已获同意的测试会话' --journal local-state/actions.sqlite
```

确认本次内容、收件人和副作用均已获授权后，真实执行还需 `--allow-device-setup --execute --allow-target YOUR_APPROVED_CHAT_ID --operation-id UNIQUE_OPERATION_ID`。操作编号应为本次意图单独生成，不能把重试当作新操作；如果状态为结果未知，先人工核查，**不要再次发送**。选择器 profile 不能仅靠改一个“已标定”标志就视为可信。详见 [安全边界](docs/SECURITY.md)。

默认严格模式还要求提交瞬间可读取当前账号的 `commit.account_identity`，或 Python API 中独立的 `account_verifier`。普通微信页面可能没有此证明，此时**拒绝执行是预期行为**。只有操作人明确接受较弱的“已独占设备且不会切换账号”假设时，才另加 `--assume-exclusive-device`；这不是安全保证，结果的 `assurance` 会注明较弱依据，仍不能证明观察与点击原子化。不要为让示例跑通默认开启该选项。

当前 API 不保证兼容 Windows 原项目的 import 路径、参数、返回值或数据库语义。提供能力状态与明确失败比返回伪造成功重要；不支持的功能不能在业务逻辑中被当作成功降级。

## 4. Docker：控制器，不是 Android 模拟器

```bash
docker build --platform linux/amd64 -t wechatauto-replica-android:local .
# 默认无设备访问、无网络，仅展示帮助
docker compose run --rm controller
docker run --rm --network none wechatauto-replica-android:local capabilities
```

镜像不包含已登录的微信、Android 系统或模拟器，不会自动创建可用设备。真机连接建议先在主机完成 ADB 授权，容器仅通过明确启用的本机连接访问；见 [Docker/远程部署](docs/DEPLOYMENT.md#docker)。不需要 `--privileged`、Docker socket 挂载、公开 ADB 端口或默认凭据。

## 5. 测试与验收

完整离线套件（含真实 SQLCipher 加密 fixture）使用 Linux x86_64、Python 3.10–3.13；这与目标手机 Android 版本无关。macOS/Windows 可先安装 UI 控制器；当前 `[root]` 的二进制驱动不提供其平台轮子，详见 [主机依赖](docs/DEPLOYMENT.md#主机安装)。

```bash
python -m pip install --require-hashes -r requirements/test.lock
python -m pip install --no-deps --no-build-isolation -e .
python -m pytest -q
python -m build --no-isolation
```

**2026-10-31 冻结版本离线结果：783 项测试通过（16.57 秒），失败/错误/跳过均为 0。** 语句覆盖率 94.64%，分支覆盖率 89.19%（合并显示 93%）；全新哈希锁定安装、`pip check`、Ruff、两个 JS 语法检查及 wheel/sdist 构建通过。**发布后的远端 CI 与实际 Docker 构建/运行仍待验证；真机验收已暂缓，完整功能等价仍未完成。** 证据口径见 [测试记录](docs/TESTING.md#本次冻结版本的离线验证记录)。

CI 分开运行确定性测试、wheel/sdist 构建与安装、Docker 构建/无设备启动；**任何一项绿色都不能代替真机功能验收**。只读硬件检查需要显式授权：

```bash
python scripts/hardware_smoke.py --confirm-authorized-device --allow-device-setup \
  --serial "$ANDROID_SERIAL" --profile /private/path/device-profile.json \
  --account YOUR_ACCOUNT_ID
```

缺少参数、设备、匹配版本或账号证据时该脚本返回非零，而不是跳过后报成功。即使通过，它也只证明本次只读检查，并不证明收发消息、群操作、朋友圈或媒体路径已通过。完整状态和待验收步骤见 [测试与验收](docs/TESTING.md)。设备截图、层级 XML 和聊天数据不要上传公开仓库。

## 文档

- [功能矩阵与桌面差异](docs/FEATURE_MATRIX.md)
- [主机、远程 ADB、Docker 和版本 profile](docs/DEPLOYMENT.md)
- [Python UI 工作流：原生 @、转发、朋友圈等边界](docs/UI_WORKFLOWS.md)
- [消息载荷解析与已观测撤回镜像](docs/MESSAGE_DATA.md)
- [测试分层、真机验收、失败排查](docs/TESTING.md)
- [安全与私有数据](docs/SECURITY.md)
- [数据库密钥获取、验证、Root 快照与限制](docs/DATABASE_KEYS.md)
- [社区密钥方案比较与许可证取舍](docs/KEY_SCHEME_COMPARISON.md)
- [许可证](LICENSE) · [参考项目声明](NOTICE)

本项目与腾讯、微信及参考项目作者无关联或背书。请在设备与账号所有者许可范围内使用，遵守适用规则与微信服务条款，不用于骚扰、批量垃圾消息或未授权数据访问。
