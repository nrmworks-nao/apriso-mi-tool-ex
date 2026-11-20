"""調査用: MI スクリプトを Windows の C# コンパイラ (csc.exe) でコンパイルチェックする試作。

使い方（仮想環境を有効にして、リポジトリのルートで実行）:
    python tools\\check_compile.py --all                      全スクリプトを検査（config.json の DB）
    python tools\\check_compile.py --all --database TESTDB01  DB 名だけ差し替えて検査
    python tools\\check_compile.py --file dump\\xxx.xml        書き出し済みの XML を検査
    python tools\\check_compile.py --all --show-wrapper 100019157   生成したラッパーを表示

仕組み:
- SCRIPT.Script の XML から、ソース・入力変数・呼び出しオペレーションの定義を取り出す。
- それらを宣言したクラスでソースを包み、csc.exe でコンパイルする（出力 DLL は捨てる）。
  - 入力変数: XML の型どおりのフィールド（System.* 以外の型は dynamic）
  - MI / Log: dynamic（メソッドの存在や引数は検査しない）
  - ExecuteOperationSync_<名前>: XML の Inputs / Outputs から引数を生成
- DB に対して実行するのは SELECT のみ。
"""

from __future__ import annotations

import argparse
import dataclasses
import glob
import html
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SCRIPT_FILE_LABEL = "script"
_ERROR_RE = re.compile(
    r"^(?P<file>.*?)\((?P<line>\d+),(?P<col>\d+)\):\s*(?P<kind>error|warning)\s+(?P<code>CS\d+):\s*(?P<msg>.*)$"
)


# ----------------------------------------------------------------------
# コンパイラの検出
# ----------------------------------------------------------------------
def find_compilers() -> list[str]:
    """利用できる csc.exe を、新しい順（Roslyn → .NET Framework 付属）で返す。"""
    found: list[str] = []
    patterns = []
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        patterns.append(os.path.join(base, "Microsoft Visual Studio", "*", "*",
                                     "MSBuild", "Current", "Bin", "Roslyn", "csc.exe"))
    for pat in patterns:
        found.extend(sorted(glob.glob(pat), reverse=True))
    windir = os.environ.get("WINDIR", r"C:\Windows")
    for fw in ("Framework64", "Framework"):
        p = os.path.join(windir, "Microsoft.NET", fw, "v4.0.30319", "csc.exe")
        if os.path.exists(p):
            found.append(p)
    return found


# ----------------------------------------------------------------------
# XML からの取り出し
# ----------------------------------------------------------------------
@dataclasses.dataclass
class Operation:
    name: str
    inputs: list[tuple[str, str]]   # (Key, 型名)
    outputs: list[tuple[str, str]]


@dataclasses.dataclass
class ScriptParts:
    source: str
    usings: str
    supplementary: str
    inputs: list[tuple[str, str]]   # (Name, 型名)
    operations: list[Operation]


def _section(xml: str, tag: str) -> str:
    m = re.search(rf"<{tag}(?:\s[^>]*)?>(.*?)</{tag}>", xml, re.S)
    return m.group(1) if m else ""


def _bag_items(block: str) -> list[tuple[str, str]]:
    return re.findall(r'<PropertyBagItem Key="([^"]+)"><Value[^>]*>([^<]*)</Value>', block)


def parse_parts(xml: str) -> ScriptParts:
    source = html.unescape(_section(xml, "Source"))
    usings = html.unescape(_section(xml, "ScriptUsings"))
    supplementary = html.unescape(_section(xml, "SupplementaryMethods"))

    # トップレベルの <Inputs> は <OperationDependencies> より前にある最初のもの
    head = xml.split("<OperationDependencies", 1)[0]
    inputs = re.findall(r'<ScriptVariable[^>]*\sName="([^"]+)"\s+Type="([^"]+)"', _section(head, "Inputs"))

    operations: list[Operation] = []
    for dep in re.findall(r"<ScriptDependency[^>]*>(.*?)</ScriptDependency>", xml, re.S):
        name = _section(dep, "Name").strip()
        if not name:
            continue
        operations.append(Operation(name, _bag_items(_section(dep, "Inputs")),
                                    _bag_items(_section(dep, "Outputs"))))
    return ScriptParts(source, usings, supplementary, inputs, operations)


# ----------------------------------------------------------------------
# ラッパーの生成
# ----------------------------------------------------------------------
def _cs_type(type_name: str) -> str:
    """XML の型名を C# の型にする。System.* 以外（Apriso 固有の型）は dynamic。"""
    t = type_name.split(",")[0].strip()
    return t if t.startswith("System.") else "dynamic"


def build_wrapper(parts: ScriptParts) -> str:
    lines: list[str] = []
    lines.append(parts.usings.strip() or "using System;")
    lines.append("public class __MiScript {")
    lines.append("    public dynamic MI; public dynamic Log;")
    for name, type_name in parts.inputs:
        lines.append(f"    public {_cs_type(type_name)} {name};")
    for op in parts.operations:
        params = ["string employeeNo = null"]
        params += [f"{_cs_type(t)} in{k} = default({_cs_type(t)})" for k, t in op.inputs]
        # out 引数は省略可能にできないため、必須引数として並べる（名前付き引数で順不同に渡せる）
        required = [f"out {_cs_type(t)} out{k}" for k, t in op.outputs]
        body = " ".join(f"out{k} = default({_cs_type(t)});" for k, t in op.outputs)
        lines.append(f"    public void ExecuteOperationSync_{op.name}({', '.join(required + params)}) {{ {body} }}")
    if parts.supplementary.strip():
        lines.append(parts.supplementary)
    lines.append("    public void __Run() {")
    # 以降の行番号を、スクリプトの行番号に合わせる
    lines.append(f'#line 1 "{SCRIPT_FILE_LABEL}"')
    lines.append(parts.source.replace("\r\n", "\n"))
    lines.append("#line hidden")
    lines.append("    }")
    lines.append("}")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------
# コンパイル
# ----------------------------------------------------------------------
@dataclasses.dataclass
class Diagnostic:
    kind: str
    code: str
    line: int
    col: int
    message: str
    in_script: bool


def compile_check(csc: str, wrapper: str) -> tuple[list[Diagnostic], str]:
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "wrapper.cs")
        with open(src, "w", encoding="utf-8-sig", newline="\n") as f:
            f.write(wrapper)
        cmd = [csc, "/nologo", "/target:library", "/utf8output",
               "/r:Microsoft.CSharp.dll", "/r:System.Core.dll",
               "/out:" + os.path.join(tmp, "out.dll"), src]
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
        out = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    diags: list[Diagnostic] = []
    for line in out.splitlines():
        m = _ERROR_RE.match(line.strip())
        if not m:
            continue
        diags.append(Diagnostic(m["kind"], m["code"], int(m["line"]), int(m["col"]), m["msg"],
                                os.path.basename(m["file"]) == SCRIPT_FILE_LABEL))
    return diags, out


# ----------------------------------------------------------------------
# 入力の取得
# ----------------------------------------------------------------------
def load_from_db(database: str | None) -> list[tuple[str, str]]:
    from core.config_manager import AUTH_SQL, ConfigManager, load_password
    from core.db import Database

    cfg = ConfigManager().load().connection
    if database:
        cfg = dataclasses.replace(cfg, database=database)
    password = load_password(cfg.server, cfg.username) if cfg.auth == AUTH_SQL else ""
    with Database(cfg, password).open(readonly=True) as conn:
        cur = conn.cursor()
        print(f"接続先: {cfg.server} / {cur.execute('SELECT DB_NAME()').fetchone()[0]}")
        cur.execute(
            "SELECT s.ID, g.Name, a.Name, s.Script "
            "FROM MI_ACTION a "
            "JOIN MI_ACTION_GROUP g ON g.ID = a.ActionGroupID "
            "JOIN SCRIPT s ON s.ID = a.ScriptId "
            "WHERE a.Active = 1 ORDER BY g.Name, a.SequenceNo"
        )
        return [(f"{row[0]} {row[1]}/{row[2]}", row[3]) for row in cur.fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser(description="MI スクリプトのコンパイルチェック（試作）")
    ap.add_argument("--all", action="store_true", help="DB の全スクリプトを検査する")
    ap.add_argument("--database", help="config.json の DB 名の代わりに使う DB 名")
    ap.add_argument("--file", action="append", default=[], help="検査する XML ファイル")
    ap.add_argument("--show-wrapper", help="指定した SCRIPT.ID（またはファイル名の一部）のラッパーを表示")
    ap.add_argument("--csc", help="使用する csc.exe のパス")
    args = ap.parse_args()

    compilers = [args.csc] if args.csc else find_compilers()
    print("見つかった C# コンパイラ:")
    for c in compilers:
        print("  " + c)
    if not compilers:
        print("csc.exe が見つかりません。")
        return 1
    csc = compilers[0]
    ver = subprocess.run([csc, "/?"], capture_output=True).stdout.decode("utf-8", errors="replace")
    print("使用: " + csc)
    print("  " + (ver.splitlines()[0] if ver else ""))

    targets: list[tuple[str, str]] = []
    for path in args.file:
        with open(path, "rb") as f:
            targets.append((os.path.basename(path), f.read().decode("utf-8")))
    if args.all:
        targets.extend(load_from_db(args.database))
    if not targets:
        print("検査対象がありません（--all または --file を指定）。")
        return 1

    ok = ng = 0
    code_counts: dict[str, int] = {}
    for label, xml in targets:
        parts = parse_parts(xml)
        wrapper = build_wrapper(parts)
        if args.show_wrapper and args.show_wrapper in label:
            print(f"----- ラッパー: {label} -----")
            print(wrapper)
            print("----- ここまで -----")
        diags, raw = compile_check(csc, wrapper)
        errors = [d for d in diags if d.kind == "error"]
        if not errors and "error" not in raw.lower():
            ok += 1
            continue
        ng += 1
        print(f"NG  {label}")
        if not errors:
            print("    " + raw.strip().replace("\n", "\n    ")[:600])
        for d in errors[:5]:
            where = f"スクリプト {d.line}行 {d.col}桁" if d.in_script else f"ラッパー {d.line}行"
            print(f"    {d.code} [{where}] {d.message}")
            code_counts[d.code] = code_counts.get(d.code, 0) + 1
        if len(errors) > 5:
            print(f"    … ほか {len(errors) - 5} 件")

    print(f"\n結果: OK {ok} 件 / NG {ng} 件（計 {ok + ng} 件）")
    if code_counts:
        print("エラーコード別（各スクリプト先頭5件まで）: " +
              ", ".join(f"{k}×{v}" for k, v in sorted(code_counts.items())))
    return 0 if ng == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
