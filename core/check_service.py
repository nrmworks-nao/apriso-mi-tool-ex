"""チェック（突合・無効参照検出・構成比較・重複検出）

いずれも MIConfigSnapshot を入力とし、表にそのまま出せる行のリスト
（CheckResult.rows: list[list[str]]）を返す。DB には触れない。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from core.models import ActionGroup, MIConfigSnapshot, access_label, data_type_label, on_off
from core.script_parser import extract_point_names_from_source


@dataclass
class CheckResult:
    title: str
    columns: list[str]
    rows: list[list[str]] = field(default_factory=list)
    summary: str = ""


# ----------------------------------------------------------------------
# スクリプトとの突合
# ----------------------------------------------------------------------
def check_script_consistency(snapshot: MIConfigSnapshot) -> CheckResult:
    """スクリプトの入力変数（標準5変数を除く）・ソース中の文字列指定と、
    ポイントの LocalName を照合する。"""
    result = CheckResult(
        title="スクリプトとの突合",
        columns=["グループ名", "アクション名", "種別", "名前", "詳細"],
    )
    checked = 0
    for g in snapshot.groups_sorted():
        pairs = [(a, snapshot.script_for_action(a)) for a in snapshot.actions_of_group(g.id)]
        pairs = [(a, s) for a, s in pairs if s is not None]
        if not pairs:
            continue
        checked += 1
        action_names = ", ".join(a.name for a, _ in pairs)

        inputs: set[str] = set()
        refs: set[str] = set()
        for a, s in pairs:
            ps = s.parsed()
            if ps.error:
                result.rows.append([g.name, a.name, "スクリプトの解析エラー", "", ps.error])
                continue
            inputs |= {v.name for v in ps.point_inputs}
            refs |= extract_point_names_from_source(ps.source)

        local_names = {
            gp.local_name for gp in snapshot.active_group_points(g.id) if gp.local_name
        }

        for name in sorted(inputs - local_names, key=str.lower):
            result.rows.append([g.name, action_names, "スクリプトにあるがポイントにない", name,
                                "入力変数"])
        for name in sorted(refs - local_names - inputs, key=str.lower):
            result.rows.append([g.name, action_names, "スクリプトにあるがポイントにない", name,
                                "ソース中の文字列指定（MI.GetPointAlias 等）"])
        for name in sorted(local_names - inputs - refs, key=str.lower):
            result.rows.append([g.name, action_names, "ポイントにあるがスクリプトにない", name,
                                "入力変数・ソース中の文字列指定のいずれにもない"])

    result.summary = f"スクリプトを持つグループ {checked} 件を照合し、{len(result.rows)} 件の差異があります。"
    return result


# ----------------------------------------------------------------------
# 無効参照の検出
# ----------------------------------------------------------------------
def check_invalid_references(snapshot: MIConfigSnapshot) -> CheckResult:
    """グループのポイントが参照する MI_POINT が Active = 0 / Enabled = 0、
    あるいは所属データソースが Enabled = 0 のものを列挙する。

    MI_POINT は Active = 1 の行のみ読み込んでいるため、読み込まれていない PointID は
    「Active = 0 または存在しない」として扱う。
    """
    result = CheckResult(
        title="無効参照の検出",
        columns=["グループ名", "ローカル名", "エイリアス", "ポイントID", "内容"],
    )
    for g in snapshot.groups_sorted():
        for gp in snapshot.active_group_points(g.id):
            reasons: list[str] = []
            p = snapshot.points.get(gp.point_id)
            if p is None:
                reasons.append("参照先ポイントが無効（Active = 0）または存在しない")
            else:
                if not p.enabled:
                    reasons.append("ポイントが無効（Enabled = 0）")
                pg = snapshot.point_groups.get(p.point_group_id)
                ds = snapshot.data_source_of_point(p.id)
                if pg is None:
                    reasons.append("所属ポイントグループが無効（Active = 0）または存在しない")
                elif ds is None:
                    reasons.append("所属データソースが無効（Active = 0）または存在しない")
                elif not ds.enabled:
                    reasons.append(f"所属データソース {ds.alias} が無効（Enabled = 0）")
            if reasons:
                result.rows.append([
                    g.name,
                    gp.local_name or "",
                    snapshot.point_path(gp.point_id),
                    str(gp.point_id),
                    " / ".join(reasons),
                ])
    result.summary = f"{len(result.rows)} 件の無効参照があります。"
    return result


# ----------------------------------------------------------------------
# 設備間の構成比較
# ----------------------------------------------------------------------
def _profile(snapshot: MIConfigSnapshot, group: ActionGroup) -> dict[str, tuple[str, str, str]]:
    """LocalName → (データタイプ, アクセス, トリガー)。LocalName が空の行は対象外。
    同一グループ内で LocalName が重複する場合は先頭の行を使う。"""
    prof: dict[str, tuple[str, str, str]] = {}
    for gp in snapshot.active_group_points(group.id):
        if not gp.local_name or gp.local_name in prof:
            continue
        p = snapshot.points.get(gp.point_id)
        prof[gp.local_name] = (
            data_type_label(p.data_type) if p else "?",
            access_label(p.access) if p else "?",
            on_off(bool(gp.triggers_execution)),
        )
    return prof


def compare_configurations(snapshot: MIConfigSnapshot) -> CheckResult:
    """処理名が同じグループで、LocalName の集合と LocalName ごとの
    データタイプ・アクセス・トリガーを比較し、基準グループとの差を示す。

    基準グループは、同じ処理名のグループの中で最も多く現れる構成を持つグループ
    （同数の場合は名前順で先頭）とする。
    """
    result = CheckResult(
        title="設備間の構成比較",
        columns=["処理名", "基準グループ", "比較グループ", "ローカル名", "差異", "基準の値", "比較の値"],
    )
    by_process: dict[str, list[ActionGroup]] = defaultdict(list)
    for g in snapshot.groups_sorted():
        if g.name_conforms:
            by_process[g.process].append(g)

    compared = 0
    diff_groups = 0
    for process in sorted(by_process, key=str.lower):
        groups = by_process[process]
        if len(groups) < 2:
            continue
        profiles = {g.id: _profile(snapshot, g) for g in groups}
        signatures = {gid: frozenset(p.items()) for gid, p in profiles.items()}
        freq = Counter(signatures.values())
        best = max(freq.values())
        base = next(g for g in groups if freq[signatures[g.id]] == best)
        base_prof = profiles[base.id]

        for g in groups:
            if g.id == base.id:
                continue
            compared += 1
            prof = profiles[g.id]
            rows: list[list[str]] = []
            for ln in sorted(set(base_prof) | set(prof), key=str.lower):
                b = base_prof.get(ln)
                t = prof.get(ln)
                if b is None:
                    rows.append([process, base.name, g.name, ln, "比較グループにのみある", "", "あり"])
                    continue
                if t is None:
                    rows.append([process, base.name, g.name, ln, "基準グループにのみある", "あり", ""])
                    continue
                for label, bv, tv in (("データタイプが異なる", b[0], t[0]),
                                      ("アクセスが異なる", b[1], t[1]),
                                      ("トリガーが異なる", b[2], t[2])):
                    if bv != tv:
                        rows.append([process, base.name, g.name, ln, label, bv, tv])
            if rows:
                diff_groups += 1
                result.rows.extend(rows)
            else:
                result.rows.append([process, base.name, g.name, "", "一致", "", ""])

    result.summary = (f"{compared} グループを基準グループと比較し、"
                      f"{diff_groups} グループに差異があります。")
    return result


# ----------------------------------------------------------------------
# ローカル名の重複
# ----------------------------------------------------------------------
def check_duplicate_local_names(snapshot: MIConfigSnapshot) -> CheckResult:
    result = CheckResult(
        title="ローカル名の重複",
        columns=["グループ名", "ローカル名", "行数", "エイリアス"],
    )
    for g in snapshot.groups_sorted():
        by_name: dict[str, list[str]] = defaultdict(list)
        for gp in snapshot.active_group_points(g.id):
            if gp.local_name:
                by_name[gp.local_name].append(snapshot.point_path(gp.point_id))
        for name in sorted(by_name, key=str.lower):
            aliases = by_name[name]
            if len(aliases) > 1:
                result.rows.append([g.name, name, str(len(aliases)), " / ".join(aliases)])
    result.summary = f"{len(result.rows)} 件の重複があります。"
    return result


# チェックの一覧（メニュー表示順）
CHECKS = [
    ("script", "スクリプトとの突合", check_script_consistency),
    ("invalid", "無効参照の検出", check_invalid_references),
    ("compare", "設備間の構成比較", compare_configurations),
    ("duplicate", "ローカル名の重複", check_duplicate_local_names),
]
