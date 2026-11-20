"""スクリプトタブ

- `<Source>` の内容をコードビューアで表示する。編集モードが ON のときだけ、メインウィンドウの
  スクリプトタブのビューアで `<Source>`（C# ソース）を編集できる（変更指示002）。
  入力変数・呼び出しオペレーション・using などは編集対象にしない。
  未保存の変更があるときは見出しのタイトル末尾に「●」を付ける。保存（DB への書き戻し）は
  メインウィンドウのツールバーの「保存」から行う。
- 「文法チェック」（F7）で、C# コンパイラによるチェックをワーカースレッドで実行する。
  結果はビューアの下の「エラー一覧」パネルに表示し、エラー行には行番号エリアに印を付ける。
- コードビューアの右側の詳細サイドバーに、入力変数（標準5変数は薄く表示）、
  呼び出しオペレーション、作成者・最終更新者・最終更新日時（JST）を縦に並べて表示する。
  サイドバーは見出しバーの「詳細」ボタン（Ctrl+Alt+B）で表示／非表示を切り替える。
- 「別ウィンドウで開く」で、スクリプトだけの独立ウィンドウ（最大化可能）を開く。
  別ウィンドウは常に読み取り専用。
- 「ファイルに保存」で .cs（UTF-8、改行 CRLF）として保存する（表示中の内容）。
"""

from __future__ import annotations

import os
import re

from PySide6.QtCore import QEventLoop, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.compile_checker import (
    COMPILE_NOTICE,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    CompileResult,
    check_source,
    unavailable,
)
from core.config_manager import ConfigManager
from core.models import Action, ActionGroup, MIConfigSnapshot, Script, format_jst
from core.script_parser import ScriptRewriteError, find_source_span
from gui import worker
from gui.code_viewer import MARKER_ERROR, MARKER_WARNING, CodeViewerPanel
from gui.styles import COLORS, refresh_style

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


class _BusyDialog(QDialog):
    """処理の完了まで閉じられない待機ダイアログ（Esc・閉じるボタンでも閉じない）。"""

    def __init__(self, title: str, text: str, parent=None):
        super().__init__(parent)
        self._finished = False
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.CustomizeWindowHint
                            | Qt.WindowType.WindowTitleHint)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(text, self))
        bar = QProgressBar(self)
        bar.setRange(0, 0)
        lay.addWidget(bar)
        self.setMinimumWidth(360)

    def finish(self) -> None:
        self._finished = True
        self.close()

    def reject(self) -> None:
        if self._finished:
            super().reject()

    def closeEvent(self, event) -> None:
        if not self._finished:
            event.ignore()
            return
        super().closeEvent(event)


_QWIDGETSIZE_MAX = 16777215
_ERROR_COLUMNS = ["種別", "行", "桁", "コード", "メッセージ"]
_LINE_ROLE = Qt.ItemDataRole.UserRole + 1
ERROR_PANEL_DEFAULT_HEIGHT = 200


class CompileErrorPanel(QWidget):
    """ビューアの下の、折りたたみ可能な「エラー一覧」パネル（文法チェックの結果）。

    行をダブルクリックすると該当行へ移動する（スクリプト内の診断のみ）。
    パネルの下部には、チェックの限界についての注意書きを常に表示する。
    """

    line_activated = Signal(int)      # スクリプトの行番号（1 始まり）
    expanded_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("compileErrorPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.table = QTableWidget(0, len(_ERROR_COLUMNS), self)
        self.table.setHorizontalHeaderLabels(_ERROR_COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        for col, width in enumerate((90, 60, 50, 80)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(col, width)
        self.section = CollapsibleSection("エラー一覧", self.table, expanded=False, parent=self)
        lay.addWidget(self.section, 1)

        self.summary_label = QLabel("", self)
        self.summary_label.setObjectName("compileSummary")
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary_label.setVisible(False)
        lay.addWidget(self.summary_label)

        self.notice_label = QLabel(COMPILE_NOTICE, self)
        self.notice_label.setObjectName("compileNotice")
        self.notice_label.setWordWrap(True)
        lay.addWidget(self.notice_label)

        self.table.cellDoubleClicked.connect(self._on_double_clicked)
        self.section.button.toggled.connect(self._on_toggled)
        self._on_toggled(False)

    # ------------------------------------------------------------------
    def is_expanded(self) -> bool:
        return self.section.button.isChecked()

    def set_expanded(self, expanded: bool) -> None:
        if self.section.button.isChecked() != expanded:
            self.section.button.setChecked(expanded)

    def _collapsed_height(self) -> int:
        h = self.section.button.sizeHint().height() + self.notice_label.sizeHint().height()
        if not self.summary_label.isHidden():
            h += self.summary_label.heightForWidth(max(200, self.width())) \
                if self.summary_label.hasHeightForWidth() else self.summary_label.sizeHint().height()
        return h + 4

    def _on_toggled(self, expanded: bool) -> None:
        if expanded:
            self.setMaximumHeight(_QWIDGETSIZE_MAX)
        else:
            # 折りたたみ時は見出しと注意書きの高さだけにする（スプリッターの残りはビューアへ）
            self.setMaximumHeight(self._collapsed_height())
        self.expanded_changed.emit(expanded)

    def _set_title(self, count: int | None) -> None:
        title = "エラー一覧" if count is None else f"エラー一覧（{count}）"
        self.section._title = title
        self.section.button.setText(title)

    def _set_summary(self, text: str, state: str) -> None:
        self.summary_label.setText(text)
        self.summary_label.setProperty("state", state)
        refresh_style(self.summary_label)
        self.summary_label.setVisible(bool(text))
        if not self.is_expanded():
            self.setMaximumHeight(self._collapsed_height())

    # ------------------------------------------------------------------
    def clear(self) -> None:
        self.table.setRowCount(0)
        self._set_title(None)
        self._set_summary("", "")

    def set_running(self) -> None:
        self._set_summary("文法チェックを実行しています…", "")

    def set_result(self, result: CompileResult | None, stale: bool = False) -> None:
        if result is None:
            self.clear()
            return
        diags = sorted(result.diagnostics,
                       key=lambda d: (not d.in_script, not d.is_error, d.line, d.col))
        self.table.setRowCount(len(diags))
        error_color = QBrush(QColor(COLORS["error"]))
        secondary = QBrush(QColor(COLORS["text_secondary"]))
        for r, d in enumerate(diags):
            if d.in_script:
                message = d.message
                tip = d.message
                line_text, col_text = str(d.line), str(d.col)
            else:
                tip = (f"定義の生成に起因する{('エラー' if d.is_error else '警告')}です"
                       f"（チェック用に生成したコードの {d.line} 行 {d.col} 桁）。スクリプトの行ではありません。")
                message = f"［定義の生成に起因］ {d.message}"
                line_text = col_text = ""
            items = [QTableWidgetItem(d.kind_label), QTableWidgetItem(line_text),
                     QTableWidgetItem(col_text), QTableWidgetItem(d.code), QTableWidgetItem(message)]
            for c, it in enumerate(items):
                it.setData(_LINE_ROLE, d.line if d.in_script else 0)
                it.setToolTip(tip)
                if c in (1, 2):
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if d.is_error:
                    if c == 0:
                        it.setForeground(error_color)
                elif not d.in_script:
                    it.setForeground(secondary)
                self.table.setItem(r, c, it)
        self._set_title(len(result.errors))

        if result.status == STATUS_UNAVAILABLE:
            self._set_summary(f"チェックできませんでした: {result.reason}", "unavailable")
            return
        text = f"文法チェック: {result.summary()}"
        if result.wrapper_errors:
            text += ("\n［定義の生成に起因］のエラーは、入力変数・呼び出しオペレーションの定義から"
                     "生成したチェック用コードで起きたものです（スクリプトの行番号ではありません）。")
        if stale:
            text += "\n（チェックした後にソースが編集されています。もう一度チェックしてください）"
        self._set_summary(text, "error" if result.status == STATUS_ERROR else "ok")

    def _on_double_clicked(self, row: int, column: int) -> None:
        item = self.table.item(row, 0)
        line = int(item.data(_LINE_ROLE) or 0) if item is not None else 0
        if line > 0:
            self.line_activated.emit(line)


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
    dirty_changed = Signal(bool)          # 未保存の変更の有無が変わった

    def __init__(self, config_manager: ConfigManager, parent=None):
        super().__init__(parent)
        self.config_manager = config_manager
        vcfg = config_manager.config.viewer
        self._snapshot: MIConfigSnapshot | None = None
        self._action: Action | None = None
        self._group: ActionGroup | None = None
        self._script: Script | None = None
        self._base_message = ""

        # 編集の状態
        self._edit_enabled = False        # 編集モード ON（かつ読み込み中・保存後の再読込待ちでない）
        self._edit_block_reason = ""      # このスクリプトを編集できない理由
        self._baseline_text = ""          # DB から読み込んだ内容（ビューアに入れた直後の値）
        self._last_text = ""
        self._dirty = False
        self._setting_code = False

        # 文法チェックの状態
        self._check_seq = 0               # 実行ごとに増やす（古い結果を捨てるため）
        self._check_running = False
        self._check_result: CompileResult | None = None
        self._checked_text: str | None = None

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
        self.check_status = QLabel("", bar)
        self.check_status.setObjectName("checkStatus")
        bar_lay.addWidget(self.check_status)
        self.check_btn = QPushButton("文法チェック", bar)
        self.check_btn.setToolTip("C# コンパイラで文法をチェックします (F7)")
        bar_lay.addWidget(self.check_btn)
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

        # コードビューア（左）+ 詳細サイドバー（右）、その下にエラー一覧
        # ショートカット Ctrl+Alt+B はメインウィンドウの表示メニューの項目が受け持つ
        self.vsplit = QSplitter(Qt.Orientation.Vertical, self)
        self.area = ScriptCodeArea(vcfg.font_family, vcfg.font_size, vcfg.word_wrap,
                                   sidebar_visible=True, sidebar_width=SIDEBAR_DEFAULT_WIDTH,
                                   parent=self.vsplit)
        self.panel = self.area.panel
        self.info = self.area.info
        self.errors = CompileErrorPanel(self.vsplit)
        self.vsplit.addWidget(self.area)
        self.vsplit.addWidget(self.errors)
        self.vsplit.setStretchFactor(0, 1)
        self.vsplit.setStretchFactor(1, 0)
        self.vsplit.setCollapsible(0, False)
        self.vsplit.setCollapsible(1, False)
        lay.addWidget(self.vsplit, 1)
        self.sidebar_btn.setChecked(self.area.is_sidebar_visible())

        # 文法チェック（F7）。タブが表示されているときだけ働く
        self._check_shortcut = QShortcut(QKeySequence(Qt.Key.Key_F7), self)
        self._check_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._check_shortcut.activated.connect(self._on_check_shortcut)

        self.sidebar_btn.toggled.connect(self.set_sidebar_visible)
        self.area.sidebar_visibility_changed.connect(self._on_sidebar_visibility_changed)
        self.window_btn.clicked.connect(self._on_open_window)
        self.save_btn.clicked.connect(self._on_save)
        self.compare_btn.clicked.connect(self._on_compare)
        self.check_btn.clicked.connect(self.run_check)
        self.wrap_cb.toggled.connect(self._on_wrap)
        self.panel.viewer.textChanged.connect(self._on_text_changed)
        self.errors.line_activated.connect(self.goto_line)
        self.errors.expanded_changed.connect(self._on_errors_expanded)
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
        """表示を空にする（未保存の変更は破棄される。呼び出し側で確認すること）。"""
        self._action = None
        self._group = None
        self._script = None
        self._edit_block_reason = ""
        self._base_message = message
        self._load_code("")
        self.info.clear()
        self._update_title()
        self._update_editable()
        self._update_buttons()

    def show_action(self, action_id: int, force: bool = False) -> None:
        """アクションのスクリプトを表示する（未保存の変更は破棄される。呼び出し側で確認すること）。"""
        snap = self._snapshot
        if snap is None:
            return
        action = snap.actions.get(action_id)
        if action is None:
            self.clear("アクションが見つかりません。")
            return
        if (not force and self._action is not None and self._action.id == action_id
                and self._script is not None):
            return  # 同じスクリプトを表示中（スクロール位置と編集内容を保つ）
        group = snap.action_groups.get(action.action_group_id)
        script = snap.script_for_action(action)
        self._action = action
        self._group = group
        self._script = script
        self._edit_block_reason = ""
        if script is None:
            self._base_message = ("このアクションにはスクリプトがありません"
                                  f"（ActionType = {action.action_type}）。")
            self._load_code("")
            self.info.clear()
        else:
            ps = script.parsed()
            self._base_message = ps.error
            if ps.error:
                self._edit_block_reason = "スクリプト定義を解析できないため、編集できません。"
            else:
                try:
                    find_source_span(script.script_xml)
                except ScriptRewriteError as e:
                    self._edit_block_reason = f"{e}（編集できません）"
            self._load_code(ps.source)
            self.info.show_script(script)
        self._update_title()
        self._update_editable()
        self._update_buttons()

    def goto_line(self, line: int) -> None:
        self.panel.viewer.goto_line(line)

    # --- 表示内容と未保存の変更 ------------------------------------------------
    def _load_code(self, text: str) -> None:
        """ビューアの内容を置き換え、未保存の変更と文法チェックの結果をリセットする。"""
        self._setting_code = True
        try:
            self.panel.set_code(text)
        finally:
            self._setting_code = False
        self._baseline_text = self._last_text = self.panel.viewer.plain_text()
        self._set_dirty(False)
        self._reset_check()

    def _on_text_changed(self) -> None:
        if self._setting_code:
            return
        text = self.panel.viewer.plain_text()
        if text == self._last_text:
            return  # 書式の更新など、内容が変わっていない
        self._last_text = text
        self._set_dirty(text != self._baseline_text)
        self._invalidate_check()

    def _set_dirty(self, dirty: bool) -> None:
        if dirty == self._dirty:
            return
        self._dirty = dirty
        self._update_title()
        self.dirty_changed.emit(dirty)

    def _update_title(self) -> None:
        if self._action is None:
            self.title_label.setText("スクリプト")
            return
        text = f"{self._group.name if self._group else '?'}  /  {self._action.name}"
        if self._dirty:
            text += "  ●"
        self.title_label.setText(text)
        self.title_label.setToolTip("未保存の変更があります" if self._dirty else "")

    def _update_message(self) -> None:
        parts = [self._base_message] if self._base_message else []
        if self._edit_enabled and self._script is not None and self._edit_block_reason:
            parts.append(self._edit_block_reason)
        text = "\n".join(parts)
        self.message_label.setText(text)
        self.message_label.setVisible(bool(text))

    def is_dirty(self) -> bool:
        """DB から読み込んだ内容から変更されているか。"""
        return self._dirty

    def current_source(self) -> str:
        """ビューアの内容（改行は \\n）。"""
        return self.panel.viewer.plain_text()

    def edit_target(self) -> tuple[Action | None, ActionGroup | None, Script | None]:
        return self._action, self._group, self._script

    def revert_edits(self) -> None:
        """DB から読み込んだ内容に戻す。"""
        if self._script is None or not self._dirty:
            return
        bar = self.panel.viewer.verticalScrollBar()
        pos = bar.value()
        self._load_code(self._script.parsed().source)
        QTimer.singleShot(0, lambda: bar.setValue(pos))

    def accept_saved(self, saved_source: str) -> None:
        """保存に成功した内容を基準にする（再読込で DB の内容に置き換わるまでの間）。

        保存した内容と表示中の内容が違う場合は、未保存の変更として残す。
        """
        self._baseline_text = saved_source
        self._last_text = self.current_source()
        self._set_dirty(self._last_text != self._baseline_text)

    def is_check_running(self) -> bool:
        return self._check_running

    # --- 編集の可否 ----------------------------------------------------------
    def set_edit_enabled(self, enabled: bool) -> None:
        """編集モード（メインウィンドウが判断）に合わせて、ビューアを編集可能にする。

        編集不可にしても、未保存の変更はそのまま残す（破棄は呼び出し側で行う）。
        """
        self._edit_enabled = bool(enabled)
        self._update_editable()

    def is_editable(self) -> bool:
        return self.panel.viewer.is_editable()

    def _update_editable(self) -> None:
        can = self._edit_enabled and self._script is not None and not self._edit_block_reason
        self.panel.viewer.set_editable(can)
        self._update_message()

    # --- 文法チェック -------------------------------------------------------
    def can_check(self) -> bool:
        return self._script is not None and not self._script.parsed().error

    def check_result(self) -> CompileResult | None:
        """現在の内容に対する文法チェックの結果（未チェック・チェック後に編集した場合は None）。"""
        if self._check_result is None or self._checked_text != self.current_source():
            return None
        return self._check_result

    def show_errors(self) -> None:
        self.errors.set_expanded(True)

    def _on_check_shortcut(self) -> None:
        if self.isVisible():
            self.run_check()

    def _set_check_status(self, state: str, text: str) -> None:
        self.check_status.setText(text)
        self.check_status.setProperty("state", state)
        refresh_style(self.check_status)

    def _reset_check(self) -> None:
        self._check_seq += 1            # 実行中のチェックの結果は捨てる
        self._check_running = False
        self._check_result = None
        self._checked_text = None
        self.errors.clear()
        self.panel.viewer.set_line_markers(None)
        self._set_check_status("unchecked", "文法チェック: 未チェック" if self._script else "")
        self._update_buttons()

    def _invalidate_check(self) -> None:
        """ソースを編集したら「未チェック」に戻す（一覧は残し、行の印は消す）。"""
        if self._check_running:
            return  # 実行中の結果が届いたときに、編集されたかどうかを判定する
        if self._check_result is not None:
            self.errors.set_result(self._check_result, stale=True)
        self.panel.viewer.set_line_markers(None)
        self._set_check_status("unchecked", "文法チェック: 未チェック")

    def _begin_check(self) -> int:
        self._check_seq += 1
        self._check_running = True
        self._set_check_status("running", "文法チェック: 実行中…")
        self.errors.set_running()
        self._update_buttons()
        return self._check_seq

    def _check_args(self, text: str) -> tuple:
        cfg = self.config_manager.config.compiler
        return self._script.script_xml, text, cfg.csc_path, cfg.timeout_seconds

    def run_check(self) -> None:
        """文法チェックをワーカースレッドで実行する（結果は画面に表示する）。"""
        if not self.can_check() or self._check_running:
            return
        text = self.current_source()
        seq = self._begin_check()
        worker.run_in_thread(
            check_source, *self._check_args(text),
            on_finished=lambda r, s=seq, t=text: self._finish_check(s, t, r),
            on_failed=lambda e, s=seq, t=text: self._finish_check(
                s, t, unavailable(f"文法チェックの実行中にエラーが発生しました: {e.message}")),
        )

    def run_check_blocking(self) -> CompileResult | None:
        """保存前の文法チェック。ワーカースレッドで実行し、終わるまで待って結果を返す。

        待っている間は進捗ダイアログを表示し、ソースを編集できないようにする。
        """
        if not self.can_check():
            return None
        text = self.current_source()
        seq = self._begin_check()
        holder: dict[str, CompileResult] = {}
        loop = QEventLoop(self)

        def done(result: CompileResult) -> None:
            holder["result"] = result
            loop.quit()

        def failed(error) -> None:
            holder["result"] = unavailable(f"文法チェックの実行中にエラーが発生しました: {error.message}")
            loop.quit()

        # 閉じられない待機ダイアログ（閉じられると、チェック中にソースを編集できてしまうため）
        progress = _BusyDialog("文法チェック", "文法チェック（C# コンパイラ）を実行しています…", self)
        progress.show()
        worker.run_in_thread(check_source, *self._check_args(text),
                             on_finished=done, on_failed=failed)
        if "result" not in holder:
            loop.exec()
        progress.finish()
        progress.deleteLater()
        result = holder["result"]
        self._finish_check(seq, text, result)
        return result

    def _finish_check(self, seq: int, text: str, result: CompileResult) -> None:
        if seq != self._check_seq:
            return  # 別のアクションに切り替えた、または新しいチェックを始めた
        self._check_running = False
        self._check_result = result
        self._checked_text = text
        stale = text != self.current_source()
        self.errors.set_result(result, stale=stale)
        markers: dict[int, str] = {}
        if not stale:
            for d in result.diagnostics:
                if not d.in_script or d.line <= 0:
                    continue
                if d.is_error:
                    markers[d.line - 1] = MARKER_ERROR
                else:
                    markers.setdefault(d.line - 1, MARKER_WARNING)
        self.panel.viewer.set_line_markers(markers)
        if stale:
            self._set_check_status("unchecked", "文法チェック: 未チェック")
        elif result.status == STATUS_OK:
            self._set_check_status("ok", f"文法チェック: {result.summary()}")
        elif result.status == STATUS_ERROR:
            self._set_check_status("error", f"文法チェック: エラー {len(result.errors)} 件")
        else:
            self._set_check_status("unavailable", "文法チェック: 実行できませんでした")
            self.check_status.setToolTip(result.reason)
        if result.status != STATUS_UNAVAILABLE:
            self.check_status.setToolTip("")
        if result.status in (STATUS_ERROR, STATUS_UNAVAILABLE):
            self.show_errors()
        self._update_buttons()

    def _on_errors_expanded(self, expanded: bool) -> None:
        if not expanded:
            return
        sizes = self.vsplit.sizes()
        total = sum(sizes)
        if len(sizes) == 2 and total > 0 and sizes[1] < 120:
            h = min(ERROR_PANEL_DEFAULT_HEIGHT, max(120, total // 3))
            self.vsplit.setSizes([max(0, total - h), h])

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
        self.check_btn.setEnabled(self.can_check() and not self._check_running)

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
        # 表示中の内容（未保存の編集を含む）をファイルに保存する
        save_script_dialog(self, self.config_manager,
                           self._group.name if self._group else "group",
                           self._action.name, self.current_source())


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
