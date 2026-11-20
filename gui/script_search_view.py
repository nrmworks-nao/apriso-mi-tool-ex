"""スクリプト横断検索

全スクリプトの <Source> を対象に、文字列または正規表現で検索する。
結果は表（グループ名、アクション名、行番号、該当行のテキスト）で表示し、
行をダブルクリックするとそのスクリプトを開いて該当行へ移動する。
グループ名のフィルタ（部分一致、例: WorkEnd）と併用できる。

検索対象はメモリ上のスナップショットのため、DB にはアクセスしない。
"""

from __future__ import annotations

import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.models import MIConfigSnapshot
from gui.styles import refresh_style
from gui.table_utils import FilterableTable, ReadOnlyTableModel

_COLUMNS = ["グループ名", "アクション名", "行", "テキスト"]
MAX_RESULTS = 20000


class ScriptSearchView(QWidget):
    """スクリプト横断検索（独立ウィンドウ）。"""

    open_script_requested = Signal(int, int)  # action_id, 行番号（1 始まり）

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("スクリプト横断検索")
        self.resize(1200, 700)
        self._snapshot: MIConfigSnapshot | None = None

        lay = QVBoxLayout(self)
        form = QGridLayout()
        self.query_edit = QLineEdit(self)
        self.query_edit.setPlaceholderText("検索する文字列（Enter で検索）")
        self.query_edit.setClearButtonEnabled(True)
        self.group_edit = QLineEdit(self)
        self.group_edit.setPlaceholderText("グループ名の部分一致（例: WorkEnd）。空欄はすべて")
        self.group_edit.setClearButtonEnabled(True)
        self.case_cb = QCheckBox("大文字小文字を区別", self)
        self.regex_cb = QCheckBox("正規表現", self)
        self.search_btn = QPushButton("検索", self)
        form.addWidget(QLabel("検索:", self), 0, 0)
        form.addWidget(self.query_edit, 0, 1)
        form.addWidget(self.case_cb, 0, 2)
        form.addWidget(self.regex_cb, 0, 3)
        form.addWidget(self.search_btn, 0, 4)
        form.addWidget(QLabel("グループ名:", self), 1, 0)
        form.addWidget(self.group_edit, 1, 1)
        form.setColumnStretch(1, 1)
        lay.addLayout(form)

        self.model = ReadOnlyTableModel(_COLUMNS, parent=self)
        self.table = FilterableTable(self.model, self)
        self.table.view.setSelectionBehavior(self.table.view.SelectionBehavior.SelectRows)
        header = self.table.view.horizontalHeader()
        header.resizeSection(0, 280)
        header.resizeSection(1, 180)
        header.resizeSection(2, 60)
        lay.addWidget(self.table, 1)

        self.status_label = QLabel("", self)
        self.status_label.setObjectName("secondaryLabel")
        lay.addWidget(self.status_label)

        self.query_edit.returnPressed.connect(self.search)
        self.group_edit.returnPressed.connect(self.search)
        self.search_btn.clicked.connect(self.search)
        self.table.view.doubleClicked.connect(self._on_double_clicked)

    def set_snapshot(self, snapshot: MIConfigSnapshot | None) -> None:
        self._snapshot = snapshot
        self.model.set_rows([])
        self._set_status("" if snapshot else "データが読み込まれていません。")

    def focus_query(self) -> None:
        self.query_edit.setFocus()
        self.query_edit.selectAll()

    def _set_status(self, text: str, kind: str = "secondaryLabel") -> None:
        self.status_label.setObjectName(kind)
        self.status_label.setText(text)
        refresh_style(self.status_label)

    def search(self) -> None:
        snap = self._snapshot
        if snap is None:
            self._set_status("データが読み込まれていません。", "errorLabel")
            return
        query = self.query_edit.text()
        if not query:
            self._set_status("検索する文字列を入力してください。", "errorLabel")
            return
        flags = 0 if self.case_cb.isChecked() else re.IGNORECASE
        try:
            pattern = re.compile(query if self.regex_cb.isChecked() else re.escape(query), flags)
        except re.error as e:
            self._set_status(f"正規表現が不正です: {e}", "errorLabel")
            return
        group_filter = self.group_edit.text().strip().lower()

        rows, payload = [], []
        scripts = 0
        truncated = False
        for g, a, s in snap.script_actions():
            if group_filter and group_filter not in g.name.lower():
                continue
            scripts += 1
            for line_no, line in enumerate(s.source.split("\n"), start=1):
                if pattern.search(line):
                    rows.append([g.name, a.name, line_no, line.strip()])
                    payload.append((a.id, line_no))
                    if len(rows) >= MAX_RESULTS:
                        truncated = True
                        break
            if truncated:
                break
        self.model.set_rows(rows, payload=payload)
        hits_scripts = len({p[0] for p in payload})
        text = f"{scripts} 件のスクリプトを検索し、{hits_scripts} 件のスクリプトで {len(rows)} 行が一致しました。"
        if truncated:
            text += f"（{MAX_RESULTS} 行で打ち切りました）"
        self._set_status(text, "warningLabel" if truncated else "secondaryLabel")

    def _on_double_clicked(self, proxy_index) -> None:
        src_row = self.table.proxy.mapToSource(proxy_index).row()
        p = self.model.payload(src_row)
        if p is not None:
            self.open_script_requested.emit(p[0], p[1])
