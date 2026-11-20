"""メインウィンドウ

左にツリー（Connector → データソース／アクショングループ → アクション）、右にタブ
（概要／ポイント／スクリプト／データソース）を置く。スプリッターで幅を変えられ、
ウィンドウは自由にリサイズ・最大化できる。

- DB の読み取りはワーカースレッドで行い、進捗をステータスバーに表示する。
- 起動時は閲覧モード。ツールバーの「編集モード」を ON にしたときだけ編集できる
  （ポイント設定と、スクリプトタブのソース。変更指示002）。
- DB への書き込みは、すべて変更プレビュー（PreviewDialog / ScriptPreviewDialog）を通す。
  スクリプトの保存では、プレビューの前に文法チェックを行う。

外観スタイルは gui/styles.py の QSS に集約しており、本モジュールでは
objectName の設定のみ行う。
"""

from __future__ import annotations

import copy
import logging
import os
from datetime import datetime

from PySide6.QtCore import QByteArray, QModelIndex, QPersistentModelIndex, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QDesktopServices,
    QKeySequence,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QToolBar,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from core import APP_NAME, APP_TITLE, APP_VERSION
from core.change_executor import ScriptChange, validate_updated_by
from core.check_service import CHECKS
from core.compile_checker import STATUS_ERROR, STATUS_UNAVAILABLE, describe_compilers
from core.config_manager import AUTH_SQL, ConfigManager, load_password
from core.db import Database
from core.import_export import (
    ImportFormatError,
    build_export_rows,
    export_table,
    read_import_file,
)
from core.mi_repository import load_snapshot
from core.models import (
    ACTION_TYPE_SCRIPT,
    MIConfigSnapshot,
    format_jst,
    on_off,
    yes_no,
)
from core.point_service import ChangeSet, build_change_set, plan_import
from core.script_parser import ScriptRewriteError, build_script_xml, detect_source_newline
from gui import worker
from gui.all_points_view import AllPointsView
from gui.check_dialog import CheckResultDialog
from gui.connection_dialog import ConnectionDialog
from gui.datasource_tab import DataSourceTab
from gui.diff_dialog import DiffDialog, SimilarGroupsDialog
from gui.point_tab import EditSession, PointTab
from gui.preview_dialog import PreviewDialog, ScriptPreviewDialog, show_error
from gui.script_search_view import ScriptSearchView
from gui.script_tab import ScriptTab, ScriptWindow, script_file_name, write_cs_file
from gui.styles import COLORS, refresh_style

logger = logging.getLogger(__name__)

# ツリーのノード種別
KIND_ROLE = Qt.ItemDataRole.UserRole + 1
ID_ROLE = Qt.ItemDataRole.UserRole + 2
KIND_CONNECTOR = "connector"
KIND_DATASOURCE = "datasource"
KIND_GROUP = "group"
KIND_ACTION = "action"
KIND_EQUIPMENT = "equipment"

# タブ
TAB_OVERVIEW = 0
TAB_POINTS = 1
TAB_SCRIPT = 2
TAB_DATASOURCE = 3

OTHER_EQUIPMENT = "（その他）"


def _b64(data: QByteArray) -> str:
    return bytes(data.toBase64()).decode("ascii")


def _from_b64(text: str) -> QByteArray:
    return QByteArray.fromBase64(text.encode("ascii"))


class OverviewPanel(QScrollArea):
    """概要タブ（項目名と値の一覧）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("overviewArea")
        self.setWidgetResizable(True)
        content = QWidget(self)
        content.setObjectName("overviewContent")
        self._lay = QVBoxLayout(content)
        self._lay.setContentsMargins(12, 10, 12, 10)
        self._title = QLabel("", content)
        self._title.setObjectName("headerLabel")
        self._title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._lay.addWidget(self._title)
        self._form_host = QWidget(content)
        self._form = QFormLayout(self._form_host)
        self._form.setContentsMargins(0, 6, 0, 0)
        self._form.setHorizontalSpacing(16)
        self._lay.addWidget(self._form_host)
        self._lay.addStretch(1)
        self.setWidget(content)

    def set_items(self, title: str, items: list[tuple[str, str]]) -> None:
        self._title.setText(title)
        while self._form.rowCount():
            self._form.removeRow(0)
        for key, value in items:
            k = QLabel(key, self._form_host)
            k.setObjectName("overviewKey")
            v = QLabel("" if value is None else str(value), self._form_host)
            v.setObjectName("overviewValue")
            v.setWordWrap(True)
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self._form.addRow(k, v)


class MainWindow(QMainWindow):
    """アプリのメインウィンドウ。"""

    def __init__(self, config_manager: ConfigManager):
        super().__init__()
        self.config_manager = config_manager
        self.config = config_manager.config
        self.snapshot: MIConfigSnapshot | None = None
        self.session = EditSession(self)
        self.edit_mode = False
        self._loading = False
        self._load_handle = None
        self._current: tuple[str, int] | None = None   # ツリーで選択中のノード
        self._point_group_id: int | None = None        # ポイントタブに表示中のグループ
        self._item_index: dict[tuple[str, int], QStandardItem] = {}
        self._reverting_selection = False
        self._script_windows: list[ScriptWindow] = []
        self._all_points_view: AllPointsView | None = None
        self._search_view: ScriptSearchView | None = None
        # スナップショットを読んだ接続先（保存はこの接続先と現在の設定が一致するときのみ）
        self._loading_conn = None
        self._snapshot_conn = None
        # 保存に成功したが、まだ再読込できていない（表示が DB と食い違っている）。
        # ポイント設定とスクリプトで別々に持つ（一方を保存しても、他方の未保存の変更は保存できる）
        self._points_stale = False
        self._script_stale = False

        self.setWindowTitle(f"{APP_TITLE} ({APP_NAME})")
        self.resize(1400, 900)
        self.setMinimumSize(900, 600)

        self._build_actions()
        self._build_menu()
        self._build_toolbar()
        self._build_central()
        self._build_statusbar()
        self._restore_window_state()

        self.session.changes_count_changed.connect(self._on_changes_count)
        self._update_actions()
        self._update_status_bar()

    # ==================================================================
    # UI 構築
    # ==================================================================
    def _build_actions(self) -> None:
        self.act_connection = QAction("接続設定…", self)
        self.act_connection.triggered.connect(lambda: self._open_connection_dialog(reload_after=True))
        self.act_reload = QAction("再読込", self)
        self.act_reload.setShortcut(QKeySequence(Qt.Key.Key_F5))
        self.act_reload.triggered.connect(lambda: self.reload(confirm=True))
        self.act_export_scripts = QAction("スクリプトをすべて書き出し…", self)
        self.act_export_scripts.triggered.connect(self._export_all_scripts)
        self.act_export_points = QAction("ポイント設定のエクスポート…", self)
        self.act_export_points.triggered.connect(self._export_points)
        self.act_import_points = QAction("ポイント設定のインポート…", self)
        self.act_import_points.triggered.connect(self._import_points)
        self.act_quit = QAction("終了", self)
        self.act_quit.triggered.connect(self.close)

        self.act_all_points = QAction("全ポイント一覧", self)
        self.act_all_points.triggered.connect(self._open_all_points)
        self.act_script_search = QAction("スクリプト横断検索", self)
        self.act_script_search.setShortcut(QKeySequence("Ctrl+Shift+F"))
        self.act_script_search.triggered.connect(self._open_script_search)
        self.act_script_compare = QAction("スクリプト比較…", self)
        self.act_script_compare.triggered.connect(self._open_script_compare)
        self.act_group_by_equipment = QAction("設備ごとにまとめる", self)
        self.act_group_by_equipment.setCheckable(True)
        self.act_group_by_equipment.setChecked(self.config.window.group_by_equipment)
        self.act_group_by_equipment.toggled.connect(self._on_group_by_equipment)

        self.check_actions: list[QAction] = []
        for key, title, fn in CHECKS:
            act = QAction(title, self)
            act.triggered.connect(lambda checked=False, f=fn: self._run_check(f))
            self.check_actions.append(act)
        self.act_open_backup = QAction("退避フォルダを開く", self)
        self.act_open_backup.triggered.connect(lambda: self._open_folder(self.config_manager.backup_dir))
        self.act_open_log = QAction("ログフォルダを開く", self)
        self.act_open_log.triggered.connect(lambda: self._open_folder(self.config_manager.log_dir))

        self.act_about = QAction("バージョン情報", self)
        self.act_about.triggered.connect(self._show_about)

        # スクリプトタブの詳細サイドバー（ボタン・メニュー・ショートカットの状態を一致させる）
        self.act_script_sidebar = QAction("スクリプト詳細サイドバー", self)
        self.act_script_sidebar.setCheckable(True)
        self.act_script_sidebar.setChecked(True)
        self.act_script_sidebar.setShortcut(QKeySequence("Ctrl+Alt+B"))
        self.act_script_sidebar.toggled.connect(
            lambda checked: self.script_tab.set_sidebar_visible(checked))

        # ツールバー
        self.act_edit_mode = QAction("編集モード", self)
        self.act_edit_mode.setCheckable(True)
        self.act_edit_mode.setToolTip("ON にしたときだけポイント設定とスクリプトを編集できます")
        self.act_edit_mode.toggled.connect(self._on_edit_mode_toggled)
        self.act_save = QAction("保存", self)
        self.act_save.setShortcut(QKeySequence.StandardKey.Save)
        self.act_save.setToolTip("未保存の変更をプレビューしてから DB へ反映します (Ctrl+S)。"
                                 "スクリプトは保存前に文法チェックを行います")
        self.act_save.triggered.connect(lambda: self.save())
        self.act_undo = QAction("元に戻す", self)
        self.act_undo.setToolTip("未保存の変更をすべて破棄し、DB から読み込んだ内容に戻します")
        self.act_undo.triggered.connect(self._undo_all)

    def _build_menu(self) -> None:
        mb = self.menuBar()
        m_file = mb.addMenu("ファイル(&F)")
        m_file.addAction(self.act_connection)
        m_file.addAction(self.act_reload)
        m_file.addSeparator()
        m_file.addAction(self.act_export_scripts)
        m_file.addAction(self.act_export_points)
        m_file.addAction(self.act_import_points)
        m_file.addSeparator()
        m_file.addAction(self.act_quit)

        m_view = mb.addMenu("表示(&V)")
        m_view.addAction(self.act_all_points)
        m_view.addAction(self.act_script_search)
        m_view.addAction(self.act_script_compare)
        m_view.addSeparator()
        m_view.addAction(self.act_group_by_equipment)
        m_view.addAction(self.act_script_sidebar)

        m_tool = mb.addMenu("ツール(&T)")
        m_check = m_tool.addMenu("チェック")
        for act in self.check_actions:
            m_check.addAction(act)
        m_tool.addSeparator()
        m_tool.addAction(self.act_open_backup)
        m_tool.addAction(self.act_open_log)

        m_help = mb.addMenu("ヘルプ(&H)")
        m_help.addAction(self.act_about)

    def _build_toolbar(self) -> None:
        tb = QToolBar("操作", self)
        tb.setObjectName("toolBar")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        tb.addAction(self.act_reload)
        tb.addAction(self.act_edit_mode)
        tb.addAction(self.act_save)
        tb.addAction(self.act_undo)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb)

    def _build_central(self) -> None:
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)

        # --- 左: フィルタ + ツリー ---
        left = QWidget(self.splitter)
        llay = QVBoxLayout(left)
        llay.setContentsMargins(6, 6, 0, 6)
        llay.setSpacing(4)
        self.tree_filter = QLineEdit(left)
        self.tree_filter.setPlaceholderText("フィルタ（部分一致、スペース区切りで AND）")
        self.tree_filter.setClearButtonEnabled(True)
        llay.addWidget(self.tree_filter)
        self.tree_model = QStandardItemModel(self)
        self.tree = QTreeView(left)
        self.tree.setModel(self.tree_model)
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        llay.addWidget(self.tree, 1)
        self.splitter.addWidget(left)

        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(200)
        self._filter_timer.timeout.connect(self._rebuild_tree)
        self.tree_filter.textChanged.connect(lambda *_: self._filter_timer.start())
        self.tree.selectionModel().currentChanged.connect(self._on_tree_current_changed)

        # --- 右: タブ ---
        right = QWidget(self.splitter)
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(0, 6, 6, 6)
        self.tabs = QTabWidget(right)
        self.overview = OverviewPanel(self.tabs)
        self.point_tab = PointTab(self.session, self.config_manager, self.tabs)
        self.script_tab = ScriptTab(self.config_manager, self.tabs)
        self.datasource_tab = DataSourceTab(self.config_manager, self.tabs)
        self.tabs.addTab(self.overview, "概要")
        self.tabs.addTab(self.point_tab, "ポイント")
        self.tabs.addTab(self.script_tab, "スクリプト")
        self.tabs.addTab(self.datasource_tab, "データソース")
        rlay.addWidget(self.tabs)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([340, 1060])
        self.setCentralWidget(self.splitter)

        self.script_tab.open_window_requested.connect(self._open_script_window)
        self.script_tab.compare_requested.connect(self._open_similar_compare)
        self.script_tab.sidebar_toggled.connect(self._on_script_sidebar_toggled)
        self.script_tab.dirty_changed.connect(lambda *_: self._on_changes_count(0))
        self._set_visible_tabs([TAB_OVERVIEW])
        self.overview.set_items("Apriso MI設定ツール", [("状態", "データが読み込まれていません。")])

    def _build_statusbar(self) -> None:
        sb = self.statusBar()
        self.conn_label = QLabel("", self)
        self.mode_label = QLabel("", self)
        self.mode_label.setObjectName("editModeLabel")
        self.counts_label = QLabel("", self)
        self.unsaved_label = QLabel("", self)
        self.unsaved_label.setObjectName("unsavedLabel")
        self.message_label = QLabel("", self)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setMaximumWidth(180)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setVisible(False)
        sb.addWidget(self.conn_label)
        sb.addWidget(self.mode_label)
        sb.addWidget(self.counts_label)
        sb.addWidget(self.unsaved_label)
        sb.addWidget(self.message_label, 1)
        sb.addPermanentWidget(self.progress_bar)

    # ==================================================================
    # ウィンドウ状態
    # ==================================================================
    def _restore_window_state(self) -> None:
        w = self.config.window
        if w.geometry:
            try:
                self.restoreGeometry(_from_b64(w.geometry))
            except Exception:
                logger.warning("ウィンドウ位置の復元に失敗しました")
        if w.splitter:
            try:
                self.splitter.restoreState(_from_b64(w.splitter))
            except Exception:
                logger.warning("スプリッター位置の復元に失敗しました")
        # スクリプトタブの詳細サイドバー
        self.script_tab.set_sidebar_width(w.script_sidebar_width)
        self.script_tab.set_sidebar_visible(w.script_sidebar_visible)
        self._on_script_sidebar_toggled(self.script_tab.is_sidebar_visible())

    def _on_script_sidebar_toggled(self, visible: bool) -> None:
        """スクリプトタブ側で切り替えたときに、メニュー項目のチェック状態を合わせる。"""
        if self.act_script_sidebar.isChecked() != visible:
            self.act_script_sidebar.blockSignals(True)
            self.act_script_sidebar.setChecked(visible)
            self.act_script_sidebar.blockSignals(False)

    def _save_window_state(self) -> None:
        w = self.config.window
        w.geometry = _b64(self.saveGeometry())
        w.splitter = _b64(self.splitter.saveState())
        w.script_sidebar_visible = self.script_tab.is_sidebar_visible()
        w.script_sidebar_width = self.script_tab.sidebar_width()
        self.point_tab.save_header()
        self.datasource_tab.save_header()
        if self._all_points_view is not None:
            self._all_points_view.save_header()
        try:
            self.config_manager.save()
        except OSError:
            logger.exception("config.json の保存に失敗しました")

    def start(self) -> None:
        """表示後に呼ぶ。接続設定が未入力ならダイアログを開き、その後に読み込む。"""
        QTimer.singleShot(0, self._startup)

    def _startup(self) -> None:
        if not self.config_manager.is_connection_configured():
            if not self._open_connection_dialog(reload_after=False,
                                                message="接続設定を入力してください。"):
                self._set_message("接続設定が未入力です。「ファイル → 接続設定」から設定してください。")
                return
        self.reload(confirm=False)

    # ==================================================================
    # 接続・読み込み
    # ==================================================================
    def _make_database(self) -> Database:
        conn = copy.deepcopy(self.config.connection)
        password = load_password(conn.server, conn.username) if conn.auth == AUTH_SQL else ""
        return Database(conn, password)

    def _open_connection_dialog(self, reload_after: bool = True, message: str = "") -> bool:
        dlg = ConnectionDialog(self.config_manager, self, message=message)
        accepted = dlg.exec() == ConnectionDialog.DialogCode.Accepted
        self._update_status_bar()
        if accepted and reload_after:
            self.reload(confirm=True)
        return accepted

    def reload(self, confirm: bool = True) -> None:
        if self._loading:
            return
        if confirm and self._has_unsaved():
            if not self._confirm_discard("再読込すると、未保存の変更は破棄されます。"):
                return
        if not self.config_manager.is_connection_configured():
            self._open_connection_dialog(reload_after=True, message="接続設定を入力してください。")
            return
        self._loading = True
        self._loading_conn = copy.deepcopy(self.config.connection)
        self._apply_edit_state()
        self._update_actions()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(True)
        self._set_message("読み込みを開始しています…")
        self._load_handle = worker.run_in_thread(
            load_snapshot,
            self._make_database(),
            on_finished=self._on_loaded,
            on_failed=self._on_load_failed,
            on_progress=self._on_load_progress,
            progress_kw="progress",
        )

    def _on_load_progress(self, label: str, done: int, total: int) -> None:
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(done)
        self._set_message(f"読み込み中: {label}（{done} / {total}）")

    def _on_loaded(self, snapshot: MIConfigSnapshot) -> None:
        self._loading = False
        self.progress_bar.setVisible(False)
        self.snapshot = snapshot
        # スナップショットを読んだ接続先を記録する（保存時に接続先が変わっていないか確認する）
        self._snapshot_conn = self._loading_conn
        self._points_stale = False
        self._script_stale = False
        self.session.reset(snapshot)
        self._apply_edit_state()
        self.script_tab.set_snapshot(snapshot)
        self.datasource_tab.set_snapshot(snapshot)
        if self._search_view is not None:
            self._search_view.set_snapshot(snapshot)
        self._rebuild_tree()
        if self._current is None or not self._select_node(*self._current):
            self._select_root()
        self._set_message(f"読み込みが完了しました（{format_jst(snapshot.loaded_at)}）。")
        self._update_actions()
        self._update_status_bar()

    def _on_load_failed(self, error: worker.TaskError) -> None:
        self._loading = False
        self.progress_bar.setVisible(False)
        self._set_message("読み込みに失敗しました。")
        self._apply_edit_state()
        self._update_actions()
        show_error(self, "接続エラー",
                   f"DB からの読み込みに失敗しました。\n{error.message}\n\n接続設定を確認してください。",
                   error.traceback_text)
        # 接続に失敗した場合は接続設定ダイアログを自動で開く
        self._open_connection_dialog(reload_after=True,
                                     message="接続に失敗しました。接続設定を確認してください。")

    # ==================================================================
    # ツリー
    # ==================================================================
    def _rebuild_tree(self) -> None:
        self._reverting_selection = True
        try:
            self.tree_model.clear()
            self._item_index = {}
            snap = self.snapshot
            if snap is None:
                return
            tokens = [t.lower() for t in self.tree_filter.text().split() if t]
            filtering = bool(tokens)
            secondary = QBrush(QColor(COLORS["text_secondary"]))

            root = QStandardItem("Connector")
            root.setEditable(False)
            root.setData(KIND_CONNECTOR, KIND_ROLE)
            root.setData(0, ID_ROLE)
            self.tree_model.appendRow(root)
            self._item_index[(KIND_CONNECTOR, 0)] = root

            def matches(text: str) -> bool:
                low = text.lower()
                return all(t in low for t in tokens)

            for ds in sorted(snap.data_sources.values(), key=lambda d: d.alias.lower()):
                if filtering and not matches(ds.alias):
                    continue
                item = QStandardItem(ds.alias)
                item.setEditable(False)
                item.setData(KIND_DATASOURCE, KIND_ROLE)
                item.setData(ds.id, ID_ROLE)
                item.setToolTip(f"データソース {ds.alias}（{'有効' if ds.enabled else '無効'}）")
                if not ds.enabled:
                    item.setForeground(secondary)
                root.appendRow(item)
                self._item_index[(KIND_DATASOURCE, ds.id)] = item

            by_equipment = self.act_group_by_equipment.isChecked()
            equip_items: dict[str, QStandardItem] = {}
            for g in snap.groups_sorted():
                actions = snap.actions_of_group(g.id)
                text = " ".join([g.name] + [a.name for a in actions])
                if filtering and not matches(text):
                    continue
                parent = root
                if by_equipment:
                    key = g.equipment or OTHER_EQUIPMENT
                    parent = equip_items.get(key)
                    if parent is None:
                        parent = QStandardItem(key)
                        parent.setEditable(False)
                        parent.setData(KIND_EQUIPMENT, KIND_ROLE)
                        parent.setData(len(equip_items) + 1, ID_ROLE)
                        parent.setData(key, Qt.ItemDataRole.UserRole + 3)
                        equip_items[key] = parent
                        self._item_index[(KIND_EQUIPMENT, len(equip_items))] = parent
                g_item = QStandardItem(g.name)
                g_item.setEditable(False)
                g_item.setData(KIND_GROUP, KIND_ROLE)
                g_item.setData(g.id, ID_ROLE)
                tip = f"アクショングループ {g.name}"
                if not g.enabled:
                    g_item.setForeground(secondary)
                    tip += "（無効）"
                g_item.setToolTip(tip)
                for a in actions:
                    a_item = QStandardItem(a.name)
                    a_item.setEditable(False)
                    a_item.setData(KIND_ACTION, KIND_ROLE)
                    a_item.setData(a.id, ID_ROLE)
                    a_item.setToolTip(f"アクション {a.name}"
                                      + ("" if a.enabled else "（無効）"))
                    if not a.enabled:
                        a_item.setForeground(secondary)
                    g_item.appendRow(a_item)
                    self._item_index[(KIND_ACTION, a.id)] = a_item
                parent.appendRow(g_item)
                self._item_index[(KIND_GROUP, g.id)] = g_item

            if by_equipment:
                others = equip_items.pop(OTHER_EQUIPMENT, None)
                for key in sorted(equip_items, key=str.lower):
                    root.appendRow(equip_items[key])
                if others is not None:
                    root.appendRow(others)

            self.tree.expand(root.index())
            if filtering:
                self.tree.expandAll()
        finally:
            self._reverting_selection = False
        # 選択の復元
        if self._current is not None:
            item = self._item_index.get(self._current)
            if item is not None:
                self._reverting_selection = True
                try:
                    self.tree.setCurrentIndex(item.index())
                    self.tree.scrollTo(item.index())
                finally:
                    self._reverting_selection = False

    def _select_root(self) -> None:
        item = self._item_index.get((KIND_CONNECTOR, 0))
        if item is not None:
            self.tree.setCurrentIndex(item.index())
        else:
            self._show_node(None)

    def _select_node(self, kind: str, node_id: int) -> bool:
        """ツリーのノードを選択する（フィルタで隠れている場合はフィルタを解除する）。"""
        item = self._item_index.get((kind, node_id))
        if item is None and self.tree_filter.text():
            self.tree_filter.blockSignals(True)
            self.tree_filter.clear()
            self.tree_filter.blockSignals(False)
            self._rebuild_tree()
            item = self._item_index.get((kind, node_id))
        if item is None:
            return False
        index = item.index()
        if self.tree.currentIndex() == index:
            self._show_node((kind, node_id))
        else:
            self.tree.setCurrentIndex(index)
        self.tree.scrollTo(index)
        return self._current == (kind, node_id)

    @staticmethod
    def _node_of(index: QModelIndex) -> tuple[str, int] | None:
        if not index.isValid():
            return None
        kind = index.data(KIND_ROLE)
        node_id = index.data(ID_ROLE)
        if kind is None:
            return None
        return kind, int(node_id or 0)

    def _group_of_node(self, node: tuple[str, int] | None) -> int | None:
        if node is None or self.snapshot is None:
            return None
        kind, node_id = node
        if kind == KIND_GROUP:
            return node_id
        if kind == KIND_ACTION:
            a = self.snapshot.actions.get(node_id)
            return a.action_group_id if a else None
        return None

    def _on_tree_current_changed(self, current: QModelIndex, previous: QModelIndex) -> None:
        if self._reverting_selection:
            return
        node = self._node_of(current)
        new_group = self._group_of_node(node)
        old_group = self._point_group_id
        prev = QPersistentModelIndex(previous)
        # 保存直後の再読込中は、読み込み完了時に変更が破棄されるため確認しない
        if not self._loading and self.script_tab.is_dirty():
            switching, _new_action = self._script_switch_of_node(node)
            if switching and not self._resolve_unsaved_script():
                # キャンセル: 元の選択に戻す
                QTimer.singleShot(0, lambda: self._restore_selection(prev))
                return
        if (old_group is not None and new_group != old_group and not self._loading
                and self.session.group_has_changes(old_group)):
            if not self._resolve_unsaved_group(old_group):
                # キャンセル: 元の選択に戻す
                QTimer.singleShot(0, lambda: self._restore_selection(prev))
                return
        self._show_node(node)

    def _script_switch_of_node(self, node: tuple[str, int] | None) -> tuple[bool, int | None]:
        """そのノードを選ぶとスクリプトタブの表示が別のアクションに替わるか。

        グループ・アクション以外（データソース等）ではスクリプトタブの内容は替わらない。
        戻り値: (替わるか, 新しいアクション ID)
        """
        if node is None or self.snapshot is None:
            return False, None
        kind, node_id = node
        if kind == KIND_ACTION:
            new_action = node_id
        elif kind == KIND_GROUP:
            first = self.snapshot.first_script_action(node_id)
            new_action = first.id if first is not None else None
        else:
            return False, None
        return new_action != self.script_tab.current_action_id(), new_action

    def _resolve_unsaved_script(self) -> bool:
        """スクリプトの表示を替える前に、未保存の変更を確認する。続行してよければ True。"""
        action, group, _script = self.script_tab.edit_target()
        name = f"{group.name if group else '?'} / {action.name if action else '?'}"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("未保存の変更")
        box.setText(f"スクリプト「{name}」に未保存の変更があります。")
        box.setInformativeText("保存しますか？　「破棄」を選ぶと、スクリプトの変更を取り消して切り替えます。")
        save_btn = box.addButton("保存", QMessageBox.ButtonRole.AcceptRole)
        discard_btn = box.addButton("破棄", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is discard_btn:
            self.script_tab.revert_edits()
            return True
        if clicked is save_btn:
            return self._save_script()
        return False

    def _restore_selection(self, prev: QPersistentModelIndex) -> None:
        if not prev.isValid():
            return
        self._reverting_selection = True
        try:
            self.tree.setCurrentIndex(self.tree_model.index(prev.row(), prev.column(), prev.parent()))
        finally:
            self._reverting_selection = False

    def _resolve_unsaved_group(self, group_id: int) -> bool:
        """グループを切り替える前に未保存の変更を確認する。続行してよければ True。"""
        g = self.snapshot.action_groups.get(group_id) if self.snapshot else None
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("未保存の変更")
        box.setText(f"グループ「{g.name if g else group_id}」に未保存の変更があります。")
        box.setInformativeText("保存しますか？　「破棄」を選ぶと、このグループの変更を取り消して切り替えます。")
        save_btn = box.addButton("保存", QMessageBox.ButtonRole.AcceptRole)
        discard_btn = box.addButton("破棄", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is discard_btn:
            self.session.revert_group(group_id)
            return True
        if clicked is save_btn:
            return self._save_points()
        return False

    # ==================================================================
    # タブ
    # ==================================================================
    def _set_visible_tabs(self, visible: list[int]) -> None:
        for i in range(self.tabs.count()):
            self.tabs.setTabVisible(i, i in visible)
        if self.tabs.currentIndex() not in visible and visible:
            self.tabs.setCurrentIndex(visible[0])

    def _show_node(self, node: tuple[str, int] | None) -> None:
        self._current = node
        snap = self.snapshot
        if snap is None or node is None:
            self._set_visible_tabs([TAB_OVERVIEW])
            self.overview.set_items("Apriso MI設定ツール", [("状態", "データが読み込まれていません。")])
            return
        kind, node_id = node

        if kind == KIND_GROUP and node_id in snap.action_groups:
            g = snap.action_groups[node_id]
            self._show_group_overview(node_id)
            self._set_point_group(node_id)
            first = snap.first_script_action(node_id)
            if first is not None:
                self.script_tab.show_action(first.id)
            else:
                self.script_tab.clear("このグループにはアクションがありません。")
            self._set_visible_tabs([TAB_OVERVIEW, TAB_POINTS, TAB_SCRIPT])
            return

        if kind == KIND_ACTION and node_id in snap.actions:
            a = snap.actions[node_id]
            self._show_action_overview(node_id)
            self._set_point_group(a.action_group_id)
            self.script_tab.show_action(node_id)
            self._set_visible_tabs([TAB_OVERVIEW, TAB_SCRIPT])
            return

        if kind == KIND_DATASOURCE and node_id in snap.data_sources:
            self._set_point_group(None)
            self.datasource_tab.show_data_source(node_id)
            self._set_visible_tabs([TAB_DATASOURCE])
            return

        self._set_point_group(None)
        if kind == KIND_EQUIPMENT:
            item = self._item_index.get(node)
            key = item.data(Qt.ItemDataRole.UserRole + 3) if item is not None else ""
            groups = [g for g in snap.groups_sorted()
                      if (g.equipment or OTHER_EQUIPMENT) == key]
            self.overview.set_items(
                f"設備: {key}",
                [("グループ数", str(len(groups)))] + [(f"グループ {i + 1}", g.name)
                                                     for i, g in enumerate(groups)],
            )
        else:
            self._show_connector_overview()
        self._set_visible_tabs([TAB_OVERVIEW])

    def _set_point_group(self, group_id: int | None) -> None:
        self._point_group_id = group_id
        if self.point_tab.group_id != group_id:
            self.point_tab.set_group(group_id)

    def _show_connector_overview(self) -> None:
        snap = self.snapshot
        if snap is None:
            return
        n_scripts = len(snap.script_actions())
        inactive_rows = sum(1 for gp in snap.group_points if not gp.active)
        self.overview.set_items("Connector", [
            ("接続先", self.config.connection.display_name()),
            ("データソース", f"{len(snap.data_sources)} 件"),
            ("ポイントグループ", f"{len(snap.point_groups)} 件"),
            ("ポイント", f"{len(snap.points)} 件"),
            ("アクショングループ", f"{len(snap.action_groups)} 件"),
            ("アクション", f"{len(snap.actions)} 件"),
            ("スクリプトを持つアクション", f"{n_scripts} 件"),
            ("グループのポイント設定（有効）", f"{snap.active_group_point_count} 件"),
            ("グループのポイント設定（Active = 0）", f"{inactive_rows} 件"),
            ("読み込み日時 (JST)", format_jst(snap.loaded_at)),
        ])

    def _show_group_overview(self, group_id: int) -> None:
        snap = self.snapshot
        g = snap.action_groups[group_id]
        actions = snap.actions_of_group(group_id)
        rows = snap.group_points_by_group.get(group_id, [])
        n_active = sum(1 for r in rows if r.active)
        self.overview.set_items(f"アクショングループ: {g.name}", [
            ("名前", g.name),
            ("ID", str(g.id)),
            ("有効", yes_no(g.enabled)),
            ("設備", g.equipment or "（名前が規則に合わないため分解しない）"),
            ("処理名", g.process),
            ("コメント", g.comment or ""),
            ("起動時に実行", on_off(g.on_startup_execution)),
            ("終了時に実行", on_off(g.on_shutdown_execution)),
            ("定期実行", on_off(g.periodical_execution)),
            ("定期実行の間隔", "" if g.periodical_interval is None else str(g.periodical_interval)),
            ("ポイント変化で実行", on_off(g.point_change_execution)),
            ("ポイント変化の条件式", g.point_change_expression or ""),
            ("リフレッシュ時に実行", on_off(g.on_refresh_execution)),
            ("アクション", ", ".join(a.name for a in actions) or "なし"),
            ("ポイント設定", f"{n_active} 行（Active = 0 の行 {len(rows) - n_active} 行）"),
        ])

    def _show_action_overview(self, action_id: int) -> None:
        snap = self.snapshot
        a = snap.actions[action_id]
        g = snap.action_groups.get(a.action_group_id)
        s = snap.script_for_action(a)
        type_text = "" if a.action_type is None else str(a.action_type)
        if a.action_type == ACTION_TYPE_SCRIPT:
            type_text += "（スクリプト）"
        items = [
            ("名前", a.name),
            ("ID", str(a.id)),
            ("アクショングループ", g.name if g else ""),
            ("有効", yes_no(a.enabled)),
            ("シーケンス番号", "" if a.sequence_no is None else str(a.sequence_no)),
            ("ActionType", type_text),
            ("スクリプト ID", "" if a.script_id is None else str(a.script_id)),
        ]
        if s is not None:
            items += [
                ("作成者", s.author or ""),
                ("最終更新者", s.last_updated_by or ""),
                ("最終更新日時 (JST)", format_jst(s.last_update_on)),
            ]
        self.overview.set_items(f"アクション: {a.name}", items)

    # ==================================================================
    # 編集モード・保存
    # ==================================================================
    def _on_edit_mode_toggled(self, checked: bool) -> None:
        if checked == self.edit_mode:
            return
        if checked:
            answer = QMessageBox.warning(
                self, "編集モード",
                "編集モードにします。\n保存すると、接続先 "
                f"{self.config.connection.display_name()} の MI_ACTION_GROUP_POINT（ポイント設定）"
                "または SCRIPT（スクリプト）に書き込みます。\n"
                "よろしいですか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._set_edit_action_checked(False)
                return
        else:
            if self._has_unsaved():
                if not self._confirm_discard("閲覧モードに戻ると、未保存の変更は破棄されます。"):
                    self._set_edit_action_checked(True)
                    return
                self._revert_all_unsaved()
        self.edit_mode = checked
        self._apply_edit_state()
        self._update_actions()
        self._update_status_bar()

    def _can_edit(self) -> bool:
        """ポイント設定を表で編集できるか（編集モード ON、かつ読み込み中・保存後の再読込待ちでない）。"""
        return self.edit_mode and not self._loading and not self._points_stale

    def _can_edit_script(self) -> bool:
        """スクリプトを編集できるか（編集モード ON、かつ読み込み中・保存後の再読込待ちでない）。"""
        return self.edit_mode and not self._loading and not self._script_stale

    def _apply_edit_state(self) -> None:
        enabled = self._can_edit()
        self.point_tab.set_edit_mode(enabled)
        if self._all_points_view is not None:
            self._all_points_view.set_edit_mode(enabled)
        self.script_tab.set_edit_enabled(self._can_edit_script())

    # --- 未保存の変更（ポイント設定 + スクリプト） ------------------------------
    def _has_unsaved(self) -> bool:
        return self.session.has_changes() or self.script_tab.is_dirty()

    def _unsaved_description(self) -> str:
        parts = []
        n = self.session.change_count()
        if n:
            parts.append(f"ポイント設定の変更 {n} 件")
        if self.script_tab.is_dirty():
            action, group, _script = self.script_tab.edit_target()
            name = f"{group.name if group else '?'} / {action.name if action else '?'}"
            parts.append(f"スクリプト（{name}）の変更")
        return "、".join(parts) or "なし"

    def _revert_all_unsaved(self) -> None:
        self.session.revert_all()
        self.script_tab.revert_edits()

    def _set_edit_action_checked(self, checked: bool) -> None:
        self.act_edit_mode.blockSignals(True)
        self.act_edit_mode.setChecked(checked)
        self.act_edit_mode.blockSignals(False)

    def _confirm_discard(self, message: str, points_only: bool = False) -> bool:
        if points_only:
            desc = f"ポイント設定の変更 {self.session.change_count()} 件"
        else:
            desc = self._unsaved_description()
        answer = QMessageBox.question(
            self, "未保存の変更",
            f"未保存の変更があります（{desc}）。\n{message}\n破棄してよろしいですか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _undo_all(self) -> None:
        if not self._has_unsaved():
            return
        if self._confirm_discard("「元に戻す」で、すべての未保存の変更を取り消し、"
                                 "DB から読み込んだ内容に戻します。"):
            self._revert_all_unsaved()

    def save(self, reload_after: bool = True) -> bool:
        """未保存の変更（ポイント設定、スクリプト）をプレビューし、実行する。

        両方に未保存の変更がある場合は、ポイント設定 → スクリプトの順に保存する。
        すべて保存できたら True。
        """
        if not self.edit_mode or self.snapshot is None:
            return False
        has_points = self.session.has_changes()
        has_script = self.script_tab.is_dirty()
        if not has_points and not has_script:
            QMessageBox.information(self, "保存", "未保存の変更はありません。")
            return False
        ok = True
        if has_points:
            ok = self._save_points(reload_after=reload_after)
        if ok and has_script:
            ok = self._save_script(reload_after=reload_after)
        return ok

    def _save_points(self, reload_after: bool = True) -> bool:
        """ポイント設定の未保存の変更をプレビューし、実行する。保存に成功したら True。"""
        if not self.edit_mode or self.snapshot is None:
            return False
        rows = self.session.pending_rows()
        if not rows:
            return False
        cs = build_change_set(self.snapshot, rows, "PointEdit")
        return self._run_preview(cs, reload_after=reload_after)

    def _save_script(self, reload_after: bool = True) -> bool:
        """スクリプトタブの未保存の変更を、文法チェック → プレビュー → 実行の順に保存する。"""
        tab = self.script_tab
        if not self.edit_mode or self.snapshot is None or not tab.is_dirty():
            return False
        if not self._check_can_write(script=True):
            return False
        if not self._ensure_updated_by():
            return False
        # 更新者名の設定で接続設定ダイアログを開いた場合に備えて、もう一度確認する
        if not self._check_can_write(script=True):
            return False
        action, group, script = tab.edit_target()
        if action is None or script is None:
            return False

        # 1. 文法チェック（ワーカースレッドで実行し、終わるまで待つ）
        source = tab.current_source()
        check = tab.run_check_blocking()
        # チェックを待つ間に状態が変わっていないこと（編集・切り替え・再読込・編集モード OFF）
        if (not self.edit_mode or self._loading or self.snapshot is None
                or tab.edit_target()[2] is not script or tab.current_source() != source):
            QMessageBox.warning(self, "保存", "文法チェックの間に表示や内容が変わったため、保存を中止しました。"
                                "もう一度保存してください。")
            return False
        if check is None:
            QMessageBox.warning(self, "保存", "このスクリプトは文法チェックできないため、保存できません。")
            return False
        if check.status == STATUS_ERROR:
            tab.show_errors()
            if self.tabs.isTabVisible(TAB_SCRIPT):
                self.tabs.setCurrentIndex(TAB_SCRIPT)
            QMessageBox.warning(
                self, "保存",
                f"文法チェックでエラーが {len(check.errors)} 件見つかったため、保存を中止しました。\n"
                "エラー一覧を確認して修正してください。",
            )
            return False
        if check.status == STATUS_UNAVAILABLE:
            tab.show_errors()
            answer = QMessageBox.warning(
                self, "文法チェック",
                f"文法チェックを実行できませんでした。\n{check.reason}\n\nチェックなしで保存しますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False

        # 2. <Source> だけを置き換えた XML を組み立てる（読み直して確認する）
        try:
            new_xml = build_script_xml(script.script_xml, source)
        except ScriptRewriteError as e:
            show_error(self, "保存", f"スクリプト定義を組み立てられないため、保存を中止しました。\n{e}")
            return False
        change = ScriptChange(
            script_id=script.id,
            action_id=action.id,
            action_name=action.name,
            group_name=group.name if group else "?",
            original_xml=script.script_xml or "",
            new_xml=new_xml,
            original_source=script.parsed().source,
            new_source=source,
            newline=detect_source_newline(script.script_xml),
            loaded_last_update_on=script.last_update_on,
            loaded_last_updated_by=script.last_updated_by,
            check_summary=check.summary() if check.checked else "未実施（承認済み）",
        )
        if change.is_empty:
            QMessageBox.information(self, "保存", "DB の内容との差分はありません。")
            return False

        # 同じ SCRIPT を複数のアクションが参照している場合は、すべてに影響することを示す
        warnings: list[str] = []
        users = [a for a in self.snapshot.actions.values() if a.script_id == script.id]
        if len(users) > 1:
            names = []
            for a in users:
                g = self.snapshot.action_groups.get(a.action_group_id)
                names.append(f"{g.name if g else '?'} / {a.name}")
            warnings.append(f"この SCRIPT（ID={script.id}）は {len(users)} 件のアクションから参照されています。"
                            "保存するとすべてに反映されます: " + "、".join(names))

        # 3. プレビュー → 実行
        dlg = ScriptPreviewDialog(change, check, self._make_database(), self.config_manager, self,
                                  warnings=warnings)
        dlg.exec()
        result = dlg.execution_result
        if result is not None and result.success:
            # 保存済みの変更は未保存として残さない。再読込が終わるまでスクリプトを編集・保存させない
            self._script_stale = True
            tab.accept_saved(change.new_source)
            self._apply_edit_state()
            self._update_actions()
            self._update_status_bar()
            self._after_saved(reload_after)
            return True
        return False

    def _after_saved(self, reload_after: bool) -> None:
        """保存の後の再読込。別の種類の未保存の変更が残っている場合は、それを破棄しないよう再読込しない。"""
        if not reload_after:
            return
        if self._has_unsaved():
            self._set_message("保存しました。ほかに未保存の変更が残っているため、まだ再読込していません。"
                              "残りの変更を保存するか、再読込（F5）してください。")
            return
        # DB を再読込して表示を更新する（未保存の変更は保存済みのため破棄してよい）
        self.reload(confirm=False)

    def _ensure_updated_by(self) -> bool:
        err = validate_updated_by(self.config.updated_by)
        if not err:
            return True
        answer = QMessageBox.warning(
            self, "更新者名", f"{err}\n\n接続設定を開きますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._open_connection_dialog(reload_after=False)
        return not validate_updated_by(self.config.updated_by)

    def _check_can_write(self, script: bool = False) -> bool:
        """書き込み前の確認（接続先が読み込み時と同じか、保存後の再読込が済んでいるか）。

        script=True: スクリプトの保存（SCRIPT）、False: ポイント設定の保存（MI_ACTION_GROUP_POINT）。
        """
        if self._loading:
            QMessageBox.information(self, "保存", "DB から読み込み中です。完了してから保存してください。")
            return False
        if self._script_stale if script else self._points_stale:
            QMessageBox.warning(
                self, "保存",
                "前回の保存後に再読込できていないため、表示が DB の内容と一致していません。\n"
                "再読込（F5）してから編集・保存してください。",
            )
            return False
        if self._snapshot_conn is None or self._snapshot_conn != self.config.connection:
            QMessageBox.warning(
                self, "保存",
                "接続設定が、表示中のデータを読み込んだときから変更されています。\n"
                f"読み込み時: {self._snapshot_conn.display_name() if self._snapshot_conn else '-'}\n"
                f"現在の設定: {self.config.connection.display_name()}\n\n"
                "別の DB へ書き込まないよう、保存は行いません。再読込してから編集し直してください。",
            )
            return False
        return True

    def _run_preview(self, change_set: ChangeSet, reload_after: bool = True) -> bool:
        if not self._check_can_write():
            return False
        if not self._ensure_updated_by():
            return False
        # 更新者名の設定で接続設定ダイアログを開いた場合に備えて、もう一度確認する
        if not self._check_can_write():
            return False
        dlg = PreviewDialog(change_set, self._make_database(), self.config_manager, self)
        dlg.exec()
        result = dlg.execution_result
        if result is not None and result.success:
            # 保存済みの変更は未保存として残さない。再読込が終わるまで編集・保存させない
            self._points_stale = True
            self.session.revert_all()
            self._apply_edit_state()
            self._update_actions()
            self._after_saved(reload_after)
            return True
        return False

    # ==================================================================
    # 入出力
    # ==================================================================
    def _choose_export_scope(self) -> list[int] | None | bool:
        """戻り値: グループ ID のリスト / None（全グループ）/ False（キャンセル）。"""
        group_id = self._point_group_id
        if group_id is None or self.snapshot is None or group_id not in self.snapshot.action_groups:
            return None
        g = self.snapshot.action_groups[group_id]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("ポイント設定のエクスポート")
        box.setText("エクスポートする範囲を選んでください。")
        cur_btn = box.addButton(f"現在のグループ（{g.name}）", QMessageBox.ButtonRole.AcceptRole)
        all_btn = box.addButton("全グループ", QMessageBox.ButtonRole.AcceptRole)
        cancel_btn = box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cur_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cur_btn:
            return [group_id]
        if clicked is all_btn:
            return None
        return False

    def _export_points(self) -> None:
        snap = self.snapshot
        if snap is None:
            return
        scope = self._choose_export_scope()
        if scope is False:
            return
        if scope:
            base = snap.action_groups[scope[0]].name
        else:
            base = "全グループ"
        paths = self.config.paths
        start = paths.last_export_dir or os.path.expanduser("~")
        default = os.path.join(start, f"ポイント設定_{base}_{datetime.now():%Y%m%d}.xlsx")
        path, selected = QFileDialog.getSaveFileName(
            self, "ポイント設定のエクスポート", default,
            "Excel ファイル (*.xlsx);;CSV ファイル（UTF-8 BOM 付き） (*.csv)",
        )
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        if ext not in (".xlsx", ".csv"):
            path += ".csv" if "csv" in selected.lower() else ".xlsx"
        try:
            rows = build_export_rows(snap, scope)
            export_table(path, rows)
        except Exception as e:
            logger.exception("エクスポートに失敗しました")
            show_error(self, "エクスポート", f"書き出しに失敗しました。\n{e}")
            return
        paths.last_export_dir = os.path.dirname(path)
        note = "\n\n※ 未保存の変更は含まれません（DB の内容を書き出しました）。" if self.session.has_changes() else ""
        QMessageBox.information(self, "エクスポート", f"{len(rows)} 行を書き出しました。\n{path}{note}")

    def _import_points(self) -> None:
        snap = self.snapshot
        if snap is None:
            return
        if not self.edit_mode:
            QMessageBox.information(self, "インポート",
                                    "インポートは編集モードで行います。ツールバーの「編集モード」を ON にしてください。")
            return
        if self.session.has_changes():
            if not self._confirm_discard("インポートは DB の内容と突き合わせるため、"
                                         "ポイント設定の未保存の変更は破棄されます。",
                                         points_only=True):
                return
            self.session.revert_all()
        paths = self.config.paths
        start = paths.last_export_dir or os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, "ポイント設定のインポート", start,
            "Excel / CSV ファイル (*.xlsx *.xlsm *.csv);;すべてのファイル (*.*)",
        )
        if not path:
            return
        try:
            data = read_import_file(path)
        except ImportFormatError as e:
            QMessageBox.warning(self, "インポート", f"ファイルの形式が正しくありません。\n{e}")
            return
        except Exception as e:
            logger.exception("インポートファイルの読み込みに失敗しました")
            show_error(self, "インポート", f"ファイルを読み込めませんでした。\n{e}")
            return
        paths.last_export_dir = os.path.dirname(path)

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("インポート")
        box.setText(f"{len(data.rows)} 行を読み込みました。")
        box.setInformativeText(
            "ファイルにない行を削除しますか？\n"
            "（削除する場合も、対象はファイルに含まれるグループの行のみです）"
        )
        keep_btn = box.addButton("削除しない", QMessageBox.ButtonRole.AcceptRole)
        delete_btn = box.addButton("削除する", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel_btn or clicked is None:
            return
        delete_missing = clicked is delete_btn

        rows, excluded = plan_import(snap, data.rows, delete_missing)
        cs = build_change_set(snap, rows, "Import", extra_excluded=excluded)
        if cs.is_empty and not cs.excluded:
            QMessageBox.information(self, "インポート", "DB の内容との差分はありません。")
            return
        self._run_preview(cs)

    def _export_all_scripts(self) -> None:
        snap = self.snapshot
        if snap is None:
            return
        paths = self.config.paths
        folder = QFileDialog.getExistingDirectory(
            self, "スクリプトの書き出し先フォルダ", paths.last_export_dir or os.path.expanduser("~")
        )
        if not folder:
            return
        used: set[str] = set()
        count = 0
        errors: list[str] = []
        for g, a, s in snap.script_actions():
            name = script_file_name(g.name, a.name)
            stem, ext = os.path.splitext(name)
            n = 2
            while name.lower() in used:
                name = f"{stem}_{n}{ext}"
                n += 1
            used.add(name.lower())
            try:
                write_cs_file(os.path.join(folder, name), s.source)
                count += 1
            except OSError as e:
                errors.append(f"{name}: {e}")
        paths.last_export_dir = folder
        if errors:
            show_error(self, "スクリプトの書き出し",
                       f"{count} 件を書き出しました。{len(errors)} 件は失敗しました。",
                       "\n".join(errors))
        else:
            QMessageBox.information(self, "スクリプトの書き出し", f"{count} 件を書き出しました。\n{folder}")

    # ==================================================================
    # 表示メニュー・ツール
    # ==================================================================
    def _open_all_points(self) -> None:
        if self.snapshot is None:
            return
        if self._all_points_view is None:
            view = AllPointsView(self.session, self.config_manager, self)
            view.set_edit_mode(self._can_edit())
            view.group_activated.connect(lambda gid: self._activate_node(KIND_GROUP, gid, TAB_POINTS))
            self._all_points_view = view
        self._all_points_view.show()
        self._all_points_view.raise_()
        self._all_points_view.activateWindow()

    def _open_script_search(self) -> None:
        if self._search_view is None:
            view = ScriptSearchView(self)
            view.set_snapshot(self.snapshot)
            view.open_script_requested.connect(self._on_open_script_line)
            self._search_view = view
        self._search_view.show()
        self._search_view.raise_()
        self._search_view.activateWindow()
        self._search_view.focus_query()

    def _on_open_script_line(self, action_id: int, line: int) -> None:
        if self._activate_node(KIND_ACTION, action_id, TAB_SCRIPT):
            # タブが表示されてから行へ移動する（非表示のままだとスクロール位置が合わない）
            QTimer.singleShot(0, lambda: self.script_tab.goto_line(line))

    def _activate_node(self, kind: str, node_id: int, tab: int) -> bool:
        ok = self._select_node(kind, node_id)
        if ok:
            self.tabs.setCurrentIndex(tab)
            self.raise_()
            self.activateWindow()
        return ok

    def _current_action_id(self) -> int | None:
        if self._current is None or self.snapshot is None:
            return None
        kind, node_id = self._current
        if kind == KIND_ACTION:
            return node_id
        if kind == KIND_GROUP:
            a = self.snapshot.first_script_action(node_id)
            return a.id if a else None
        return None

    def _open_script_compare(self) -> None:
        if self.snapshot is None:
            return
        dlg = DiffDialog(self.snapshot, self.config_manager, left_action_id=self._current_action_id(),
                         parent=self)
        dlg.show()

    def _open_similar_compare(self, action_id: int) -> None:
        if self.snapshot is None or action_id not in self.snapshot.actions:
            return
        dlg = SimilarGroupsDialog(self.snapshot, self.config_manager, action_id, self)
        dlg.show()

    def _open_script_window(self, action_id: int) -> None:
        snap = self.snapshot
        if snap is None:
            return
        a = snap.actions.get(action_id)
        s = snap.script_for_action(a)
        if a is None or s is None:
            return
        g = snap.action_groups.get(a.action_group_id)
        win = ScriptWindow(self.config_manager, g.name if g else "?", a.name, s.source, script=s)
        self._script_windows.append(win)
        win.destroyed.connect(lambda *args, w=win: self._forget_script_window(w))
        win.show()

    def _forget_script_window(self, win: ScriptWindow) -> None:
        try:
            self._script_windows.remove(win)
        except ValueError:
            pass

    def _on_group_by_equipment(self, checked: bool) -> None:
        self.config.window.group_by_equipment = checked
        self._rebuild_tree()

    def _run_check(self, fn) -> None:
        snap = self.snapshot
        if snap is None:
            return
        try:
            result = fn(snap)
        except Exception as e:
            logger.exception("チェックに失敗しました")
            import traceback

            show_error(self, "チェック", f"チェックに失敗しました。\n{e}", traceback.format_exc())
            return
        if self.session.has_changes():
            result.summary += "（未保存の変更は含まれません）"
        dlg = CheckResultDialog(result, self.config_manager, self)
        dlg.show()

    def _open_folder(self, path: str) -> None:
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "フォルダを開く", f"フォルダを作成できませんでした。\n{e}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _show_about(self) -> None:
        try:
            compilers = describe_compilers(self.config.compiler.csc_path)
        except Exception as e:  # 検出に失敗してもバージョン情報は表示する
            logger.exception("C# コンパイラの検出に失敗しました")
            compilers = f"使用する C# コンパイラ: 検出に失敗しました（{e}）"
        QMessageBox.about(
            self, "バージョン情報",
            f"{APP_TITLE} ({APP_NAME})\nバージョン {APP_VERSION}\n\n"
            "DELMIA Apriso の Machine Integrator 設定を閲覧・編集するツールです。\n"
            "アクショングループのポイント設定と、スクリプトのソースを編集できます"
            "（スクリプトは保存前に文法チェックを行います）。\n\n"
            f"{compilers}",
        )

    # ==================================================================
    # 状態表示
    # ==================================================================
    def _set_message(self, text: str) -> None:
        self.message_label.setText(text)

    def _on_changes_count(self, count: int) -> None:
        self._update_actions()
        self._update_status_bar()

    def _update_actions(self) -> None:
        loaded = self.snapshot is not None
        busy = self._loading
        has_changes = self.session.has_changes()
        script_dirty = self.script_tab.is_dirty()
        self.act_reload.setEnabled(not busy)
        self.act_connection.setEnabled(not busy)
        self.act_edit_mode.setEnabled(loaded and not busy)
        self.act_save.setEnabled(loaded and ((self._can_edit() and has_changes)
                                             or (self._can_edit_script() and script_dirty)))
        self.act_undo.setEnabled((self._can_edit() and has_changes)
                                 or (self._can_edit_script() and script_dirty))
        for act in (self.act_export_scripts, self.act_export_points, self.act_all_points,
                    self.act_script_compare, *self.check_actions):
            act.setEnabled(loaded and not busy)
        self.act_import_points.setEnabled(loaded and self._can_edit())

    def _update_status_bar(self) -> None:
        self.conn_label.setText(f"接続: {self.config.connection.display_name()}")
        self.setWindowTitle(f"{APP_TITLE} ({APP_NAME}) - {self.config.connection.display_name()}")
        self.mode_label.setText("編集モード" if self.edit_mode else "閲覧モード")
        self.mode_label.setProperty("editMode", "true" if self.edit_mode else "false")
        refresh_style(self.mode_label)
        snap = self.snapshot
        if snap is not None:
            self.counts_label.setText(
                f"グループ {len(snap.action_groups)} / ポイント {snap.active_group_point_count}"
            )
        else:
            self.counts_label.setText("")
        n = self.session.change_count()
        script_dirty = self.script_tab.is_dirty()
        parts = []
        if n:
            parts.append(f"未保存の変更 {n} 件")
        if script_dirty:
            parts.append("スクリプト未保存")
        self.unsaved_label.setText(" / ".join(parts))
        self.unsaved_label.setProperty("hasChanges", "true" if parts else "false")
        refresh_style(self.unsaved_label)

    # ==================================================================
    # 終了
    # ==================================================================
    def closeEvent(self, event) -> None:
        if self._loading:
            QMessageBox.information(self, "終了", "DB から読み込み中です。完了してから終了してください。")
            event.ignore()
            return
        if self.script_tab.is_check_running():
            QMessageBox.information(self, "終了", "文法チェックの実行中です。完了してから終了してください。")
            event.ignore()
            return
        if self._has_unsaved():
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("未保存の変更")
            box.setText(f"未保存の変更があります（{self._unsaved_description()}）。")
            box.setInformativeText("終了する前に保存しますか？")
            save_btn = box.addButton("保存", QMessageBox.ButtonRole.AcceptRole)
            discard_btn = box.addButton("破棄して終了", QMessageBox.ButtonRole.DestructiveRole)
            cancel_btn = box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(cancel_btn)
            box.exec()
            clicked = box.clickedButton()
            if clicked is save_btn:
                if not self.save(reload_after=False):
                    event.ignore()
                    return
            elif clicked is not discard_btn:
                event.ignore()
                return

        self._save_window_state()
        for win in list(self._script_windows):
            win.close()
        if self._all_points_view is not None:
            self._all_points_view.close()
        if self._search_view is not None:
            self._search_view.close()
        worker.wait_all(5000)
        event.accept()
