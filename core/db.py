"""DB 接続管理モジュール（pyodbc）

- 接続は操作のたびに開いて閉じる（常時接続を保持しない）。
- 読み取り用接続は autocommit=True とし、
  SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED を設定して
  稼働中の MES をロックで待たせない。
- 書き込み用接続は autocommit=False（呼び出し側で commit / rollback する）。

本モジュールの関数は GUI スレッドから呼ばないこと（ワーカースレッドで使う）。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import pyodbc

from core import APP_NAME
from core.config_manager import AUTH_SQL, ConnectionConfig


def _braced(value: str) -> str:
    """ODBC 接続文字列の値を {} で囲む（} は }} にエスケープ）。"""
    return "{" + value.replace("}", "}}") + "}"


def _odbc_value(value: str) -> str:
    """特殊文字を含む場合のみ {} で囲む。"""
    if any(ch in value for ch in ";{}=") or value != value.strip():
        return _braced(value)
    return value


def build_connection_string(config: ConnectionConfig, password: str = "") -> str:
    """接続文字列を組み立てる。"""
    parts = [
        f"DRIVER={_braced(config.driver)}",
        f"SERVER={_odbc_value(config.server.strip())}",
        f"DATABASE={_odbc_value(config.database.strip())}",
    ]
    if config.auth == AUTH_SQL:
        parts.append(f"UID={_odbc_value(config.username)}")
        parts.append(f"PWD={_braced(password)}")
    else:
        parts.append("Trusted_Connection=yes")
    if config.trust_server_certificate:
        parts.append("TrustServerCertificate=yes")
    # SQL Server 側のセッション一覧で本アプリの接続と分かるようにする
    parts.append(f"APP={APP_NAME}")
    return ";".join(parts) + ";"


def mask_connection_string(conn_str: str) -> str:
    """ログ出力用にパスワード部分を伏せる。"""
    out = []
    for part in conn_str.split(";"):
        if part.upper().startswith("PWD="):
            out.append("PWD=****")
        else:
            out.append(part)
    return ";".join(out)


def list_sql_server_drivers() -> list[str]:
    """pyodbc.drivers() のうち SQL Server 用のドライバー名を返す（新しいもの優先）。"""
    try:
        drivers = pyodbc.drivers()
    except Exception:
        return []
    sql_drivers = [d for d in drivers if "SQL Server" in d]

    def sort_key(name: str):
        # "ODBC Driver NN for SQL Server" を番号の大きい順、その他は後ろへ
        if name.startswith("ODBC Driver"):
            digits = "".join(ch for ch in name if ch.isdigit())
            return (0, -int(digits) if digits else 0, name)
        return (1, 0, name)

    return sorted(sql_drivers, key=sort_key)


class Database:
    """接続情報を保持し、必要なときに接続を開く。"""

    def __init__(self, config: ConnectionConfig, password: str = ""):
        self.config = config
        self.password = password

    def connection_string(self) -> str:
        return build_connection_string(self.config, self.password)

    def connect(self, readonly: bool) -> pyodbc.Connection:
        """接続を開く。

        readonly=True: autocommit=True、READ UNCOMMITTED。
        readonly=False: autocommit=False（トランザクションは呼び出し側で管理）。
        """
        conn = pyodbc.connect(
            self.connection_string(),
            autocommit=readonly,
            timeout=max(1, int(self.config.timeout_seconds)),
        )
        if readonly:
            cur = conn.cursor()
            try:
                cur.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
            finally:
                cur.close()
        return conn

    @contextmanager
    def open(self, readonly: bool) -> Iterator[pyodbc.Connection]:
        """with 文で使う接続（ブロックを抜けると閉じる）。"""
        conn = self.connect(readonly)
        try:
            yield conn
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def test_connection(self) -> str:
        """SELECT 1 を実行し、結果の説明文を返す。失敗時は例外を送出する。"""
        with self.open(readonly=True) as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            row = cur.fetchone()
            value = row[0] if row else None
        return f"接続に成功しました（SELECT 1 → {value}）"
