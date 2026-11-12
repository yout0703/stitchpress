# Windows 自动构建与验证

2026-11-12，StitchPress 0.1.0.dev9。用户授权先跑通 GitHub Windows 自动构建，云桌面和实际交互验收后续准备。本记录更新此前 docs/15、docs/16 中「尚未执行 Windows 构建」的历史状态；一期总验收仍以 TODO.md 为准。

## 自动执行规则

工作流为 `.github/workflows/windows-manual.yml`，Actions 中显示 **Windows x64 build and verification**。保留原文件名以免已有入口失效。

- `main` 或 `codex/initial-implementation` 分支修改代码、测试、夹具、构建工具、依赖或本工作流并推送时自动执行。
- Pull Request 涉及上述文件时执行；仅修改文档不会重复构建。
- Actions 页仍可用 **Run workflow** 手动触发。同一分支或 PR 的新任务取消旧任务，避免重复消耗。
- 运行环境：GitHub 托管 `windows-2022` x64、Python 3.12、`uv.lock` 锁定依赖；Qt 自动检查使用 offscreen。
- 总超时 45 分钟，测试步骤超时 25 分钟；逐项显示测试名，保存 JUnit XML 和耗时，单项超过 180 秒输出调用栈，便于定位阻塞。

依次执行：锁定依赖安装 → Ruff 检查与格式检查 → 全量 pytest → Windows 本机 PyInstaller 打包 → 将包复制到中文目录 → 用真实 `StitchPress.exe` 验证 CLI、后台工作进程、修订与扩展、参考全套输出 → 压缩及上传测试包。

CI 只使用自有夹具和允许再分发的资源，不上传客户符号库、图纸、原图或密钥，也不调用付费模型。已有二期代码仅随既有回归检查，本次没有扩张功能范围。

## 本次发现与修复

首次运行 [43157743551](https://github.com/yout0703/stitchpress/actions/runs/43157743551) 完成了 188 项测试（1 项按 Windows 平台跳过）和 PyInstaller 打包。打包后的程序也生成了基础导出文件，但校验脚本打印含中文路径的报告时触发 `UnicodeEncodeError: charmap`，默认 CP1252 标准输出无法表示中文，导致流水线失败。

在整个 Windows job 设置 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`，统一构建工具链的默认文本和标准输出编码。修复没有跳过校验或把失败改为忽略。

另将原来仅手动执行的工作流接入 push/PR，增加并发取消、日志留存、测试超时和缺失应用产物时报错。新推送自动触发与旧任务取消已实际验证。

修复后的运行 [43158861729](https://github.com/yout0703/stitchpress/actions/runs/43158861729) 因新增的 15 分钟测试上限中止：日志显示测试持续推进到 76%，没有失败或单项 180 秒的卡点；首次全套约 7 分半，说明托管 Runner 存在明显耗时波动。将测试上限调整为 25 分钟、总上限 45 分钟，并改为逐项输出测试名称；保留全部用例及超时保护。

随后运行 [43160804927](https://github.com/yout0703/stitchpress/actions/runs/43160804927) 全量测试通过（188 passed、1 skipped，15 分 38 秒），构建、正常原生工作进程、约一亿像素 TIFF 反读、预览和参考全套导出均继续执行。取消任务时回执已写为 `cancelled`、暂存输出已清理，但进程没有退出，180 秒超时。

本地用 CP1252 重定向管道精确复现：`worker.run()` 写完失败/取消回执后，打印中文错误又触发 `UnicodeEncodeError`。冻结程序不保证继承父进程的 `PYTHONUTF8` 配置，不能只修 CI 环境。PyInstaller Windows 无控制台引导器会对未处理异常弹出错误窗口，后台进程因此可能等待交互；正常 `SystemExit` 则另作处理，不能笼统归因于非零退出码。[PyInstaller 6.22.3 引导器源码](https://github.com/pyinstaller/pyinstaller/blob/v6.22.3/bootloader/src/pyi_launch.c#L324-L367)

dev9 在程序入口显式将存在的标准输出/错误管道设置为 UTF-8，并继续为缺失句柄提供空设备；不改变失败状态、退出码或清理逻辑。新增失败与真实参考导出取消的 CP1252 回归，两项修复前均失败、修复后通过。打包后校验也增加未知操作失败路径，继续断言 `failed/cancelled` 回执及退出码 2，不能把出错任务伪装为成功。

Mac 本地 dev9 全量回归 191 项通过（201.43 秒），Ruff 检查及 112 文件格式检查通过。证据：`reports/local/windows-ci-20261112/mac-tests-dev9.xml` 和同名 `.log`。

## 最终通过记录

- 运行：[43163616669](https://github.com/yout0703/stitchpress/actions/runs/43163616669)，结论 **success**；2026-11-12 14:05（北京时间）完成。
- 验证源码提交：`c9113384d418ec4d7e0e1f56af357573675a5350`。此后只补文档及TODO，不改变该包的源代码。
- Windows全量：**190 passed，1 skipped，0 failed**，929.28秒；跳过项为Windows创建符号链接需要单独权限验证的写入锁别名用例，未计为通过。
- Ruff及112文件格式检查通过，Windows本机PyInstaller构建通过。
- 中文安装目录下的真实EXE报告：Windows / AMD64 / Python 3.12.10 / Qt 6.12.0，`compiled=true`，后台使用同一EXE；基本导出7文件、本地转换及修订检查通过。
- 大图扩展：9834×10261像素生产TIFF按条带写出并反读，像素哈希核验通过；自定义阅读PDF/材料及12张商品图通过。
- 一期参考：全套92文件校验通过，18预览、17图库，设置重新载入及源作品未变通过。
- 负路径：取消返回`cancelled`、未知操作返回`failed`，均退出码2；取消不发布目录，不再等待180秒超时。
- 被运行的EXE SHA256：`d3ae57bdab36b45b6495125d1168bbff3234667d2b746c06918a7a3bd2d1159e`。

应用：[windows-app-43163616669](https://github.com/yout0703/stitchpress/actions/runs/43163616669/artifacts/13920325367)。检查：[windows-checks-43163616669](https://github.com/yout0703/stitchpress/actions/runs/43163616669/artifacts/13919049908)。运行与Artifact元数据、日志、JUnit及原生回执留底于`reports/local/windows-ci-20261112/`。

下载后独立核验通过：ZIP CRC完整，EXE为AMD64 Windows GUI格式，EXE哈希与以上三个原生校验回执一致，Python/Qt运行库、字体、品牌图标和中文帮助齐全。Windows生成的92文件在Mac再次通过哈希核验，便携设置重新打开成功。

本地程序ZIP：`dist/windows/ci-43163616669/StitchPress-windows-x64-local.zip`，85,926,064字节；SHA256为`312e1ba8ac4d4913fc364ce33e7eb01cdf18f20c4a99a4f519bed379503e9ba5`。独立核验脚本和回执为`reports/local/windows-ci-20261112/verify_download.py`、`download-verification.json`；交付副本另存于用户主项目的`outputs/2026-11-12/软件构建-StitchPressWindows自动构建/`，保留`README.md`、`handoff.json`及关键证据，不依赖GitHub的30天留存期。

## 构建产物与使用

成功运行包含两个 Artifact，保留 30 天：

- `windows-app-<run_id>`：含 `StitchPress-windows-x64-local.zip`。
- `windows-checks-<run_id>`：测试 XML、构建日志以及打包后真实执行的检查回执和输出。

在 GitHub 仓库 **Actions → 成功运行 → Artifacts** 下载。应用 Artifact 外层归档中还有程序 ZIP；将程序 ZIP 完整解压，运行 `StitchPress/StitchPress.exe`，不能只拷贝 EXE，必须保留 `_internal` 及所有资源。此包是未签名的便携测试版，不是安装向导或正式发布版。

维护者也可以执行：

```bash
gh workflow run windows-manual.yml --repo yout0703/stitchpress --ref codex/initial-implementation
gh run watch <run_id> --repo yout0703/stitchpress --exit-status
gh run download <run_id> --repo yout0703/stitchpress --name windows-app-<run_id> --dir dist/windows
```

手动执行和下载行为依据 GitHub 官方说明：[手动执行工作流](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)、[下载构建产物](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/download-workflow-artifacts)。

## 验收边界

托管 Runner 是 Windows Server 环境，能够验证 x64 原生依赖、真实 EXE 与子进程、中文路径和输出反读。它不等同于客户 Windows 10/11 桌面。Runner 本身安装了 Python；打包程序使用自带运行时，不代表已经完成无 Python 干净机器验收。

后续云桌面需要继续验证：正常启动与文件关联、100%/125%/150% 缩放、日夜主题及中文字体、鼠标键盘完整业务流程、文件占用与保存恢复、PDF/TIFF/Excel 目标软件、无 Python 环境。签名、安装向导、最低系统版本、打印及实物验收另列。

旧校验脚本中的 `windows: pending` 保留完整 Windows 验收待完成的含义；CI 是否通过以本次运行结论、`doctor.os/arch/compiled`、原生任务和逐文件回执判断，不能用这个静态字段代替平台执行证据。
