# 部署、版本适配与故障定位

## 支持范围

目标为 **Android 平台的微信应用 8.0+**（包名 `com.tencent.mm`）。系统目标为 **Android 11 / API 30+**，对应“2020 年以后的 Android 系统”的当前解释；微信版本要求与 Android 系统版本要求独立检查。具体系统/厂商组合仍须实测，当前没有已验证的真实设备/Android OS/微信版本组合。

| 层次 | 当前可声称的范围 | 不能据此声称 |
|---|---|---|
| Python | 项目声明 Python 3.10+；CI 配置逐版本测试 | 未运行的 Python/操作系统组合已通过 |
| Android 系统策略 | API 30+；已标定 profile 必须列出经过确认的精确 SDK | 任意厂商/未来 Android 版本均已测试 |
| 微信版本策略 | 目标微信 >=8.0；执行使用精确版本 profile | 任意 8.x、9.x 或未来版本自动兼容 |
| `profiles/wechat-8.0-semantic-draft.json` | 未标定的角色/配置草案，版本字段为 8.0.0 | 微信 8.0.0 真机验证通过 |
| Root 数据读取 | 独立授权、加密参数/表结构校验的路径 | 有 root 就自动得到所有库/媒体/朋友圈 |
| Docker | Python 控制器打包 | Android 系统、模拟器或已登录微信 |

## 主机安装

使用 [README](../README.md) 的虚拟环境安装。实际连接设备需要 `[android]` extra；只有档案处理则无需该 extra，完整确定性测试必须装齐可选依赖。依赖钉住 UI SDK 版本，升级 SDK 后必须重新测试身份检查、元素唯一匹配、错误返回及 SDK 自动重试行为；不能仅更新依赖后沿用旧真机结论。

主机操作系统与手机 Android 版本是两回事：

- `[android]`：设备 UI 控制依赖，macOS/Windows/Linux 是否连通仍需各自主机 ADB 环境验证。本地媒体复制当前要求 POSIX，不支持原生 Windows 该模块。
- `[root]`：固定 `sqlcipher3-binary==0.5.4`；该发行版仅有 CPython manylinux **x86_64** 轮子，没有 macOS/Windows/ARM 轮子及 sdist，不能承诺任意主机 `pip install` 成功。[PyPI 文件列表](https://pypi.org/project/sqlcipher3-binary/0.5.4/#files)
- `[keycapture]`：固定 `frida==16.7.19` 的实验性主机依赖；不包含设备端 Frida server 的安装/配置/权限，也不代表已验证微信版本。
- 完整测试环境：Linux x86_64 / CPython3.10–3.13，`pip install -e '.[android,root,keycapture,test]'`。Mac/Windows 用户可以将**本地授权快照**交给本机 Linux/amd64 Docker 解密，或按 [SQLCipher Python 驱动上游说明](https://github.com/coleifer/sqlcipher3) 自行构建并验证兼容驱动；本项目未验证其原生编译路径，不能把构建失败当作密钥错误。

Apple Silicon 的 Docker 需显式 `--platform linux/amd64`（Compose 已声明），可能使用架构模拟；未实测其设备网络/性能。默认镜像包含 `[android,root]` 和 Debian 官方 ADB 命令行包，可运行 UI 控制器、本地快照解密及显式获准的 Root 快照；不包含 Android 系统或设备端 Frida。Root 快照也可直接在已配置 ADB 的获准主机运行。

**keycapture 在同一 Python 进程中捕获并用 SQLCipher 验证候选**，并非 Mac 捕获后自动把密钥交给 Docker 的分离架构。Mac 上仅能安装 Frida 客户端不表示该完整路径可用；需要本机已构建/验证的 SQLCipher 驱动，或在具备获准设备传输的 Linux x86_64 环境执行。原生 Mac 驱动构建与所有真机传输组合仍未验证。

### 哈希锁定的安装与更新

完整 Linux 测试/开发环境使用锁文件，避免可选 SDK 和传递依赖随安装时漂移：

```bash
python -m pip install --require-hashes -r requirements/test.lock
python -m pip install --no-deps --no-build-isolation -e .
python -m pytest -q
python -m build --no-isolation
```

`runtime.lock` 对应 UI+Root，`keycapture.lock` 对应 UI+Root+实验 Frida，`test.lock` 再包括完整测试/构建工具，`build.lock` 仅构建工具。Docker 下载/安装第三方轮子均校验哈希，自己的 wheel 从本次源码构建后以 `--no-deps` 安装；CI 同理。Python 哈希锁不锁定 Debian ADB 包或 Docker 基础镜像摘要，不能把整个系统镜像称作字节级可复现。

维护者更新依赖时，在干净分支使用已有可信 `uv` 工具重新生成并审查锁文件，不手工改哈希：

```bash
uv pip compile requirements/build.in --universal --python-version 3.10 --generate-hashes -o requirements/build.lock
uv pip compile pyproject.toml --extra android --extra root --universal --python-version 3.10 --generate-hashes -o requirements/runtime.lock
uv pip compile pyproject.toml --extra android --extra root --extra keycapture --universal --python-version 3.10 --generate-hashes -o requirements/keycapture.lock
uv pip compile pyproject.toml requirements/build.in --extra android --extra root --extra keycapture --extra test --universal --python-version 3.10 --generate-hashes -o requirements/test.lock
```

随后重新安装锁定依赖、跑完整套件、wheel/sdist 安装与 Docker CI；更新 UI/Frida/SQLCipher 后重新审查内部 API、binary-key 行为与版本 profile，不能继承旧真机结论。

用官方 [Platform-Tools](https://developer.android.com/tools/releases/platform-tools) 安装 ADB。设备连接与无线配对操作参见 [Android ADB 文档](https://developer.android.com/tools/adb)。USB 调试 RSA 授权由操作人在设备上核对，项目不会自动接受。安装 SDK 不需要脚本替用户批量同意许可证。

```bash
adb version
adb devices -l
```

必须确定目标序列号为 `device` 状态；`unauthorized` 需设备确认，`offline` 需解决连接，不应更换到另一台可用设备。连接/启动自动化服务可能弹出安装或系统提示，操作人自行检查与确认。微信应由操作人手动登录、解锁并置于前台。

## Profile 标定：精确版本，失败即停

1. **记录环境**：Android OS/API/build、厂商型号、微信完整 `versionName`/`versionCode`、语言、字体/显示缩放、方向、UI SDK 版本和 profile SHA256。只公开脱敏后的信息。
2. **复制草案至私有目录**：不要直接把草案标成已验证。示例没有经过真实界面标定，缺失关键角色应保留为不可执行。
3. **最小只读采样**：在获准的测试账号上，分别采集账号身份、会话目标身份、输入框、提交按钮和操作后状态。UI XML/截图含聊天与身份数据，应保存在本地私有目录。`snapshot` 本身会连接 UI 自动化服务。
4. **选择器必须唯一**：用真实资源 ID/精确文本/描述组合；仅 display title 不足以证明稳定联系人 ID。不得使用“第一个匹配”或泛化坐标绕过歧义。群 ID 未能可靠读取时，不启用群写操作。
5. **先验证纯配置**：`wechatauto-android profile-check /private/path/device-profile.json` 只证明结构可解析，不证明手机存在或选择器正确。
6. **只读验收**：`doctor` 只检查设备/包/版本元数据，输出 `account_verified=false`；`scripts/hardware_smoke.py` 才执行账号身份读取/导航检查。导航页面本身会改变 UI，但不会提交微信业务操作。两者都须显式允许 UI helper 初始化（`--allow-device-setup`）。
7. **受控写验收**：得到具体测试收件人和动作授权后，执行一项最小操作并独立核查结果；覆盖同名、账号切换、断线、错误输入、遮挡、重复点击及不确定结果，记录 [验收模板](TESTING.md#真机验收记录模板)。
8. **发布 profile 证据**：精确微信版本+Android SDK 列表+环境组合+通过的操作列表+已知缺口。`calibrated`/`evidence` 是操作人声明，不是系统自动认证，不应伪造证据。

提交前账号证明是单独的安全门：严格模式需要当前提交快照中的 `commit.account_identity` 含精确 `{account_id}`，或使用 Python 独立账号校验回调。没有该证据时，早先在个人页面读到的账号不能自动视为始终有效。`--assume-exclusive-device` 是明确接受较弱先验的可选项，仅适用于操作者控制的独占设备、无人触摸/无其他控制器且不会切换账号的测试窗口；其 `assurance` 会标注弱保证。它不取消收件人检查，也不是原子性/送达保证。

### 微信升级后的迁移

升级前保留旧 profile 与测试记录。升级后先只读探测真实版本；版本不匹配时停用写操作，不修改通配符或简单扩大版本区间。复制一个新的精确版本 profile，重新采集关键角色、对比层级变化，运行全部离线回归与该版本实际受控验收。若只有文本功能通过，不要将文件、转发、标签、朋友圈标为同版本通过。

数据库 profile 也需单独迁移：核对数据库名称、SQLCipher 参数、页大小、字段/类型、账号/聊天/发送者标识、消息排序与 WAL 处理。UI profile 通过不代表数据库 schema 兼容，反之亦然。无法验证的新表或消息类型应保留原始元数据并报不支持，不能凭表名相似自动套旧 schema。

## 远程 ADB

推荐把控制器部署在 USB 设备所在的可信主机上，仅通过 SSH/VPN 登录该主机运行程序。这样 ADB server 保持监听 loopback，无须暴露调试端口。

确需跨主机连接时，使用管理员配置的可信私网或绑定 loopback 的受控隧道，并同时考虑 uiautomator2 使用的 ADB 转发回环端口；只转发 5037 不一定足够，必须验证完整连接路径。不要公开 `5037`、设备 `5555` 或 UI 自动化端口，不使用 `adb -a`/公网安全组放行，不把调试密钥复制给无关容器/用户。Android 无线调试配对由操作人按系统页面确认；不自动更改设备的 TCP 调试模式。

## Docker

Dockerfile 是非 root 的**主机控制器**，不是模拟器。离线服务默认 `network_mode: none`、只读文件系统、去除 capabilities、无默认凭据、无设备挂载。默认启动只输出帮助。

```bash
docker build --platform linux/amd64 -t wechatauto-replica-android:local .
docker compose config --quiet
docker compose run --rm controller
```

只有在 Linux 主机上已完成 ADB 设备授权且确有操作授权时，才显式启用 `device` profile；该服务使用 host 网络访问本机 loopback ADB server，不向公网新开端口：

```bash
# 主机先确认目标设备，且微信已在前台；变量均需自行设置
adb devices -l
docker compose --profile device run --rm \
  -v '/absolute/private/device-profile.json:/config/profile.json:ro' \
  android doctor --allow-device-setup --serial "$ANDROID_SERIAL" --profile /config/profile.json
```

`network_mode: host` 会削弱网络隔离，不能称作网络沙箱。[Docker 官方文档](https://docs.docker.com/engine/network/drivers/host/) 说明 Linux 原生支持、Docker Desktop 需另行启用相应功能；本仓库未实测 Desktop 路径。遇到转发不可达优先回到主机运行，不用 `--privileged` 或全盘挂载补救。

`device` 服务同时为 adbutils 设置 `ANDROID_ADB_SERVER_HOST/PORT`、为 ADB CLI 设置 `ADB_SERVER_SOCKET=tcp:127.0.0.1:5037`，两者指向已获准的本机 server。先比较主机/镜像的 `adb version`，避免客户端/server 不兼容导致连接中断；不启动公网 ADB、不替用户改变调试端口。执行自己的受审查 Root 读取脚本时可用 `docker compose --profile device run --entrypoint python ... android /config/read_snapshot.py`，只读挂载脚本、仅挂载专用可写输出目录；按 [密钥文档](DATABASE_KEYS.md) 显式配置账号与静止状态许可。默认命令仍是 `--help`，绝不自动捕获设备数据。

如确需实验性主机 Frida 依赖，可显式构建 `--build-arg RUNTIME_LOCK=requirements/keycapture.lock`；这不会安装或启动设备端 runtime，也不会自动授权进程 attach。默认镜像不带该依赖。

**ADB 连通不等于 Frida USB 传输连通。** Frida 的 USB 设备发现有自己的传输实现，不能假设它遵循 adbutils 环境变量或 `ADB_SERVER_SOCKET`；本仓库没有验证 Docker/远程 Frida USB 透传，不自动挂载 USB 总线或映射 runtime 端口。不得将 5037、设备调试口或 Frida 常见 27042 端口开放公网。CI 配置仅面向离线镜像，实际构建/运行结果以对应提交的检查为准；真实传输/设备验收仍暂缓。

Compose 不默认挂载任何私人档案、ADB keys 或操作日志。若需要读档案，只读挂载具体文件；执行操作时另行挂载由 UID 10001 可写的专用日志目录，不能使用宿主根目录。并发容器的 `/tmp` 锁不共享，不能依赖它们互斥控制同一设备；一台设备只运行一个控制器实例。设备在执行过程中也不应有人手动操作。

发送附件前，操作者需在设备共享存储中准备专用 `Download/wechatauto` 目录。适配器不会借上传自动创建目录，不允许越出该目录、覆盖、符号链接或大于 100 MiB 的文件。文件会留在设备共享存储中，测试后由操作者按授权范围清理；不自动删除其他文件。

## 失败排查

| 现象 | 先检查 | 不应做 |
|---|---|---|
| 未发现/未授权设备 | 线缆、USB 驱动、`adb devices -l`、设备授权页面 | 自动改用另一序列号 |
| 前台包/版本不符 | 微信是否前台、精确 versionName、profile | 跳过包/版本核对 |
| 选择器缺失/多匹配 | 脱敏层级、语言/字体、账号/聊天页面、资源 ID | 任取第一个或盲点坐标 |
| 账号/收件人 ID 不可读 | 是否有可靠稳定标识、是否正确 profile | 仅按昵称发送 |
| 提交后超时/断线 | 查操作日志与设备/接收端实际状态 | 生成新 ID 再次发送 |
| Root 读取/解密失败 | 已授权 root、限定路径、密钥/参数、schema、WAL 一致性 | root 全盘扫描、忽略 MAC/完整性错误 |
| 容器连接失败 | 主机先跑通、host 网络、ADB 转发、只读目录限制 | 开公网端口或加 privileged |

上报时记录命令（去掉内容/ID/密钥）、异常类型、软件版本、profile 名称/哈希以及预期与实际；证据保持本地私有，见 [安全文档](SECURITY.md)。
