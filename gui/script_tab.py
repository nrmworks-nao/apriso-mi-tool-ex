"""スクリプトタブ（閲覧のみ）

- `<Source>` の内容を読み取り専用のコードビューアで表示する。
- 下部の折りたたみパネルに、入力変数（標準5変数は薄く表示）、呼び出しオペレーション、
  作成者・最終更新者・最終更新日時（JST）を表示する。
- 「別ウィンドウで開く」で、スクリプトだけの独立ウィンドウ（最大化可能）を開く。
- 「ファイルに保存」で .cs（UTF-8、改行 CRLF）として保存する。

第1版では DB への書き戻しは行わない。
"""

from __future__ import annotations

import os
import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
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
    """入力変数・呼び出しオペレーション・作成者等の表示。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(8)

        # 入力変数
        left = QVBoxLayout()
        left.addWidget(QLabel("入力変数", self))
        self.inputs_table = QTableWidget(0, 2, self)
        self.inputs_table.setHorizontalHeaderLabels(["名前", "型"])
        self.inputs_table.verticalHeader().setVisible(False)
        self.inputs_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.inputs_table.setAlternatingRowColors(True)
        self.inputs_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.inputs_table.horizontalHeader().setStretchLastSection(True)
        self.inputs_table.setColumnWidth(0, 200)
        left.addWidget(self.inputs_table)
        lay.addLayout(left, 3)

        # 呼び出しオペレーション
        mid = QVBoxLayout()
        mid.addWidget(QLabel("呼び出しオペレーション", self))
        self.ops_list = QListWidget(self)
        mid.addWidget(self.ops_list)
        lay.addLayout(mid, 2)

        # 作成者等
        right = QVBoxLayout()
        right.addWidget(QLabel("更新情報", self))
        form_host = QWidget(self)
        form = QFormLayout(form_host)
        form.setContentsMargins(0, 0, 0, 0)
        self.author_label = QLabel("", form_host)
        self.updated_by_label = QLabel("", form_host)
        self.updated_on_label = QLabel("", form_host)
        self.language_label = QLabel("", form_host)
        self.usings_label = QLabel("", form_host)
        self.usings_label.setWordWrap(True)
        for lbl in (self.author_label, self.updated_by_label, self.updated_on_label,
                    self.language_label, self.usings_label):
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("作成者:", self.author_label)
        form.addRow("最終更新者:", self.updated_by_label)
        form.addRow("最終更新日時 (JST):", self.updated_on_label)
        form.addRow("言語:", self.language_label)
        form.addRow("using:", self.usings_label)
        right.addWidget(form_host)
        right.addStretch(1)
        lay.addLayout(right, 2)

        self.setMinimumHeight(150)
        self.setMaximumHeight(240)

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


class ScriptTab(QWidget):
    """スクリプトタブ。"""

    open_window_requested = Signal(int)   # action_id
    compare_requested = Signal(int)       # action_id

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
        lay.addWidget(bar)

        self.message_label = QLabel("", self)
        self.message_label.setObjectName("secondaryLabel")
        lay.addWidget(self.message_label)

        self.panel = CodeViewerPanel(self, font_family=vcfg.font_family,
                                     font_size=vcfg.font_size, word_wrap=vcfg.word_wrap)
        lay.addWidget(self.panel, 1)

        self.info = ScriptInfoPanel(self)
        self.section = CollapsibleSection("詳細（入力変数・呼び出しオペレーション・更新情報）",
                                          self.info, expanded=True, parent=self)
        lay.addWidget(self.section)

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
    """スクリプトだけの独立ウィンドウ（複数同時に開ける）。"""

    def __init__(self, config_manager: ConfigManager, group_name: str, action_name: str,
                 source: str, parent=None):
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
        lay.addWidget(bar)

        self.panel = CodeViewerPanel(central, font_family=vcfg.font_family,
                                     font_size=vcfg.font_size, word_wrap=vcfg.word_wrap)
        self.panel.set_code(source)
        lay.addWidget(self.panel, 1)
        self.setCentralWidget(central)

        wrap_cb.toggled.connect(self.panel.viewer.set_word_wrap)
        save_btn.clicked.connect(self._on_save)

    def _on_save(self) -> None:
        save_script_dialog(self, self._config_manager, self._group_name, self._action_name,
                           self._source)
