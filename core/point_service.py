"""ポイント設定の変更計画（ChangeSet）生成

GUI から渡された編集内容（追加・更新・削除、置換ルール、インポートデータ）を検証し、
ChangeSet を返す。**DB には触れない。**

検証内容:
  - エイリアスの解決（フルパス → MI_POINT.ID。Active = 1 のポイントのみ）
  - 一意制約 (ActionGroupID, PointID)。Active = 0 の行も含めて判定する（未確認7）
  - LocalName の長さ（255）
  - 同一グループ内の LocalName 重複（警告）
  - RowVersionStamp が NULL の行は同時更新の検出ができないため更新・削除の対象外
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from core.change_executor import delete_frees_point
from core.models import GroupPoint, ImportRow, MIConfigSnapshot

LOCAL_NAME_MAX_LENGTH = 255

# 対象外の理由
REASON_UNRESOLVED = "未解決のエイリアス（一致するポイントがありません）"
REASON_POINT_MISSING = "参照先のポイントが存在しないか無効（Active = 0）です"
REASON_INACTIVE_DUP = "無効化された同一ポイントの行が存在する（Active = 0）ため、一意制約に抵触します"
REASON_DUP = "同じポイントの行がグループ内に既に存在するため、一意制約に抵触します"
REASON_LOCAL_NAME_TOO_LONG = f"ローカル名が {LOCAL_NAME_MAX_LENGTH} 文字を超えています"
REASON_NO_ROW_VERSION = "RowVersionStamp が NULL のため同時更新の検出ができません"
REASON_GROUP_MISSING = "アクショングループが存在しません"


def normalize_local_name(value: str | None) -> str | None:
    """空文字は NULL として扱う。"""
    if value is None:
        return None
    return value if value != "" else None


def _trig_eq(a: bool | None, b: bool | None) -> bool:
    """TriggersExecution の比較（NULL と 0 は同じとみなす）。"""
    return bool(a) == bool(b)


# ----------------------------------------------------------------------
# 編集中の行
# ----------------------------------------------------------------------
@dataclass(eq=False)
class EditRow:
    """編集中の MI_ACTION_GROUP_POINT の 1 行（未保存の変更を保持する）。

    original が None なら追加行。deleted が True なら削除予定。
    point_id が None の行は「未解決」（unresolved_alias に入力されたエイリアスを保持）。
    """

    action_group_id: int
    original: GroupPoint | None
    point_id: int | None
    local_name: str | None
    triggers_execution: bool | None
    deleted: bool = False
    unresolved_alias: str = ""

    @classmethod
    def from_group_point(cls, gp: GroupPoint) -> "EditRow":
        return cls(
            action_group_id=gp.action_group_id,
            original=gp,
            point_id=gp.point_id,
            local_name=gp.local_name,
            triggers_execution=gp.triggers_execution,
        )

    @classmethod
    def new(cls, action_group_id: int, point_id: int | None, local_name: str | None,
            triggers_execution: bool | None, unresolved_alias: str = "") -> "EditRow":
        return cls(
            action_group_id=action_group_id,
            original=None,
            point_id=point_id,
            local_name=normalize_local_name(local_name),
            triggers_execution=triggers_execution,
            unresolved_alias=unresolved_alias,
        )

    @property
    def is_new(self) -> bool:
        return self.original is None

    def changed_fields(self) -> set[str]:
        """元の値から変わった項目（"point_id" / "local_name" / "triggers_execution"）。"""
        if self.original is None:
            return {"point_id", "local_name", "triggers_execution"}
        o = self.original
        changed: set[str] = set()
        if self.point_id != o.point_id:
            changed.add("point_id")
        if normalize_local_name(self.local_name) != normalize_local_name(o.local_name):
            changed.add("local_name")
        if not _trig_eq(self.triggers_execution, o.triggers_execution):
            changed.add("triggers_execution")
        return changed

    @property
    def is_modified(self) -> bool:
        return self.original is not None and not self.deleted and bool(self.changed_fields())

    @property
    def has_change(self) -> bool:
        return self.is_new or self.deleted or self.is_modified

    def revert(self) -> None:
        """元の値に戻す（追加行には使わない）。"""
        if self.original is None:
            return
        self.point_id = self.original.point_id
        self.local_name = self.original.local_name
        self.triggers_execution = self.original.triggers_execution
        self.deleted = False
        self.unresolved_alias = ""


def row_alias(snapshot: MIConfigSnapshot, row: EditRow) -> str:
    """行のエイリアス（フルパス）。未解決の行は入力されたエイリアス。"""
    if row.point_id is None:
        return row.unresolved_alias
    return snapshot.point_path(row.point_id)


# ----------------------------------------------------------------------
# ChangeSet
# ----------------------------------------------------------------------
@dataclass
class InsertOp:
    action_group_id: int
    group_name: str
    point_id: int
    alias: str
    local_name: str | None
    triggers_execution: bool | None


@dataclass
class UpdateOp:
    before: GroupPoint
    group_name: str
    before_alias: str
    point_id: int
    alias: str
    local_name: str | None
    triggers_execution: bool | None


@dataclass
class DeleteOp:
    before: GroupPoint
    group_name: str
    alias: str


@dataclass
class ExcludedRow:
    """対象外にした行（理由付き）。"""

    group_name: str
    alias: str
    local_name: str
    reason: str


@dataclass
class ChangeSet:
    operation: str
    inserts: list[InsertOp] = field(default_factory=list)
    updates: list[UpdateOp] = field(default_factory=list)
    deletes: list[DeleteOp] = field(default_factory=list)
    excluded: list[ExcludedRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.inserts or self.updates or self.deletes)

    @property
    def total(self) -> int:
        return len(self.inserts) + len(self.updates) + len(self.deletes)

    def summary(self) -> str:
        return (f"追加 {len(self.inserts)} 件 / 更新 {len(self.updates)} 件 / "
                f"削除 {len(self.deletes)} 件 / 対象外 {len(self.excluded)} 件")


def build_change_set(
    snapshot: MIConfigSnapshot,
    rows: list[EditRow],
    operation: str,
    extra_excluded: list[ExcludedRow] | None = None,
) -> ChangeSet:
    """編集中の行（変更のない行を含んでもよい）から ChangeSet を作る。

    rows は一部のグループ・一部の行だけでもよい。一意制約は DB 上の現在の行
    （スナップショット）を基準に判定する。
    """
    cs = ChangeSet(operation=operation)
    if extra_excluded:
        cs.excluded.extend(extra_excluded)

    frees = delete_frees_point()

    by_group: dict[int, list[EditRow]] = defaultdict(list)
    for r in rows:
        if r.has_change:
            by_group[r.action_group_id].append(r)

    groups = sorted(
        by_group.keys(),
        key=lambda gid: (snapshot.action_groups[gid].name.lower()
                         if gid in snapshot.action_groups else "", gid),
    )
    for gid in groups:
        grows = by_group[gid]
        group = snapshot.action_groups.get(gid)
        if group is None:
            for r in grows:
                cs.excluded.append(ExcludedRow(f"(ID={gid})", row_alias(snapshot, r),
                                               r.local_name or "", REASON_GROUP_MISSING))
            continue
        gname = group.name

        def exclude(r: EditRow, reason: str) -> None:
            cs.excluded.append(ExcludedRow(gname, row_alias(snapshot, r), r.local_name or "", reason))

        active_rows = snapshot.active_group_points(gid)
        inactive = snapshot.inactive_point_ids(gid)

        # --- 削除 ---
        accepted_delete_ids: set[int] = set()
        for r in grows:
            if r.original is None or not r.deleted:
                continue
            if r.original.row_version_stamp is None:
                exclude(r, REASON_NO_ROW_VERSION)
                continue
            accepted_delete_ids.add(r.original.id)
            cs.deletes.append(DeleteOp(before=r.original, group_name=gname,
                                       alias=snapshot.point_path(r.original.point_id)))

        # 使用中のポイント（DB 上の現在の行。物理削除する行は解放される）
        occupied: set[int] = {
            gp.point_id for gp in active_rows
            if not (frees and gp.id in accepted_delete_ids)
        }

        # 最終的なローカル名（重複の警告用）: キー → ローカル名
        final_names: dict[tuple[str, int], str | None] = {
            ("db", gp.id): gp.local_name for gp in active_rows if gp.id not in accepted_delete_ids
        }

        # --- 更新 ---
        for r in grows:
            if r.original is None or r.deleted:
                continue
            changed = r.changed_fields()
            if not changed:
                continue
            if r.point_id is None:
                exclude(r, REASON_UNRESOLVED)
                continue
            if "point_id" in changed and r.point_id not in snapshot.points:
                exclude(r, REASON_POINT_MISSING)
                continue
            # ローカル名を変えていない行は元の値（'' を含む）をそのまま書き戻す
            ln = (normalize_local_name(r.local_name) if "local_name" in changed
                  else r.original.local_name)
            if ln is not None and len(ln) > LOCAL_NAME_MAX_LENGTH:
                exclude(r, REASON_LOCAL_NAME_TOO_LONG)
                continue
            if r.original.row_version_stamp is None:
                exclude(r, REASON_NO_ROW_VERSION)
                continue
            if "point_id" in changed:
                if r.point_id in inactive:
                    exclude(r, REASON_INACTIVE_DUP)
                    continue
                if r.point_id in occupied:
                    # 同じ保存の中で別の行がポイントを手放す場合も、実行順によっては
                    # 一意制約に抵触するため安全側で対象外にする（元のポイントは解放しない）
                    exclude(r, REASON_DUP)
                    continue
                occupied.add(r.point_id)
            cs.updates.append(
                UpdateOp(
                    before=r.original,
                    group_name=gname,
                    before_alias=snapshot.point_path(r.original.point_id),
                    point_id=r.point_id,
                    alias=snapshot.point_path(r.point_id),
                    local_name=ln,
                    triggers_execution=r.triggers_execution,
                )
            )
            final_names[("db", r.original.id)] = ln

        # --- 追加 ---
        for r in grows:
            if r.original is not None or r.deleted:
                continue
            if r.point_id is None:
                exclude(r, REASON_UNRESOLVED)
                continue
            if r.point_id not in snapshot.points:
                exclude(r, REASON_POINT_MISSING)
                continue
            ln = normalize_local_name(r.local_name)
            if ln is not None and len(ln) > LOCAL_NAME_MAX_LENGTH:
                exclude(r, REASON_LOCAL_NAME_TOO_LONG)
                continue
            if r.point_id in inactive:
                exclude(r, REASON_INACTIVE_DUP)
                continue
            if r.point_id in occupied:
                exclude(r, REASON_DUP)
                continue
            occupied.add(r.point_id)
            cs.inserts.append(
                InsertOp(
                    action_group_id=gid,
                    group_name=gname,
                    point_id=r.point_id,
                    alias=snapshot.point_path(r.point_id),
                    local_name=ln,
                    triggers_execution=r.triggers_execution,
                )
            )
            final_names[("new", id(r))] = ln

        # --- ローカル名の重複（警告） ---
        counts = Counter(n for n in final_names.values() if n)
        for name, n in sorted(counts.items()):
            if n > 1:
                cs.warnings.append(f"{gname}: ローカル名「{name}」が {n} 行で重複しています")

    return cs


# ----------------------------------------------------------------------
# 置換ルール
# ----------------------------------------------------------------------
@dataclass
class ReplaceRule:
    find: str
    replace: str
    regex: bool = False

    def validate(self) -> str:
        """問題があればエラーメッセージを返す。"""
        if self.regex and self.find:
            try:
                pattern = re.compile(self.find)
            except re.error as e:
                return f"正規表現が不正です（{self.find}）: {e}"
            try:
                # 置換文字列（\1 や \ の扱い）もここで検査する
                pattern.sub(self.replace, "")
            except (re.error, IndexError) as e:
                return (f"置換文字列が不正です（{self.replace}）: {e}\n"
                        "正規表現モードでは \\ を \\\\ と書いてください。")
        return ""

    def apply(self, text: str) -> str:
        if not self.find:
            return text
        if self.regex:
            try:
                return re.sub(self.find, self.replace, text)
            except (re.error, IndexError) as e:
                raise ValueError(f"置換に失敗しました（{self.find} → {self.replace}）: {e}") from e
        return text.replace(self.find, self.replace)


def apply_rules(text: str, rules: list[ReplaceRule]) -> str:
    for rule in rules:
        text = rule.apply(text)
    return text


def validate_rules(rules: list[ReplaceRule]) -> str:
    for rule in rules:
        err = rule.validate()
        if err:
            return err
    return ""


# ----------------------------------------------------------------------
# 一括編集
# ----------------------------------------------------------------------
@dataclass
class BulkEditSpec:
    alias_rule: ReplaceRule | None = None
    local_name_rule: ReplaceRule | None = None
    prefix: str = ""
    suffix: str = ""
    triggers: bool | None = None  # None は「変更しない」


@dataclass
class BulkEditItem:
    row: EditRow
    group_name: str
    before_alias: str
    after_alias: str
    before_local_name: str
    after_local_name: str
    before_triggers: bool | None
    after_triggers: bool | None
    new_point_id: int | None
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def changed(self) -> bool:
        return (
            self.new_point_id != self.row.point_id
            or self.after_local_name != self.before_local_name
            or not _trig_eq(self.after_triggers, self.before_triggers)
        )


def plan_bulk_edit(snapshot: MIConfigSnapshot, rows: list[EditRow],
                   spec: BulkEditSpec) -> list[BulkEditItem]:
    """一括編集の「変更前 → 変更後」を計算する（行は変更しない）。

    置換後のエイリアスに一致するポイントがない行は error（未解決）とし、適用対象から外す。
    """
    for rule in (spec.alias_rule, spec.local_name_rule):
        if rule is not None:
            err = rule.validate()
            if err:
                raise ValueError(err)

    items: list[BulkEditItem] = []
    for r in rows:
        group = snapshot.action_groups.get(r.action_group_id)
        before_alias = row_alias(snapshot, r)
        before_ln = r.local_name or ""
        item = BulkEditItem(
            row=r,
            group_name=group.name if group else "",
            before_alias=before_alias,
            after_alias=before_alias,
            before_local_name=before_ln,
            after_local_name=before_ln,
            before_triggers=r.triggers_execution,
            after_triggers=r.triggers_execution,
            new_point_id=r.point_id,
        )
        if r.deleted:
            item.error = "削除予定の行のため対象外"
            items.append(item)
            continue

        if spec.alias_rule is not None and spec.alias_rule.find:
            after_alias = spec.alias_rule.apply(before_alias)
            item.after_alias = after_alias
            if after_alias != before_alias or r.point_id is None:
                pid = snapshot.resolve_path(after_alias)
                if pid is None:
                    item.error = "未解決: 置換後のエイリアスに一致するポイントがありません"
                else:
                    item.new_point_id = pid

        ln = before_ln
        if spec.local_name_rule is not None and spec.local_name_rule.find:
            ln = spec.local_name_rule.apply(ln)
        if spec.prefix or spec.suffix:
            ln = f"{spec.prefix}{ln}{spec.suffix}"
        item.after_local_name = ln
        if len(ln) > LOCAL_NAME_MAX_LENGTH and not item.error:
            item.error = REASON_LOCAL_NAME_TOO_LONG

        if spec.triggers is not None:
            item.after_triggers = spec.triggers
        items.append(item)
    return items


def apply_bulk_items(items: list[BulkEditItem]) -> list[EditRow]:
    """エラーのない変更を行へ反映し、変更した行を返す。"""
    changed: list[EditRow] = []
    for it in items:
        if not it.ok or not it.changed:
            continue
        r = it.row
        r.point_id = it.new_point_id
        if r.point_id is not None:
            r.unresolved_alias = ""
        r.local_name = normalize_local_name(it.after_local_name)
        r.triggers_execution = it.after_triggers
        changed.append(r)
    return changed


# ----------------------------------------------------------------------
# 他グループから複製 / 連番生成
# ----------------------------------------------------------------------
@dataclass
class NewRowCandidate:
    """取り込み候補の行（複製・連番生成）。"""

    label: str               # 複製元エイリアス、または連番の番号
    alias: str               # 置換後 / 生成したエイリアス
    point_id: int | None
    local_name: str | None
    triggers_execution: bool | None
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def _occupied_points(snapshot: MIConfigSnapshot, target_group_id: int,
                     current_rows: list[EditRow], replace_existing: bool) -> set[int]:
    """取り込み先グループで使用中のポイント（編集中の状態を反映）。"""
    if replace_existing:
        if delete_frees_point():
            return set()
        # 論理削除の場合、削除した行も一意制約に残る
        return {r.original.point_id for r in current_rows if r.original is not None}
    occupied = {r.point_id for r in current_rows if not r.deleted and r.point_id is not None}
    # 削除予定でも元のポイントは（物理削除でなければ）解放されない
    if not delete_frees_point():
        occupied |= {r.original.point_id for r in current_rows if r.original is not None}
    return occupied


def _check_candidate(c: NewRowCandidate, inactive: set[int], occupied: set[int]) -> None:
    if c.point_id is None:
        c.error = REASON_UNRESOLVED
    elif c.point_id in inactive:
        c.error = REASON_INACTIVE_DUP
    elif c.point_id in occupied:
        c.error = REASON_DUP
    elif c.local_name is not None and len(c.local_name) > LOCAL_NAME_MAX_LENGTH:
        c.error = REASON_LOCAL_NAME_TOO_LONG
    else:
        occupied.add(c.point_id)


def plan_copy_from_group(
    snapshot: MIConfigSnapshot,
    source_group_id: int,
    target_group_id: int,
    rules: list[ReplaceRule],
    replace_existing: bool,
    current_target_rows: list[EditRow],
) -> list[NewRowCandidate]:
    """複製元グループのポイント構成を、置換ルールを適用して取り込み先へ複製する計画。

    ローカル名と TriggersExecution は複製元の値を引き継ぐ。
    未解決の行・一意制約に抵触する行は error を設定する（取り込み対象外）。
    """
    err = validate_rules(rules)
    if err:
        raise ValueError(err)
    inactive = snapshot.inactive_point_ids(target_group_id)
    occupied = _occupied_points(snapshot, target_group_id, current_target_rows, replace_existing)

    out: list[NewRowCandidate] = []
    for gp in snapshot.active_group_points(source_group_id):
        src_alias = snapshot.point_path(gp.point_id)
        tgt_alias = apply_rules(src_alias, rules)
        c = NewRowCandidate(
            label=src_alias,
            alias=tgt_alias,
            point_id=snapshot.resolve_path(tgt_alias),
            local_name=normalize_local_name(gp.local_name),
            triggers_execution=gp.triggers_execution,
        )
        _check_candidate(c, inactive, occupied)
        out.append(c)
    return out


_SEQ_TOKEN_RE = re.compile(r"\{n(?::(?P<spec>[^{}]*))?\}")
SEQUENCE_MAX_COUNT = 10000


def expand_sequence_pattern(pattern: str, n: int) -> str:
    """{n} / {n:03} を番号に置き換える（それ以外の文字はそのまま）。"""
    return _SEQ_TOKEN_RE.sub(lambda m: format(n, m.group("spec") or "d"), pattern)


def plan_sequence(
    snapshot: MIConfigSnapshot,
    target_group_id: int,
    local_name_pattern: str,
    alias_pattern: str,
    start: int,
    end: int,
    triggers_execution: bool,
    current_target_rows: list[EditRow],
) -> list[NewRowCandidate]:
    """連番ポイントの行を生成する計画。MI_POINT は作成しない。"""
    if not _SEQ_TOKEN_RE.search(alias_pattern):
        raise ValueError("エイリアスのパターンに {n} が含まれていません。")
    if end < start:
        raise ValueError("終了番号は開始番号以上にしてください。")
    if end - start + 1 > SEQUENCE_MAX_COUNT:
        raise ValueError(f"一度に生成できるのは {SEQUENCE_MAX_COUNT} 件までです。")
    try:
        expand_sequence_pattern(alias_pattern, start)
        expand_sequence_pattern(local_name_pattern, start)
    except ValueError as e:
        raise ValueError(f"番号の書式が不正です: {e}") from e

    inactive = snapshot.inactive_point_ids(target_group_id)
    occupied = _occupied_points(snapshot, target_group_id, current_target_rows, False)
    out: list[NewRowCandidate] = []
    for n in range(start, end + 1):
        alias = expand_sequence_pattern(alias_pattern, n)
        ln = expand_sequence_pattern(local_name_pattern, n) if local_name_pattern else ""
        c = NewRowCandidate(
            label=str(n),
            alias=alias,
            point_id=snapshot.resolve_path(alias),
            local_name=normalize_local_name(ln),
            triggers_execution=triggers_execution,
        )
        _check_candidate(c, inactive, occupied)
        out.append(c)
    return out


# ----------------------------------------------------------------------
# インポート
# ----------------------------------------------------------------------
def plan_import(
    snapshot: MIConfigSnapshot,
    import_rows: list[ImportRow],
    delete_missing: bool,
) -> tuple[list[EditRow], list[ExcludedRow]]:
    """インポートデータを現在の DB と突き合わせ、編集行（追加・更新・削除）を作る。

    キーは「グループ名 + エイリアス」。編集可能なのはローカル名・エイリアス・
    トリガーの実行のみ（エイリアスを変えた行はキーが変わるため追加として扱う）。
    delete_missing=True の場合、ファイルに含まれるグループについて、
    ファイルにない行を削除する（ファイルに現れないグループには触れない）。
    """
    edit_rows: list[EditRow] = []
    excluded: list[ExcludedRow] = []

    seen: set[tuple[int, str]] = set()
    matched_ids: set[int] = set()
    groups_in_file: list[int] = []

    # グループごとに、既存行をエイリアス（フルパス）で引けるようにする
    existing_by_path: dict[int, dict[str, GroupPoint]] = {}

    for ir in import_rows:
        line = f"{ir.line_no} 行目"
        if ir.error:
            excluded.append(ExcludedRow(ir.group_name, ir.alias, ir.local_name or "",
                                        f"{ir.error}（{line}）"))
            continue
        group = snapshot.action_group_by_name.get(ir.group_name)
        if group is None:
            excluded.append(ExcludedRow(ir.group_name, ir.alias, ir.local_name or "",
                                        f"存在しないグループ名です（{line}）"))
            continue
        if group.id not in existing_by_path:
            existing_by_path[group.id] = {
                snapshot.point_path(gp.point_id): gp for gp in snapshot.active_group_points(group.id)
            }
            groups_in_file.append(group.id)

        alias = ir.alias.strip()
        key = (group.id, alias)
        if key in seen:
            excluded.append(ExcludedRow(group.name, alias, ir.local_name or "",
                                        f"ファイル内で「グループ名 + エイリアス」が重複しています（{line}）"))
            continue
        seen.add(key)

        existing = existing_by_path[group.id].get(alias)
        if existing is not None:
            matched_ids.add(existing.id)
            er = EditRow.from_group_point(existing)
            if ir.has_local_name:
                er.local_name = normalize_local_name(ir.local_name)
            if ir.has_triggers and ir.triggers_execution is not None:
                er.triggers_execution = ir.triggers_execution
            if er.has_change:
                edit_rows.append(er)
            continue

        pid = snapshot.resolve_path(alias)
        if pid is None:
            excluded.append(ExcludedRow(group.name, alias, ir.local_name or "",
                                        f"未解決のエイリアスです（{line}）"))
            continue
        trig = ir.triggers_execution if ir.has_triggers and ir.triggers_execution is not None else False
        edit_rows.append(EditRow.new(group.id, pid, ir.local_name if ir.has_local_name else None, trig))

    if delete_missing:
        for gid in groups_in_file:
            for gp in snapshot.active_group_points(gid):
                if gp.id not in matched_ids:
                    er = EditRow.from_group_point(gp)
                    er.deleted = True
                    edit_rows.append(er)

    return edit_rows, excluded
