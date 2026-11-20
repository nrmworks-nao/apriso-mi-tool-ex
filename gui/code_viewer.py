"""コードビューア

QPlainTextEdit を継承したビューア（既定は読み取り専用）。
  - 行番号エリア（差分表示用に任意のラベルも表示できる。文法チェックのエラー行には印を付ける）
  - 現在行の強調
  - QSyntaxHighlighter による C# ハイライト（キーワード・文字列・コメント・数値）
  - 検索バー（Ctrl+F、次へ F3 / 前へ Shift+F3、大文字小文字の区別、正規表現）
  - フォントサイズ変更（Ctrl+ホイール、Ctrl+0 で既定に戻す）
  - 編集（set_editable(True) のときのみ。変更指示002）:
    元に戻す / やり直し（Ctrl+Z / Ctrl+Y）、タブ文字はタブのまま入力、
    複数行選択での Tab / Shift+Tab によるインデント増減、Enter で前行のインデントを引き継ぐ。
    内部の改行は \\n に統一して扱う（plain_text()）。
"""

from __future__ import annotations

import re

from PySide6.QtCore import QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QKeySequence,
    QPainter,
    QShortcut,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
)
from PySide6.QtCore import QRegularExpression
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from gui.styles import COLORS, MONO_FONT_SIZE_DEFAULT, code_font_qss, refresh_style

# ----------------------------------------------------------------------
# C# シンタックスハイライト
# ----------------------------------------------------------------------
CSHARP_KEYWORDS = frozenset(
    """
    abstract as base bool break byte case catch char checked class const continue decimal
    default delegate do double else enum event explicit extern false finally fixed float for
    foreach goto if implicit in int interface internal is lock long namespace new null object
    operator out override params private protected public readonly ref return sbyte sealed
    short sizeof stackalloc static string struct switch this throw true try typeof uint ulong
    unchecked unsafe ushort using virtual void volatile while
    add alias ascending async await by descending dynamic equals from get global group into
    join let nameof on orderby partial remove select set value var when where yield record
    init not and or with
    """.split()
)

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER_RE = re.compile(
    r"(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?)[fFdDmMuUlL]*"
)

_STATE_NORMAL = 0
_STATE_BLOCK_COMMENT = 1
_STATE_VERBATIM = 2


def _find_verbatim_end(text: str, start: int) -> int:
    """逐語的文字列（@"..."）の終端 " の位置。"" はエスケープ。見つからなければ -1。"""
    j = start
    n = len(text)
    while j < n:
        if text[j] == '"':
            if j + 1 < n and text[j + 1] == '"':
                j += 2
                continue
            return j
        j += 1
    return -1


def _find_string_end(text: str, start: int, quote: str) -> int:
    """通常の文字列・文字リテラルの終端の次の位置（行末までに閉じなければ行末）。"""
    j = start
    n = len(text)
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == quote:
            return j + 1
        j += 1
    return n


class CSharpHighlighter(QSyntaxHighlighter):
    """C# の簡易シンタックスハイライト（複数行コメント・逐語的文字列に対応）。"""

    def __init__(self, document: QTextDocument):
        super().__init__(document)
        self.f_keyword = QTextCharFormat()
        self.f_keyword.setForeground(QColor(COLORS["code_keyword"]))
        self.f_keyword.setFontWeight(QFont.Weight.Bold)
        self.f_string = QTextCharFormat()
        self.f_string.setForeground(QColor(COLORS["code_string"]))
        self.f_comment = QTextCharFormat()
        self.f_comment.setForeground(QColor(COLORS["code_comment"]))
        self.f_number = QTextCharFormat()
        self.f_number.setForeground(QColor(COLORS["code_number"]))

    def highlightBlock(self, text: str) -> None:
        n = len(text)
        i = 0
        state = self.previousBlockState()
        if state == _STATE_BLOCK_COMMENT:
            end = text.find("*/")
            if end == -1:
                self.setFormat(0, n, self.f_comment)
                self.setCurrentBlockState(_STATE_BLOCK_COMMENT)
                return
            self.setFormat(0, end + 2, self.f_comment)
            i = end + 2
        elif state == _STATE_VERBATIM:
            end = _find_verbatim_end(text, 0)
            if end == -1:
                self.setFormat(0, n, self.f_string)
                self.setCurrentBlockState(_STATE_VERBATIM)
                return
            self.setFormat(0, end + 1, self.f_string)
            i = end + 1

        while i < n:
            ch = text[i]
            nxt = text[i + 1] if i + 1 < n else ""

            # 行コメント
            if ch == "/" and nxt == "/":
                self.setFormat(i, n - i, self.f_comment)
                break
            # ブロックコメント
            if ch == "/" and nxt == "*":
                end = text.find("*/", i + 2)
                if end == -1:
                    self.setFormat(i, n - i, self.f_comment)
                    self.setCurrentBlockState(_STATE_BLOCK_COMMENT)
                    return
                self.setFormat(i, end + 2 - i, self.f_comment)
                i = end + 2
                continue
            # 逐語的文字列 @"..." / $@"..." / @$"..."
            prefix_len = 0
            if ch == "@" and nxt == '"':
                prefix_len = 2
            elif text.startswith('$@"', i) or text.startswith('@$"', i):
                prefix_len = 3
            if prefix_len:
                end = _find_verbatim_end(text, i + prefix_len)
                if end == -1:
                    self.setFormat(i, n - i, self.f_string)
                    self.setCurrentBlockState(_STATE_VERBATIM)
                    return
                self.setFormat(i, end + 1 - i, self.f_string)
                i = end + 1
                continue
            # 補間文字列 $"..." / 通常の文字列 / 文字リテラル
            if ch == "$" and nxt == '"':
                end = _find_string_end(text, i + 2, '"')
                self.setFormat(i, end - i, self.f_string)
                i = end
                continue
            if ch == '"' or ch == "'":
                end = _find_string_end(text, i + 1, ch)
                self.setFormat(i, end - i, self.f_string)
                i = end
                continue
            # 数値（識別子の途中は除く）
            if ch.isdigit():
                m = _NUMBER_RE.match(text, i)
                if m:
                    self.setFormat(i, m.end() - i, self.f_number)
                    i = m.end()
                    continue
            # 識別子・キーワード
            if ch.isalpha() or ch == "_":
                m = _IDENT_RE.match(text, i)
                if m:
                    if m.group(0) in CSHARP_KEYWORDS:
                        self.setFormat(i, m.end() - i, self.f_keyword)
                    i = m.end()
                    continue
                # ASCII 以外の文字（日本語の識別子など）は 1 文字進める
            i += 1

        self.setCurrentBlockState(_STATE_NORMAL)


# ----------------------------------------------------------------------
# ビューア本体
# ----------------------------------------------------------------------
class LineNumberArea(QWidget):
    def __init__(self, editor: "CodeViewer"):
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self._editor.line_number_area_width(), 0)

    def paintEvent(self, event) -> None:
        self._editor.paint_line_numbers(event)


MARKER_ERROR = "error"
MARKER_WARNING = "warning"
_MARKER_WIDTH = 10


class CodeViewer(QPlainTextEdit):
    """コードビューア（既定は読み取り専用。set_editable(True) で編集可能）。"""

    font_size_changed = Signal(int)

    MIN_FONT = 6
    MAX_FONT = 40
    MAX_MATCH_HIGHLIGHTS = 5000

    def __init__(self, parent=None, font_family: str = "Consolas",
                 font_size: int = MONO_FONT_SIZE_DEFAULT, word_wrap: bool = False,
                 highlight: bool = True):
        super().__init__(parent)
        self.setObjectName("codeViewer")
        self._editable = False
        self._apply_read_only()
        # Tab キーはフォーカス移動ではなくタブ文字の入力に使う
        self.setTabChangesFocus(False)
        self.setTabStopDistance(4 * max(1, self.fontMetrics().horizontalAdvance(" ")))
        self.set_word_wrap(word_wrap)

        self._font_family = font_family or "Consolas"
        self._default_font_size = font_size or MONO_FONT_SIZE_DEFAULT
        self._font_size = self._default_font_size
        self._apply_font()

        self._line_area = LineNumberArea(self)
        self._line_labels: list[str] | None = None
        self._line_backgrounds: dict[int, QColor] = {}
        self._line_markers: dict[int, str] = {}     # 行（0 始まり）→ MARKER_ERROR / MARKER_WARNING
        self._match_selections: list[QTextEdit.ExtraSelection] = []

        self.highlighter = CSharpHighlighter(self.document()) if highlight else None

        self.blockCountChanged.connect(self._update_line_area_width)
        self.updateRequest.connect(self._update_line_area)
        self.cursorPositionChanged.connect(self._update_extra_selections)
        self._update_line_area_width()
        self._update_extra_selections()

    # --- 表示内容 -------------------------------------------------------
    def set_code(self, text: str) -> None:
        """内容を置き換える（元に戻す／やり直しの履歴は消える）。"""
        self._line_labels = None
        self._line_backgrounds = {}
        self._line_markers = {}
        self._match_selections = []
        self.setPlainText(text or "")
        self.document().setModified(False)
        self.moveCursor(QTextCursor.MoveOperation.Start)
        self._update_line_area_width()
        self._update_extra_selections()

    def plain_text(self) -> str:
        """内容（改行は \\n）。

        toPlainText() は改行なしスペース（U+00A0）を通常の空白に変えてしまうため、
        toRawText() を使い、段落区切り（U+2029）だけを \\n に戻す。
        """
        doc = self.document()
        raw = getattr(doc, "toRawText", None)
        text = raw() if raw is not None else doc.toPlainText()
        return text.replace(" ", "\n")

    # --- 編集 -------------------------------------------------------------
    def is_editable(self) -> bool:
        return self._editable

    def set_editable(self, editable: bool) -> None:
        editable = bool(editable)
        if editable == self._editable:
            return
        self._editable = editable
        self._apply_read_only()
        self._update_extra_selections()

    def _apply_read_only(self) -> None:
        self.setReadOnly(not self._editable)
        if self._editable:
            self.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        else:
            # 読み取り専用でもキーボードでカーソル移動・選択できるようにする（現在行の強調に使う）
            self.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.TextSelectableByKeyboard
            )

    def keyPressEvent(self, event) -> None:
        if self._editable and not self.isReadOnly():
            key = event.key()
            mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
            no_mods = mods == Qt.KeyboardModifier.NoModifier
            shift_only = mods == Qt.KeyboardModifier.ShiftModifier
            if key == Qt.Key.Key_Tab and no_mods and self._selection_spans_lines():
                self._indent_selection()
                event.accept()
                return
            if key == Qt.Key.Key_Backtab or (key == Qt.Key.Key_Tab and shift_only):
                self._unindent_selection()
                event.accept()
                return
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (no_mods or shift_only):
                # Shift+Enter も通常の改行にする（行区切り U+2028 を入れない）
                self._newline_with_indent()
                event.accept()
                return
        super().keyPressEvent(event)

    def _selection_spans_lines(self) -> bool:
        cur = self.textCursor()
        if not cur.hasSelection():
            return False
        doc = self.document()
        return (doc.findBlock(cur.selectionStart()).blockNumber()
                != doc.findBlock(cur.selectionEnd()).blockNumber())

    def _selected_block_range(self) -> tuple[int, int]:
        """選択範囲（なければカーソル行）の (先頭行, 末尾行)。末尾が行頭ちょうどならその行は含めない。"""
        cur = self.textCursor()
        doc = self.document()
        first = doc.findBlock(cur.selectionStart())
        last = doc.findBlock(cur.selectionEnd())
        if (cur.hasSelection() and last.blockNumber() > first.blockNumber()
                and cur.selectionEnd() == last.position()):
            last = last.previous()
        return first.blockNumber(), last.blockNumber()

    def _reselect_blocks(self, first_no: int, last_no: int) -> None:
        doc = self.document()
        first = doc.findBlockByNumber(first_no)
        last = doc.findBlockByNumber(last_no)
        cur = QTextCursor(first)
        cur.setPosition(last.position() + len(last.text()), QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(cur)

    def _indent_selection(self) -> None:
        first_no, last_no = self._selected_block_range()
        doc = self.document()
        cur = QTextCursor(doc)
        cur.beginEditBlock()
        for n in range(first_no, last_no + 1):
            block = doc.findBlockByNumber(n)
            cur.setPosition(block.position())
            cur.insertText("\t")
        cur.endEditBlock()
        self._reselect_blocks(first_no, last_no)

    def _unindent_selection(self) -> None:
        had_selection = self.textCursor().hasSelection() and self._selection_spans_lines()
        first_no, last_no = self._selected_block_range()
        doc = self.document()
        cur = QTextCursor(doc)
        cur.beginEditBlock()
        for n in range(first_no, last_no + 1):
            block = doc.findBlockByNumber(n)
            text = block.text()
            if text.startswith("\t"):
                remove = 1
            else:
                remove = len(text) - len(text.lstrip(" "))
                remove = min(remove, 4)
            if remove:
                cur.setPosition(block.position())
                cur.setPosition(block.position() + remove, QTextCursor.MoveMode.KeepAnchor)
                cur.removeSelectedText()
        cur.endEditBlock()
        if had_selection:
            self._reselect_blocks(first_no, last_no)

    def _newline_with_indent(self) -> None:
        cur = self.textCursor()
        block = cur.block()
        text = block.text()
        indent_len = len(text) - len(text.lstrip(" \t"))
        # カーソルが行頭の空白の途中にある場合は、カーソルより左の空白だけを引き継ぐ
        col = cur.position() - block.position()
        indent = text[:min(indent_len, col)]
        cur.beginEditBlock()
        cur.insertText("\n" + indent)
        cur.endEditBlock()
        self.setTextCursor(cur)
        self.ensureCursorVisible()

    def set_line_labels(self, labels: list[str] | None) -> None:
        """行番号の代わりに表示するラベル（差分表示で使う。None で通常の行番号）。"""
        self._line_labels = labels
        self._update_line_area_width()
        self._line_area.update()

    def set_line_backgrounds(self, backgrounds: dict[int, QColor]) -> None:
        """行（0 始まり）ごとの背景色。"""
        self._line_backgrounds = dict(backgrounds)
        self._update_extra_selections()

    def set_line_markers(self, markers: dict[int, str] | None) -> None:
        """文法チェックの印（行は 0 始まり、値は MARKER_ERROR / MARKER_WARNING）。

        行番号エリアに印を付け、行の背景をエラー色（警告色）にする。
        """
        self._line_markers = dict(markers or {})
        self._update_line_area_width()
        self._update_extra_selections()

    def set_word_wrap(self, enabled: bool) -> None:
        self.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth if enabled else QPlainTextEdit.LineWrapMode.NoWrap
        )

    def goto_line(self, line: int) -> None:
        """1 始まりの行番号へ移動する。"""
        block = self.document().findBlockByNumber(max(0, line - 1))
        if not block.isValid():
            return
        cursor = QTextCursor(block)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(cursor)
        self.centerCursor()
        self.setFocus()

    # --- フォント --------------------------------------------------------
    def _apply_font(self) -> None:
        self.setStyleSheet(code_font_qss(self._font_family, self._font_size))
        self.setTabStopDistance(4 * max(1, self.fontMetrics().horizontalAdvance(" ")))
        if hasattr(self, "_line_area"):
            self._update_line_area_width()
            self._line_area.update()

    def font_size(self) -> int:
        return self._font_size

    def set_font_size(self, size: int) -> None:
        size = max(self.MIN_FONT, min(self.MAX_FONT, int(size)))
        if size == self._font_size:
            return
        self._font_size = size
        self._apply_font()
        self.font_size_changed.emit(size)

    def reset_font_size(self) -> None:
        self.set_font_size(self._default_font_size)

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0:
                self.set_font_size(self._font_size + 1)
            elif delta < 0:
                self.set_font_size(self._font_size - 1)
            event.accept()
            return
        super().wheelEvent(event)

    # --- 行番号エリア ------------------------------------------------------
    def line_number_area_width(self) -> int:
        if self._line_labels:
            chars = max((len(s) for s in self._line_labels), default=1)
        else:
            chars = len(str(max(1, self.blockCount())))
        chars = max(chars, 3)
        marker = _MARKER_WIDTH if self._line_markers else 0
        return 12 + marker + self.fontMetrics().horizontalAdvance("9") * chars

    def _update_line_area_width(self, *args) -> None:
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _update_line_area(self, rect: QRect, dy: int) -> None:
        if dy:
            self._line_area.scroll(0, dy)
        else:
            self._line_area.update(0, rect.y(), self._line_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_line_area_width()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._line_area.setGeometry(QRect(cr.left(), cr.top(), self.line_number_area_width(), cr.height()))

    def paint_line_numbers(self, event) -> None:
        painter = QPainter(self._line_area)
        painter.fillRect(event.rect(), QColor(COLORS["code_line_number_bg"]))
        painter.setFont(self.font())
        normal_pen = QColor(COLORS["code_line_number_text"])
        current_pen = QColor(COLORS["code_keyword"])
        current_block = self.textCursor().blockNumber()
        width = self._line_area.width() - 6
        height = self.fontMetrics().height()
        marker_colors = {
            MARKER_ERROR: QColor(COLORS["code_error_marker"]),
            MARKER_WARNING: QColor(COLORS["code_warning_marker"]),
        }

        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                if self._line_labels is not None:
                    label = self._line_labels[number] if number < len(self._line_labels) else ""
                else:
                    label = str(number + 1)
                kind = self._line_markers.get(number)
                if kind in marker_colors:
                    # 行番号の左に丸印
                    d = max(6, min(_MARKER_WIDTH - 2, height - 4))
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(marker_colors[kind])
                    painter.drawEllipse(3, top + (height - d) // 2, d, d)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(current_pen if number == current_block else normal_pen)
                painter.drawText(0, top, width, height, Qt.AlignmentFlag.AlignRight, label)
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            number += 1
        painter.end()

    # --- 強調表示 ----------------------------------------------------------
    def _update_extra_selections(self) -> None:
        selections: list[QTextEdit.ExtraSelection] = []

        doc = self.document()
        for line, color in self._line_backgrounds.items():
            block = doc.findBlockByNumber(line)
            if not block.isValid():
                continue
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(color)
            sel.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
            sel.cursor = QTextCursor(block)
            selections.append(sel)

        if not self._line_backgrounds:
            # 現在行（差分表示では行の色を優先するため表示しない）
            cur = QTextEdit.ExtraSelection()
            cur.format.setBackground(QColor(COLORS["code_current_line"]))
            cur.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
            cur.cursor = self.textCursor()
            cur.cursor.clearSelection()
            selections.append(cur)

        # 文法チェックのエラー行（現在行の強調より後に置き、エラー色が見えるようにする）
        line_colors = {
            MARKER_ERROR: QColor(COLORS["code_error_line"]),
            MARKER_WARNING: QColor(COLORS["code_warning_line"]),
        }
        for line, kind in self._line_markers.items():
            color = line_colors.get(kind)
            block = doc.findBlockByNumber(line)
            if color is None or not block.isValid():
                continue
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(color)
            sel.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
            sel.cursor = QTextCursor(block)
            selections.append(sel)

        selections.extend(self._match_selections)
        self.setExtraSelections(selections)
        self._line_area.update()

    # --- 検索 --------------------------------------------------------------
    @staticmethod
    def _make_regex(pattern: str, case_sensitive: bool, use_regex: bool) -> QRegularExpression:
        expr = pattern if use_regex else QRegularExpression.escape(pattern)
        rx = QRegularExpression(expr)
        if not case_sensitive:
            rx.setPatternOptions(QRegularExpression.PatternOption.CaseInsensitiveOption)
        return rx

    def highlight_matches(self, pattern: str, case_sensitive: bool, use_regex: bool) -> int:
        """一致箇所をすべて強調し、件数を返す（正規表現が不正なら -1）。"""
        self._match_selections = []
        count = 0
        if pattern:
            rx = self._make_regex(pattern, case_sensitive, use_regex)
            if not rx.isValid():
                self._update_extra_selections()
                return -1
            fmt_color = QColor(COLORS["code_search_match"])
            doc = self.document()
            cursor = QTextCursor(doc)
            while True:
                cursor = doc.find(rx, cursor)
                if cursor.isNull():
                    break
                if cursor.selectionStart() == cursor.selectionEnd():
                    # 空一致は無限ループになるため 1 文字進める
                    if not cursor.movePosition(QTextCursor.MoveOperation.NextCharacter):
                        break
                    continue
                count += 1
                if count <= self.MAX_MATCH_HIGHLIGHTS:
                    sel = QTextEdit.ExtraSelection()
                    sel.format.setBackground(fmt_color)
                    sel.cursor = QTextCursor(cursor)
                    self._match_selections.append(sel)
        self._update_extra_selections()
        return count

    def find_text(self, pattern: str, case_sensitive: bool, use_regex: bool,
                  backward: bool = False) -> bool | None:
        """次（前）の一致へ移動する。末尾（先頭）に達したら折り返す。

        戻り値: 見つかった True / 見つからない False / 正規表現が不正 None
        """
        if not pattern:
            return False
        rx = self._make_regex(pattern, case_sensitive, use_regex)
        if not rx.isValid():
            return None
        flags = QTextDocument.FindFlag(0)
        if backward:
            flags |= QTextDocument.FindFlag.FindBackward
        doc = self.document()
        found = doc.find(rx, self.textCursor(), flags)
        if found.isNull():
            start = QTextCursor(doc)
            if backward:
                start.movePosition(QTextCursor.MoveOperation.End)
            found = doc.find(rx, start, flags)
        if found.isNull():
            return False
        self.setTextCursor(found)
        self.centerCursor()
        return True


# ----------------------------------------------------------------------
# 検索バー + ビューア
# ----------------------------------------------------------------------
class SearchBar(QWidget):
    find_requested = Signal(bool)   # backward
    pattern_changed = Signal()
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("searchBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(6)

        lay.addWidget(QLabel("検索:", self))
        self.edit = QLineEdit(self)
        self.edit.setPlaceholderText("検索する文字列（Enter: 次へ / Shift+Enter: 前へ）")
        self.edit.setClearButtonEnabled(True)
        lay.addWidget(self.edit, 1)
        self.case_cb = QCheckBox("大文字小文字を区別", self)
        lay.addWidget(self.case_cb)
        self.regex_cb = QCheckBox("正規表現", self)
        lay.addWidget(self.regex_cb)
        self.prev_btn = QPushButton("前へ", self)
        self.prev_btn.setToolTip("前を検索 (Shift+F3)")
        lay.addWidget(self.prev_btn)
        self.next_btn = QPushButton("次へ", self)
        self.next_btn.setToolTip("次を検索 (F3)")
        lay.addWidget(self.next_btn)
        self.status = QLabel("", self)
        self.status.setObjectName("searchStatus")
        self.status.setMinimumWidth(90)
        lay.addWidget(self.status)
        self.close_btn = QPushButton("閉じる", self)
        lay.addWidget(self.close_btn)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self.pattern_changed.emit)

        self.edit.textChanged.connect(lambda *_: self._timer.start())
        self.case_cb.toggled.connect(lambda *_: self._timer.start())
        self.regex_cb.toggled.connect(lambda *_: self._timer.start())
        self.edit.returnPressed.connect(self._on_return)
        self.next_btn.clicked.connect(lambda: self.find_requested.emit(False))
        self.prev_btn.clicked.connect(lambda: self.find_requested.emit(True))
        self.close_btn.clicked.connect(self.closed.emit)

    def _on_return(self) -> None:
        from PySide6.QtWidgets import QApplication

        backward = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)
        self.find_requested.emit(backward)

    def set_status(self, text: str, not_found: bool = False) -> None:
        self.status.setText(text)
        self.status.setProperty("notFound", "true" if not_found else "false")
        refresh_style(self.status)

    def pattern(self) -> str:
        return self.edit.text()

    def case_sensitive(self) -> bool:
        return self.case_cb.isChecked()

    def use_regex(self) -> bool:
        return self.regex_cb.isChecked()


class CodeViewerPanel(QWidget):
    """CodeViewer + 検索バー（ショートカット付き）。"""

    def __init__(self, parent=None, font_family: str = "Consolas",
                 font_size: int = MONO_FONT_SIZE_DEFAULT, word_wrap: bool = False):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.viewer = CodeViewer(self, font_family=font_family, font_size=font_size,
                                 word_wrap=word_wrap)
        lay.addWidget(self.viewer, 1)
        self.search_bar = SearchBar(self)
        self.search_bar.hide()
        lay.addWidget(self.search_bar)

        self.search_bar.find_requested.connect(self.find)
        self.search_bar.pattern_changed.connect(self._on_pattern_changed)
        self.search_bar.closed.connect(self.close_search)

        ctx = Qt.ShortcutContext.WidgetWithChildrenShortcut
        for seq, slot in (
            (QKeySequence.StandardKey.Find, self.open_search),
            (QKeySequence(Qt.Key.Key_F3), lambda: self.find(False)),
            (QKeySequence(Qt.Modifier.SHIFT | Qt.Key.Key_F3), lambda: self.find(True)),
            (QKeySequence(Qt.Modifier.CTRL | Qt.Key.Key_0), self.viewer.reset_font_size),
            (QKeySequence(Qt.Key.Key_Escape), self.close_search),
        ):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(ctx)
            sc.activated.connect(slot)

    def set_code(self, text: str) -> None:
        self.viewer.set_code(text)
        if self.search_bar.isVisible() and self.search_bar.pattern():
            self._on_pattern_changed()

    def open_search(self) -> None:
        selected = self.viewer.textCursor().selectedText()
        if selected and " " not in selected:
            self.search_bar.edit.setText(selected)
        self.search_bar.show()
        self.search_bar.edit.setFocus()
        self.search_bar.edit.selectAll()
        self._on_pattern_changed()

    def close_search(self) -> None:
        if not self.search_bar.isVisible():
            return
        self.search_bar.hide()
        self.viewer.highlight_matches("", False, False)
        self.viewer.setFocus()

    def _on_pattern_changed(self) -> None:
        sb = self.search_bar
        count = self.viewer.highlight_matches(sb.pattern(), sb.case_sensitive(), sb.use_regex())
        if not sb.pattern():
            sb.set_status("")
        elif count < 0:
            sb.set_status("正規表現が不正", not_found=True)
        elif count == 0:
            sb.set_status("見つかりません", not_found=True)
        else:
            sb.set_status(f"{count} 件")

    def find(self, backward: bool = False) -> None:
        sb = self.search_bar
        if not sb.isVisible() or not sb.pattern():
            self.open_search()
            return
        result = self.viewer.find_text(sb.pattern(), sb.case_sensitive(), sb.use_regex(), backward)
        if result is None:
            sb.set_status("正規表現が不正", not_found=True)
        elif not result:
            sb.set_status("見つかりません", not_found=True)
