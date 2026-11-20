"""CSV / Excel 入出力

エクスポート列:
  グループ名, ローカル名, エイリアス(フルパス), トリガーの実行, データタイプ, アクセス,
  OPC UA キー, 公開間隔

- CSV は csv モジュール。UTF-8 BOM 付きで書き出し、読み込みは BOM の有無を両方受け付ける。
- Excel は openpyxl。ヘッダー行を固定し、オートフィルターを設定する。
- インポートで使う（編集可能な）列は ローカル名・エイリアス・トリガーの実行 のみ。
  他の列は無視する。
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field

from core.models import ImportRow, MIConfigSnapshot, access_label, data_type_label

COL_GROUP = "グループ名"
COL_LOCAL_NAME = "ローカル名"
COL_ALIAS = "エイリアス(フルパス)"
COL_TRIGGER = "トリガーの実行"
COL_DATA_TYPE = "データタイプ"
COL_ACCESS = "アクセス"
COL_OPC_KEY = "OPC UA キー"
COL_PUBLISHING = "公開間隔"

EXPORT_COLUMNS = [
    COL_GROUP,
    COL_LOCAL_NAME,
    COL_ALIAS,
    COL_TRIGGER,
    COL_DATA_TYPE,
    COL_ACCESS,
    COL_OPC_KEY,
    COL_PUBLISHING,
]

# インポート時に見出しの別名として受け付けるもの
_HEADER_ALIASES = {
    COL_ALIAS: (COL_ALIAS, "エイリアス", "エイリアス（フルパス）"),
}

_TRUE_TEXTS = {"1", "true", "on", "yes", "y", "はい", "○", "〇", "✓", "有効", "-1"}
_FALSE_TEXTS = {"0", "false", "off", "no", "n", "いいえ", "×", "無効"}


class ImportFormatError(Exception):
    """インポートファイルの形式エラー（必須列がない等）。"""


def triggers_to_text(value: bool | None) -> str:
    if value is None:
        return ""
    return "1" if value else "0"


def parse_triggers(text: str) -> tuple[bool | None, bool]:
    """トリガーの実行の値を解釈する。戻り値: (値, 正しく解釈できたか)。空欄は (None, True)。"""
    t = (text or "").strip().lower()
    if t == "":
        return None, True
    if t in _TRUE_TEXTS:
        return True, True
    if t in _FALSE_TEXTS:
        return False, True
    return None, False


# ----------------------------------------------------------------------
# エクスポート
# ----------------------------------------------------------------------
def build_export_rows(snapshot: MIConfigSnapshot, group_ids: list[int] | None = None) -> list[list[str]]:
    """エクスポートする行（DB の内容。未保存の変更は含まない）。

    group_ids が None なら全グループ。
    """
    if group_ids is None:
        groups = snapshot.groups_sorted()
    else:
        wanted = set(group_ids)
        groups = [g for g in snapshot.groups_sorted() if g.id in wanted]

    rows: list[list[str]] = []
    for g in groups:
        for gp in snapshot.active_group_points(g.id):
            p = snapshot.points.get(gp.point_id)
            rows.append([
                g.name,
                gp.local_name or "",
                snapshot.point_path(gp.point_id),
                triggers_to_text(gp.triggers_execution),
                data_type_label(p.data_type) if p else "",
                access_label(p.access) if p else "",
                p.opc_key if p else "",
                p.publishing_interval if p else "",
            ])
    return rows


def export_csv(path: str, rows: list[list[str]], columns: list[str] | None = None) -> None:
    """UTF-8 BOM 付き CSV で書き出す。"""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(columns or EXPORT_COLUMNS)
        writer.writerows(rows)


def export_xlsx(path: str, rows: list[list[str]], columns: list[str] | None = None,
                sheet_title: str = "ポイント設定") -> None:
    """Excel（.xlsx）で書き出す。ヘッダー行を固定し、オートフィルターを設定する。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    columns = columns or EXPORT_COLUMNS
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title[:31]

    header_font = Font(bold=True, color="333333")
    header_fill = PatternFill(fill_type="solid", start_color="E8EEF4", end_color="E8EEF4")
    for c, name in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=c, value=name)
        cell.font = header_font
        cell.fill = header_fill

    widths = [len(str(c)) for c in columns]
    for r, row in enumerate(rows, start=2):
        for c, value in enumerate(row, start=1):
            text = "" if value is None else str(value)
            cell = ws.cell(row=r, column=c)
            cell.value = text
            # "=" で始まる値が数式として扱われないよう文字列型にする
            cell.data_type = "s"
            if c - 1 < len(widths):
                widths[c - 1] = max(widths[c - 1], len(text))

    for c, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(c)].width = min(max(w + 2, 8), 80)

    ws.freeze_panes = "A2"
    last_row = max(1, len(rows) + 1)
    ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{last_row}"
    wb.save(path)


def export_table(path: str, rows: list[list[str]], columns: list[str] | None = None) -> None:
    """拡張子で CSV / Excel を切り替えて書き出す。"""
    if os.path.splitext(path)[1].lower() in (".xlsx", ".xlsm"):
        export_xlsx(path, rows, columns)
    else:
        export_csv(path, rows, columns)


# ----------------------------------------------------------------------
# インポート
# ----------------------------------------------------------------------
@dataclass
class ImportData:
    rows: list[ImportRow] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _read_csv_rows(path: str) -> list[list[str]]:
    # BOM の有無は utf-8-sig で両方受け付ける。Excel で保存した Shift_JIS の CSV も読めるようにする
    for encoding in ("utf-8-sig", "cp932"):
        try:
            with open(path, "r", encoding=encoding, newline="") as f:
                return [list(r) for r in csv.reader(f)]
        except UnicodeDecodeError:
            continue
    raise ImportFormatError("CSV の文字コードを判別できません（UTF-8 で保存してください）。")


def _read_xlsx_rows(path: str) -> list[list[str]]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        return [[_cell_text(v) for v in row] for row in ws.iter_rows(values_only=True)]
    finally:
        wb.close()


def _find_column(header: list[str], name: str) -> int:
    candidates = _HEADER_ALIASES.get(name, (name,))
    normalized = [h.strip() for h in header]
    for cand in candidates:
        if cand in normalized:
            return normalized.index(cand)
    return -1


def read_import_file(path: str) -> ImportData:
    """インポートファイル（CSV / Excel）を読む。必須列がなければ ImportFormatError。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        raw = _read_xlsx_rows(path)
    else:
        raw = _read_csv_rows(path)

    if not raw:
        raise ImportFormatError("ファイルが空です。")
    header = [_cell_text(h) for h in raw[0]]
    i_group = _find_column(header, COL_GROUP)
    i_alias = _find_column(header, COL_ALIAS)
    i_local = _find_column(header, COL_LOCAL_NAME)
    i_trig = _find_column(header, COL_TRIGGER)
    missing = [n for n, i in ((COL_GROUP, i_group), (COL_ALIAS, i_alias)) if i < 0]
    if missing:
        raise ImportFormatError(f"必須の列がありません: {', '.join(missing)}")

    data = ImportData(columns=header)

    def cell(row: list[str], idx: int) -> str:
        if idx < 0 or idx >= len(row):
            return ""
        return row[idx] if row[idx] is not None else ""

    for line_no, row in enumerate(raw[1:], start=2):
        if not any((v or "").strip() for v in row):
            continue  # 空行
        group_name = cell(row, i_group).strip()
        alias = cell(row, i_alias).strip()
        local_name = cell(row, i_local).strip() if i_local >= 0 else ""
        trig_text = cell(row, i_trig) if i_trig >= 0 else ""
        trig, ok = parse_triggers(trig_text)
        error = ""
        if not group_name:
            error = "グループ名が空です"
        elif not alias:
            error = "エイリアスが空です"
        elif not ok:
            error = f"トリガーの実行の値が不正です（{trig_text}）"
        data.rows.append(
            ImportRow(
                line_no=line_no,
                group_name=group_name,
                alias=alias,
                local_name=local_name or None,
                triggers_execution=trig,
                has_local_name=i_local >= 0,
                has_triggers=i_trig >= 0,
                error=error,
            )
        )
    return data
