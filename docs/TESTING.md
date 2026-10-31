# 测试分层与真机验收

## 什么算通过

| 证据层 | 执行内容 | 能证明 | 不能证明 |
|---|---|---|---|
| 配置/单元 | 纯 Python、合成 JSON/XML、假 SDK、失败注入 | 校验、解析、状态机、权限/路径/去重保护符合该测试输入 | Android 或微信真实界面可用 |
| 模拟工作流 | 控制器在模拟页面间导航与提交 | 预期角色协议和失败分支 | 真实微信存在那些确认页面/稳定 ID |
| 打包 | wheel/sdist 构建、全新环境安装、CLI 无设备启动 | 发行物可安装、入口可用 | 手机连接、业务功能 |
| Docker | 镜像构建、非 root、无网络帮助/能力输出 | 容器可运行控制器 | Android 模拟器/微信端到端测试 |
| 真机只读 smoke | 精确设备、包、版本、SDK、账号读取 | 本次获准设备的只读前置条件 | 发送成功、全量历史或完整功能兼容 |
| 真机业务验收 | 具体版本+具体授权对象的业务操作、独立核查 | 被记录的单项功能在该环境通过 | 所有 8.x/所有 Android11+ 组合通用 |

`skip`、`xfail`、缺设备、缺权限、没有执行、模拟通过，都不能写成真机通过。没有校准 profile 的功能保持不可执行，不为了“全绿”伪造元素或成功结果。`submitted` 表示发出了提交尝试，**不等于 `verified` 或对端送达**；不确定结果不得自动重试。

## 本地与 CI

以下完整套件使用 Linux x86_64 / Python 3.10–3.13，所有可选 SDK 必须真实安装；缺 SQLCipher/Frida/uiautomator2 不能以跳过测试换取“全部通过”。这不是对手机系统版本的限制。

```bash
python -m pip install --require-hashes -r requirements/test.lock
python -m pip install --no-deps --no-build-isolation -e .
python -m pytest -q
python -m ruff check src tests scripts
python -m build --no-isolation
```

`.github/workflows/ci.yml` 的三类 job 分别报告 fixture tests、wheel/sdist 和 Docker，不连接真实设备或账号。CI 不注入微信凭据/ADB keys、不自动启动真机验收、不收集或上传截图/层级/聊天导出。最终测试数字以实际命令输出和对应 commit 的 CI 为准，不把本文件中的计划视作执行结果。

若更新控制器、SDK、profile、导出 schema、Root/cipher 参数，必须重跑相关完整套件和打包检查。涉及依赖内部 RPC 的适配代码需要固定真实 SDK 的合约测试；只有假对象不能证明 SDK 内部调用签名仍正确。

## 本次冻结版本的离线验证记录

2026-10-31，云端 Linux x86_64 的全新虚拟环境按哈希锁文件安装依赖后，对冻结的源码、测试和锁文件运行完整套件；运行期间这些输入未改变。

| 检查 | 本次结果 |
|---|---|
| 完整离线测试 | **783 passed，16.57 秒；0 failed / 0 errors / 0 skipped** |
| 语句覆盖 | 3358 / 3548，**94.64%** |
| 分支覆盖 | 1147 / 1286，**89.19%** |
| 合并覆盖显示 | **93%**（pytest-cov 合并统计，不等同于语句覆盖率） |
| 哈希锁定依赖全新安装 / `pip check` | 通过 |
| Ruff / 两个运行时观察 JS 的 `node --check` | 通过 |
| wheel + sdist 构建 | 通过 |
| 发布后的远端 CI / 实际 Docker 镜像构建与运行 | **待验证** |
| 真机微信 / Root 采集 / Frida attach 或 spawn | **未执行；按用户要求暂缓** |

以上是离线测试、依赖与构建证据，包含真实 SDK 合约和真实 SQLCipher 加密合成数据库，但**不包含真实微信业务验证，也不表示全部功能已等价实现**。发布后应以对应提交的 CI 结果补充远端证据；不能把工作流配置文件的存在写成运行成功。全部 53 个参考语义分组仍按 [功能矩阵](FEATURE_MATRIX.md) 分别报告实现、离线覆盖与缺口。

## 当前真机状态

**已暂缓，未执行，不是通过。** 本阶段按要求继续云端开发、离线验证、打包/CI 和源码提交，不再把连接本地电脑作为这些工作的前提。尚无用于本次验收的设备/模拟器、确切微信版本与测试账号，未完成受控业务副作用授权；真实已验证版本矩阵为空。已知目标：微信 **8.0+**；Android **11 / API30+**；用户允许 root 读取方案。Root 许可不等于设备已接入，也不等于允许自动 root、刷机、强停微信或任意聊天数据外传。

当前未发布任何“已真机验证”的 profile。示例草案的精确版本号只是配置演示，不代表对应版本通过。后续版本验收应按 [部署迁移流程](DEPLOYMENT.md#微信升级后的迁移) 逐项记录。

## 只读硬件检查

先确认微信位于该 profile 所期待的首页并允许账号信息读取；脚本会导航到个人信息页面后返回，不提交消息/通话等业务动作，但启动 UI helper、读取内容和导航仍需授权。

```bash
python scripts/hardware_smoke.py --confirm-authorized-device --allow-device-setup \
  --serial YOUR_AUTHORIZED_SERIAL --profile /private/path/device-profile.json \
  --account YOUR_ACCOUNT_ID
```

无参数、未明确授权、未标定 profile、SDK/微信版本不匹配、没有设备或账号证明失败都返回非零。该脚本不是全功能验收器；它不发送消息、不拨号、不执行 root 快照。若需业务验收，按下表逐项取得对象/动作授权并独立确认结果。

## 真机验收计划

1. **身份与版本**：两台设备/账号相似信息不能串用；错误序列号、非微信前台、屏幕关闭、账号切换、未知 SDK 或微信版本必须阻止写入。
2. **文本/附件**：获准测试收件人；中文、英文、emoji、多行、空白；同名联系人；输入被替换；发送前后断网/断线；文件名/哈希/接收端内容比对。未知结果只人工核查，不重复提交。
3. **引用/撤回/拍一拍/通话**：唯一源消息/成员身份、过期撤回、重复正文；核查真实界面的提交边界；当前撤回菜单项、语音/视频选项本身就是最终提交，不虚构额外确认页。不将会直接产生副作用的按钮放进“准备步骤”。通话限事先同意的测试账号。
4. **转发、标签、备注、加好友、朋友圈**：只有实际实现并提供该版本 profile 后才能验收；没有实现不能靠通用点击或离线集合查询补齐。使用专门测试标签/群/帖子，验证中断/部分完成和重复请求。
5. **历史与 Root 数据库**：已有 root 的获准设备；明确账号目录、密码/密钥提供方式、精确 cipher/schema；在用户确认的静止状态取得一致 DB/WAL 副本；核对消息总数、重复序号、跨聊天 ID、群发送者、未知类型和缺失媒体。样例数据库通过不等于真实 EnMicroMsg 已通过。
6. **监听与媒体**：来源必须是真实获准的持续更新数据；断线、重启、大量积压、相同 sequence、回调失败、媒体缺失/损坏/过期和路径拒绝分别测试。监听静态导出文件只证明档案刷新逻辑，不证明微信后台事件流。
7. **多版本**：每个支持的微信精确版本与 Android SDK/OEM 组合分别验收；升级会让原结果失效，不能继承通配符通过。

原项目某些功能本身也有 beta、局部语义或兼容空实现，不应伪造更强承诺。全部 53 个语义分组的状态见 [功能矩阵](FEATURE_MATRIX.md)。

## 真机验收记录模板

只在本地私有目录创建记录；公开报告时脱敏，不提交真实账号/聊天正文：

```text
Commit:
Python / uiautomator2 / ADB:
Android OS / API / build / device model:
WeChat package / versionName / versionCode / language / font scale:
UI profile name / SHA256 / calibrated SDKs:
Root / cipher / database schema profile (if used):
Feature ID (F01..F53) / exact API:
Device/account/recipient authorization reference (private):
Initial UI/data state:
Input / expected outcome (private):
Observed outcome / independent confirmation:
Operation ID / journal state (where applicable):
Local redacted evidence location:
Result: PASS_DEVICE | FAIL_DEVICE | NOT_RUN | BLOCKED | UNSUPPORTED
Missing coverage / cleanup:
```

## 调试与证据保护

先用无设备 `profile-check` 和确定性测试排查。再获准读取设备元数据；确需查看层级时，使用 `snapshot --allow-device-setup --serial ... --out /private/new-file.xml` 将证据写到新的私有文件。输出路径应在已创建的私有目录下，避免重用旧截图造成错误判断。共享前人工脱敏；不要把原始 XML、截图、数据库、WAL、密钥或完整命令参数上传 Actions artifacts 或 Issue。

当测试失败在提交之后，记录为 **结果未知** 并保留原操作 ID；人工核对接收端/微信界面，不能删日志后重放。测试恢复同样必须遵守安全与授权边界。
