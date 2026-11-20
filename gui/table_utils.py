"""表（QTableView）の共通部品

- ColumnFilterProxy: 列ごとのテキスト部分一致フィルタ + 並べ替え
- ColumnFilterBar:   表の列に合わせて並ぶフィルタ入力欄
- CopyableTableView: 選択範囲のコピー（Ctrl+C、タブ区切り。Excel へ貼り付け可能）
- HighlightDelegate: モデルの背景色（変更セル・追加行・削除行）を確実に描画する
- ReadOnlyTableModel: 文字列の 2 次元リストを表示する読み取り専用モデル
- FilterableTable:   上記をまとめたウィジェット
- ヘッダー状態（列幅）の保存・復元
"""

from __future__ import annotations

from PySide6.QtCore import (
    QAbstractTableModel,
    QByteArray,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    QTimer,
)
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QStyle,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

# 並べ替えに使う値（モデルが返す）
SORT_ROLE = Qt.ItemDataRole.UserRole + 1
# コピー時の文字列（未指定なら DisplayRole）
COPY_ROLE = Qt.ItemDataRole.UserRole + 2
# フィルタ対象の文字列（未指定なら DisplayRole）
FILTER_ROLE = Qt.ItemDataRole.UserRole + 3
# True のとき文字に取り消し線を引く（削除予定の行）
STRIKE_ROLE = Qt.ItemDataRole.UserRole + 4


# ----------------------------------------------------------------------
# フィルタ + 並べ替え
# ----------------------------------------------------------------------
class ColumnFilterProxy(QSortFilterProxyModel):
    """列ごとのテキスト部分一致フィルタ（大文字小文字を区別しない、AND 条件）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._filters: dict[int, str] = {}
        self.setSortRole(SORT_ROLE)
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setDynamicSortFilter(False)

    def _change_filter(self, fn) -> None:
        # Qt 6.10 以降は beginFilterChange / endFilterChange、それ以前は invalidateFilter
        if hasattr(self, "beginFilterChange"):
            self.beginFilterChange()
            fn()
            self.endFilterChange()
        else:
            fn()
            self.invalidateFilter()

    def set_filter(self, column: int, text: str) -> None:
        text = (text or "").strip().lower()

        def apply():
            if text:
                self._filters[column] = text
            else:
                self._filters.pop(column, None)

        self._change_filter(apply)

    def clear_filters(self) -> None:
        self._change_filter(self._filters.clear)

    def refilter(self) -> None:
        self._change_filter(lambda: None)

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if not self._filters:
            return True
        model = self.sourceModel()
        for col, text in self._filters.items():
            idx = model.index(source_row, col, source_parent)
            value = model.data(idx, FILTER_ROLE)
            if value is None:
                value = model.data(idx, Qt.ItemDataRole.DisplayRole)
            if text not in ("" if value is None else str(value)).lower():
                return False
        return True


class ColumnFilterBar(QWidget):
    """表の各列の真上にフィルタ入力欄を並べる。"""

    def __init__(self, view: QTableView, proxy: ColumnFilterProxy, parent=None):
        super().__init__(parent)
        self._view = view
        self._proxy = proxy
        self._edits: list[QLineEdit] = []
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._apply_filters)

        header = view.horizontalHeader()
        header.sectionResized.connect(self._relayout)
        header.sectionMoved.connect(self._relayout)
        header.geometriesChanged.connect(self._relayout)
        view.verticalHeader().geometriesChanged.connect(self._relayout)
        view.horizontalScrollBar().valueChanged.connect(self._relayout)
        proxy.modelReset.connect(self._rebuild_if_needed)
        proxy.columnsInserted.connect(self._rebuild_if_needed)
        proxy.columnsRemoved.connect(self._rebuild_if_needed)
        self._rebuild_if_needed()

    def _rebuild_if_needed(self, *args) -> None:
        count = self._proxy.columnCount()
        if count == len(self._edits):
            self._relayout()
            return
        for e in self._edits:
            e.deleteLater()
        self._edits = []
        model = self._proxy
        for col in range(count):
            e = QLineEdit(self)
            e.setObjectName("columnFilter")
            title = model.headerData(col, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole)
            e.setPlaceholderText(f"{title or ''} で絞り込み")
            e.setClearButtonEnabled(True)
            e.textChanged.connect(lambda *_: self._timer.start())
            e.show()
            self._edits.append(e)
        if self._edits:
            self.setFixedHeight(self._edits[0].sizeHint().height())
        self._relayout()

    def _apply_filters(self) -> None:
        for col, e in enumerate(self._edits):
            self._proxy.set_filter(col, e.text())

    def clear(self) -> None:
        for e in self._edits:
            e.blockSignals(True)
            e.clear()
            e.blockSignals(False)
        self._proxy.clear_filters()

    def _relayout(self, *args) -> None:
        header = self._view.horizontalHeader()
        offset = self._view.frameWidth()
        vh = self._view.verticalHeader()
        if vh.isVisible():
            offset += vh.width()
        h = self.height()
        for col, e in enumerate(self._edits):
            if col >= header.count() or header.isSectionHidden(col):
                e.hide()
                continue
            x = header.sectionViewportPosition(col) + offset
            w = header.sectionSize(col)
            e.setGeometry(x, 0, max(0, w), h)
            e.show()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout()


# ----------------------------------------------------------------------
# 表示
# ----------------------------------------------------------------------
class HighlightDelegate(QStyledItemDelegate):
    """BackgroundRole の色を、QSS の影響を受けずに描画する。

    STRIKE_ROLE が True のセルは、表のフォントのまま取り消し線を引く
    （FontRole で QFont を返すと QSS のフォント指定が失われるため）。
    """

    def initStyleOption(self, option, index) -> None:
        super().initStyleOption(option, index)
        if index.data(STRIKE_ROLE):
            font = option.font
            font.setStrikeOut(True)
            option.font = font

    def paint(self, painter, option, index) -> None:
        if not (option.state & QStyle.StateFlag.State_Selected):
            brush = index.data(Qt.ItemDataRole.BackgroundRole)
            if brush is not None:
                if isinstance(brush, QColor):
                    brush = QBrush(brush)
                painter.fillRect(option.rect, brush)
        super().paint(painter, option, index)


class CopyableTableView(QTableView):
    """複数選択と選択範囲のコピー（Ctrl+C）に対応した表。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(True)
        self.setWordWrap(False)
        self.setItemDelegate(HighlightDelegate(self))
        self.horizontalHeader().setSectionsMovable(True)
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setHighlightSections(False)
        self.verticalHeader().setDefaultSectionSize(22)
        self.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_selection()
            event.accept()
            return
        super().keyPressEvent(event)

    def copy_selection(self) -> None:
        """選択範囲をタブ区切りでクリップボードへコピーする（列は表示順）。"""
        indexes = self.selectedIndexes()
        if not indexes:
            return
        header = self.horizontalHeader()
        rows = sorted({i.row() for i in indexes})
        cols = sorted({i.column() for i in indexes}, key=header.visualIndex)
        selected = {(i.row(), i.column()) for i in indexes}
        model = self.model()
        lines = []
        for r in rows:
            cells = []
            for c in cols:
                if (r, c) not in selected:
                    cells.append("")
                    continue
                idx = model.index(r, c)
                v = model.data(idx, COPY_ROLE)
                if v is None:
                    v = model.data(idx, Qt.ItemDataRole.DisplayRole)
                text = "" if v is None else str(v)
                cells.append(text.replace("\t", " ").replace("\r", " ").replace("\n", " "))
            lines.append("\t".join(cells))
        QGuiApplication.clipboard().setText("\r\n".join(lines))

    def selected_source_rows(self) -> list[int]:
        """選択中の行（ソースモデルの行番号、表示順）。"""
        model = self.model()
        proxy_rows = sorted({i.row() for i in self.selectedIndexes()})
        if isinstance(model, QSortFilterProxyModel):
            return [model.mapToSource(model.index(r, 0)).row() for r in proxy_rows]
        return proxy_rows


class ReadOnlyTableModel(QAbstractTableModel):
    """文字列の 2 次元リストを表示する読み取り専用モデル。

    row_colors（任意）: 行番号 → 文字色（QColor）
    """

    def __init__(self, columns: list[str], rows: list[list] | None = None, parent=None):
        super().__init__(parent)
        self._columns = list(columns)
        self._rows: list[list] = rows or []
        self._fg: dict[int, QColor] = {}
        self._bg: dict[int, QColor] = {}
        self._payload: list = []

    def set_rows(self, rows: list[list], payload: list | None = None,
                 foreground: dict[int, QColor] | None = None,
                 background: dict[int, QColor] | None = None) -> None:
        """rows: 表示する値。payload: 行ごとに保持する任意のデータ（行番号で取得）。"""
        self.beginResetModel()
        self._rows = rows
        self._payload = payload or []
        self._fg = foreground or {}
        self._bg = background or {}
        self.endResetModel()

    def set_columns(self, columns: list[str]) -> None:
        self.beginResetModel()
        self._columns = list(columns)
        self.endResetModel()

    def payload(self, row: int):
        if 0 <= row < len(self._payload):
            return self._payload[row]
        return None

    def rows(self) -> list[list]:
        return self._rows

    def columns(self) -> list[str]:
        return self._columns

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._columns)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        value = row[index.column()] if index.column() < len(row) else ""
        if role in (Qt.ItemDataRole.DisplayRole, COPY_ROLE, Qt.ItemDataRole.ToolTipRole):
            return "" if value is None else str(value)
        if role == SORT_ROLE:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
            text = "" if value is None else str(value)
            # 数字だけの列は数値として並べる
            return int(text) if text.isdigit() and len(text) < 16 else text.lower()
        if role == Qt.ItemDataRole.ForegroundRole:
            return self._fg.get(index.row())
        if role == Qt.ItemDataRole.BackgroundRole:
            return self._bg.get(index.row())
        return None

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self._columns[section] if section < len(self._columns) else None
        return str(section + 1)


class FilterableTable(QWidget):
    """列フィルタ欄 + 表 + プロキシをまとめたウィジェット。"""

    def __init__(self, source_model: QAbstractTableModel, parent=None, show_filter: bool = True):
        super().__init__(parent)
        self.source_model = source_model
        self.proxy = ColumnFilterProxy(self)
        self.proxy.setSourceModel(source_model)
        self.view = CopyableTableView(self)
        self.view.setModel(self.proxy)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.filter_bar: ColumnFilterBar | None = None
        if show_filter:
            self.filter_bar = ColumnFilterBar(self.view, self.proxy, self)
            layout.addWidget(self.filter_bar)
        layout.addWidget(self.view, 1)

    def selected_source_rows(self) -> list[int]:
        return self.view.selected_source_rows()

    def source_row_of(self, proxy_index: QModelIndex) -> int:
        return self.proxy.mapToSource(proxy_index).row()

    def visible_row_count(self) -> int:
        return self.proxy.rowCount()


# ----------------------------------------------------------------------
# ヘッダー状態（列幅）の保存・復元
# ----------------------------------------------------------------------
def save_header_state(header: QHeaderView) -> str:
    return bytes(header.saveState().toBase64()).decode("ascii")


def restore_header_state(header: QHeaderView, state: str | None) -> bool:
    if not state:
        return False
    try:
        return header.restoreState(QByteArray.fromBase64(state.encode("ascii")))
    except Exception:
        return False


def fit_columns(view: QTableView, max_width: int = 400) -> None:
    """内容に合わせて列幅を調整する（上限付き）。"""
    view.resizeColumnsToContents()
    header = view.horizontalHeader()
    for col in range(header.count()):
        if header.sectionSize(col) > max_width:
            header.resizeSection(col, max_width)


def make_hbox(*widgets, margins=(0, 0, 0, 0), spacing: int = 6, stretch_at: int | None = None):
    """ウィジェットを横に並べたレイアウトを返す（stretch_at の位置に伸縮スペース）。"""
    lay = QHBoxLayout()
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    for i, w in enumerate(widgets):
        if stretch_at is not None and i == stretch_at:
            lay.addStretch(1)
        lay.addWidget(w)
    if stretch_at is not None and stretch_at >= len(widgets):
        lay.addStretch(1)
    return lay
