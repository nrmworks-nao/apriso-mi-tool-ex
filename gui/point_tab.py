"""ポイント設定（グループ単位）の表示と編集

- EditSession: 全グループの MI_ACTION_GROUP_POINT（Active = 1）を EditRow として保持し、
  未保存の変更を管理する。ポイントタブと全ポイント一覧は同じ EditSession を共有する。
- PointTableModel: EditSession の行を表示する QAbstractTableModel
  （変更セル・追加行・削除行を色で区別する）。
- PointPickerDialog: ポイント選択ダイアログ（複数選択で一括追加できる）。
- PointEditorBase / PointTab: 表・フィルタ・並べ替え・複数選択・コピー・編集操作。

編集はすぐに DB へ書かず、未保存の変更として保持する。「保存」でプレビューを経て反映する。
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.config_manager import ConfigManager
from core.models import MIConfigSnapshot, access_label, data_type_label, yes_no
from core.point_service import EditRow, apply_bulk_items, row_alias
from gui.styles import COLORS
from gui.table_utils import (
    COPY_ROLE,
    SORT_ROLE,
    STRIKE_ROLE,
    FilterableTable,
    ReadOnlyTableModel,
    fit_columns,
    restore_header_state,
    save_header_state,
)


# ----------------------------------------------------------------------
# 編集セッション
# ----------------------------------------------------------------------
class EditSession(QObject):
    """未保存の変更を含む、全グループのポイント設定。"""

    rows_changed = Signal(object)        # list[EditRow]（値の変更）
    structure_changed = Signal(object)   # set[int]（行の増減があったグループ ID）。None は全体
    changes_count_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.snapshot: MIConfigSnapshot | None = None
        self._rows_by_group: dict[int, list[EditRow]] = {}
        self._all_cache: list[EditRow] | None = None

    # --- 読み込み -------------------------------------------------------
    def reset(self, snapshot: MIConfigSnapshot | None) -> None:
        """スナップショットから作り直す（未保存の変更は破棄される）。"""
        self.snapshot = snapshot
        self._rows_by_group = {}
        if snapshot is not None:
            for g in snapshot.action_groups.values():
                self._rows_by_group[g.id] = [
                    EditRow.from_group_point(gp) for gp in snapshot.active_group_points(g.id)
                ]
        self._all_cache = None
        self.structure_changed.emit(None)
        self.changes_count_changed.emit(self.change_count())

    # --- 参照 ----------------------------------------------------------
    def rows_for_group(self, group_id: int) -> list[EditRow]:
        return self._rows_by_group.get(group_id, [])

    def all_rows(self) -> list[EditRow]:
        """全グループの行（グループ名順）。"""
        if self._all_cache is None:
            out: list[EditRow] = []
            snap = self.snapshot
            if snap is not None:
                for g in snap.groups_sorted():
                    out.extend(self._rows_by_group.get(g.id, []))
            self._all_cache = out
        return self._all_cache

    def pending_rows(self) -> list[EditRow]:
        return [r for rows in self._rows_by_group.values() for r in rows if r.has_change]

    def change_count(self) -> int:
        return sum(1 for rows in self._rows_by_group.values() for r in rows if r.has_change)

    def has_changes(self) -> bool:
        return any(r.has_change for rows in self._rows_by_group.values() for r in rows)

    def group_has_changes(self, group_id: int) -> bool:
        return any(r.has_change for r in self._rows_by_group.get(group_id, []))

    def changed_group_ids(self) -> list[int]:
        return [gid for gid, rows in self._rows_by_group.items() if any(r.has_change for r in rows)]

    # --- 変更 ----------------------------------------------------------
    def _emit_rows(self, rows: list[EditRow]) -> None:
        if rows:
            self.rows_changed.emit(list(rows))
        self.changes_count_changed.emit(self.change_count())

    def _emit_structure(self, group_ids: set[int]) -> None:
        self._all_cache = None
        self.structure_changed.emit(set(group_ids))
        self.changes_count_changed.emit(self.change_count())

    def add_rows(self, group_id: int, rows: list[EditRow]) -> None:
        if not rows:
            return
        self._rows_by_group.setdefault(group_id, []).extend(rows)
        self._emit_structure({group_id})

    def delete_rows(self, rows: list[EditRow]) -> None:
        """追加行はその場で取り除き、既存行は削除予定にする。"""
        removed_groups: set[int] = set()
        marked: list[EditRow] = []
        for r in rows:
            if r.is_new:
                lst = self._rows_by_group.get(r.action_group_id, [])
                if r in lst:
                    lst.remove(r)
                    removed_groups.add(r.action_group_id)
            elif not r.deleted:
                r.deleted = True
                marked.append(r)
        if removed_groups:
            self._emit_structure(removed_groups)
        if marked:
            self._emit_rows(marked)

    def undelete_rows(self, rows: list[EditRow]) -> None:
        changed = []
        for r in rows:
            if r.deleted:
                r.deleted = False
                changed.append(r)
        self._emit_rows(changed)

    def set_local_name(self, row: EditRow, value: str | None) -> None:
        value = value if value else None
        if row.local_name == value:
            return
        row.local_name = value
        self._emit_rows([row])

    def set_triggers(self, row: EditRow, value: bool) -> None:
        if row.triggers_execution is not None and bool(row.triggers_execution) == value:
            return
        row.triggers_execution = value
        self._emit_rows([row])

    def set_point(self, row: EditRow, point_id: int) -> None:
        if row.point_id == point_id:
            return
        row.point_id = point_id
        row.unresolved_alias = ""
        self._emit_rows([row])

    def notify_rows_changed(self, rows: list[EditRow]) -> None:
        """行を直接変更した後（一括編集の適用など）に呼ぶ。"""
        self._emit_rows(rows)

    def revert_group(self, group_id: int) -> None:
        rows = self._rows_by_group.get(group_id, [])
        kept = [r for r in rows if not r.is_new]
        for r in kept:
            r.revert()
        self._rows_by_group[group_id] = kept
        self._emit_structure({group_id})

    def revert_all(self) -> None:
        for gid in list(self._rows_by_group):
            rows = self._rows_by_group[gid]
            kept = [r for r in rows if not r.is_new]
            for r in kept:
                r.revert()
            self._rows_by_group[gid] = kept
        self._all_cache = None
        self.structure_changed.emit(None)
        self.changes_count_changed.emit(self.change_count())


# ----------------------------------------------------------------------
# 表のモデル
# ----------------------------------------------------------------------
COL_GROUP = "group"
COL_LOCAL_NAME = "local_name"
COL_ALIAS = "alias"
COL_TRIGGERS = "triggers"
COL_DATA_TYPE = "data_type"
COL_ACCESS = "access"
COL_ENABLED = "enabled"
COL_OPC_KEY = "opc_key"
COL_PUBLISHING = "publishing"

COLUMN_TITLES = {
    COL_GROUP: "グループ名",
    COL_LOCAL_NAME: "ローカル名",
    COL_ALIAS: "エイリアス",
    COL_TRIGGERS: "トリガーの実行",
    COL_DATA_TYPE: "データタイプ",
    COL_ACCESS: "アクセス",
    COL_ENABLED: "ポイント有効",   # 未確認2: Apriso 画面の「有効」列の対応先は未確認のため読み取り専用
    COL_OPC_KEY: "OPC UA キー",
    COL_PUBLISHING: "公開間隔",
}

# 列 → 変更検出の項目（EditRow.changed_fields の値）
_COLUMN_FIELD = {
    COL_LOCAL_NAME: "local_name",
    COL_ALIAS: "point_id",
    COL_DATA_TYPE: "point_id",
    COL_ACCESS: "point_id",
    COL_ENABLED: "point_id",
    COL_OPC_KEY: "point_id",
    COL_PUBLISHING: "point_id",
    COL_TRIGGERS: "triggers_execution",
}


def _is_checked(value) -> bool:
    try:
        return Qt.CheckState(value) == Qt.CheckState.Checked
    except (TypeError, ValueError):
        return value == Qt.CheckState.Checked


class PointTableModel(QAbstractTableModel):
    """EditSession の行を表示するモデル。group_id が None なら全グループ。"""

    def __init__(self, session: EditSession, show_group: bool, parent=None):
        super().__init__(parent)
        self.session = session
        self.group_id: int | None = None
        self.all_groups = show_group
        self._cols = ([COL_GROUP] if show_group else []) + [
            COL_LOCAL_NAME, COL_ALIAS, COL_TRIGGERS, COL_DATA_TYPE, COL_ACCESS,
            COL_ENABLED, COL_OPC_KEY, COL_PUBLISHING,
        ]
        self._rows: list[EditRow] = []
        self._row_index: dict[int, int] = {}
        self.editable = False

        self._c_changed = QBrush(QColor(COLORS["changed_cell"]))
        self._c_added = QBrush(QColor(COLORS["added_row"]))
        self._c_deleted = QBrush(QColor(COLORS["deleted_row"]))
        self._c_error = QBrush(QColor(COLORS["error"]))
        self._c_secondary = QBrush(QColor(COLORS["text_secondary"]))

        session.structure_changed.connect(self._on_structure_changed)
        session.rows_changed.connect(self._on_rows_changed)

    # --- 行の管理 --------------------------------------------------------
    def set_group(self, group_id: int | None) -> None:
        self.group_id = group_id
        self.reload()

    def reload(self) -> None:
        self.beginResetModel()
        if self.all_groups:
            self._rows = list(self.session.all_rows())
        elif self.group_id is not None:
            self._rows = list(self.session.rows_for_group(self.group_id))
        else:
            self._rows = []
        self._row_index = {id(r): i for i, r in enumerate(self._rows)}
        self.endResetModel()

    def _on_structure_changed(self, group_ids) -> None:
        if group_ids is None or self.all_groups or (self.group_id in group_ids):
            self.reload()

    def _on_rows_changed(self, rows) -> None:
        last = len(self._cols) - 1
        for r in rows:
            i = self._row_index.get(id(r))
            if i is not None:
                self.dataChanged.emit(self.index(i, 0), self.index(i, last))

    def row_at(self, row: int) -> EditRow | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def rows(self) -> list[EditRow]:
        return self._rows

    def column_key(self, col: int) -> str:
        return self._cols[col]

    def column_of(self, key: str) -> int:
        return self._cols.index(key) if key in self._cols else -1

    def set_editable(self, editable: bool) -> None:
        self.editable = editable
        if self._rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self._rows) - 1, len(self._cols) - 1))

    # --- Qt モデル -------------------------------------------------------
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._cols)

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return COLUMN_TITLES[self._cols[section]]
        return str(section + 1)

    def _value(self, r: EditRow, key: str):
        snap = self.session.snapshot
        if key == COL_GROUP:
            g = snap.action_groups.get(r.action_group_id) if snap else None
            return g.name if g else ""
        if key == COL_LOCAL_NAME:
            return r.local_name or ""
        if key == COL_ALIAS:
            return row_alias(snap, r) if snap else ""
        if key == COL_TRIGGERS:
            return r.triggers_execution
        p = snap.points.get(r.point_id) if (snap and r.point_id is not None) else None
        if key == COL_DATA_TYPE:
            return data_type_label(p.data_type) if p else ""
        if key == COL_ACCESS:
            return access_label(p.access) if p else ""
        if key == COL_ENABLED:
            return yes_no(p.enabled) if p else ""
        if key == COL_OPC_KEY:
            return p.opc_key if p else ""
        if key == COL_PUBLISHING:
            return p.publishing_interval if p else ""
        return ""

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        r = self._rows[index.row()]
        key = self._cols[index.column()]

        if role == Qt.ItemDataRole.DisplayRole:
            if key == COL_TRIGGERS:
                return None
            return self._value(r, key)
        if role == Qt.ItemDataRole.EditRole:
            if key == COL_LOCAL_NAME:
                return r.local_name or ""
            return self._value(r, key)
        if role == Qt.ItemDataRole.CheckStateRole and key == COL_TRIGGERS:
            return Qt.CheckState.Checked if r.triggers_execution else Qt.CheckState.Unchecked
        if role == COPY_ROLE:
            if key == COL_TRIGGERS:
                v = r.triggers_execution
                return "" if v is None else ("1" if v else "0")
            return self._value(r, key)
        if role == SORT_ROLE:
            v = self._value(r, key)
            if key == COL_TRIGGERS:
                return 1 if v else 0
            if key == COL_PUBLISHING and isinstance(v, str) and v.isdigit():
                return int(v)
            return str(v).lower()
        if role == Qt.ItemDataRole.BackgroundRole:
            if r.deleted:
                return self._c_deleted
            if r.is_new:
                return self._c_added
            field = _COLUMN_FIELD.get(key)
            if field and field in r.changed_fields():
                return self._c_changed
            return None
        if role == STRIKE_ROLE:
            return r.deleted
        if role == Qt.ItemDataRole.ForegroundRole:
            if r.point_id is None and key == COL_ALIAS:
                return self._c_error
            if key == COL_ALIAS and self.session.snapshot and r.point_id not in self.session.snapshot.points:
                return self._c_error
            if r.deleted:
                return self._c_secondary
            return None
        if role == Qt.ItemDataRole.ToolTipRole:
            notes = []
            if r.deleted:
                notes.append("削除予定（未保存）")
            elif r.is_new:
                notes.append("追加予定（未保存）")
            elif r.is_modified:
                notes.append("変更あり（未保存）")
            if key == COL_ALIAS:
                notes.insert(0, str(self._value(r, key)))
                snap = self.session.snapshot
                if r.point_id is None:
                    notes.append("未解決のエイリアス")
                elif snap and r.point_id not in snap.points:
                    notes.append("参照先ポイントが無効（Active = 0）または存在しません")
            if key == COL_TRIGGERS and r.triggers_execution is None:
                notes.append("値は NULL です")
            if key == COL_ENABLED:
                notes.append("MI_POINT.Enabled（読み取り専用）")
            return "\n".join(notes) if notes else None
        return None

    def flags(self, index: QModelIndex):
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if not index.isValid():
            return base
        r = self._rows[index.row()]
        key = self._cols[index.column()]
        if self.editable and not r.deleted:
            if key == COL_LOCAL_NAME:
                return base | Qt.ItemFlag.ItemIsEditable
            if key == COL_TRIGGERS:
                return base | Qt.ItemFlag.ItemIsUserCheckable
        return base

    def setData(self, index: QModelIndex, value, role=Qt.ItemDataRole.EditRole) -> bool:
        if not index.isValid() or not self.editable:
            return False
        r = self._rows[index.row()]
        if r.deleted:
            return False
        key = self._cols[index.column()]
        if key == COL_LOCAL_NAME and role == Qt.ItemDataRole.EditRole:
            text = "" if value is None else str(value)
            self.session.set_local_name(r, text)
            return True
        if key == COL_TRIGGERS and role == Qt.ItemDataRole.CheckStateRole:
            self.session.set_triggers(r, _is_checked(value))
            return True
        return False


# ----------------------------------------------------------------------
# ポイント選択ダイアログ
# ----------------------------------------------------------------------
_PICKER_COLUMNS = ["エイリアス(フルパス)", "データタイプ", "アクセス", "ポイント有効", "OPC UA キー",
                   "公開間隔", "このグループ"]


class PointPickerDialog(QDialog):
    """ポイント（MI_POINT、Active = 1）を選ぶダイアログ。"""

    def __init__(self, snapshot: MIConfigSnapshot, multi: bool, used_point_ids: set[int],
                 title: str = "ポイントの選択", initial_filter: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1000, 640)
        self.setSizeGripEnabled(True)
        self._multi = multi

        lay = QVBoxLayout(self)
        hint = QLabel(
            "列の上の入力欄で絞り込めます。"
            + ("Shift / Ctrl で複数選択できます（Ctrl+A ですべて選択）。" if multi else "ダブルクリックで決定します。"),
            self,
        )
        hint.setObjectName("secondaryLabel")
        lay.addWidget(hint)

        self.model = ReadOnlyTableModel(_PICKER_COLUMNS, parent=self)
        rows = []
        payload = []
        fg = {}
        secondary = QColor(COLORS["text_secondary"])
        points = sorted(snapshot.points.values(), key=lambda p: snapshot.point_path(p.id).lower())
        for i, p in enumerate(points):
            used = p.id in used_point_ids
            rows.append([
                snapshot.point_path(p.id),
                data_type_label(p.data_type),
                access_label(p.access),
                yes_no(p.enabled),
                p.opc_key,
                p.publishing_interval,
                "登録済み" if used else "",
            ])
            payload.append(p.id)
            if used:
                fg[i] = secondary
        self.model.set_rows(rows, payload=payload, foreground=fg)

        self.table = FilterableTable(self.model, self)
        if multi:
            self.table.view.setSelectionBehavior(self.table.view.SelectionBehavior.SelectRows)
        else:
            self.table.view.setSelectionBehavior(self.table.view.SelectionBehavior.SelectRows)
            self.table.view.setSelectionMode(self.table.view.SelectionMode.SingleSelection)
        self.table.view.horizontalHeader().resizeSection(0, 460)
        lay.addWidget(self.table, 1)
        if initial_filter and self.table.filter_bar is not None:
            self.table.proxy.set_filter(0, initial_filter)

        self.count_label = QLabel("", self)
        self.count_label.setObjectName("secondaryLabel")
        lay.addWidget(self.count_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("選択")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("キャンセル")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self.table.view.doubleClicked.connect(lambda _idx: self._accept())
        self.table.view.selectionModel().selectionChanged.connect(self._update_count)
        self.table.proxy.layoutChanged.connect(self._update_count)
        self.table.proxy.rowsInserted.connect(self._update_count)
        self.table.proxy.rowsRemoved.connect(self._update_count)
        self.table.proxy.modelReset.connect(self._update_count)
        self._update_count()

    def _update_count(self, *args) -> None:
        n_sel = len(self.table.selected_source_rows())
        self.count_label.setText(
            f"表示 {self.table.visible_row_count()} 件 / 全 {self.model.rowCount()} 件 / 選択 {n_sel} 件"
        )

    def _accept(self) -> None:
        if not self.selected_point_ids():
            QMessageBox.information(self, "ポイントの選択", "ポイントを選択してください。")
            return
        self.accept()

    def selected_point_ids(self) -> list[int]:
        ids = []
        for r in self.table.selected_source_rows():
            pid = self.model.payload(r)
            if pid is not None:
                ids.append(pid)
        return ids


# ----------------------------------------------------------------------
# 表と編集操作（ポイントタブ・全ポイント一覧の共通部分）
# ----------------------------------------------------------------------
class PointEditorBase(QWidget):
    """ポイント設定の表と、行に対する編集操作。"""

    def __init__(self, session: EditSession, config_manager: ConfigManager, show_group: bool,
                 header_key: str, parent=None):
        super().__init__(parent)
        self.session = session
        self.config_manager = config_manager
        self.header_key = header_key
        self.edit_mode = False

        self.model = PointTableModel(session, show_group=show_group, parent=self)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        self.toolbar = QWidget(self)
        self.toolbar.setObjectName("panelToolBar")
        self.toolbar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.toolbar_layout = QHBoxLayout(self.toolbar)
        self.toolbar_layout.setContentsMargins(6, 4, 6, 4)
        self.toolbar_layout.setSpacing(6)
        self.title_label = QLabel("", self.toolbar)
        self.title_label.setObjectName("headerLabel")
        self.toolbar_layout.addWidget(self.title_label)
        self.toolbar_layout.addStretch(1)
        lay.addWidget(self.toolbar)

        self.table = FilterableTable(self.model, self)
        self.view = self.table.view
        self.view.setEditTriggers(
            self.view.EditTrigger.DoubleClicked
            | self.view.EditTrigger.EditKeyPressed
            | self.view.EditTrigger.AnyKeyPressed
        )
        lay.addWidget(self.table, 1)

        self.status_label = QLabel("", self)
        self.status_label.setObjectName("secondaryLabel")
        lay.addWidget(self.status_label)

        # 共通の操作ボタン
        self.delete_btn = self._add_button("削除", self._on_delete, "選択した行を削除予定にする (Delete)")
        self.undelete_btn = self._add_button("削除を取消", self._on_undelete, "削除予定を取り消す")
        self.bulk_btn = self._add_button("一括編集…", self._on_bulk_edit, "選択した行をまとめて編集する")

        sc = QShortcut(QKeySequence(Qt.Key.Key_Delete), self.view)
        sc.setContext(Qt.ShortcutContext.WidgetShortcut)
        sc.activated.connect(self._on_delete)

        self.view.doubleClicked.connect(self._on_double_clicked)
        self.view.selectionModel().selectionChanged.connect(self._update_buttons)
        self.model.modelReset.connect(self._update_status)
        self.model.dataChanged.connect(self._update_status)
        self.table.proxy.layoutChanged.connect(self._update_status)
        session.changes_count_changed.connect(self._update_status)

        restore_header_state(self.view.horizontalHeader(),
                             config_manager.config.window.table_headers.get(header_key))

    def _add_button(self, text: str, slot, tooltip: str = "") -> QPushButton:
        btn = QPushButton(text, self.toolbar)
        if tooltip:
            btn.setToolTip(tooltip)
        btn.clicked.connect(slot)
        self.toolbar_layout.addWidget(btn)
        return btn

    # --- 状態 ----------------------------------------------------------
    def set_edit_mode(self, enabled: bool) -> None:
        self.edit_mode = enabled
        self.model.set_editable(enabled)
        self._update_buttons()

    def save_header(self) -> None:
        self.config_manager.config.window.table_headers[self.header_key] = save_header_state(
            self.view.horizontalHeader()
        )

    def selected_rows(self) -> list[EditRow]:
        out = []
        for r in self.table.selected_source_rows():
            row = self.model.row_at(r)
            if row is not None:
                out.append(row)
        return out

    def _update_buttons(self, *args) -> None:
        rows = self.selected_rows()
        has_snapshot = self.session.snapshot is not None
        self.delete_btn.setEnabled(self.edit_mode and has_snapshot
                                   and any(not r.deleted for r in rows))
        self.undelete_btn.setEnabled(self.edit_mode and has_snapshot and any(r.deleted for r in rows))
        self.bulk_btn.setEnabled(self.edit_mode and has_snapshot and any(not r.deleted for r in rows))

    def _update_status(self, *args) -> None:
        rows = self.model.rows()
        pending = sum(1 for r in rows if r.has_change)
        text = f"{len(rows)} 行（表示 {self.table.visible_row_count()} 行）"
        if pending:
            text += f" / 未保存の変更 {pending} 件"
        self.status_label.setText(text)
        self._update_buttons()

    # --- 操作 ----------------------------------------------------------
    def _on_delete(self) -> None:
        if not self.edit_mode:
            return
        rows = [r for r in self.selected_rows() if not r.deleted]
        if rows:
            self.session.delete_rows(rows)

    def _on_undelete(self) -> None:
        if not self.edit_mode:
            return
        rows = [r for r in self.selected_rows() if r.deleted]
        if rows:
            self.session.undelete_rows(rows)

    def _on_bulk_edit(self) -> None:
        if not self.edit_mode or self.session.snapshot is None:
            return
        rows = [r for r in self.selected_rows() if not r.deleted]
        if not rows:
            QMessageBox.information(self, "一括編集", "編集する行を選択してください。")
            return
        from gui.bulk_edit_dialog import BulkEditDialog

        dlg = BulkEditDialog(self.session.snapshot, rows, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        changed = apply_bulk_items(dlg.applicable_items())
        self.session.notify_rows_changed(changed)

    def _on_double_clicked(self, proxy_index: QModelIndex) -> None:
        """エイリアス列のダブルクリックでポイント選択ダイアログを開く。"""
        if not self.edit_mode or self.session.snapshot is None:
            return
        src = self.table.proxy.mapToSource(proxy_index)
        if self.model.column_key(src.column()) != COL_ALIAS:
            return
        row = self.model.row_at(src.row())
        if row is None or row.deleted:
            return
        snap = self.session.snapshot
        used = {r.point_id for r in self.session.rows_for_group(row.action_group_id)
                if not r.deleted and r.point_id is not None}
        current = row_alias(snap, row)
        # 現在のポイントグループで絞り込んだ状態で開く
        initial = current.rsplit("\\", 1)[0] if "\\" in current else ""
        dlg = PointPickerDialog(snap, multi=False, used_point_ids=used,
                                title="エイリアスの変更（ポイントの選択）", initial_filter=initial,
                                parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        ids = dlg.selected_point_ids()
        if ids:
            self.session.set_point(row, ids[0])


class PointTab(PointEditorBase):
    """ポイントタブ（アクショングループ単位）。"""

    def __init__(self, session: EditSession, config_manager: ConfigManager, parent=None):
        super().__init__(session, config_manager, show_group=False, header_key="point_tab",
                         parent=parent)
        self.group_id: int | None = None
        # グループ単位の操作
        self.add_btn = QPushButton("ポイント追加…", self.toolbar)
        self.add_btn.setToolTip("ポイントを選んで行を追加する（複数選択可）")
        self.add_btn.clicked.connect(self._on_add)
        self.toolbar_layout.insertWidget(2, self.add_btn)
        self.copy_btn = self._add_button("他グループから複製…", self._on_copy_from_group,
                                         "他のアクショングループのポイント構成を取り込む")
        self.seq_btn = self._add_button("連番生成…", self._on_sequence,
                                        "ローカル名とエイリアスのパターンから行をまとめて生成する")
        self.set_group(None)

    def set_group(self, group_id: int | None) -> None:
        self.group_id = group_id
        snap = self.session.snapshot
        g = snap.action_groups.get(group_id) if (snap and group_id is not None) else None
        self.title_label.setText(g.name if g else "ポイント")
        self.model.set_group(group_id if g else None)
        if g is not None and not self.config_manager.config.window.table_headers.get(self.header_key):
            fit_columns(self.view)
        self._update_status()

    def _update_buttons(self, *args) -> None:
        super()._update_buttons()
        enabled = self.edit_mode and self.session.snapshot is not None and getattr(self, "group_id", None) is not None
        for btn in (getattr(self, "add_btn", None), getattr(self, "copy_btn", None),
                    getattr(self, "seq_btn", None)):
            if btn is not None:
                btn.setEnabled(enabled)

    def _current_rows(self) -> list[EditRow]:
        return self.session.rows_for_group(self.group_id) if self.group_id is not None else []

    def _on_add(self) -> None:
        if not self.edit_mode or self.group_id is None or self.session.snapshot is None:
            return
        used = {r.point_id for r in self._current_rows() if not r.deleted and r.point_id is not None}
        dlg = PointPickerDialog(self.session.snapshot, multi=True, used_point_ids=used,
                                title="ポイント追加", parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        ids = dlg.selected_point_ids()
        dup = [pid for pid in ids if pid in used]
        new_ids = [pid for pid in ids if pid not in used]
        if dup:
            QMessageBox.information(
                self, "ポイント追加",
                f"既にこのグループに登録されているポイント {len(dup)} 件は追加しませんでした。",
            )
        rows = [EditRow.new(self.group_id, pid, None, False) for pid in new_ids]
        self.session.add_rows(self.group_id, rows)

    def _on_copy_from_group(self) -> None:
        if not self.edit_mode or self.group_id is None or self.session.snapshot is None:
            return
        from gui.copy_group_dialog import CopyGroupDialog

        dlg = CopyGroupDialog(self.session.snapshot, self.group_id, self._current_rows(), self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        candidates = dlg.applicable_candidates()
        if dlg.replace_existing():
            existing = [r for r in self._current_rows() if not r.deleted]
            self.session.delete_rows(existing)
        rows = [EditRow.new(self.group_id, c.point_id, c.local_name, c.triggers_execution)
                for c in candidates]
        self.session.add_rows(self.group_id, rows)

    def _on_sequence(self) -> None:
        if not self.edit_mode or self.group_id is None or self.session.snapshot is None:
            return
        from gui.sequence_dialog import SequenceDialog

        dlg = SequenceDialog(self.session.snapshot, self.group_id, self._current_rows(), self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        rows = [EditRow.new(self.group_id, c.point_id, c.local_name, c.triggers_execution)
                for c in dlg.applicable_candidates()]
        self.session.add_rows(self.group_id, rows)
