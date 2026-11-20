"""スクリプトの文法チェック（C# コンパイラ csc.exe によるコンパイル）

tools/check_compile.py（検証済みの試作）から次の部分をそのまま移植している:
find_compilers、parse_parts、_cs_type、build_wrapper、compile_check、診断の正規表現。

仕組み:
- SCRIPT.Script の XML から、ソース・入力変数・呼び出しオペレーションの定義を取り出す。
- それらを宣言したクラスでソースを包み、csc.exe でコンパイルする（出力 DLL は捨てる）。
  - 入力変数: XML の型どおりのフィールド（System.* 以外の型は dynamic）
  - MI / Log: dynamic（メソッドの存在や引数は検査しない）
  - ExecuteOperationSync_<名前>: XML の Inputs / Outputs から引数を生成
- ソースの直前に #line 1 "script" を置き、エラーの行番号をスクリプトの行番号に合わせる。
- 入力変数とオペレーションの定義は DB から読み込んだ元の XML のものを使い、
  ソースだけを編集中の内容に差し替えてチェックする（check_source）。

このチェックは Apriso 本体のコンパイルと同一ではない（MI・Log のメソッド名や引数は検査しない）。
DB には一切アクセスしない。ワーカースレッドから呼ぶこと（GUI に依存しない）。

前提（変更指示002 の未確認事項）:
- 未確認2: ExecuteOperationSync_ 以外のオペレーション呼び出し形式（非同期など）の有無は未確認。
  未知の形式はコンパイルエラーとしてそのまま表示する。
- 未確認3: <SupplementaryMethods> に内容があるスクリプトは現状ない。内容がある場合は
  試作と同じくクラス本体に展開する。
"""

from __future__ import annotations

import dataclasses
import glob
import html
import os
import re
import subprocess
import tempfile
import time

SCRIPT_FILE_LABEL = "script"
_ERROR_RE = re.compile(
    r"^(?P<file>.*?)\((?P<line>\d+),(?P<col>\d+)\):\s*(?P<kind>error|warning)\s+(?P<code>CS\d+):\s*(?P<msg>.*)$"
)
# 位置情報のない診断（例: "error CS2001: ..."、"fatal error CS...")
_UNLOCATED_ERROR_RE = re.compile(r"\berror\s+CS\d+", re.IGNORECASE)

DEFAULT_TIMEOUT_SECONDS = 60

# コンソールウィンドウを出さない（Windows 以外では 0）
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

COMPILE_NOTICE = ("このチェックは Apriso 本体のコンパイルと同一ではありません。"
                  "MI・Log のメソッド名や引数は検査されません。")


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


def select_compiler(csc_path: str = "") -> tuple[str, str]:
    """使用する csc.exe を決める。

    設定 compiler.csc_path が空でなければそれを使う。空なら検出した中の先頭を使う。
    戻り値: (パス, 使えない場合の理由)。使えない場合のパスは空文字。
    """
    path = (csc_path or "").strip()
    if path:
        path = os.path.expandvars(os.path.expanduser(path))
        if os.path.isfile(path):
            return path, ""
        return "", f"設定 compiler.csc_path に指定された C# コンパイラが見つかりません: {path}"
    compilers = find_compilers()
    if compilers:
        return compilers[0], ""
    return "", ("C# コンパイラ（csc.exe）が見つかりません。Visual Studio（Build Tools 可）を"
                "インストールするか、config.json の compiler.csc_path に csc.exe のパスを指定してください。")


def describe_compilers(csc_path: str = "") -> str:
    """バージョン情報に表示する、コンパイラの検出結果。"""
    selected, reason = select_compiler(csc_path)
    lines = []
    if selected:
        lines.append(f"使用する C# コンパイラ: {selected}")
    else:
        lines.append(f"使用する C# コンパイラ: なし（{reason}）")
    if (csc_path or "").strip():
        lines.append("（config.json の compiler.csc_path で指定）")
    else:
        found = find_compilers()
        if len(found) > 1:
            lines.append("検出したほかのコンパイラ:")
            lines.extend("  " + p for p in found[1:])
    return "\n".join(lines)


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
        # 未確認3: 内容がある場合はクラス本体に展開する（試作と同じ）
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
    kind: str        # "error" / "warning"
    code: str        # 例: CS1002
    line: int        # in_script のときはスクリプトの行番号（1 始まり）、それ以外はラッパーの行番号
    col: int
    message: str
    in_script: bool  # False: ラッパー内（定義の生成に起因する）

    @property
    def is_error(self) -> bool:
        return self.kind == "error"

    @property
    def kind_label(self) -> str:
        base = "エラー" if self.is_error else "警告"
        return base if self.in_script else f"定義の{base}"


def compile_check(csc: str, wrapper: str,
                  timeout: int = DEFAULT_TIMEOUT_SECONDS) -> tuple[list[Diagnostic], str, int]:
    """ラッパーを一時フォルダに書き出して csc でコンパイルする（出力 DLL は捨てる）。

    戻り値: (診断のリスト, コンパイラの出力, 終了コード)。
    タイムアウト時は subprocess.TimeoutExpired、起動できない場合は OSError を送出する。
    """
    with tempfile.TemporaryDirectory(prefix="AprisoMIToolEx_", ignore_cleanup_errors=True) as tmp:
        src = os.path.join(tmp, "wrapper.cs")
        with open(src, "w", encoding="utf-8-sig", newline="\n") as f:
            f.write(wrapper)
        cmd = [csc, "/nologo", "/target:library", "/utf8output",
               "/r:Microsoft.CSharp.dll", "/r:System.Core.dll",
               "/out:" + os.path.join(tmp, "out.dll"), src]
        # コンソールのない exe からでも動くよう、標準入力は DEVNULL にする
        proc = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL,
                              timeout=timeout, creationflags=_CREATE_NO_WINDOW)
        out = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    diags: list[Diagnostic] = []
    for line in out.splitlines():
        m = _ERROR_RE.match(line.strip())
        if not m:
            continue
        diags.append(Diagnostic(m["kind"], m["code"], int(m["line"]), int(m["col"]), m["msg"],
                                os.path.basename(m["file"]) == SCRIPT_FILE_LABEL))
    return diags, out, proc.returncode


# ----------------------------------------------------------------------
# アプリから使う入口
# ----------------------------------------------------------------------
STATUS_OK = "ok"                    # エラーなし（警告はあり得る）
STATUS_ERROR = "error"              # エラーあり
STATUS_UNAVAILABLE = "unavailable"  # チェックできなかった（コンパイラなし・タイムアウト等）


@dataclasses.dataclass
class CompileResult:
    status: str
    diagnostics: list[Diagnostic] = dataclasses.field(default_factory=list)
    compiler: str = ""
    reason: str = ""        # STATUS_UNAVAILABLE の理由
    raw_output: str = ""
    elapsed_seconds: float = 0.0

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.is_error]

    @property
    def script_errors(self) -> list[Diagnostic]:
        return [d for d in self.errors if d.in_script]

    @property
    def wrapper_errors(self) -> list[Diagnostic]:
        """定義の生成に起因するエラー（ラッパー内）。"""
        return [d for d in self.errors if not d.in_script]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if not d.is_error]

    @property
    def checked(self) -> bool:
        return self.status in (STATUS_OK, STATUS_ERROR)

    def summary(self) -> str:
        """「OK」「エラー 3 件」などの短い説明。"""
        if self.status == STATUS_OK:
            n = len(self.warnings)
            return "OK" + (f"（警告 {n} 件）" if n else "")
        if self.status == STATUS_ERROR:
            text = f"エラー {len(self.errors)} 件"
            n_wrap = len(self.wrapper_errors)
            if n_wrap:
                text += f"（うち定義の生成に起因するエラー {n_wrap} 件）"
            return text
        return "チェックできませんでした"


def unavailable(reason: str, compiler: str = "", raw_output: str = "") -> CompileResult:
    return CompileResult(STATUS_UNAVAILABLE, compiler=compiler, reason=reason, raw_output=raw_output)


def check_source(original_xml: str | None, source: str, csc_path: str = "",
                 timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> CompileResult:
    """元の XML の定義（入力変数・オペレーション・using）で、source をコンパイルチェックする。"""
    csc, reason = select_compiler(csc_path)
    if not csc:
        return unavailable(reason)
    timeout = int(timeout_seconds) if timeout_seconds and int(timeout_seconds) > 0 else DEFAULT_TIMEOUT_SECONDS

    parts = parse_parts(original_xml or "")
    parts = dataclasses.replace(parts, source=(source or "").replace("\r\n", "\n"))
    wrapper = build_wrapper(parts)

    started = time.monotonic()
    try:
        diags, raw, returncode = compile_check(csc, wrapper, timeout)
    except subprocess.TimeoutExpired:
        return unavailable(f"コンパイルが {timeout} 秒以内に終わらなかったため、中止しました（タイムアウト）。",
                           compiler=csc)
    except OSError as e:
        return unavailable(f"C# コンパイラを起動できませんでした: {e}", compiler=csc)
    elapsed = time.monotonic() - started

    result = CompileResult(STATUS_OK, diagnostics=diags, compiler=csc, raw_output=raw,
                           elapsed_seconds=elapsed)
    if result.errors:
        result.status = STATUS_ERROR
        return result
    unlocated = [ln for ln in raw.splitlines()
                 if _UNLOCATED_ERROR_RE.search(ln) and not _ERROR_RE.match(ln.strip())]
    if returncode != 0 or unlocated:
        # エラー終了したが、位置付きの診断として解析できなかった（コマンドラインの誤りなど）
        detail = raw.strip()[:2000]
        return unavailable("C# コンパイラがエラー終了しましたが、結果を解析できませんでした。"
                           + (f"\n{detail}" if detail else f"（終了コード {returncode}）"),
                           compiler=csc, raw_output=raw)
    return result
