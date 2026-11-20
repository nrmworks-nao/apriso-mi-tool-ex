"""全ポイント一覧

全グループの MI_ACTION_GROUP_POINT（Active = 1）を 1 つの表で表示する
（ポイントタブの列に「グループ名」を加える）。フィルタ・並べ替え・複数選択・コピー・
一括編集が使える。編集内容はポイントタブと同じ EditSession に保持され、
メインウィンドウの「保存」でまとめて反映する。
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, Qt, Signal

from core.config_manager import ConfigManager
from gui.point_tab import COL_GROUP, EditSession, PointEditorBase


class AllPointsView(PointEditorBase):
    """全ポイント一覧（独立ウィンドウ）。"""

    group_activated = Signal(int)  # グループ名のダブルクリック → ツリーでそのグループを選ぶ

    def __init__(self, session: EditSession, config_manager: ConfigManager, parent=None):
        super().__init__(session, config_manager, show_group=True, header_key="all_points",
                         parent=parent)
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setWindowTitle("全ポイント一覧")
        self.resize(1400, 800)
        self.title_label.setText("全ポイント一覧")
        self.model.reload()
        self.status_label.setToolTip("グループ名をダブルクリックすると、ツリーでそのグループを選択します。")

    def _on_double_clicked(self, proxy_index: QModelIndex) -> None:
        src = self.table.proxy.mapToSource(proxy_index)
        if self.model.column_key(src.column()) == COL_GROUP:
            row = self.model.row_at(src.row())
            if row is not None:
                self.group_activated.emit(row.action_group_id)
            return
        super()._on_double_clicked(proxy_index)

    def closeEvent(self, event) -> None:
        self.save_header()
        super().closeEvent(event)
