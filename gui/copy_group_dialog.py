"""他グループから複製ダイアログ

複製元のアクショングループを選び、そのポイント構成を現在のグループへ取り込む。
  - 置換ルールを複数指定できる（例: HJM1_OPC_019 → HJM1_OPC_020）。置換はエイリアスの
    フルパスに適用し、一致する MI_POINT を探す。
  - 取り込み方法: 「追加のみ（既存行は残す）」／「置き換え（既存行を削除して入れ替え）」
  - ローカル名と TriggersExecution は複製元の値を引き継ぐ。
  - 未解決の行、および一意制約 (ActionGroupID, PointID) に抵触する行は一覧で示し、
    取り込み対象から外す。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.models import MIConfigSnapshot, on_off
from core.point_service import EditRow, NewRowCandidate, ReplaceRule, plan_copy_from_group
from gui.styles import COLORS, refresh_style
from gui.table_utils import FilterableTable, ReadOnlyTableModel

_COLUMNS = ["複製元エイリアス", "取り込むエイリアス", "ローカル名", "トリガーの実行", "状態"]


class CopyGroupDialog(QDialog):
    def __init__(self, snapshot: MIConfigSnapshot, target_group_id: int,
                 current_rows: list[EditRow], parent=None):
        super().__init__(parent)
        self._snapshot = snapshot
        self._target_id = target_group_id
        self._current_rows = current_rows
        self._candidates: list[NewRowCandidate] = []
        target = snapshot.action_groups[target_group_id]
        self.setWindowTitle(f"他グループから複製 → {target.name}")
        self.resize(1200, 780)
        self.setSizeGripEnabled(True)

        lay = QVBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Vertical, self)
        lay.addWidget(splitter, 1)

        # --- 上段: 複製元・置換ルール・取り込み方法 ---
        top = QWidget(splitter)
        top_lay = QHBoxLayout(top)
        top_lay.setContentsMargins(0, 0, 0, 0)

        src_box = QGroupBox("複製元のアクショングループ", top)
        src_lay = QVBoxLayout(src_box)
        self.src_filter = QLineEdit(src_box)
        self.src_filter.setPlaceholderText("グループ名で絞り込み（スペース区切りで AND）")
        self.src_filter.setClearButtonEnabled(True)
        src_lay.addWidget(self.src_filter)
        self.src_list = QListWidget(src_box)
        for g in snapshot.groups_sorted():
            if g.id == target_group_id:
                continue
            n = len(snapshot.active_group_points(g.id))
            item = QListWidgetItem(f"{g.name}  （{n} 行）")
            item.setData(Qt.ItemDataRole.UserRole, g.id)
            item.setData(Qt.ItemDataRole.UserRole + 1, g.name.lower())
            self.src_list.addItem(item)
        src_lay.addWidget(self.src_list, 1)
        top_lay.addWidget(src_box, 3)

        right = QVBoxLayout()
        rule_box = QGroupBox("置換ルール（エイリアスのフルパスに上から順に適用）", top)
        rule_lay = QVBoxLayout(rule_box)
        self.rules = QTableWidget(3, 2, rule_box)
        self.rules.setHorizontalHeaderLabels(["検索", "置換"])
        self.rules.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.rules.verticalHeader().setVisible(False)
        self.rules.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        rule_lay.addWidget(self.rules, 1)
        rb_lay = QHBoxLayout()
        self.add_rule_btn = QPushButton("行を追加", rule_box)
        self.del_rule_btn = QPushButton("行を削除", rule_box)
        self.rule_regex = QCheckBox("正規表現", rule_box)
        rb_lay.addWidget(self.add_rule_btn)
        rb_lay.addWidget(self.del_rule_btn)
        rb_lay.addStretch(1)
        rb_lay.addWidget(self.rule_regex)
        rule_lay.addLayout(rb_lay)
        right.addWidget(rule_box, 1)

        mode_box = QGroupBox("取り込み方法", top)
        mode_lay = QVBoxLayout(mode_box)
        self.mode_append = QRadioButton("追加のみ（既存行は残す）", mode_box)
        self.mode_replace = QRadioButton("置き換え（既存行を削除して入れ替え）", mode_box)
        self.mode_append.setChecked(True)
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self.mode_append)
        self._mode_group.addButton(self.mode_replace)
        mode_lay.addWidget(self.mode_append)
        mode_lay.addWidget(self.mode_replace)
        right.addWidget(mode_box)
        top_lay.addLayout(right, 4)
        splitter.addWidget(top)

        # --- 下段: プレビュー ---
        bottom = QWidget(splitter)
        bottom_lay = QVBoxLayout(bottom)
        bottom_lay.setContentsMargins(0, 0, 0, 0)
        bottom_lay.addWidget(QLabel("取り込み内容の確認", bottom))
        self.model = ReadOnlyTableModel(_COLUMNS, parent=self)
        self.table = FilterableTable(self.model, bottom)
        bottom_lay.addWidget(self.table, 1)
        self.summary_label = QLabel("複製元のグループを選択してください。", bottom)
        self.summary_label.setObjectName("secondaryLabel")
        bottom_lay.addWidget(self.summary_label)
        splitter.addWidget(bottom)
        splitter.setSizes([300, 420])

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        self.ok_btn = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_btn.setText("取り込み")
        self.ok_btn.setEnabled(False)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("キャンセル")
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._refresh)

        self.src_filter.textChanged.connect(self._apply_src_filter)
        self.src_list.currentItemChanged.connect(lambda *a: self._timer.start())
        self.rules.itemChanged.connect(lambda *a: self._timer.start())
        self.rule_regex.toggled.connect(lambda *a: self._timer.start())
        self.mode_append.toggled.connect(lambda *a: self._timer.start())
        self.add_rule_btn.clicked.connect(self._add_rule_row)
        self.del_rule_btn.clicked.connect(self._delete_rule_rows)

        self.table.view.horizontalHeader().resizeSection(0, 380)
        self.table.view.horizontalHeader().resizeSection(1, 380)

    # ------------------------------------------------------------------
    def _apply_src_filter(self, text: str) -> None:
        tokens = [t.lower() for t in text.split() if t]
        for i in range(self.src_list.count()):
            item = self.src_list.item(i)
            name = item.data(Qt.ItemDataRole.UserRole + 1) or ""
            item.setHidden(not all(t in name for t in tokens))

    def _add_rule_row(self) -> None:
        self.rules.insertRow(self.rules.rowCount())

    def _delete_rule_rows(self) -> None:
        rows = sorted({i.row() for i in self.rules.selectedIndexes()}, reverse=True)
        for r in rows:
            self.rules.removeRow(r)
        self._timer.start()

    def _rules(self) -> list[ReplaceRule]:
        out = []
        regex = self.rule_regex.isChecked()
        for r in range(self.rules.rowCount()):
            f = self.rules.item(r, 0)
            t = self.rules.item(r, 1)
            find = f.text() if f else ""
            repl = t.text() if t else ""
            if find:
                out.append(ReplaceRule(find, repl, regex))
        return out

    def _source_group_id(self) -> int | None:
        item = self.src_list.currentItem()
        if item is None or item.isHidden():
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def replace_existing(self) -> bool:
        return self.mode_replace.isChecked()

    def _set_summary(self, text: str, kind: str = "secondaryLabel") -> None:
        self.summary_label.setObjectName(kind)
        self.summary_label.setText(text)
        refresh_style(self.summary_label)

    def _refresh(self) -> None:
        src_id = self._source_group_id()
        if src_id is None:
            self._candidates = []
            self.model.set_rows([])
            self._set_summary("複製元のグループを選択してください。")
            self.ok_btn.setEnabled(False)
            return
        try:
            self._candidates = plan_copy_from_group(
                self._snapshot, src_id, self._target_id, self._rules(),
                self.replace_existing(), self._current_rows,
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
            rows.append([c.label, c.alias, c.local_name or "", on_off(c.triggers_execution),
                         "取り込み" if c.ok else c.error])
            bg[i] = c_ok if c.ok else c_err
            n_ok += 1 if c.ok else 0
        self.model.set_rows(rows, background=bg)
        n_err = len(self._candidates) - n_ok
        existing = sum(1 for r in self._current_rows if not r.deleted)
        text = f"取り込み {n_ok} 行 / 対象外 {n_err} 行"
        if self.replace_existing():
            text += f" / 既存の {existing} 行は削除予定になります"
        if n_err:
            text += "　※ 対象外の行（未解決・一意制約に抵触）は取り込まれません"
        self._set_summary(text, "warningLabel" if n_err else "secondaryLabel")
        self.ok_btn.setEnabled(n_ok > 0)

    def _accept(self) -> None:
        self._refresh()
        if not self.applicable_candidates():
            return
        self.accept()

    def applicable_candidates(self) -> list[NewRowCandidate]:
        return [c for c in self._candidates if c.ok]
