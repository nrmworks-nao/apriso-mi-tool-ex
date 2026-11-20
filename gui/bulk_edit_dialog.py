"""一括編集ダイアログ

選択した行に対して、まとめて次の編集を行う。
  - エイリアスの置換（フルパスに「検索 → 置換」を適用し、一致する MI_POINT を探して
    PointID を差し替える。一致しない行は「未解決」として適用対象から外す）
  - ローカル名の置換、接頭辞／接尾辞の付与
  - トリガーの実行を一括で ON / OFF

適用前に、行ごとの「変更前 → 変更後」を表で確認できる。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QRadioButton,
    QVBoxLayout,
)

from core.models import MIConfigSnapshot, on_off
from core.point_service import BulkEditItem, BulkEditSpec, EditRow, ReplaceRule, plan_bulk_edit
from gui.styles import COLORS, refresh_style
from gui.table_utils import FilterableTable, ReadOnlyTableModel

_COLUMNS = ["グループ名", "ローカル名（変更前）", "ローカル名（変更後）", "エイリアス（変更前）",
            "エイリアス（変更後）", "トリガー（変更前）", "トリガー（変更後）", "状態"]


class BulkEditDialog(QDialog):
    def __init__(self, snapshot: MIConfigSnapshot, rows: list[EditRow], parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"一括編集（{len(rows)} 行）")
        self.resize(1200, 720)
        self.setSizeGripEnabled(True)
        self._snapshot = snapshot
        self._rows = rows
        self._items: list[BulkEditItem] = []

        lay = QVBoxLayout(self)

        # エイリアス
        alias_box = QGroupBox("エイリアスの置換（フルパスに適用し、一致するポイントに差し替え）", self)
        ag = QGridLayout(alias_box)
        self.alias_find = QLineEdit(alias_box)
        self.alias_find.setPlaceholderText("例: HJM1_OPC_019")
        self.alias_repl = QLineEdit(alias_box)
        self.alias_repl.setPlaceholderText("例: HJM1_OPC_020")
        self.alias_regex = QCheckBox("正規表現", alias_box)
        ag.addWidget(QLabel("検索:", alias_box), 0, 0)
        ag.addWidget(self.alias_find, 0, 1)
        ag.addWidget(QLabel("置換:", alias_box), 0, 2)
        ag.addWidget(self.alias_repl, 0, 3)
        ag.addWidget(self.alias_regex, 0, 4)
        lay.addWidget(alias_box)

        # ローカル名
        ln_box = QGroupBox("ローカル名", self)
        lg = QGridLayout(ln_box)
        self.ln_find = QLineEdit(ln_box)
        self.ln_repl = QLineEdit(ln_box)
        self.ln_regex = QCheckBox("正規表現", ln_box)
        self.ln_prefix = QLineEdit(ln_box)
        self.ln_suffix = QLineEdit(ln_box)
        lg.addWidget(QLabel("検索:", ln_box), 0, 0)
        lg.addWidget(self.ln_find, 0, 1)
        lg.addWidget(QLabel("置換:", ln_box), 0, 2)
        lg.addWidget(self.ln_repl, 0, 3)
        lg.addWidget(self.ln_regex, 0, 4)
        lg.addWidget(QLabel("接頭辞:", ln_box), 1, 0)
        lg.addWidget(self.ln_prefix, 1, 1)
        lg.addWidget(QLabel("接尾辞:", ln_box), 1, 2)
        lg.addWidget(self.ln_suffix, 1, 3)
        lay.addWidget(ln_box)

        # トリガー
        trig_box = QGroupBox("トリガーの実行", self)
        tl = QHBoxLayout(trig_box)
        self.trig_keep = QRadioButton("変更しない", trig_box)
        self.trig_on = QRadioButton("すべて ON", trig_box)
        self.trig_off = QRadioButton("すべて OFF", trig_box)
        self.trig_keep.setChecked(True)
        self._trig_group = QButtonGroup(self)
        for rb in (self.trig_keep, self.trig_on, self.trig_off):
            self._trig_group.addButton(rb)
            tl.addWidget(rb)
        tl.addStretch(1)
        lay.addWidget(trig_box)

        # プレビュー
        self.model = ReadOnlyTableModel(_COLUMNS, parent=self)
        self.table = FilterableTable(self.model, self)
        lay.addWidget(self.table, 1)
        self.summary_label = QLabel("", self)
        lay.addWidget(self.summary_label)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        self.ok_btn = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_btn.setText("適用")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("キャンセル")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._refresh)
        for e in (self.alias_find, self.alias_repl, self.ln_find, self.ln_repl,
                  self.ln_prefix, self.ln_suffix):
            e.textChanged.connect(lambda *_: self._timer.start())
        for cb in (self.alias_regex, self.ln_regex, self.trig_keep, self.trig_on, self.trig_off):
            cb.toggled.connect(lambda *_: self._timer.start())

        self._refresh()
        self.table.view.horizontalHeader().resizeSection(3, 320)
        self.table.view.horizontalHeader().resizeSection(4, 320)

    def _spec(self) -> BulkEditSpec:
        trig = None
        if self.trig_on.isChecked():
            trig = True
        elif self.trig_off.isChecked():
            trig = False
        return BulkEditSpec(
            alias_rule=ReplaceRule(self.alias_find.text(), self.alias_repl.text(),
                                   self.alias_regex.isChecked()),
            local_name_rule=ReplaceRule(self.ln_find.text(), self.ln_repl.text(),
                                        self.ln_regex.isChecked()),
            prefix=self.ln_prefix.text(),
            suffix=self.ln_suffix.text(),
            triggers=trig,
        )

    def _refresh(self) -> None:
        try:
            self._items = plan_bulk_edit(self._snapshot, self._rows, self._spec())
        except ValueError as e:
            self._items = []
            self.model.set_rows([])
            self.summary_label.setObjectName("errorLabel")
            self.summary_label.setText(str(e))
            refresh_style(self.summary_label)  # objectName の変更を QSS に反映させる
            self.ok_btn.setEnabled(False)
            return

        rows = []
        bg = {}
        fg = {}
        c_err = QColor(COLORS["deleted_row"])
        c_chg = QColor(COLORS["changed_cell"])
        c_sec = QColor(COLORS["text_secondary"])
        n_ok = n_err = n_same = 0
        for i, it in enumerate(self._items):
            if not it.ok:
                state = it.error
                bg[i] = c_err
                n_err += 1
            elif it.changed:
                state = "変更"
                bg[i] = c_chg
                n_ok += 1
            else:
                state = "変更なし"
                fg[i] = c_sec
                n_same += 1
            rows.append([
                it.group_name,
                it.before_local_name,
                it.after_local_name,
                it.before_alias,
                it.after_alias,
                on_off(it.before_triggers),
                on_off(it.after_triggers),
                state,
            ])
        self.model.set_rows(rows, foreground=fg, background=bg)
        self.summary_label.setObjectName("warningLabel" if n_err else "secondaryLabel")
        refresh_style(self.summary_label)
        self.summary_label.setText(
            f"変更 {n_ok} 行 / 変更なし {n_same} 行 / 対象外（未解決・エラー） {n_err} 行"
            + ("　※ 対象外の行は適用されません" if n_err else "")
        )
        self.ok_btn.setEnabled(n_ok > 0)

    def applicable_items(self) -> list[BulkEditItem]:
        """適用する（エラーがなく変更のある）項目。"""
        return [it for it in self._items if it.ok and it.changed]
