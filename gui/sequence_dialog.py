"""連番ポイントの一括生成ダイアログ

ローカル名とエイリアスのパターンから、グループのポイント行をまとめて生成する。
  例: ローカル名 iRecipe{n:03}、
      エイリアス HN_DS02\\OPCUA_CAO_HJM1_OPC_019_W\\_PALETTE_INFO_A_RECIPE{n:03}、開始 1、終了 11

生成するのは MI_ACTION_GROUP_POINT の行のみ（MI_POINT は作成しない）。
エイリアスに一致する既存ポイントがない番号は「未解決」として対象から外す。
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
)

from core.models import MIConfigSnapshot, on_off
from core.point_service import (
    SEQUENCE_MAX_COUNT,
    EditRow,
    NewRowCandidate,
    plan_sequence,
)
from gui.styles import COLORS, refresh_style
from gui.table_utils import FilterableTable, ReadOnlyTableModel

_COLUMNS = ["番号", "ローカル名", "エイリアス", "トリガーの実行", "状態"]


class SequenceDialog(QDialog):
    def __init__(self, snapshot: MIConfigSnapshot, target_group_id: int,
                 current_rows: list[EditRow], parent=None):
        super().__init__(parent)
        self._snapshot = snapshot
        self._target_id = target_group_id
        self._current_rows = current_rows
        self._candidates: list[NewRowCandidate] = []
        target = snapshot.action_groups[target_group_id]
        self.setWindowTitle(f"連番生成 → {target.name}")
        self.resize(1100, 700)
        self.setSizeGripEnabled(True)

        lay = QVBoxLayout(self)
        box = QGroupBox("パターン", self)
        g = QGridLayout(box)
        self.ln_pattern = QLineEdit(box)
        self.ln_pattern.setPlaceholderText("例: iRecipe{n:03}")
        self.alias_pattern = QLineEdit(box)
        self.alias_pattern.setPlaceholderText(
            r"例: HN_DS02\OPCUA_CAO_HJM1_OPC_019_W\_PALETTE_INFO_A_RECIPE{n:03}"
        )
        self.start_spin = QSpinBox(box)
        self.start_spin.setRange(0, 999999)
        self.start_spin.setValue(1)
        self.end_spin = QSpinBox(box)
        self.end_spin.setRange(0, 999999)
        self.end_spin.setValue(10)
        self.trig_cb = QCheckBox("トリガーの実行を ON にする", box)
        g.addWidget(QLabel("ローカル名:", box), 0, 0)
        g.addWidget(self.ln_pattern, 0, 1, 1, 3)
        g.addWidget(QLabel("エイリアス:", box), 1, 0)
        g.addWidget(self.alias_pattern, 1, 1, 1, 3)
        g.addWidget(QLabel("開始:", box), 2, 0)
        g.addWidget(self.start_spin, 2, 1)
        g.addWidget(QLabel("終了:", box), 2, 2)
        g.addWidget(self.end_spin, 2, 3)
        g.addWidget(self.trig_cb, 3, 1, 1, 3)
        hint = QLabel(
            "{n} は番号、{n:03} は 3 桁ゼロ埋めの番号に置き換わります（Python の書式指定）。"
            f"一度に生成できるのは {SEQUENCE_MAX_COUNT} 件までです。",
            box,
        )
        hint.setObjectName("secondaryLabel")
        hint.setWordWrap(True)
        g.addWidget(hint, 4, 0, 1, 4)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(3, 1)
        lay.addWidget(box)

        self.model = ReadOnlyTableModel(_COLUMNS, parent=self)
        self.table = FilterableTable(self.model, self)
        lay.addWidget(self.table, 1)
        self.summary_label = QLabel("", self)
        self.summary_label.setObjectName("secondaryLabel")
        lay.addWidget(self.summary_label)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        self.ok_btn = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_btn.setText("生成")
        self.ok_btn.setEnabled(False)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("キャンセル")
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self._refresh)
        self.ln_pattern.textChanged.connect(lambda *_: self._timer.start())
        self.alias_pattern.textChanged.connect(lambda *_: self._timer.start())
        self.start_spin.valueChanged.connect(lambda *_: self._timer.start())
        self.end_spin.valueChanged.connect(lambda *_: self._timer.start())
        self.trig_cb.toggled.connect(lambda *_: self._timer.start())

        self.table.view.horizontalHeader().resizeSection(2, 520)
        self._refresh()

    def _set_summary(self, text: str, kind: str = "secondaryLabel") -> None:
        self.summary_label.setObjectName(kind)
        self.summary_label.setText(text)
        refresh_style(self.summary_label)

    def _refresh(self) -> None:
        if not self.alias_pattern.text().strip():
            self._candidates = []
            self.model.set_rows([])
            self._set_summary("エイリアスのパターンを入力してください。")
            self.ok_btn.setEnabled(False)
            return
        try:
            self._candidates = plan_sequence(
                self._snapshot, self._target_id,
                self.ln_pattern.text(), self.alias_pattern.text().strip(),
                self.start_spin.value(), self.end_spin.value(),
                self.trig_cb.isChecked(), self._current_rows,
            )
        except ValueError as e:
            self._candidates = []
            self.model.set_rows([])
            self._set_summary(str(e), "errorLabel")
            self.ok_btn.setEnabled(False)
            return

        rows = []
        bg = {}
        c_err = QColor(COLORS["deleted_row"])
        c_ok = QColor(COLORS["added_row"])
        n_ok = 0
        for i, c in enumerate(self._candidates):
            rows.append([c.label, c.local_name or "", c.alias, on_off(c.triggers_execution),
                         "生成" if c.ok else c.error])
            bg[i] = c_ok if c.ok else c_err
            n_ok += 1 if c.ok else 0
        self.model.set_rows(rows, background=bg)
        n_err = len(self._candidates) - n_ok
        text = f"生成 {n_ok} 行 / 対象外 {n_err} 行"
        if n_err:
            text += "　※ 未解決・一意制約に抵触する番号は生成されません"
        self._set_summary(text, "warningLabel" if n_err else "secondaryLabel")
        self.ok_btn.setEnabled(n_ok > 0)

    def _accept(self) -> None:
        self._refresh()
        if not self.applicable_candidates():
            return
        self.accept()

    def applicable_candidates(self) -> list[NewRowCandidate]:
        return [c for c in self._candidates if c.ok]
