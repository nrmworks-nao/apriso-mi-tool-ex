"""スクリプト差分比較

- DiffView: 左右 2 枚のコードビューアを並べ、difflib で求めた差分を色分け表示する。
  スクロールは左右で同期する。相手側にない行は空行（詰め物）で揃える。
- DiffDialog: 2 つのスクリプトを選んで比較する。
- SimilarGroupsDialog: 選択中のグループと処理名が同じグループ（例: すべての *_WorkEnd）を
  一覧し、各スクリプトとの一致／不一致と差分行数を表で示す。行を選ぶと左右比較を表示する。

比較時のオプション: 空白の違いを無視、設備固有の文字列（グループ名中の設備コード）を正規化。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from core.config_manager import ConfigManager
from core.models import Action, ActionGroup, MIConfigSnapshot
from gui.code_viewer import CodeViewerPanel
from gui.styles import COLORS, refresh_style
from gui.table_utils import FilterableTable, ReadOnlyTableModel

EQUIPMENT_PLACEHOLDER = "<設備>"


# ----------------------------------------------------------------------
# 差分計算
# ----------------------------------------------------------------------
def _normalize_line(line: str, ignore_whitespace: bool, equipment: str) -> str:
    if equipment:
        line = line.replace(equipment, EQUIPMENT_PLACEHOLDER)
    if ignore_whitespace:
        line = " ".join(line.split())
    return line


@dataclass
class DiffResult:
    left_lines: list[str] = field(default_factory=list)
    right_lines: list[str] = field(default_factory=list)
    left_labels: list[str] = field(default_factory=list)
    right_labels: list[str] = field(default_factory=list)
    left_kinds: list[str] = field(default_factory=list)   # equal / changed / deleted / pad
    right_kinds: list[str] = field(default_factory=list)  # equal / changed / added / pad
    diff_line_count: int = 0
    hunk_starts: list[int] = field(default_factory=list)  # 表示行番号（0 始まり）

    @property
    def identical(self) -> bool:
        return self.diff_line_count == 0


def compute_diff(left_text: str, right_text: str, ignore_whitespace: bool = False,
                 left_equipment: str = "", right_equipment: str = "") -> DiffResult:
    """行単位の差分。比較は正規化した行で行い、表示は元の行を使う。"""
    a = (left_text or "").split("\n")
    b = (right_text or "").split("\n")
    a_cmp = [_normalize_line(x, ignore_whitespace, left_equipment) for x in a]
    b_cmp = [_normalize_line(x, ignore_whitespace, right_equipment) for x in b]
    sm = difflib.SequenceMatcher(None, a_cmp, b_cmp, autojunk=False)
    res = DiffResult()

    def add_left(i: int | None, kind: str) -> None:
        res.left_lines.append(a[i] if i is not None else "")
        res.left_labels.append(str(i + 1) if i is not None else "")
        res.left_kinds.append(kind if i is not None else "pad")

    def add_right(j: int | None, kind: str) -> None:
        res.right_lines.append(b[j] if j is not None else "")
        res.right_labels.append(str(j + 1) if j is not None else "")
        res.right_kinds.append(kind if j is not None else "pad")

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                add_left(i1 + k, "equal")
                add_right(j1 + k, "equal")
            continue
        res.hunk_starts.append(len(res.left_lines))
        n_left = i2 - i1
        n_right = j2 - j1
        m = max(n_left, n_right)
        res.diff_line_count += m
        left_kind = "changed" if tag == "replace" else "deleted"
        right_kind = "changed" if tag == "replace" else "added"
        for k in range(m):
            add_left(i1 + k if k < n_left else None, left_kind)
            add_right(j1 + k if k < n_right else None, right_kind)
    return res


def group_equipment(group: ActionGroup | None) -> str:
    return group.equipment if group is not None else ""


# ----------------------------------------------------------------------
# 左右比較ビュー
# ----------------------------------------------------------------------
class DiffView(QWidget):
    def __init__(self, config_manager: ConfigManager, parent=None):
        super().__init__(parent)
        vcfg = config_manager.config.viewer
        self._result: DiffResult | None = None
        self._syncing = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        head = QHBoxLayout()
        self.left_title = QLabel("", self)
        self.left_title.setObjectName("headerLabel")
        self.right_title = QLabel("", self)
        self.right_title.setObjectName("headerLabel")
        head.addWidget(self.left_title, 1)
        head.addWidget(self.right_title, 1)
        lay.addLayout(head)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.left = CodeViewerPanel(self.splitter, font_family=vcfg.font_family,
                                    font_size=vcfg.font_size, word_wrap=False)
        self.right = CodeViewerPanel(self.splitter, font_family=vcfg.font_family,
                                     font_size=vcfg.font_size, word_wrap=False)
        self.splitter.addWidget(self.left)
        self.splitter.addWidget(self.right)
        lay.addWidget(self.splitter, 1)

        foot = QHBoxLayout()
        self.summary_label = QLabel("", self)
        self.summary_label.setObjectName("secondaryLabel")
        foot.addWidget(self.summary_label, 1)
        self.prev_btn = QPushButton("前の差分", self)
        self.next_btn = QPushButton("次の差分", self)
        foot.addWidget(self.prev_btn)
        foot.addWidget(self.next_btn)
        lay.addLayout(foot)

        lv, rv = self.left.viewer, self.right.viewer
        lv.verticalScrollBar().valueChanged.connect(
            lambda v: self._sync(rv.verticalScrollBar(), v))
        rv.verticalScrollBar().valueChanged.connect(
            lambda v: self._sync(lv.verticalScrollBar(), v))
        lv.horizontalScrollBar().valueChanged.connect(
            lambda v: self._sync(rv.horizontalScrollBar(), v))
        rv.horizontalScrollBar().valueChanged.connect(
            lambda v: self._sync(lv.horizontalScrollBar(), v))
        lv.font_size_changed.connect(rv.set_font_size)
        rv.font_size_changed.connect(lv.set_font_size)
        self.prev_btn.clicked.connect(lambda: self._jump(-1))
        self.next_btn.clicked.connect(lambda: self._jump(1))
        self._update_nav()

    def _sync(self, target_bar, value: int) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            target_bar.setValue(value)
        finally:
            self._syncing = False

    def clear(self, message: str = "") -> None:
        self._result = None
        self.left_title.setText("")
        self.right_title.setText("")
        self.left.set_code("")
        self.right.set_code("")
        self._set_summary(message, "secondaryLabel")
        self._update_nav()

    def _set_summary(self, text: str, kind: str) -> None:
        self.summary_label.setObjectName(kind)
        self.summary_label.setText(text)
        refresh_style(self.summary_label)

    def show_diff(self, left_title: str, left_text: str, right_title: str, right_text: str,
                  ignore_whitespace: bool, left_equipment: str, right_equipment: str) -> DiffResult:
        res = compute_diff(left_text, right_text, ignore_whitespace, left_equipment, right_equipment)
        self._result = res
        self.left_title.setText(left_title)
        self.right_title.setText(right_title)

        colors = {
            "changed": QColor(COLORS["diff_changed"]),
            "deleted": QColor(COLORS["diff_deleted"]),
            "added": QColor(COLORS["diff_added"]),
            "pad": QColor(COLORS["diff_placeholder"]),
        }
        for panel, lines, labels, kinds in (
            (self.left, res.left_lines, res.left_labels, res.left_kinds),
            (self.right, res.right_lines, res.right_labels, res.right_kinds),
        ):
            panel.set_code("\n".join(lines))
            panel.viewer.set_line_labels(labels)
            panel.viewer.set_line_backgrounds(
                {i: colors[k] for i, k in enumerate(kinds) if k in colors}
            )

        if res.identical:
            self._set_summary("一致しています（差分なし）。", "successLabel")
        else:
            self._set_summary(
                f"差分 {res.diff_line_count} 行（{len(res.hunk_starts)} か所）　"
                "黄: 変更 / 赤: 左のみ / 緑: 右のみ / 灰: 相手側にない行",
                "warningLabel",
            )
        self._update_nav()
        return res

    def _update_nav(self) -> None:
        has = bool(self._result and self._result.hunk_starts)
        self.prev_btn.setEnabled(has)
        self.next_btn.setEnabled(has)

    def _jump(self, direction: int) -> None:
        if not self._result or not self._result.hunk_starts:
            return
        current = self.left.viewer.textCursor().blockNumber()
        starts = self._result.hunk_starts
        if direction > 0:
            target = next((s for s in starts if s > current), starts[0])
        else:
            target = next((s for s in reversed(starts) if s < current), starts[-1])
        self.left.viewer.goto_line(target + 1)
        self.right.viewer.goto_line(target + 1)


# ----------------------------------------------------------------------
# スクリプト比較ダイアログ
# ----------------------------------------------------------------------
def _script_choices(snapshot: MIConfigSnapshot) -> list[tuple[str, int]]:
    return [(f"{g.name} / {a.name}", a.id) for g, a, _s in snapshot.script_actions()]


def _action_label(snapshot: MIConfigSnapshot, action: Action) -> str:
    g = snapshot.action_groups.get(action.action_group_id)
    return f"{g.name if g else '?'} / {action.name}"


class _OptionsBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.ignore_ws = QCheckBox("空白の違いを無視", self)
        self.normalize = QCheckBox("設備固有の文字列（グループ名中の設備コード）を正規化", self)
        lay.addWidget(self.ignore_ws)
        lay.addWidget(self.normalize)
        lay.addStretch(1)


class DiffDialog(QDialog):
    """2 つのスクリプトを選んで左右に並べて比較する（モードレス）。"""

    def __init__(self, snapshot: MIConfigSnapshot, config_manager: ConfigManager,
                 left_action_id: int | None = None, right_action_id: int | None = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("スクリプト比較")
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(1400, 860)
        self.setSizeGripEnabled(True)
        self._snapshot = snapshot

        lay = QVBoxLayout(self)
        sel = QHBoxLayout()
        self.left_combo = self._make_combo()
        self.right_combo = self._make_combo()
        sel.addWidget(QLabel("左:", self))
        sel.addWidget(self.left_combo, 1)
        sel.addWidget(QLabel("右:", self))
        sel.addWidget(self.right_combo, 1)
        lay.addLayout(sel)
        self.options = _OptionsBar(self)
        lay.addWidget(self.options)
        self.view = DiffView(config_manager, self)
        lay.addWidget(self.view, 1)

        choices = _script_choices(snapshot)
        for combo in (self.left_combo, self.right_combo):
            combo.blockSignals(True)
            for label, aid in choices:
                combo.addItem(label, aid)
            combo.blockSignals(False)
        self._select(self.left_combo, left_action_id, 0)
        self._select(self.right_combo, right_action_id, 1 if len(choices) > 1 else 0)

        self.left_combo.currentIndexChanged.connect(self._refresh)
        self.right_combo.currentIndexChanged.connect(self._refresh)
        self.options.ignore_ws.toggled.connect(self._refresh)
        self.options.normalize.toggled.connect(self._refresh)
        self._refresh()

    def _make_combo(self) -> QComboBox:
        combo = QComboBox(self)
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        completer = combo.completer()
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        combo.setMinimumContentsLength(30)
        return combo

    @staticmethod
    def _select(combo: QComboBox, action_id: int | None, fallback: int) -> None:
        idx = combo.findData(action_id) if action_id is not None else -1
        if idx < 0 and combo.count() > 0:
            idx = min(fallback, combo.count() - 1)
        combo.blockSignals(True)
        combo.setCurrentIndex(idx)
        combo.blockSignals(False)

    def _refresh(self, *args) -> None:
        snap = self._snapshot
        la = snap.actions.get(self.left_combo.currentData())
        ra = snap.actions.get(self.right_combo.currentData())
        if la is None or ra is None:
            self.view.clear("比較するスクリプトを左右で選択してください。")
            return
        ls = snap.script_for_action(la)
        rs = snap.script_for_action(ra)
        normalize = self.options.normalize.isChecked()
        lg = snap.action_groups.get(la.action_group_id)
        rg = snap.action_groups.get(ra.action_group_id)
        self.view.show_diff(
            _action_label(snap, la), ls.source if ls else "",
            _action_label(snap, ra), rs.source if rs else "",
            self.options.ignore_ws.isChecked(),
            group_equipment(lg) if normalize else "",
            group_equipment(rg) if normalize else "",
        )


# ----------------------------------------------------------------------
# 同種グループと比較
# ----------------------------------------------------------------------
_SIMILAR_COLUMNS = ["グループ名", "アクション名", "結果", "差分行数"]


class SimilarGroupsDialog(QDialog):
    """処理名が同じグループのスクリプトと一致／不一致を比較する（モードレス）。"""

    def __init__(self, snapshot: MIConfigSnapshot, config_manager: ConfigManager,
                 base_action_id: int, parent=None):
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(1400, 900)
        self.setSizeGripEnabled(True)
        self._snapshot = snapshot
        self._base_action = snapshot.actions[base_action_id]
        self._base_group = snapshot.action_groups.get(self._base_action.action_group_id)
        base_label = _action_label(snapshot, self._base_action)
        process = self._base_group.process if self._base_group else ""
        self.setWindowTitle(f"同種グループと比較 — 基準: {base_label}")

        lay = QVBoxLayout(self)
        head = QLabel(
            f"基準: {base_label}　処理名: {process or '(規則に合わない名前のため同種グループなし)'}",
            self,
        )
        head.setObjectName("headerLabel")
        lay.addWidget(head)
        self.options = _OptionsBar(self)
        self.options.normalize.setChecked(True)
        lay.addWidget(self.options)

        splitter = QSplitter(Qt.Orientation.Vertical, self)
        lay.addWidget(splitter, 1)
        top = QWidget(splitter)
        top_lay = QVBoxLayout(top)
        top_lay.setContentsMargins(0, 0, 0, 0)
        self.model = ReadOnlyTableModel(_SIMILAR_COLUMNS, parent=self)
        self.table = FilterableTable(self.model, top)
        self.table.view.setSelectionBehavior(self.table.view.SelectionBehavior.SelectRows)
        self.table.view.setSelectionMode(self.table.view.SelectionMode.SingleSelection)
        self.table.view.horizontalHeader().resizeSection(0, 360)
        self.table.view.horizontalHeader().resizeSection(1, 220)
        top_lay.addWidget(self.table, 1)
        self.summary_label = QLabel("", top)
        self.summary_label.setObjectName("secondaryLabel")
        top_lay.addWidget(self.summary_label)
        splitter.addWidget(top)
        self.view = DiffView(config_manager, splitter)
        splitter.addWidget(self.view)
        splitter.setSizes([260, 640])

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._refresh)
        self.options.ignore_ws.toggled.connect(lambda *_: self._timer.start())
        self.options.normalize.toggled.connect(lambda *_: self._timer.start())
        self.table.view.selectionModel().currentRowChanged.connect(self._on_row_changed)
        self._refresh()

    def _refresh(self) -> None:
        snap = self._snapshot
        base_script = snap.script_for_action(self._base_action)
        base_src = base_script.source if base_script else ""
        ignore_ws = self.options.ignore_ws.isChecked()
        normalize = self.options.normalize.isChecked()
        base_equip = group_equipment(self._base_group) if normalize else ""

        rows, payload, bg = [], [], {}
        c_diff = QColor(COLORS["diff_changed"])
        n_same = n_diff = 0
        groups = snap.groups_with_same_process(self._base_group) if self._base_group else []
        for g in groups:
            if self._base_group is not None and g.id == self._base_group.id:
                continue
            action = snap.first_script_action(g.id)
            script = snap.script_for_action(action)
            if action is None or script is None:
                rows.append([g.name, action.name if action else "", "スクリプトなし", ""])
                payload.append(None)
                continue
            res = compute_diff(base_src, script.source, ignore_ws, base_equip,
                               group_equipment(g) if normalize else "")
            if res.identical:
                n_same += 1
                rows.append([g.name, action.name, "一致", 0])
            else:
                n_diff += 1
                bg[len(rows)] = c_diff
                rows.append([g.name, action.name, "不一致", res.diff_line_count])
            payload.append(action.id)
        self.model.set_rows(rows, payload=payload, background=bg)
        self.summary_label.setText(
            f"同種グループ {len(rows)} 件: 一致 {n_same} 件 / 不一致 {n_diff} 件"
            "（行を選ぶと下に左右比較を表示します）"
        )
        self.view.clear("上の表で比較するグループを選択してください。")

    def _on_row_changed(self, current, previous) -> None:
        if not current.isValid():
            return
        src_row = self.table.proxy.mapToSource(current).row()
        action_id = self.model.payload(src_row)
        snap = self._snapshot
        action = snap.actions.get(action_id) if action_id is not None else None
        if action is None:
            self.view.clear("このグループにはスクリプトがありません。")
            return
        base_script = snap.script_for_action(self._base_action)
        script = snap.script_for_action(action)
        normalize = self.options.normalize.isChecked()
        g = snap.action_groups.get(action.action_group_id)
        self.view.show_diff(
            _action_label(snap, self._base_action), base_script.source if base_script else "",
            _action_label(snap, action), script.source if script else "",
            self.options.ignore_ws.isChecked(),
            group_equipment(self._base_group) if normalize else "",
            group_equipment(g) if normalize else "",
        )
