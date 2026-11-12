"""Use the bundled Qt Chinese translations for standard native dialogs too."""

from PySide6.QtCore import QLibraryInfo, QTranslator


def install_chinese(app):
    if hasattr(app, "_chinese_translator"):
        return
    translator = QTranslator(app)
    if translator.load(
        "qtbase_zh_CN", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    ):
        app.installTranslator(translator)
        app._chinese_translator = translator
