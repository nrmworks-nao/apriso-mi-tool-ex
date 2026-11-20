"""アプリケーション全体のスタイル定義

DELMIA Apriso（業務用 MES アプリケーション）風の配色・フォントを QSS として
一元定義する（既存アプリ mani-mes-file-sync-app の styles.py と同じ配色）。
QSS はこのモジュールに集約し、各ウィジェットファイルには objectName の設定のみを書く。
モデルの BackgroundRole 等で使う色も、このモジュールの COLORS を参照する。

フォントサイズは pt 単位を使用する（px は DPI 依存のため、PyInstaller で
exe 化した際にスタイルが崩れる可能性がある）。
"""

from __future__ import annotations

# カラー定数
COLORS = {
    "header_bg": "#1a3a5c",            # メニューバー背景（濃紺）
    "header_bg_light": "#2c5f8a",      # メニューバー選択・ホバー背景
    "header_text": "#ffffff",          # メニューバー文字色
    "accent": "#2980b9",               # アクセントカラー（明るい青）
    "accent_hover": "#3498db",         # アクセントのホバー色
    "bg_main": "#f5f5f5",              # 背景色（メイン）
    "bg_panel": "#ffffff",             # 背景色（フォーム・パネル）
    "border": "#cccccc",               # ボーダー色
    "table_header_bg": "#e8eef4",      # テーブルヘッダー・ツールバー背景（薄い青グレー）
    "table_header_text": "#333333",    # テーブルヘッダー文字色
    "table_alt_row": "#f9fbfd",        # テーブル交互行色
    "spin_button_hover": "#d0dce8",    # QSpinBox 上下ボタンのホバー背景
    "status_bg": "#1a3a5c",            # ステータスバー背景（濃紺）
    "status_text": "#ffffff",          # ステータスバー文字色
    "button_bg": "#2980b9",            # ボタン背景
    "button_text": "#ffffff",          # ボタン文字色
    "button_hover": "#3498db",         # ボタンホバー
    "button_pressed": "#1a6da0",       # ボタン押下
    "button_disabled": "#95a5a6",      # 無効ボタン背景
    "button_disabled_text": "#d0d0d0", # 無効ボタン文字色
    "text_primary": "#333333",         # 基本文字色
    "text_secondary": "#666666",       # 補助文字色
    "success": "#27ae60",              # 成功
    "warning": "#f39c12",              # 警告
    "error": "#e74c3c",                # エラー
    # --- 本アプリで追加 ---
    "changed_cell": "#fff4d6",         # 未保存の変更セル
    "added_row": "#e3f4e8",            # 追加行
    "deleted_row": "#fbe3e0",          # 削除行（文字に取り消し線）
    "diff_added": "#e3f4e8",           # 差分: 追加
    "diff_deleted": "#fbe3e0",         # 差分: 削除
    "diff_changed": "#fff4d6",         # 差分: 変更
    "diff_placeholder": "#eeeeee",     # 差分: 相手側にない行の詰め物
    "code_keyword": "#1a3a5c",         # コード: キーワード（太字）
    "code_string": "#a31515",          # コード: 文字列
    "code_comment": "#3a7d44",         # コード: コメント
    "code_number": "#2980b9",          # コード: 数値
    "code_current_line": "#eef4fa",    # コード: 現在行
    "code_search_match": "#ffe58a",    # コード: 検索一致
    "code_line_number_bg": "#e8eef4",  # コード: 行番号エリア背景
    "code_line_number_text": "#666666",  # コード: 行番号文字
    "splitter_handle": "#d6dee6",      # スプリッターの境界
}

# フォント定数
FONT_FAMILY = '"Meiryo UI", "Yu Gothic UI", sans-serif'
FONT_FAMILY_MONO = '"Consolas", "MS Gothic", monospace'
FONT_SIZE_NORMAL = "9pt"
FONT_SIZE_HEADER = "10pt"

# コードビューアの既定（config.json の viewer で上書きされる）
MONO_FONT_FAMILIES = ["Consolas", "MS Gothic"]
MONO_FONT_SIZE_DEFAULT = 10


def get_app_stylesheet() -> str:
    """アプリ全体に適用する QSS を返す。

    QApplication.setStyleSheet() でアプリ全体へ適用する。
    セレクタの詳細度が同じ場合は後勝ちのため、全体既定（QWidget）を先頭に置く。
    """
    c = COLORS
    return f"""
/* ================= 全体既定 ================= */
QWidget {{
    font-family: {FONT_FAMILY};
    font-size: {FONT_SIZE_NORMAL};
    color: {c["text_primary"]};
}}
QMainWindow, QDialog {{
    background-color: {c["bg_main"]};
}}

/* ================= メニューバー ================= */
QMenuBar {{
    background-color: {c["header_bg"]};
    color: {c["header_text"]};
}}
QMenuBar::item {{
    background: transparent;
    padding: 4px 10px;
}}
QMenuBar::item:selected {{
    background-color: {c["header_bg_light"]};
}}
QMenu {{
    background-color: {c["bg_panel"]};
    color: {c["text_primary"]};
    border: 1px solid {c["border"]};
}}
QMenu::item {{
    padding: 4px 24px 4px 16px;
}}
QMenu::item:selected {{
    background-color: {c["accent"]};
    color: {c["button_text"]};
}}
QMenu::item:disabled {{
    color: {c["button_disabled"]};
}}
QMenu::separator {{
    height: 1px;
    background: {c["border"]};
    margin: 3px 6px;
}}

/* ================= ツールバー（メイン） ================= */
QToolBar#toolBar {{
    background-color: {c["table_header_bg"]};
    border: none;
    border-bottom: 1px solid {c["border"]};
    padding: 4px 6px;
    spacing: 6px;
}}
QToolBar#toolBar QToolButton {{
    background-color: {c["button_bg"]};
    color: {c["button_text"]};
    border: none;
    border-radius: 2px;
    padding: 5px 14px;
}}
QToolBar#toolBar QToolButton:hover {{
    background-color: {c["button_hover"]};
}}
QToolBar#toolBar QToolButton:pressed {{
    background-color: {c["button_pressed"]};
}}
QToolBar#toolBar QToolButton:checked {{
    background-color: {c["warning"]};
    color: {c["button_text"]};
}}
QToolBar#toolBar QToolButton:disabled {{
    background-color: {c["button_disabled"]};
    color: {c["button_disabled_text"]};
}}

/* ================= パネル内のツールバー（タブ・ダイアログ内） ================= */
QWidget#panelToolBar {{
    background-color: {c["table_header_bg"]};
    border: 1px solid {c["border"]};
}}
QWidget#panelToolBar QPushButton {{
    padding: 4px 10px;
}}

/* ================= ボタン ================= */
QPushButton {{
    background-color: {c["button_bg"]};
    color: {c["button_text"]};
    border: none;
    border-radius: 2px;
    padding: 6px 16px;
    font-weight: normal;
}}
QPushButton:hover {{
    background-color: {c["button_hover"]};
}}
QPushButton:pressed {{
    background-color: {c["button_pressed"]};
}}
QPushButton:checked {{
    background-color: {c["button_pressed"]};
}}
QPushButton:disabled {{
    background-color: {c["button_disabled"]};
    color: {c["button_disabled_text"]};
}}

/* ================= 折りたたみパネルの見出しボタン ================= */
QToolButton#collapseButton {{
    background-color: {c["table_header_bg"]};
    color: {c["table_header_text"]};
    border: 1px solid {c["border"]};
    font-weight: bold;
    padding: 3px 6px;
    text-align: left;
}}
QToolButton#collapseButton:hover {{
    background-color: {c["spin_button_hover"]};
}}

/* ================= ステータスバー ================= */
QStatusBar {{
    background-color: {c["status_bg"]};
    color: {c["status_text"]};
    font-size: {FONT_SIZE_NORMAL};
}}
QStatusBar QLabel {{
    color: {c["status_text"]};
    background: transparent;
    padding: 0 6px;
}}
QStatusBar::item {{
    border: none;
}}
QStatusBar QLabel#editModeLabel[editMode="true"] {{
    background-color: {c["warning"]};
    color: {c["button_text"]};
    font-weight: bold;
}}
QStatusBar QLabel#unsavedLabel[hasChanges="true"] {{
    color: {c["changed_cell"]};
    font-weight: bold;
}}
QStatusBar QProgressBar {{
    background-color: {c["header_bg_light"]};
    border: 1px solid {c["header_bg_light"]};
    border-radius: 2px;
    color: {c["status_text"]};
    max-height: 14px;
}}
QStatusBar QProgressBar::chunk {{
    background-color: {c["accent_hover"]};
}}

/* ================= テーブル ================= */
QTableWidget, QTableView {{
    background-color: {c["bg_panel"]};
    alternate-background-color: {c["table_alt_row"]};
    border: 1px solid {c["border"]};
    gridline-color: #e0e0e0;
    selection-background-color: {c["accent"]};
    selection-color: {c["button_text"]};
    font-size: {FONT_SIZE_NORMAL};
}}
QTableWidget::item {{
    border: none;
    padding: 4px;
}}
/* ::item にスタイルを当てると selection-background-color が無効化されるため、
   選択状態の配色を明示的に定義する。QTableView は変更セルの背景色（モデルの
   BackgroundRole）を活かすため、非選択時の ::item には背景を指定しない */
QTableWidget::item:selected, QTableView::item:selected {{
    background-color: {c["accent"]};
    color: {c["button_text"]};
}}
QHeaderView::section {{
    background-color: {c["table_header_bg"]};
    color: {c["table_header_text"]};
    border: 1px solid {c["border"]};
    font-weight: bold;
    padding: 4px 6px;
}}
QTableCornerButton::section {{
    background-color: {c["table_header_bg"]};
    border: 1px solid {c["border"]};
}}

/* ================= ツリー ================= */
QTreeView {{
    background-color: {c["bg_panel"]};
    border: 1px solid {c["border"]};
    selection-background-color: {c["accent"]};
    selection-color: {c["button_text"]};
    show-decoration-selected: 1;
    outline: none;
}}
QTreeView::item {{
    padding: 2px 2px;
}}
QTreeView::item:hover {{
    background-color: {c["table_header_bg"]};
}}
QTreeView::item:selected {{
    background-color: {c["accent"]};
    color: {c["button_text"]};
}}

/* ================= リスト ================= */
QListWidget {{
    background-color: {c["bg_panel"]};
    border: 1px solid {c["border"]};
    selection-background-color: {c["accent"]};
    selection-color: {c["button_text"]};
}}

/* ================= タブ ================= */
QTabWidget::pane {{
    background-color: {c["bg_panel"]};
    border: 1px solid {c["border"]};
    top: -1px;
}}
QTabBar::tab {{
    background-color: {c["table_header_bg"]};
    color: {c["text_primary"]};
    border: 1px solid {c["border"]};
    border-bottom: none;
    padding: 5px 16px;
    margin-right: 2px;
}}
QTabBar::tab:hover {{
    background-color: {c["spin_button_hover"]};
}}
QTabBar::tab:selected {{
    background-color: {c["bg_panel"]};
    color: {c["header_bg"]};
    font-weight: bold;
    border-top: 2px solid {c["accent"]};
}}
QTabBar::tab:disabled {{
    color: {c["button_disabled"]};
}}

/* ================= スプリッター ================= */
QSplitter::handle {{
    background-color: {c["splitter_handle"]};
}}
QSplitter::handle:hover {{
    background-color: {c["accent_hover"]};
}}
QSplitter::handle:horizontal {{
    width: 5px;
}}
QSplitter::handle:vertical {{
    height: 5px;
}}

/* ================= コードビューア ================= */
QPlainTextEdit#codeViewer {{
    background-color: {c["bg_panel"]};
    border: 1px solid {c["border"]};
    color: {c["text_primary"]};
    selection-background-color: {c["accent"]};
    selection-color: {c["button_text"]};
}}
QPlainTextEdit#sqlViewer, QPlainTextEdit#detailText {{
    background-color: {c["bg_panel"]};
    border: 1px solid {c["border"]};
    font-family: {FONT_FAMILY_MONO};
    font-size: {FONT_SIZE_NORMAL};
    color: {c["text_primary"]};
}}
QWidget#searchBar {{
    background-color: {c["table_header_bg"]};
    border: 1px solid {c["border"]};
    border-top: none;
}}
QWidget#searchBar QPushButton {{
    padding: 3px 10px;
}}
QLabel#searchStatus {{
    color: {c["text_secondary"]};
}}
QLabel#searchStatus[notFound="true"] {{
    color: {c["error"]};
    font-weight: bold;
}}

/* ================= 設定グループ ================= */
QGroupBox {{
    font-weight: bold;
    color: {c["accent"]};
    border: 1px solid {c["border"]};
    border-radius: 4px;
    margin-top: 12px;
    padding-top: 20px;
    background-color: {c["bg_panel"]};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 6px;
    background-color: {c["bg_panel"]};
}}
QGroupBox QLabel, QGroupBox QRadioButton, QGroupBox QCheckBox {{
    color: {c["text_primary"]};
    font-weight: normal;
}}

/* ================= 入力フィールド ================= */
QLineEdit, QSpinBox, QComboBox {{
    background-color: {c["bg_panel"]};
    border: 1px solid {c["border"]};
    border-radius: 2px;
    padding: 4px 8px;
    color: {c["text_primary"]};
    font-weight: normal;
    selection-background-color: {c["accent"]};
    selection-color: {c["button_text"]};
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{
    border-color: {c["accent"]};
}}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    background-color: {c["bg_main"]};
    color: {c["text_secondary"]};
}}
QLineEdit#columnFilter {{
    padding: 2px 4px;
    border-radius: 0;
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QComboBox QLineEdit {{
    border: none;
    padding: 0;
    background: transparent;
}}
QComboBox QAbstractItemView {{
    background-color: {c["bg_panel"]};
    color: {c["text_primary"]};
    border: 1px solid {c["border"]};
    selection-background-color: {c["accent"]};
    selection-color: {c["button_text"]};
}}
QSpinBox::up-button, QSpinBox::down-button {{
    background-color: {c["table_header_bg"]};
    border: 1px solid {c["border"]};
    width: 20px;
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
    background-color: {c["spin_button_hover"]};
}}
/* 矢印は画像を使わず QSS のボーダーで三角形を描画する
   （既定の矢印画像は明るい背景で見えないため） */
QSpinBox::up-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 5px solid {c["text_primary"]};
    width: 0;
    height: 0;
}}
QSpinBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {c["text_primary"]};
    width: 0;
    height: 0;
}}

/* ================= チェックボックス・ラジオボタン ================= */
QCheckBox, QRadioButton {{
    color: {c["text_primary"]};
    font-weight: normal;
}}

/* ================= プログレスバー（ダイアログ内） ================= */
QProgressBar {{
    background-color: {c["bg_panel"]};
    border: 1px solid {c["border"]};
    border-radius: 2px;
    text-align: center;
}}
QProgressBar::chunk {{
    background-color: {c["accent"]};
}}

/* ================= ラベル ================= */
QLabel#secondaryLabel {{
    color: {c["text_secondary"]};
    font-weight: normal;
}}
QLabel#headerLabel {{
    font-size: {FONT_SIZE_HEADER};
    font-weight: bold;
    color: {c["header_bg"]};
}}
QLabel#warningLabel {{
    color: {c["warning"]};
    font-weight: bold;
}}
QLabel#errorLabel {{
    color: {c["error"]};
    font-weight: bold;
}}
QLabel#successLabel {{
    color: {c["success"]};
    font-weight: bold;
}}
QLabel#overviewKey {{
    color: {c["text_secondary"]};
}}
QLabel#overviewValue {{
    color: {c["text_primary"]};
}}
QScrollArea#overviewArea {{
    background-color: {c["bg_panel"]};
    border: none;
}}
QWidget#overviewContent {{
    background-color: {c["bg_panel"]};
}}
"""


def code_font_qss(family: str, size_pt: int) -> str:
    """コードビューア用のフォント指定（ウィジェット単位の QSS）。

    アプリ全体の QSS が QWidget に font-family / font-size を指定しているため、
    setFont() では上書きできない。等幅フォントとズーム後のサイズはこの QSS で指定する。
    """
    families = [family] + [f for f in MONO_FONT_FAMILIES if f != family]
    family_list = ", ".join(f'"{f}"' for f in families if f) + ", monospace"
    return f"font-family: {family_list}; font-size: {int(size_pt)}pt;"


def refresh_style(widget) -> None:
    """動的プロパティ（setProperty）を変えたあと QSS を再適用する。"""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()
