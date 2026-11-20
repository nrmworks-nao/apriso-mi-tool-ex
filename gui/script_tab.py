"""スクリプトタブ（閲覧のみ）

- `<Source>` の内容を読み取り専用のコードビューアで表示する。
- コードビューアの右側の詳細サイドバーに、入力変数（標準5変数は薄く表示）、
  呼び出しオペレーション、作成者・最終更新者・最終更新日時（JST）を縦に並べて表示する。
  サイドバーは見出しバーの「詳細」ボタン（Ctrl+Alt+B）で表示／非表示を切り替える。
- 「別ウィンドウで開く」で、スクリプトだけの独立ウィンドウ（最大化可能）を開く。
- 「ファイルに保存」で .cs（UTF-8、改行 CRLF）として保存する。

第1版では DB への書き戻しは行わない。
"""

from __future__ import annotations

import os
import re

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.config_manager import ConfigManager
from core.models import Action, ActionGroup, MIConfigSnapshot, Script, format_jst
from gui.code_viewer import CodeViewerPanel
from gui.styles import COLORS

_INVALID_FILENAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def script_file_name(group_name: str, action_name: str) -> str:
    """書き出しファイル名「<グループ名>__<アクション名>.cs」（使えない文字は _ に置換）。"""
    name = f"{group_name}__{action_name}"
    name = _INVALID_FILENAME_RE.sub("_", name).strip(" .")
    return (name or "script") + ".cs"


def write_cs_file(path: str, source: str) -> None:
    """UTF-8、改行 CRLF で書き出す。"""
    text = source.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


class CollapsibleSection(QWidget):
    """見出しボタンで開閉できるパネル。"""

    def __init__(self, title: str, content: QWidget, expanded: bool = True, parent=None):
        super().__init__(parent)
        self._title = title
        self.button = QToolButton(self)
        self.button.setObjectName("collapseButton")
        self.button.setCheckable(True)
        self.button.setChecked(expanded)
        self.button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.content = content
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.button)
        lay.addWidget(content)
        self.button.toggled.connect(self._on_toggled)
        self._on_toggled(expanded)

    def _on_toggled(self, expanded: bool) -> None:
        self.button.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.button.setText(self._title)
        self.content.setVisible(expanded)


class ScriptInfoPanel(QWidget):
    """詳細サイドバー（入力変数・呼び出しオペレーション・更新情報を縦に並べる）。

    各セクションは CollapsibleSection で個別に折りたためる。
    入力変数の表が残りの高さを使い切る（折りたたんだときは末尾の余白が伸びる）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("scriptSidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        self._lay = lay

        # 入力変数
        self.inputs_table = QTableWidget(0, 2, self)
        self.inputs_table.setHorizontalHeaderLabels(["名前", "型"])
        self.inputs_table.verticalHeader().setVisible(False)
        self.inputs_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.inputs_table.setAlternatingRowColors(True)
        self.inputs_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.inputs_table.horizontalHeader().setStretchLastSection(True)
        self.inputs_table.setColumnWidth(0, 150)
        self.inputs_section = CollapsibleSection("入力変数", self.inputs_table, expanded=True,
                                                 parent=self)
        lay.addWidget(self.inputs_section, 1)

        # 呼び出しオペレーション
        self.ops_list = QListWidget(self)
        self.ops_section = CollapsibleSection("呼び出しオペレーション", self.ops_list,
                                              expanded=True, parent=self)
        lay.addWidget(self.ops_section, 0)

        # 更新情報
        form_host = QWidget(self)
        form = QFormLayout(form_host)
        form.setContentsMargins(4, 4, 4, 4)
        # 幅が狭くても崩れないよう、長い行は項目名の下に値を折り返す
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.author_label = QLabel("", form_host)
        self.updated_by_label = QLabel("", form_host)
        self.updated_on_label = QLabel("", form_host)
        self.language_label = QLabel("", form_host)
        self.usings_label = QLabel("", form_host)
        for lbl in (self.author_label, self.updated_by_label, self.updated_on_label,
                    self.language_label, self.usings_label):
            lbl.setWordWrap(True)
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("作成者:", self.author_label)
        form.addRow("最終更新者:", self.updated_by_label)
        form.addRow("最終更新日時 (JST):", self.updated_on_label)
        form.addRow("言語:", self.language_label)
        form.addRow("using:", self.usings_label)
        self.info_section = CollapsibleSection("更新情報", form_host, expanded=True, parent=self)
        lay.addWidget(self.info_section, 0)

        # 入力変数を折りたたんだときに残りの高さを受け持つ余白
        lay.addStretch(0)
        self._stretch_index = lay.count() - 1
        self.inputs_section.button.toggled.connect(self._update_stretch)
        self._update_stretch(self.inputs_section.button.isChecked())

    def _update_stretch(self, inputs_expanded: bool) -> None:
        self._lay.setStretch(0, 1 if inputs_expanded else 0)
        self._lay.setStretch(self._stretch_index, 0 if inputs_expanded else 1)

    def clear(self) -> None:
        self.inputs_table.setRowCount(0)
        self.ops_list.clear()
        for lbl in (self.author_label, self.updated_by_label, self.updated_on_label,
                    self.language_label, self.usings_label):
            lbl.setText("")

    def show_script(self, script: Script) -> None:
        ps = script.parsed()
        secondary = QBrush(QColor(COLORS["text_secondary"]))
        self.inputs_table.setRowCount(len(ps.inputs))
        for r, var in enumerate(ps.inputs):
            name_item = QTableWidgetItem(var.name)
            type_item = QTableWidgetItem(var.type)
            if var.is_standard:
                # MI 標準の変数（ポイントではない）は薄く表示する
                for it in (name_item, type_item):
                    it.setForeground(secondary)
                    it.setToolTip("MI 標準の入力変数（ポイントではありません）")
            self.inputs_table.setItem(r, 0, name_item)
            self.inputs_table.setItem(r, 1, type_item)
        self.ops_list.clear()
        self.ops_list.addItems(ps.operations)
        self.author_label.setText(script.author or "")
        self.updated_by_label.setText(script.last_updated_by or "")
        self.updated_on_label.setText(format_jst(script.last_update_on))
        self.language_label.setText(ps.language or script.language or "")
        self.usings_label.setText(ps.usings)


SIDEBAR_DEFAULT_WIDTH = 320
SIDEBAR_MIN_WIDTH = 220
SIDEBAR_SHORTCUT = "Ctrl+Alt+B"


class ScriptCodeArea(QWidget):
    """コードビューア（左）と詳細サイドバー（右）を水平の QSplitter に並べる。

    - ウィンドウ幅を変えたときはコードビューア側だけが伸び縮みする。
    - サイドバーはスプリッターで幅 0 まで潰せない。非表示は set_sidebar_visible() で行い、
      再表示すると非表示にする前の幅に戻る。
    """

    sidebar_visibility_changed = Signal(bool)

    def __init__(self, font_family: str, font_size: int, word_wrap: bool,
                 sidebar_visible: bool = True, sidebar_width: int = SIDEBAR_DEFAULT_WIDTH,
                 parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.panel = CodeViewerPanel(self.splitter, font_family=font_family,
                                     font_size=font_size, word_wrap=word_wrap)
        self.info = ScriptInfoPanel(self.splitter)
        self.info.setMinimumWidth(SIDEBAR_MIN_WIDTH)
        self.splitter.addWidget(self.panel)
        self.splitter.addWidget(self.info)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setCollapsible(1, False)
        lay.addWidget(self.splitter, 1)

        self._sidebar_width = max(SIDEBAR_MIN_WIDTH, int(sidebar_width or SIDEBAR_DEFAULT_WIDTH))
        self._width_pending = True   # 表示されたときにサイドバーの幅を適用する
        self.info.setVisible(sidebar_visible)
        self.splitter.splitterMoved.connect(self._on_splitter_moved)

    # --- 状態 ----------------------------------------------------------
    def is_sidebar_visible(self) -> bool:
        # タブ自体が非表示でも判定できるよう、明示的に隠したかどうかで判定する
        return not self.info.isHidden()

    def set_sidebar_visible(self, visible: bool) -> None:
        if visible == self.is_sidebar_visible():
            return
        if visible:
            self.info.show()
            self._width_pending = True
            self._apply_width()
        else:
            self._remember_width()
            self.info.hide()
        self.sidebar_visibility_changed.emit(visible)

    def sidebar_width(self) -> int:
        """サイドバーの幅（非表示中は非表示にする前の幅）。"""
        if self.is_sidebar_visible() and not self._width_pending:
            self._remember_width()
        return self._sidebar_width

    def set_sidebar_width(self, width: int) -> None:
        self._sidebar_width = max(SIDEBAR_MIN_WIDTH, int(width or SIDEBAR_DEFAULT_WIDTH))
        self._width_pending = True
        self._apply_width()

    # --- 幅の管理 --------------------------------------------------------
    def _on_splitter_moved(self, pos: int, index: int) -> None:
        self._remember_width()

    def _remember_width(self) -> None:
        sizes = self.splitter.sizes()
        if len(sizes) == 2 and sizes[1] > 0 and self.splitter.width() > 0:
            self._sidebar_width = max(SIDEBAR_MIN_WIDTH, sizes[1])

    def _apply_width(self) -> None:
        if not self._width_pending or not self.is_sidebar_visible() or not self.isVisible():
            return
        total = sum(self.splitter.sizes())
        if total <= 0 or self.splitter.width() <= 0:
            return
        width = min(self._sidebar_width, max(SIDEBAR_MIN_WIDTH, total - 100))
        self.splitter.setSizes([max(0, total - width), width])
        self._width_pending = False

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # レイアウトが確定してから幅を適用する
        QTimer.singleShot(0, self._apply_width)


def make_sidebar_toggle_button(parent: QWidget) -> QPushButton:
    """見出しバー右端の「詳細」切り替えボタン（押されている状態 = 表示中）。"""
    btn = QPushButton("詳細", parent)
    btn.setObjectName("sidebarToggle")
    btn.setCheckable(True)
    btn.setToolTip(f"スクリプト詳細サイドバーの表示／非表示 ({SIDEBAR_SHORTCUT})")
    return btn


class ScriptTab(QWidget):
    """スクリプトタブ。"""

    open_window_requested = Signal(int)   # action_id
    compare_requested = Signal(int)       # action_id
    sidebar_toggled = Signal(bool)        # 詳細サイドバーの表示状態が変わった

    def __init__(self, config_manager: ConfigManager, parent=None):
        super().__init__(parent)
        self.config_manager = config_manager
        vcfg = config_manager.config.viewer
        self._snapshot: MIConfigSnapshot | None = None
        self._action: Action | None = None
        self._group: ActionGroup | None = None
        self._script: Script | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        # 見出しと操作ボタン
        bar = QWidget(self)
        bar.setObjectName("panelToolBar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(6, 4, 6, 4)
        self.title_label = QLabel("", bar)
        self.title_label.setObjectName("headerLabel")
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bar_lay.addWidget(self.title_label, 1)
        self.wrap_cb = QCheckBox("折り返し", bar)
        self.wrap_cb.setChecked(vcfg.word_wrap)
        bar_lay.addWidget(self.wrap_cb)
        self.window_btn = QPushButton("別ウィンドウで開く", bar)
        bar_lay.addWidget(self.window_btn)
        self.save_btn = QPushButton("ファイルに保存…", bar)
        bar_lay.addWidget(self.save_btn)
        self.compare_btn = QPushButton("同種グループと比較…", bar)
        bar_lay.addWidget(self.compare_btn)
        self.sidebar_btn = make_sidebar_toggle_button(bar)
        bar_lay.addWidget(self.sidebar_btn)
        lay.addWidget(bar)

        self.message_label = QLabel("", self)
        self.message_label.setObjectName("secondaryLabel")
        lay.addWidget(self.message_label)

        # コードビューア（左）+ 詳細サイドバー（右）
        # ショートカット Ctrl+Alt+B はメインウィンドウの表示メニューの項目が受け持つ
        self.area = ScriptCodeArea(vcfg.font_family, vcfg.font_size, vcfg.word_wrap,
                                   sidebar_visible=True, sidebar_width=SIDEBAR_DEFAULT_WIDTH,
                                   parent=self)
        self.panel = self.area.panel
        self.info = self.area.info
        lay.addWidget(self.area, 1)
        self.sidebar_btn.setChecked(self.area.is_sidebar_visible())

        self.sidebar_btn.toggled.connect(self.set_sidebar_visible)
        self.area.sidebar_visibility_changed.connect(self._on_sidebar_visibility_changed)
        self.window_btn.clicked.connect(self._on_open_window)
        self.save_btn.clicked.connect(self._on_save)
        self.compare_btn.clicked.connect(self._on_compare)
        self.wrap_cb.toggled.connect(self._on_wrap)
        self.clear("ツリーでアクションを選択してください。")

    # ------------------------------------------------------------------
    def set_snapshot(self, snapshot: MIConfigSnapshot | None) -> None:
        self._snapshot = snapshot
        if snapshot is None or self._action is None or self._action.id not in snapshot.actions:
            self.clear("ツリーでアクションを選択してください。")
        else:
            # 再読込後は新しいスナップショットの内容で表示し直す
            self.show_action(self._action.id, force=True)

    def current_action_id(self) -> int | None:
        return self._action.id if self._action else None

    def clear(self, message: str = "") -> None:
        self._action = None
        self._group = None
        self._script = None
        self.title_label.setText("スクリプト")
        self.message_label.setText(message)
        self.message_label.setVisible(bool(message))
        self.panel.set_code("")
        self.info.clear()
        self._update_buttons()

    def show_action(self, action_id: int, force: bool = False) -> None:
        snap = self._snapshot
        if snap is None:
            return
        action = snap.actions.get(action_id)
        if action is None:
            self.clear("アクションが見つかりません。")
            return
        if (not force and self._action is not None and self._action.id == action_id
                and self._script is not None):
            return  # 同じスクリプトを表示中（スクロール位置を保つ）
        group = snap.action_groups.get(action.action_group_id)
        script = snap.script_for_action(action)
        self._action = action
        self._group = group
        self._script = script
        self.title_label.setText(f"{group.name if group else '?'}  /  {action.name}")
        if script is None:
            self.message_label.setText("このアクションにはスクリプトがありません"
                                       f"（ActionType = {action.action_type}）。")
            self.message_label.setVisible(True)
            self.panel.set_code("")
            self.info.clear()
        else:
            ps = script.parsed()
            if ps.error:
                self.message_label.setText(ps.error)
                self.message_label.setVisible(True)
            else:
                self.message_label.setVisible(False)
            self.panel.set_code(ps.source)
            self.info.show_script(script)
        self._update_buttons()

    def goto_line(self, line: int) -> None:
        self.panel.viewer.goto_line(line)

    # --- 詳細サイドバー --------------------------------------------------
    def is_sidebar_visible(self) -> bool:
        return self.area.is_sidebar_visible()

    def set_sidebar_visible(self, visible: bool) -> None:
        self.area.set_sidebar_visible(bool(visible))
        self._sync_sidebar_button()

    def toggle_sidebar(self) -> None:
        self.set_sidebar_visible(not self.is_sidebar_visible())

    def sidebar_width(self) -> int:
        return self.area.sidebar_width()

    def set_sidebar_width(self, width: int) -> None:
        self.area.set_sidebar_width(width)

    def _sync_sidebar_button(self) -> None:
        visible = self.area.is_sidebar_visible()
        if self.sidebar_btn.isChecked() != visible:
            self.sidebar_btn.blockSignals(True)
            self.sidebar_btn.setChecked(visible)
            self.sidebar_btn.blockSignals(False)

    def _on_sidebar_visibility_changed(self, visible: bool) -> None:
        self._sync_sidebar_button()
        self.sidebar_toggled.emit(visible)

    def _update_buttons(self) -> None:
        has_script = self._script is not None
        self.window_btn.setEnabled(has_script)
        self.save_btn.setEnabled(has_script)
        self.compare_btn.setEnabled(has_script)

    # ------------------------------------------------------------------
    def _on_open_window(self) -> None:
        if self._action is not None:
            self.open_window_requested.emit(self._action.id)

    def _on_compare(self) -> None:
        if self._action is not None:
            self.compare_requested.emit(self._action.id)

    def _on_wrap(self, enabled: bool) -> None:
        self.panel.viewer.set_word_wrap(enabled)
        self.config_manager.config.viewer.word_wrap = enabled

    def _on_save(self) -> None:
        if self._script is None or self._action is None:
            return
        save_script_dialog(self, self.config_manager,
                           self._group.name if self._group else "group",
                           self._action.name, self._script.source)


def save_script_dialog(parent: QWidget, config_manager: ConfigManager, group_name: str,
                       action_name: str, source: str) -> None:
    """「ファイルに保存」ダイアログを開いて .cs を保存する。"""
    paths = config_manager.config.paths
    start_dir = paths.last_export_dir or os.path.expanduser("~")
    default = os.path.join(start_dir, script_file_name(group_name, action_name))
    path, _ = QFileDialog.getSaveFileName(parent, "スクリプトを保存", default,
                                          "C# ファイル (*.cs);;すべてのファイル (*.*)")
    if not path:
        return
    try:
        write_cs_file(path, source)
    except OSError as e:
        QMessageBox.critical(parent, "保存エラー", f"ファイルを保存できませんでした。\n{e}")
        return
    paths.last_export_dir = os.path.dirname(path)


class ScriptWindow(QMainWindow):
    """スクリプトだけの独立ウィンドウ（複数同時に開ける）。

    詳細サイドバーの初期状態は非表示（コードを広く見るためのウィンドウのため）。
    切り替えはこのウィンドウだけに作用し、設定には保存しない。
    """

    def __init__(self, config_manager: ConfigManager, group_name: str, action_name: str,
                 source: str, parent=None, script: Script | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle(f"{group_name} / {action_name}")
        self.resize(1100, 800)
        self._config_manager = config_manager
        self._group_name = group_name
        self._action_name = action_name
        self._source = source
        vcfg = config_manager.config.viewer

        central = QWidget(self)
        lay = QVBoxLayout(central)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        bar = QWidget(central)
        bar.setObjectName("panelToolBar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(6, 4, 6, 4)
        title = QLabel(f"{group_name}  /  {action_name}", bar)
        title.setObjectName("headerLabel")
        bar_lay.addWidget(title, 1)
        wrap_cb = QCheckBox("折り返し", bar)
        wrap_cb.setChecked(vcfg.word_wrap)
        bar_lay.addWidget(wrap_cb)
        save_btn = QPushButton("ファイルに保存…", bar)
        bar_lay.addWidget(save_btn)
        self.sidebar_btn = make_sidebar_toggle_button(bar)
        bar_lay.addWidget(self.sidebar_btn)
        lay.addWidget(bar)

        self.area = ScriptCodeArea(vcfg.font_family, vcfg.font_size, vcfg.word_wrap,
                                   sidebar_visible=False, sidebar_width=SIDEBAR_DEFAULT_WIDTH,
                                   parent=central)
        self.panel = self.area.panel
        self.panel.set_code(source)
        if script is not None:
            self.area.info.show_script(script)
        lay.addWidget(self.area, 1)
        self.setCentralWidget(central)
        self.sidebar_btn.setChecked(self.area.is_sidebar_visible())

        # このウィンドウだけに作用するショートカット
        self._sidebar_shortcut = QShortcut(QKeySequence(SIDEBAR_SHORTCUT), self)
        self._sidebar_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._sidebar_shortcut.activated.connect(self._toggle_sidebar)

        wrap_cb.toggled.connect(self.panel.viewer.set_word_wrap)
        save_btn.clicked.connect(self._on_save)
        self.sidebar_btn.toggled.connect(self.area.set_sidebar_visible)
        self.area.sidebar_visibility_changed.connect(self._sync_sidebar_button)

    def _toggle_sidebar(self) -> None:
        self.area.set_sidebar_visible(not self.area.is_sidebar_visible())

    def _sync_sidebar_button(self, visible: bool) -> None:
        if self.sidebar_btn.isChecked() != visible:
            self.sidebar_btn.blockSignals(True)
            self.sidebar_btn.setChecked(visible)
            self.sidebar_btn.blockSignals(False)

    def _on_save(self) -> None:
        save_script_dialog(self, self._config_manager, self._group_name, self._action_name,
                           self._source)
