"""読み取りクエリ

起動時（および「再読込」時）に MI 設定を一括で読み、MIConfigSnapshot を返す。
接続は読み取り用（autocommit、READ UNCOMMITTED）を使い、終わったら閉じる。

本モジュールは GUI スレッドから呼ばないこと（ワーカースレッドで使う）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from core.db import Database
from core.models import (
    Action,
    ActionGroup,
    DataSource,
    GroupPoint,
    MIConfigSnapshot,
    Point,
    PointGroup,
    Script,
)

# 進捗コールバック: (表示名, 完了したテーブル数, 全テーブル数)
ProgressCallback = Callable[[str, int, int], None]

# 読み取りクエリ（テーブル名・列名は固定文字列。パラメータなし）
SQL_DATA_SOURCES = "SELECT ID, Alias, Enabled FROM MI_DATA_SOURCE WHERE Active = 1;"

SQL_POINT_GROUPS = (
    "SELECT ID, DataSourceID, Alias, ParentID FROM MI_POINT_GROUP WHERE Active = 1;"
)

SQL_POINTS = """
SELECT ID, PointGroupID, Alias, DataType, Access_, Enabled, InitialValue, Parameters
FROM MI_POINT WHERE Active = 1;
"""

SQL_ACTION_GROUPS = """
SELECT ID, Name, Enabled, Comment_, OnStartupExecution, OnShutdownExecution,
       PeriodicalExecution, PeriodicalInterval, PointChangeExecution,
       PointChangeExpression, OnRefreshExecution
FROM MI_ACTION_GROUP WHERE Active = 1;
"""

SQL_ACTIONS = """
SELECT ID, Name, ActionGroupID, ActionType, Enabled, SequenceNo, ScriptId
FROM MI_ACTION WHERE Active = 1;
"""

SQL_SCRIPTS = """
SELECT s.ID, s.Language, s.Script, s.Author, s.LastUpdateOn, s.LastUpdatedBy
FROM SCRIPT s
WHERE s.ID IN (SELECT ScriptId FROM MI_ACTION WHERE Active = 1 AND ScriptId IS NOT NULL);
"""

# Active = 0 の行も読む（無効参照の検出と一意制約の判定に使う）
SQL_GROUP_POINTS = """
SELECT ID, ActionGroupID, PointID, TriggersExecution, LocalName, Active, RowVersionStamp,
       CreatedOn, CreatedBy, LastUpdateOn, LastUpdatedBy
FROM MI_ACTION_GROUP_POINT;
"""

_STEPS = [
    ("データソース", SQL_DATA_SOURCES),
    ("ポイントグループ", SQL_POINT_GROUPS),
    ("ポイント", SQL_POINTS),
    ("アクショングループ", SQL_ACTION_GROUPS),
    ("アクション", SQL_ACTIONS),
    ("スクリプト", SQL_SCRIPTS),
    ("グループのポイント設定", SQL_GROUP_POINTS),
]


def _bool(v) -> bool:
    return bool(v) if v is not None else False


def _opt_bool(v) -> bool | None:
    return None if v is None else bool(v)


def _opt_int(v) -> int | None:
    return None if v is None else int(v)


def load_snapshot(database: Database, progress: ProgressCallback | None = None) -> MIConfigSnapshot:
    """4.8 のクエリを順に実行して MIConfigSnapshot を返す。"""
    total = len(_STEPS)
    results: dict[str, list] = {}

    with database.open(readonly=True) as conn:
        cur = conn.cursor()
        for i, (label, sql) in enumerate(_STEPS):
            if progress:
                progress(label, i, total)
            cur.execute(sql)
            results[label] = cur.fetchall()
        cur.close()
    if progress:
        progress("索引の作成", total, total)

    data_sources = {
        int(r[0]): DataSource(id=int(r[0]), alias=r[1] or "", enabled=_bool(r[2]))
        for r in results["データソース"]
    }
    point_groups = {
        int(r[0]): PointGroup(
            id=int(r[0]), data_source_id=int(r[1]), alias=r[2] or "", parent_id=_opt_int(r[3])
        )
        for r in results["ポイントグループ"]
    }
    points = {
        int(r[0]): Point(
            id=int(r[0]),
            point_group_id=int(r[1]),
            alias=r[2] or "",
            data_type=_opt_int(r[3]),
            access=_opt_int(r[4]),
            enabled=_bool(r[5]),
            initial_value=r[6],
            parameters=r[7],
        )
        for r in results["ポイント"]
    }
    action_groups = {
        int(r[0]): ActionGroup(
            id=int(r[0]),
            name=r[1] or "",
            enabled=_bool(r[2]),
            comment=r[3],
            on_startup_execution=_opt_bool(r[4]),
            on_shutdown_execution=_opt_bool(r[5]),
            periodical_execution=_opt_bool(r[6]),
            periodical_interval=_opt_int(r[7]),
            point_change_execution=_opt_bool(r[8]),
            point_change_expression=r[9],
            on_refresh_execution=_opt_bool(r[10]),
        )
        for r in results["アクショングループ"]
    }
    actions = {
        int(r[0]): Action(
            id=int(r[0]),
            name=r[1] or "",
            action_group_id=int(r[2]),
            action_type=_opt_int(r[3]),
            enabled=_bool(r[4]),
            sequence_no=_opt_int(r[5]),
            script_id=_opt_int(r[6]),
        )
        for r in results["アクション"]
    }
    scripts = {
        int(r[0]): Script(
            id=int(r[0]),
            language=r[1],
            script_xml=r[2],
            author=r[3],
            last_update_on=r[4],
            last_updated_by=r[5],
        )
        for r in results["スクリプト"]
    }
    group_points = [
        GroupPoint(
            id=int(r[0]),
            action_group_id=int(r[1]),
            point_id=int(r[2]),
            triggers_execution=_opt_bool(r[3]),
            local_name=r[4],
            active=_bool(r[5]),
            row_version_stamp=_opt_int(r[6]),
            created_on=r[7],
            created_by=r[8],
            last_update_on=r[9],
            last_updated_by=r[10],
        )
        for r in results["グループのポイント設定"]
    ]

    return MIConfigSnapshot(
        data_sources=data_sources,
        point_groups=point_groups,
        points=points,
        action_groups=action_groups,
        actions=actions,
        scripts=scripts,
        group_points=group_points,
        loaded_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
