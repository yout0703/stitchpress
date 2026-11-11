# 本机开发版的第三方资源与依赖

安装依赖的准确版本记录在`uv.lock`，后续发行前须检查并随包提供适用许可和通知。

## 字体

- 随源码提供`NotoSans-Regular.ttf`，取自[Noto Fonts官方仓库](https://github.com/notofonts/noto-fonts/tree/main/hinted/ttf/NotoSans)，许可为SIL Open Font License；完整文本见`src/stitchpress/resources/fonts/OFL.txt`。
- 当前通用输出只绑定这一字体；它不包含全部中文字符，缺字必须提示，不能默默印成方框。CJK输出字体导入和版式仍待补齐。

## 程序库

PySide6/Qt、Pillow/LittleCMS/libtiff、NumPy、pypdf、openpyxl、defusedxml、keyring均为运行依赖；PyInstaller为当前Mac构建工具、Nuitka为保留试验工具，pytest/pytest-qt/ruff/PDFium为开发检查工具。源码中不附带参考EXE的程序或美术资源。

2026-11-11新增QtAwesome/QtPy，用于高DPI图标；界面使用其Material Design Icons 6线性图标。QtAwesome/QtPy的许可及随库图标字体通知需随安装资源保留；构建脚本显式收集QtAwesome数据。界面字体使用操作系统已安装的PingFang SC / Segoe UI及中文回退，不复制系统字体。

Qt 6.12部署配置指定的Nuitka 4.1.1已做试验，但未通过中文安装目录迁移验证；Mac当前改用PyInstaller 6.22.3。其他平台仍需各自构建和验收。仓库中的构建脚本不构成商用发行许可检查完成的声明。

## 客户资源

66符号、455线色及厂家ICC仅由本机明确导入，不进入通用安装包、公开夹具或Git。原件SHA和原ID保留；历史客户预设仅作为当前合作配置，不提升为行业通用规则。
