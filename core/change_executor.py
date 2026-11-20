"""ChangeSet の退避・実行・ログ

DB への書き込みはすべて本モジュールを通す（書き込み対象は MI_ACTION_GROUP_POINT のみ）。

手順（指示書 5.10）:
  1. 実行直前に、更新・削除対象の行（全列）を backup\\YYYYMMDD_HHMMSS_<操作名>.json に
     書き出し、元に戻すための SQL（.sql）も出力する。退避に失敗した場合は実行しない。
  2. 1 つのトランザクションで DELETE → UPDATE → INSERT の順に実行する。
     1 行でも失敗したら全体をロールバックする。
  3. UPDATE / DELETE は WHERE ID = ? AND RowVersionStamp = ? とし、影響行数が 1 で
     なければ「他で更新された」としてロールバックする。
  4. log\\AprisoMIToolEx_YYYYMM.log に、日時、実行者、操作、対象件数、退避ファイル名、
     結果を記録する。

SQL はすべてパラメータ化クエリで実行する。preview_sql() が返すパラメータ埋め込み済みの
文字列は表示用であり、実行には使わない。

前提（未確認4）: MI_ACTION_GROUP_POINT にトリガーは定義されていないものとして実装している。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QObject, Signal, Slot

from core import APP_NAME, APP_VERSION
from core.config_manager import UPDATED_BY_MAX_LENGTH

if TYPE_CHECKING:
    from core.db import Database
    from core.point_service import ChangeSet

# ----------------------------------------------------------------------
# 削除方式（未確認3）
#   Apriso 標準画面でポイントを外したときに物理削除か論理削除（Active = 0）かは未確認。
#   既定は物理削除。切り替える場合はこの定数のみを "logical" に変更する。
# ----------------------------------------------------------------------
DELETE_MODE = "physical"  # "physical" または "logical"

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 書き込み SQL（すべてパラメータ化。テーブル名・列名は固定文字列のみ）
# ----------------------------------------------------------------------
# Active と RowVersionStamp は既定値（1）に任せる
SQL_INSERT = (
    "INSERT INTO MI_ACTION_GROUP_POINT\n"
    "    (ActionGroupID, PointID, TriggersExecution, LocalName,\n"
    "     CreatedOn, CreatedBy, LastUpdateOn, LastUpdatedBy)\n"
    "VALUES (?, ?, ?, ?, GETUTCDATE(), ?, GETUTCDATE(), ?);"
)

SQL_UPDATE = (
    "UPDATE MI_ACTION_GROUP_POINT\n"
    "SET PointID = ?, TriggersExecution = ?, LocalName = ?,\n"
    "    LastUpdateOn = GETUTCDATE(), LastUpdatedBy = ?,\n"
    "    RowVersionStamp = ISNULL(RowVersionStamp, 0) + 1\n"
    "WHERE ID = ? AND RowVersionStamp = ?;"
)

# 物理削除（既定）
SQL_DELETE_PHYSICAL = (
    "DELETE FROM MI_ACTION_GROUP_POINT\n"
    "WHERE ID = ? AND RowVersionStamp = ?;"
)

# 論理削除（DELETE_MODE = "logical" のときのみ使用。未確認3）
SQL_DELETE_LOGICAL = (
    "UPDATE MI_ACTION_GROUP_POINT\n"
    "SET Active = 0,\n"
    "    LastUpdateOn = GETUTCDATE(), LastUpdatedBy = ?,\n"
    "    RowVersionStamp = ISNULL(RowVersionStamp, 0) + 1\n"
    "WHERE ID = ? AND RowVersionStamp = ?;"
)

# 実行後の案内（未確認1: MI への反映タイミングは未確認のため、アプリは反映操作をしない）
MI_REFRESH_NOTICE = "MI側への反映には、MI のリフレッシュまたは再起動が必要な場合があります。"


class ConcurrencyError(Exception):
    """他で更新された（UPDATE / DELETE の影響行数が 1 でない）。"""


@dataclass
class ExecutionResult:
    success: bool
    message: str
    detail: str = ""
    backup_json: str = ""
    backup_sql: str = ""
    inserted: int = 0
    updated: int = 0
    deleted: int = 0


def delete_frees_point() -> bool:
    """削除で (ActionGroupID, PointID) の一意制約が解放されるか（物理削除のときのみ）。"""
    return DELETE_MODE == "physical"


def validate_updated_by(updated_by: str | None) -> str:
    """更新者名の検査。問題があればエラーメッセージ、なければ空文字を返す。"""
    v = (updated_by or "").strip()
    if not v:
        return ("更新者名が設定されていません。\n"
                "「ファイル → 接続設定」で、監査列に記録する更新者名を設定してください。")
    if len(v) > UPDATED_BY_MAX_LENGTH:
        return f"更新者名は {UPDATED_BY_MAX_LENGTH} 文字以内で設定してください（現在 {len(v)} 文字）。"
    return ""


# ----------------------------------------------------------------------
# SQL 文の組み立て
# ----------------------------------------------------------------------
def build_statements(change_set: "ChangeSet", updated_by: str) -> list[tuple[str, str, tuple, object]]:
    """実行する文を DELETE → UPDATE → INSERT の順に返す。

    戻り値: [(種別, SQL, パラメータ, 操作オブジェクト), ...]
    """
    stmts: list[tuple[str, str, tuple, object]] = []
    for op in change_set.deletes:
        if DELETE_MODE == "physical":
            stmts.append(
                ("delete", SQL_DELETE_PHYSICAL, (op.before.id, op.before.row_version_stamp), op)
            )
        else:
            stmts.append(
                (
                    "delete",
                    SQL_DELETE_LOGICAL,
                    (updated_by, op.before.id, op.before.row_version_stamp),
                    op,
                )
            )
    for op in change_set.updates:
        stmts.append(
            (
                "update",
                SQL_UPDATE,
                (
                    op.point_id,
                    op.triggers_execution,
                    op.local_name,
                    updated_by,
                    op.before.id,
                    op.before.row_version_stamp,
                ),
                op,
            )
        )
    for op in change_set.inserts:
        stmts.append(
            (
                "insert",
                SQL_INSERT,
                (
                    op.action_group_id,
                    op.point_id,
                    op.triggers_execution,
                    op.local_name,
                    updated_by,
                    updated_by,
                ),
                op,
            )
        )
    return stmts


def sql_literal(value) -> str:
    """表示用の SQL リテラル。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, datetime):
        # 言語設定に依存しない ISO 8601 形式
        return "'" + value.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "'"
    return "N'" + str(value).replace("'", "''") + "'"


def embed_params(sql: str, params: tuple) -> str:
    """? をパラメータのリテラルで置き換えた文字列（表示用）。"""
    parts = sql.split("?")
    if len(parts) - 1 != len(params):
        raise ValueError("パラメータ数が一致しません")
    out = [parts[0]]
    for p, rest in zip(params, parts[1:]):
        out.append(sql_literal(p))
        out.append(rest)
    return "".join(out)


_KIND_LABELS = {"delete": "削除", "update": "更新", "insert": "追加"}


def preview_sql(change_set: "ChangeSet", updated_by: str = "") -> list[str]:
    """表示用に、パラメータを埋め込んだ SQL 文字列を返す（実行には使わない）。"""
    user = updated_by.strip() if updated_by else ""
    out: list[str] = []
    for kind, sql, params, op in build_statements(change_set, user or "<更新者名>"):
        header = f"-- [{_KIND_LABELS[kind]}] {op.group_name} / {getattr(op, 'alias', '')}"
        out.append(header + "\n" + embed_params(sql, params))
    return out


# ----------------------------------------------------------------------
# 退避
# ----------------------------------------------------------------------
def _safe_filename(text: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", text or "").strip("_")
    return s or "change"


def _unique_base(backup_dir: str, base: str) -> str:
    candidate = base
    n = 2
    while os.path.exists(os.path.join(backup_dir, candidate + ".json")) or os.path.exists(
        os.path.join(backup_dir, candidate + ".sql")
    ):
        candidate = f"{base}_{n}"
        n += 1
    return candidate


def build_backup_payload(change_set: "ChangeSet", now: datetime) -> dict:
    """退避 JSON の内容。更新・削除対象の行は全列を記録する。"""
    return {
        "app": f"{APP_NAME} {APP_VERSION}",
        "created_at_local": now.strftime("%Y-%m-%d %H:%M:%S"),
        "operation": change_set.operation,
        "table": "MI_ACTION_GROUP_POINT",
        "delete_mode": DELETE_MODE,
        "note": "日時の列は DB の値（UTC）",
        "updates": [
            {
                "group_name": op.group_name,
                "before_alias": op.before_alias,
                "before": op.before.to_backup_dict(),
                "after": {
                    "PointID": op.point_id,
                    "Alias": op.alias,
                    "TriggersExecution": op.triggers_execution,
                    "LocalName": op.local_name,
                },
            }
            for op in change_set.updates
        ],
        "deletes": [
            {"group_name": op.group_name, "alias": op.alias, "before": op.before.to_backup_dict()}
            for op in change_set.deletes
        ],
        # 追加は実行前のため行がない。参考として計画内容を記録する
        "inserts_planned": [
            {
                "group_name": op.group_name,
                "ActionGroupID": op.action_group_id,
                "PointID": op.point_id,
                "Alias": op.alias,
                "TriggersExecution": op.triggers_execution,
                "LocalName": op.local_name,
            }
            for op in change_set.inserts
        ],
    }


def build_restore_sql(change_set: "ChangeSet", now: datetime) -> str:
    """同じ内容を元に戻すための SQL（手動実行用。アプリからは実行しない）。"""
    L = sql_literal
    lines = [
        f"-- {APP_NAME} 復元用 SQL",
        f"-- 作成日時: {now:%Y-%m-%d %H:%M:%S}",
        f"-- 操作: {change_set.operation}",
        f"-- 削除方式: {DELETE_MODE}",
        "-- 注意: 本ファイルはアプリからは実行されません。内容を確認したうえで手動で実行してください。",
        "-- 実行順: 1. 追加した行の削除 → 2. 更新の取り消し → 3. 削除した行の復元",
        "",
        "SET XACT_ABORT ON;",
        "BEGIN TRANSACTION;",
        "",
    ]

    if change_set.inserts:
        lines.append("-- 1. 追加した行の削除（ActionGroupID + PointID は一意）")
        for op in change_set.inserts:
            lines.append(f"-- {op.group_name} / {op.alias}")
            lines.append(
                "DELETE FROM MI_ACTION_GROUP_POINT "
                f"WHERE ActionGroupID = {L(op.action_group_id)} AND PointID = {L(op.point_id)};"
            )
        lines.append("")

    if change_set.updates:
        lines.append("-- 2. 更新した行を更新前の値に戻す")
        for op in change_set.updates:
            b = op.before
            lines.append(f"-- {op.group_name} / {op.before_alias}")
            lines.append(
                "UPDATE MI_ACTION_GROUP_POINT SET "
                f"PointID = {L(b.point_id)}, TriggersExecution = {L(b.triggers_execution)}, "
                f"LocalName = {L(b.local_name)}, LastUpdateOn = {L(b.last_update_on)}, "
                f"LastUpdatedBy = {L(b.last_updated_by)}, RowVersionStamp = {L(b.row_version_stamp)} "
                f"WHERE ID = {L(b.id)};"
            )
        lines.append("")

    if change_set.deletes:
        if DELETE_MODE == "physical":
            lines.append("-- 3. 削除した行の復元（元の ID を維持するため IDENTITY_INSERT を使用）")
            lines.append("SET IDENTITY_INSERT MI_ACTION_GROUP_POINT ON;")
            for op in change_set.deletes:
                b = op.before
                lines.append(f"-- {op.group_name} / {op.alias}")
                lines.append(
                    "INSERT INTO MI_ACTION_GROUP_POINT "
                    "(ID, ActionGroupID, PointID, TriggersExecution, LocalName, Active, "
                    "RowVersionStamp, CreatedOn, CreatedBy, LastUpdateOn, LastUpdatedBy) VALUES ("
                    f"{L(b.id)}, {L(b.action_group_id)}, {L(b.point_id)}, "
                    f"{L(b.triggers_execution)}, {L(b.local_name)}, {L(b.active)}, "
                    f"{L(b.row_version_stamp)}, {L(b.created_on)}, {L(b.created_by)}, "
                    f"{L(b.last_update_on)}, {L(b.last_updated_by)});"
                )
            lines.append("SET IDENTITY_INSERT MI_ACTION_GROUP_POINT OFF;")
        else:
            lines.append("-- 3. 論理削除した行を有効に戻す")
            for op in change_set.deletes:
                b = op.before
                lines.append(f"-- {op.group_name} / {op.alias}")
                lines.append(
                    "UPDATE MI_ACTION_GROUP_POINT SET "
                    f"Active = {L(b.active)}, LastUpdateOn = {L(b.last_update_on)}, "
                    f"LastUpdatedBy = {L(b.last_updated_by)}, RowVersionStamp = {L(b.row_version_stamp)} "
                    f"WHERE ID = {L(b.id)};"
                )
        lines.append("")

    lines.append("COMMIT TRANSACTION;")
    lines.append("")
    return "\n".join(lines)


def write_backup(change_set: "ChangeSet", backup_dir: str,
                 now: datetime | None = None) -> tuple[str, str]:
    """退避ファイル（.json と .sql）を書き出し、そのパスを返す。失敗時は例外を送出する。"""
    now = now or datetime.now()
    os.makedirs(backup_dir, exist_ok=True)
    base = _unique_base(backup_dir, f"{now:%Y%m%d_%H%M%S}_{_safe_filename(change_set.operation)}")
    json_path = os.path.join(backup_dir, base + ".json")
    sql_path = os.path.join(backup_dir, base + ".sql")

    payload = build_backup_payload(change_set, now)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())

    # SSMS で文字化けしないよう BOM 付き UTF-8、改行 CRLF
    with open(sql_path, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(build_restore_sql(change_set, now))
        f.flush()
        os.fsync(f.fileno())

    # 書き出した内容を読み戻して確認する（JSON として読めること）
    with open(json_path, "r", encoding="utf-8") as f:
        json.load(f)
    return json_path, sql_path


# ----------------------------------------------------------------------
# ログ
# ----------------------------------------------------------------------
_log_lock = threading.Lock()


def log_file_path(log_dir: str, now: datetime | None = None) -> str:
    now = now or datetime.now()
    return os.path.join(log_dir, f"{APP_NAME}_{now:%Y%m}.log")


def append_log_line(log_dir: str, text: str, now: datetime | None = None) -> None:
    """ログファイルへ 1 行（複数行可）追記する。"""
    now = now or datetime.now()
    os.makedirs(log_dir, exist_ok=True)
    with _log_lock:
        with open(log_file_path(log_dir, now), "a", encoding="utf-8") as f:
            f.write(f"{now:%Y-%m-%d %H:%M:%S} {text}\n")


def write_operation_log(log_dir: str, user: str, operation: str, counts: str,
                        backup_file: str, result: str) -> None:
    """操作ログ（日時、実行者、操作、対象件数、退避ファイル名、結果）を記録する。"""
    try:
        append_log_line(
            log_dir,
            f"[OPERATION] 実行者={user or '-'} 操作={operation} 対象件数={counts} "
            f"退避ファイル={os.path.basename(backup_file) if backup_file else '-'} 結果={result}",
        )
    except Exception:
        logger.exception("操作ログの書き込みに失敗しました")


def _counts_text(change_set: "ChangeSet") -> str:
    return (f"追加 {len(change_set.inserts)} / 更新 {len(change_set.updates)} / "
            f"削除 {len(change_set.deletes)}")


# ----------------------------------------------------------------------
# 実行
# ----------------------------------------------------------------------
def execute_change_set(
    change_set: "ChangeSet",
    database: "Database",
    updated_by: str,
    backup_dir: str,
    log_dir: str,
    progress: Callable[[int, int], None] | None = None,
) -> ExecutionResult:
    """退避 → トランザクション実行 → ログ。ワーカースレッドから呼ぶこと。"""
    user = (updated_by or "").strip()
    counts = _counts_text(change_set)

    err = validate_updated_by(user)
    if err:
        return ExecutionResult(False, err)
    if change_set.is_empty:
        return ExecutionResult(False, "実行する変更がありません。")

    # 1. 退避（失敗したら実行しない）
    try:
        json_path, sql_path = write_backup(change_set, backup_dir)
    except Exception as e:
        logger.exception("退避ファイルの書き出しに失敗しました")
        write_operation_log(log_dir, user, change_set.operation, counts, "",
                            f"中止（退避失敗: {e}）")
        return ExecutionResult(
            False,
            "退避ファイルの書き出しに失敗したため、実行を中止しました。DB は変更されていません。",
            detail=traceback.format_exc(),
        )

    # 2. 1 トランザクションで実行（DELETE → UPDATE → INSERT）
    stmts = build_statements(change_set, user)
    total = len(stmts)
    conn = None
    try:
        conn = database.connect(readonly=False)
        cur = conn.cursor()
        # サーバー既定が NOCOUNT ON だと rowcount が -1 になり同時更新の検出ができないため、
        # セッション設定として明示的に OFF にする（データは変更しない）
        cur.execute("SET NOCOUNT OFF;")
        for i, (kind, sql, params, op) in enumerate(stmts, start=1):
            cur.execute(sql, params)
            if kind in ("update", "delete"):
                affected = cur.rowcount
                if affected != 1:
                    raise ConcurrencyError(
                        f"{_KIND_LABELS[kind]}対象の行（ID={op.before.id}、{op.group_name} / "
                        f"{getattr(op, 'alias', '')}）の影響行数が {affected} でした。"
                        "読み込み後に他で更新または削除された可能性があります。"
                    )
            if progress:
                progress(i, total)
        conn.commit()
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                logger.exception("ロールバックに失敗しました")
        is_conflict = isinstance(e, ConcurrencyError)
        if is_conflict:
            message = ("他で更新された行があったため、すべての変更を取り消しました（ロールバック）。\n"
                       "再読込してから、もう一度編集してください。")
        else:
            message = "DB への書き込みに失敗したため、すべての変更を取り消しました（ロールバック）。"
        logger.error("ChangeSet の実行に失敗しました: %s", e, exc_info=not is_conflict)
        write_operation_log(log_dir, user, change_set.operation, counts, json_path,
                            f"失敗（ロールバック）: {e}")
        return ExecutionResult(
            False,
            message,
            detail=f"{e}\n\n{traceback.format_exc()}",
            backup_json=json_path,
            backup_sql=sql_path,
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    write_operation_log(log_dir, user, change_set.operation, counts, json_path, "成功")
    return ExecutionResult(
        True,
        f"保存しました（{counts}）。",
        backup_json=json_path,
        backup_sql=sql_path,
        inserted=len(change_set.inserts),
        updated=len(change_set.updates),
        deleted=len(change_set.deletes),
    )


class ChangeSetExecutor(QObject):
    """execute_change_set をワーカースレッドで実行し、結果を Signal で返す。"""

    progress = Signal(int, int)   # (完了数, 全体数)
    finished = Signal(object)     # ExecutionResult

    def __init__(self, change_set: "ChangeSet", database: "Database", updated_by: str,
                 backup_dir: str, log_dir: str):
        super().__init__()
        self._change_set = change_set
        self._database = database
        self._updated_by = updated_by
        self._backup_dir = backup_dir
        self._log_dir = log_dir

    @Slot()
    def run(self) -> None:
        try:
            result = execute_change_set(
                self._change_set,
                self._database,
                self._updated_by,
                self._backup_dir,
                self._log_dir,
                progress=lambda done, total: self.progress.emit(done, total),
            )
        except Exception as e:  # 想定外の例外も必ず結果として返す
            logger.exception("ChangeSet の実行中に予期しないエラーが発生しました")
            result = ExecutionResult(False, f"予期しないエラーが発生しました: {e}",
                                     detail=traceback.format_exc())
        self.finished.emit(result)
