"""XML 解析モジュール

- SCRIPT.Script（スクリプト定義 XML）から C# ソース・入力変数・呼び出し
  オペレーション等を取り出す。
- MI_POINT.Parameters（外側 XML の <SerializedData> にエスケープされた内側 XML が
  入る二重構造）から OPC UA のポイント属性を取り出す。

標準ライブラリの xml.etree.ElementTree を使う。解析に失敗しても例外は送出せず、
空の結果とエラーメッセージ（error 属性）を返す。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

# MI 標準の入力変数（ポイントではない）
STANDARD_SCRIPT_INPUTS = frozenset(
    {
        "MachineConnectorName",
        "ActionGroupName",
        "TriggerItemAlias",
        "TriggerTime",
        "ActionContext",
    }
)

_XML_DECL_RE = re.compile(r"^\s*<\?xml[^>]*\?>", re.IGNORECASE)


def _strip_declaration(text: str) -> str:
    """BOM と XML 宣言を除去する。

    宣言の encoding（utf-8 / utf-16 等）と実際の文字列（str）が食い違うと
    解析に失敗することがあるため、str として解析する前に取り除く。
    """
    text = text.lstrip("﻿")
    return _XML_DECL_RE.sub("", text, count=1)


def _local(tag) -> str:
    """名前空間を除いたタグ名。"""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child(elem: ET.Element, name: str) -> ET.Element | None:
    for c in elem:
        if _local(c.tag) == name:
            return c
    return None


def _children(elem: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in elem if _local(c.tag) == name]


def _find_descendant(elem: ET.Element, name: str) -> ET.Element | None:
    for e in elem.iter():
        if _local(e.tag) == name:
            return e
    return None


def _text(elem: ET.Element | None) -> str:
    if elem is None:
        return ""
    return elem.text or ""


def normalize_newlines(text: str) -> str:
    """改行コードを LF に統一する。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


# ----------------------------------------------------------------------
# SCRIPT.Script
# ----------------------------------------------------------------------
@dataclass
class ScriptVariable:
    """スクリプトの入力変数。"""

    name: str
    type: str
    is_standard: bool = False


@dataclass
class ParsedScript:
    """SCRIPT.Script の解析結果。"""

    source: str = ""
    inputs: list[ScriptVariable] = field(default_factory=list)
    operations: list[str] = field(default_factory=list)
    usings: str = ""
    language: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def point_inputs(self) -> list[ScriptVariable]:
        """標準 5 変数を除いた入力変数（ポイントの LocalName と対応する）。"""
        return [v for v in self.inputs if not v.is_standard]


def parse_script(xml_text: str | None) -> ParsedScript:
    """SCRIPT.Script の XML を解析する。"""
    result = ParsedScript()
    if not xml_text or not xml_text.strip():
        result.error = "スクリプト定義が空です"
        return result
    try:
        root = ET.fromstring(_strip_declaration(xml_text))
    except ET.ParseError as e:
        result.error = f"スクリプト定義の XML 解析に失敗しました: {e}"
        return result

    result.source = normalize_newlines(_text(_child(root, "Source")))

    inputs = _child(root, "Inputs")
    if inputs is not None:
        for var in _children(inputs, "ScriptVariable"):
            name = var.get("Name", "")
            if not name:
                continue
            result.inputs.append(
                ScriptVariable(
                    name=name,
                    type=var.get("Type", ""),
                    is_standard=name in STANDARD_SCRIPT_INPUTS,
                )
            )

    deps = _child(root, "OperationDependencies")
    if deps is not None:
        for dep in _children(deps, "ScriptDependency"):
            name = _text(_child(dep, "Name")).strip()
            if name:
                result.operations.append(name)

    result.usings = _text(_child(root, "ScriptUsings")).strip()
    result.language = _text(_child(root, "ScriptLanguage")).strip()
    # <ScriptId> は用途未確認（内容のハッシュである可能性）。第1版では読まない（未確認6）。
    return result


# ----------------------------------------------------------------------
# ソース中のポイント名の抽出
# ----------------------------------------------------------------------
# MI.GetPointAlias("名前" / MI.SetPointValue("名前" / MI.GetPointValue(@"名前" など、
# 名前に "Point" を含む MI のメソッドの第1引数に文字列リテラルで指定された名前を拾う。
_POINT_CALL_RE = re.compile(
    r'\bMI\s*\.\s*(?P<method>\w*Point\w*)\s*\(\s*@?"(?P<name>(?:[^"\\\n]|\\.)*)"'
)


@dataclass
class PointReference:
    """ソース中でのポイント名の文字列指定。"""

    method: str
    name: str
    line: int


def extract_point_references(source: str) -> list[PointReference]:
    """MI.GetPointAlias("...") 等で文字列指定された名前を、メソッド名・行番号付きで返す。"""
    refs: list[PointReference] = []
    if not source:
        return refs
    for m in _POINT_CALL_RE.finditer(source):
        name = m.group("name")
        if not name:
            continue
        line = source.count("\n", 0, m.start()) + 1
        refs.append(PointReference(method=m.group("method"), name=name, line=line))
    return refs


def extract_point_names_from_source(source: str) -> set[str]:
    """MI.GetPointAlias("名前" などの文字列引数を抽出する。"""
    return {r.name for r in extract_point_references(source)}


# ----------------------------------------------------------------------
# MI_POINT.Parameters
# ----------------------------------------------------------------------
@dataclass
class OpcUaPointProps:
    """OPC UA ポイントの属性（文字列のまま保持する）。"""

    namespace: str = ""
    key: str = ""
    publishing_interval: str = ""
    sampling_interval: str = ""
    priority: str = ""
    queue_size: str = ""
    original_point_type: str = ""
    register_ua_node: str = ""
    error: str = ""


_OPC_FIELDS = {
    "Namespace": "namespace",
    "Key": "key",
    "PublishingInterval": "publishing_interval",
    "SamplingInterval": "sampling_interval",
    "Priority": "priority",
    "QueueSize": "queue_size",
    "OriginalPointType": "original_point_type",
    "RegisterUaNode": "register_ua_node",
}


def parse_point_parameters(xml_text: str | None) -> OpcUaPointProps | None:
    """MI_POINT.Parameters を解析する。

    Parameters が空なら None を返す。解析に失敗した場合（OPC UA 以外の
    データソース等）は、各項目を空にした OpcUaPointProps に error を設定して返す。
    """
    if not xml_text or not xml_text.strip():
        return None
    props = OpcUaPointProps()
    try:
        outer = ET.fromstring(_strip_declaration(xml_text))
    except ET.ParseError as e:
        props.error = f"Parameters の XML 解析に失敗しました: {e}"
        return props

    serialized = _find_descendant(outer, "SerializedData")
    if serialized is not None and _text(serialized).strip():
        try:
            inner = ET.fromstring(_strip_declaration(_text(serialized)))
        except ET.ParseError as e:
            props.error = f"SerializedData の XML 解析に失敗しました: {e}"
            return props
    else:
        # 二重構造になっていない場合は外側をそのまま対象にする
        inner = outer

    if not _local(inner.tag).lower().startswith("opcua"):
        props.error = f"OPC UA 以外のポイント定義です（{_local(inner.tag)}）"
        return props

    for tag, attr in _OPC_FIELDS.items():
        el = _child(inner, tag)
        if el is not None:
            setattr(props, attr, _text(el).strip())
    return props
