"""データソースタブ（読み取り専用）

ツリーでデータソースを選んだときに、配下のポイントグループとポイントの一覧を表示する。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from core.config_manager import ConfigManager
from core.models import MIConfigSnapshot, access_label, data_type_label, yes_no
from gui.styles import COLORS
from gui.table_utils import FilterableTable, ReadOnlyTableModel, restore_header_state, save_header_state

_COLUMNS = ["ポイントグループ", "エイリアス", "データタイプ", "アクセス", "ポイント有効", "初期値",
            "Namespace", "OPC UA キー", "公開間隔", "サンプリング間隔", "参照グループ数"]


class DataSourceTab(QWidget):
    HEADER_KEY = "datasource_tab"

    def __init__(self, config_manager: ConfigManager, parent=None):
        super().__init__(parent)
        self.config_manager = config_manager
        self._snapshot: MIConfigSnapshot | None = None
        self._ds_id: int | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        bar = QWidget(self)
        bar.setObjectName("panelToolBar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(6, 4, 6, 4)
        self.title_label = QLabel("データソース", bar)
        self.title_label.setObjectName("headerLabel")
        bar_lay.addWidget(self.title_label, 1)
        lay.addWidget(bar)

        self.model = ReadOnlyTableModel(_COLUMNS, parent=self)
        self.table = FilterableTable(self.model, self)
        lay.addWidget(self.table, 1)
        self.status_label = QLabel("", self)
        self.status_label.setObjectName("secondaryLabel")
        lay.addWidget(self.status_label)
        if not restore_header_state(self.table.view.horizontalHeader(),
                                    config_manager.config.window.table_headers.get(self.HEADER_KEY)):
            header = self.table.view.horizontalHeader()
            header.resizeSection(0, 260)
            header.resizeSection(1, 280)
            header.resizeSection(7, 260)

    def save_header(self) -> None:
        self.config_manager.config.window.table_headers[self.HEADER_KEY] = save_header_state(
            self.table.view.horizontalHeader()
        )

    def set_snapshot(self, snapshot: MIConfigSnapshot | None) -> None:
        self._snapshot = snapshot
        if snapshot is None or self._ds_id not in (snapshot.data_sources if snapshot else {}):
            self.model.set_rows([])
            self.title_label.setText("データソース")
            self.status_label.setText("")
            self._ds_id = None
        else:
            self.show_data_source(self._ds_id, force=True)

    def show_data_source(self, ds_id: int, force: bool = False) -> None:
        snap = self._snapshot
        if snap is None:
            return
        if ds_id == self._ds_id and not force:
            return
        self._ds_id = ds_id
        ds = snap.data_sources.get(ds_id)
        if ds is None:
            self.model.set_rows([])
            return
        self.title_label.setText(f"{ds.alias}（{'有効' if ds.enabled else '無効'}）")

        # ポイント → 参照しているグループ数（Active = 1 の行）
        ref_count: dict[int, int] = {}
        for gp in snap.group_points:
            if gp.active:
                ref_count[gp.point_id] = ref_count.get(gp.point_id, 0) + 1

        rows = []
        fg = {}
        secondary = QColor(COLORS["text_secondary"])
        groups = set()
        for p in snap.points_by_data_source.get(ds_id, []):
            props = p.opc_props()
            groups.add(p.point_group_id)
            if not p.enabled:
                fg[len(rows)] = secondary
            rows.append([
                snap.point_group_path(p.point_group_id),
                p.alias,
                data_type_label(p.data_type),
                access_label(p.access),
                yes_no(p.enabled),
                p.initial_value or "",
                props.namespace if props else "",
                props.key if props else "",
                props.publishing_interval if props else "",
                props.sampling_interval if props else "",
                ref_count.get(p.id, 0),
            ])
        self.model.set_rows(rows, foreground=fg)
        self.status_label.setText(f"ポイントグループ {len(groups)} 件 / ポイント {len(rows)} 件"
                                  "（読み取り専用）")
