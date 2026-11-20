"""チェック結果の表示

check_service の結果（CheckResult）を表で表示し、CSV に出力できる。
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from core.check_service import CheckResult
from core.config_manager import ConfigManager
from core.import_export import export_csv
from gui.table_utils import FilterableTable, ReadOnlyTableModel, fit_columns


class CheckResultDialog(QDialog):
    def __init__(self, result: CheckResult, config_manager: ConfigManager, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"チェック結果 — {result.title}")
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(1200, 700)
        self.setSizeGripEnabled(True)
        self._result = result
        self._config_manager = config_manager

        lay = QVBoxLayout(self)
        title = QLabel(result.title, self)
        title.setObjectName("headerLabel")
        lay.addWidget(title)
        summary = QLabel(result.summary, self)
        summary.setObjectName("warningLabel" if result.rows else "successLabel")
        summary.setWordWrap(True)
        lay.addWidget(summary)

        self.model = ReadOnlyTableModel(result.columns, parent=self)
        self.model.set_rows(result.rows)
        self.table = FilterableTable(self.model, self)
        lay.addWidget(self.table, 1)
        fit_columns(self.table.view, 420)

        foot = QHBoxLayout()
        self.count_label = QLabel(f"{len(result.rows)} 行", self)
        self.count_label.setObjectName("secondaryLabel")
        foot.addWidget(self.count_label, 1)
        csv_btn = QPushButton("CSV 出力…", self)
        close_btn = QPushButton("閉じる", self)
        foot.addWidget(csv_btn)
        foot.addWidget(close_btn)
        lay.addLayout(foot)

        csv_btn.clicked.connect(self._on_csv)
        close_btn.clicked.connect(self.close)

    def _on_csv(self) -> None:
        paths = self._config_manager.config.paths
        start = paths.last_export_dir or os.path.expanduser("~")
        default = os.path.join(start, f"チェック_{self._result.title}.csv")
        path, _ = QFileDialog.getSaveFileName(self, "CSV 出力", default, "CSV ファイル (*.csv)")
        if not path:
            return
        try:
            export_csv(path, [[str(v) for v in r] for r in self._result.rows], self._result.columns)
        except OSError as e:
            QMessageBox.critical(self, "CSV 出力", f"ファイルを書き出せませんでした。\n{e}")
            return
        paths.last_export_dir = os.path.dirname(path)
        QMessageBox.information(self, "CSV 出力", f"書き出しました。\n{path}")
