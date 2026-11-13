# StitchPress 协作规则

- 使用中文沟通；Python依赖和运行使用uv；若引入前端，使用pnpm。
- 任务和完成证据统一维护在TODO.md。未通过平台、真实模型或人工业务验收的任务不能勾选完成。
- 核心在`src/stitchpress/core`，不导入Qt；UI与协调器在`app`，系统差异在`platform`。长任务使用独立进程，工作进程不能直接写作品包。
- 运行不依赖其他项目或开发者绝对路径。用户明确授权的内置彩色符号及ICC随新作品自包含保存；其他客户符号、色库、ICC、原图通过明确的本地导入进入自包含作品。
- `.local/`、`reports/local/`、`dist/`为本机资源、验收与构建留底，均不提交Git。安装资源仅使用自有或用户明确授权内置的内容；2026-11-13用户已要求将客户152彩色符号及配套ICC作为默认内置库，资源来源记录在`src/stitchpress/resources/symbols/SOURCE.md`。不得据此加入其他客户数据或参考软件资源。
- 密钥只存系统钥匙串/凭据管理器；不进作品、配置JSON、日志、测试快照或Git。真实付费模型调用由用户配置后在软件中明确发起，不继承聊天工具的凭据。
- 单一母版、唯一符号映射和共享LayoutPlan控制所有产物。严格区分空白与白色有针；局部修改必须保留旧修订和区域外语义。
- 保留旧版本；保存/发布应原子执行。数字、外观、交付、试印和洗水分别记录。
- 代码变更检查：`uv run ruff check src tests tools`、`uv run ruff format --check src tests tools`、`QT_QPA_PLATFORM=offscreen uv run pytest -q`。正在构建时所有uv命令带`--all-groups`，避免移除构建进程正在使用的依赖。
- Mac通过不代表Windows通过；模拟响应不算真实AI通过；构建成功不代表已验证干净安装、原生操作或工厂生产。
- 新分支使用`codex/`前缀。用户指定origin为`git@wozaigithub:yout0703/stitchpress.git`，禁止强推或覆盖其他工作。
- 除非用户明确授权，不使用多agent。
