"""データクラス

DB から読み込んだ MI 設定（データソース・ポイントグループ・ポイント・
アクショングループ・アクション・スクリプト・グループのポイント設定）と、
それらを保持して索引を持つ MIConfigSnapshot を定義する。

日時は内部では UTC（DB の値のまま）で保持し、表示時のみ JST に変換する。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from core.script_parser import (
    OpcUaPointProps,
    ParsedScript,
    parse_point_parameters,
    parse_script,
)

# スクリプトアクションの ActionType
ACTION_TYPE_SCRIPT = 5

# MI_POINT.DataType（確認済みは 1・2 のみ。他の値はコード値をそのまま表示する: 未確認5）
DATA_TYPE_LABELS = {1: "文字", 2: "整数"}

# MI_POINT.Access_
ACCESS_LABELS = {0: "ReadOnly", 1: "WriteOnly", 2: "ReadWrite"}

JST_OFFSET = timedelta(hours=9)


# ----------------------------------------------------------------------
# 表示用ヘルパー
# ----------------------------------------------------------------------
def data_type_label(value: int | None) -> str:
    if value is None:
        return ""
    return DATA_TYPE_LABELS.get(value, str(value))


def access_label(value: int | None) -> str:
    if value is None:
        return ""
    return ACCESS_LABELS.get(value, str(value))


def to_jst(dt: datetime | None) -> datetime | None:
    """UTC の日時を JST（UTC+9）に変換する。tz なしの値は UTC とみなす。"""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt + JST_OFFSET


def format_jst(dt: datetime | None) -> str:
    """UTC の日時を JST の文字列にする。"""
    jst = to_jst(dt)
    if jst is None:
        return ""
    return jst.strftime("%Y/%m/%d %H:%M:%S")


def yes_no(value: bool | None) -> str:
    if value is None:
        return ""
    return "はい" if value else "いいえ"


def on_off(value: bool | None) -> str:
    if value is None:
        return "(NULL)"
    return "ON" if value else "OFF"


def split_group_name(name: str) -> tuple[str, str]:
    """アクショングループ名を（設備, 処理名）に分解する。

    "AG_" を除き、最後の "_" で分ける。
      AG_HJMG56501101_PaletteInquiry → ("HJMG56501101", "PaletteInquiry")
      AG_BATEY0080_S                 → ("BATEY0080", "S")
    規則に合わないもの（"AG_" で始まらない、"_" がない、設備部分に "_" を含む。
    例: AG_BATEY0080_S_finished）は、処理名を空にせず名前全体を保持し、設備は空とする。
    """
    if name.startswith("AG_"):
        body = name[3:]
        equipment, sep, process = body.rpartition("_")
        if sep and equipment and process and "_" not in equipment:
            return equipment, process
    return "", name


# ----------------------------------------------------------------------
# エンティティ
# ----------------------------------------------------------------------
@dataclass
class DataSource:
    id: int
    alias: str
    enabled: bool


@dataclass
class PointGroup:
    id: int
    data_source_id: int
    alias: str
    parent_id: int | None


@dataclass
class Point:
    id: int
    point_group_id: int
    alias: str
    data_type: int | None
    access: int | None
    enabled: bool
    initial_value: str | None
    parameters: str | None = field(default=None, repr=False)

    _props: OpcUaPointProps | None = field(default=None, init=False, repr=False, compare=False)
    _props_parsed: bool = field(default=False, init=False, repr=False, compare=False)

    def opc_props(self) -> OpcUaPointProps | None:
        """Parameters の解析結果（初回参照時に解析してキャッシュする）。"""
        if not self._props_parsed:
            self._props = parse_point_parameters(self.parameters)
            self._props_parsed = True
        return self._props

    @property
    def opc_key(self) -> str:
        p = self.opc_props()
        return p.key if p else ""

    @property
    def publishing_interval(self) -> str:
        p = self.opc_props()
        return p.publishing_interval if p else ""


@dataclass
class ActionGroup:
    id: int
    name: str
    enabled: bool
    comment: str | None
    on_startup_execution: bool | None
    on_shutdown_execution: bool | None
    periodical_execution: bool | None
    periodical_interval: int | None
    point_change_execution: bool | None
    point_change_expression: str | None
    on_refresh_execution: bool | None
    equipment: str = field(default="", init=False)
    process: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self.equipment, self.process = split_group_name(self.name)

    @property
    def name_conforms(self) -> bool:
        """グループ名が AG_<設備>_<処理> の規則に合うか。"""
        return bool(self.equipment)


@dataclass
class Action:
    id: int
    name: str
    action_group_id: int
    action_type: int | None
    enabled: bool
    sequence_no: int | None
    script_id: int | None


@dataclass
class Script:
    id: int
    language: str | None
    script_xml: str | None = field(repr=False)
    author: str | None
    last_update_on: datetime | None
    last_updated_by: str | None

    _parsed: ParsedScript | None = field(default=None, init=False, repr=False, compare=False)

    def parsed(self) -> ParsedScript:
        """SCRIPT.Script の解析結果（初回参照時に解析してキャッシュする）。"""
        if self._parsed is None:
            self._parsed = parse_script(self.script_xml)
        return self._parsed

    @property
    def source(self) -> str:
        return self.parsed().source


@dataclass
class GroupPoint:
    """MI_ACTION_GROUP_POINT の 1 行。"""

    id: int
    action_group_id: int
    point_id: int
    triggers_execution: bool | None
    local_name: str | None
    active: bool
    row_version_stamp: int | None
    created_on: datetime | None
    created_by: str | None
    last_update_on: datetime | None
    last_updated_by: str | None

    def to_backup_dict(self) -> dict:
        """退避用（全列）。日時は UTC の ISO 形式文字列。"""

        def iso(dt: datetime | None) -> str | None:
            return dt.isoformat(sep=" ") if dt is not None else None

        return {
            "ID": self.id,
            "ActionGroupID": self.action_group_id,
            "PointID": self.point_id,
            "TriggersExecution": self.triggers_execution,
            "LocalName": self.local_name,
            "Active": self.active,
            "RowVersionStamp": self.row_version_stamp,
            "CreatedOn": iso(self.created_on),
            "CreatedBy": self.created_by,
            "LastUpdateOn": iso(self.last_update_on),
            "LastUpdatedBy": self.last_updated_by,
        }


@dataclass
class ImportRow:
    """インポートファイルの 1 行（編集可能な列のみ）。"""

    line_no: int
    group_name: str
    alias: str
    local_name: str | None
    triggers_execution: bool | None
    has_local_name: bool = True   # ファイルにローカル名の列があるか
    has_triggers: bool = True     # ファイルにトリガーの実行の列があるか
    error: str = ""               # 値の形式エラー（あれば取り込み対象外）


# ----------------------------------------------------------------------
# スナップショット
# ----------------------------------------------------------------------
class MIConfigSnapshot:
    """DB から一括で読み込んだ MI 設定と索引。"""

    def __init__(
        self,
        data_sources: dict[int, DataSource],
        point_groups: dict[int, PointGroup],
        points: dict[int, Point],
        action_groups: dict[int, ActionGroup],
        actions: dict[int, Action],
        scripts: dict[int, Script],
        group_points: list[GroupPoint],
        loaded_at: datetime | None = None,
    ):
        self.data_sources = data_sources
        self.point_groups = point_groups
        self.points = points
        self.action_groups = action_groups
        self.actions = actions
        self.scripts = scripts
        self.group_points = group_points
        self.loaded_at = loaded_at or datetime.now(timezone.utc).replace(tzinfo=None)

        self.point_path_by_id: dict[int, str] = {}
        self.point_id_by_path: dict[str, int] = {}
        self.group_points_by_group: dict[int, list[GroupPoint]] = {}
        self.group_point_by_id: dict[int, GroupPoint] = {}
        self.actions_by_group: dict[int, list[Action]] = {}
        self.action_group_by_name: dict[str, ActionGroup] = {}
        self.points_by_data_source: dict[int, list[Point]] = {}
        self._build_indexes()

    # --- 索引 ---------------------------------------------------------
    def _build_indexes(self) -> None:
        # ポイントのフルパス
        for p in self.points.values():
            path = self._build_point_path(p)
            self.point_path_by_id[p.id] = path
            # 同じパスが複数ある場合（通常は一意制約で起こらない）は先勝ち
            self.point_id_by_path.setdefault(path, p.id)

        # データソース → ポイント
        by_ds: dict[int, list[Point]] = defaultdict(list)
        for p in self.points.values():
            pg = self.point_groups.get(p.point_group_id)
            if pg is not None:
                by_ds[pg.data_source_id].append(p)
        for ds_id, pts in by_ds.items():
            pts.sort(key=lambda x: self.point_path_by_id.get(x.id, ""))
        self.points_by_data_source = dict(by_ds)

        # グループ → GroupPoint（Active = 0 の行も含む。ID 順）
        by_group: dict[int, list[GroupPoint]] = defaultdict(list)
        for gp in self.group_points:
            by_group[gp.action_group_id].append(gp)
            self.group_point_by_id[gp.id] = gp
        for rows in by_group.values():
            rows.sort(key=lambda r: r.id)
        self.group_points_by_group = dict(by_group)

        # グループ → アクション（SequenceNo, ID 順）
        acts: dict[int, list[Action]] = defaultdict(list)
        for a in self.actions.values():
            acts[a.action_group_id].append(a)
        for rows in acts.values():
            rows.sort(key=lambda a: (a.sequence_no if a.sequence_no is not None else 0, a.id))
        self.actions_by_group = dict(acts)

        self.action_group_by_name = {g.name: g for g in self.action_groups.values()}

    def point_group_path(self, point_group_id: int) -> str:
        """ポイントグループの「データソース\\親\\…\\子」部分。ParentID を辿る。"""
        parts: list[str] = []
        visited: set[int] = set()
        cur = self.point_groups.get(point_group_id)
        ds_id: int | None = cur.data_source_id if cur else None
        if cur is None:
            parts.append(f"?PG{point_group_id}")
        while cur is not None and cur.id not in visited:
            visited.add(cur.id)
            parts.append(cur.alias)
            if cur.parent_id is None:
                break
            parent = self.point_groups.get(cur.parent_id)
            if parent is None:
                # 親が読み込まれていない（Active = 0 等）
                parts.append(f"?PG{cur.parent_id}")
                break
            cur = parent
        parts.reverse()
        ds = self.data_sources.get(ds_id) if ds_id is not None else None
        ds_alias = ds.alias if ds else f"?DS{ds_id if ds_id is not None else ''}"
        return "\\".join([ds_alias] + parts)

    def _build_point_path(self, p: Point) -> str:
        return f"{self.point_group_path(p.point_group_id)}\\{p.alias}"

    # --- 参照 ---------------------------------------------------------
    def point_path(self, point_id: int | None) -> str:
        """ポイントのフルパス。読み込まれていないポイントは「?ID=nnn」を返す。"""
        if point_id is None:
            return ""
        path = self.point_path_by_id.get(point_id)
        if path is None:
            return f"?ID={point_id}"
        return path

    def resolve_path(self, path: str) -> int | None:
        """フルパスに一致するポイント ID（Active = 1 のポイントのみ）。"""
        if not path:
            return None
        return self.point_id_by_path.get(path.strip())

    def active_group_points(self, group_id: int) -> list[GroupPoint]:
        return [gp for gp in self.group_points_by_group.get(group_id, []) if gp.active]

    def inactive_point_ids(self, group_id: int) -> set[int]:
        """Active = 0 の行が参照しているポイント ID（一意制約の判定に使う: 未確認7）。"""
        return {gp.point_id for gp in self.group_points_by_group.get(group_id, []) if not gp.active}

    def data_source_of_point(self, point_id: int) -> DataSource | None:
        p = self.points.get(point_id)
        if p is None:
            return None
        pg = self.point_groups.get(p.point_group_id)
        if pg is None:
            return None
        return self.data_sources.get(pg.data_source_id)

    def actions_of_group(self, group_id: int) -> list[Action]:
        return self.actions_by_group.get(group_id, [])

    def script_for_action(self, action: Action | None) -> Script | None:
        if action is None or action.script_id is None:
            return None
        return self.scripts.get(action.script_id)

    def first_script_action(self, group_id: int) -> Action | None:
        """グループの先頭のアクション（スクリプトを持つものを優先）。"""
        acts = self.actions_of_group(group_id)
        for a in acts:
            if self.script_for_action(a) is not None:
                return a
        return acts[0] if acts else None

    def script_actions(self) -> list[tuple[ActionGroup, Action, Script]]:
        """スクリプトを持つアクションの一覧（グループ名・アクション名順）。"""
        out: list[tuple[ActionGroup, Action, Script]] = []
        for g in self.groups_sorted():
            for a in self.actions_of_group(g.id):
                s = self.script_for_action(a)
                if s is not None:
                    out.append((g, a, s))
        return out

    def groups_sorted(self) -> list[ActionGroup]:
        return sorted(self.action_groups.values(), key=lambda g: g.name.lower())

    def groups_with_same_process(self, group: ActionGroup) -> list[ActionGroup]:
        """処理名が同じグループ（自身を含む、名前順）。規則に合わない名前は自身のみ。"""
        if not group.name_conforms:
            return [group]
        return [
            g
            for g in self.groups_sorted()
            if g.name_conforms and g.process == group.process
        ]

    @property
    def active_group_point_count(self) -> int:
        return sum(1 for gp in self.group_points if gp.active)
