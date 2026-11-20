"""Apriso MI設定ツール (AprisoMIToolEx) エントリーポイント

QApplication を生成し、get_app_stylesheet() を適用、設定を読み込み、MainWindow を表示する。
接続設定が未入力ならば、MainWindow.start() が接続設定ダイアログを先に開く。

ログ: log\\AprisoMIToolEx_YYYYMM.log（操作ログと例外を記録する）

PyInstaller での exe 化:
    build.bat を実行する（設定は AprisoMIToolEx.spec に定義）
"""

from __future__ import annotations

import logging
import sys
import traceback

from PySide6.QtWidgets import QApplication, QMessageBox

from core import APP_NAME, APP_TITLE
from core.change_executor import append_log_line
from core.config_manager import ConfigManager


class MonthlyFileHandler(logging.Handler):
    """log\\AprisoMIToolEx_YYYYMM.log へ追記するハンドラー（月が変われば別ファイル）。"""

    def __init__(self, log_dir: str):
        super().__init__(level=logging.INFO)
        self.log_dir = log_dir

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = f"[{record.levelname}] {record.name}: {record.getMessage()}"
            if record.exc_info:
                text += "\n" + "".join(traceback.format_exception(*record.exc_info)).rstrip()
            append_log_line(self.log_dir, text)
        except Exception:
            self.handleError(record)


def _setup_logging(log_dir: str) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(MonthlyFileHandler(log_dir))
    if not getattr(sys, "frozen", False):
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        root.addHandler(console)


def _install_excepthook() -> None:
    """未処理の例外をログに記録し、利用者にはメッセージボックスで要点を示す。"""

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logging.getLogger(APP_NAME).error("未処理の例外", exc_info=(exc_type, exc, tb))
        try:
            box = QMessageBox()
            box.setIcon(QMessageBox.Icon.Critical)
            box.setWindowTitle(APP_TITLE)
            box.setText(f"予期しないエラーが発生しました。\n{exc}")
            box.setDetailedText("".join(traceback.format_exception(exc_type, exc, tb)))
            box.exec()
        except Exception:
            pass

    sys.excepthook = hook


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_TITLE)

    from gui.styles import get_app_stylesheet

    # DELMIA Apriso 風の外観スタイル（QSS）をアプリ全体へ適用
    app.setStyleSheet(get_app_stylesheet())

    # 設定読み込み（初回は config.json を自動生成）
    config_manager = ConfigManager()
    config_manager.load()

    _setup_logging(config_manager.log_dir)
    _install_excepthook()
    logging.getLogger(APP_NAME).info("起動しました")

    from gui.main_window import MainWindow

    window = MainWindow(config_manager)
    window.show()
    window.start()

    code = app.exec()
    logging.getLogger(APP_NAME).info("終了しました")
    return code


if __name__ == "__main__":
    sys.exit(main())
