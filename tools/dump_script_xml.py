"""調査用: SCRIPT.Script の生の XML をファイルへ書き出す（読み取りのみ）。

使い方（仮想環境を有効にして、リポジトリのルートで実行）:
    python tools\\dump_script_xml.py 100019210 100019225
    python tools\\dump_script_xml.py --database TESTDB01 100021025

- --database を付けると、config.json のサーバー・認証はそのままで DB 名だけ差し替える。

- アプリの config.json の接続設定をそのまま使う（SQL Server 認証のパスワードは keyring から取得）。
- 実行するのは SELECT のみ。接続は READ UNCOMMITTED・自動コミット。
- 出力先: dump\\<DB名>_script_<ID>.xml（UTF-8、BOM なし、DB の文字列を一切加工しない）。
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.config_manager import AUTH_SQL, ConfigManager, load_password  # noqa: E402
from core.db import Database  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="SCRIPT.Script の生 XML を書き出す（読み取りのみ）")
    parser.add_argument("--database", help="config.json の DB 名の代わりに使う DB 名")
    parser.add_argument("ids", nargs="+", type=int, help="SCRIPT.ID")
    args = parser.parse_args()

    cfg = ConfigManager().load().connection
    if args.database:
        cfg = dataclasses.replace(cfg, database=args.database)
    password = load_password(cfg.server, cfg.username) if cfg.auth == AUTH_SQL else ""
    out_dir = os.path.join(ROOT, "dump")
    os.makedirs(out_dir, exist_ok=True)

    with Database(cfg, password).open(readonly=True) as conn:
        cur = conn.cursor()
        db_name = cur.execute("SELECT DB_NAME()").fetchone()[0]
        print(f"接続先: {cfg.server} / {db_name}")
        for script_id in args.ids:
            cur.execute(
                "SELECT Script, RowVersionStamp, LastUpdateOn, LastUpdatedBy FROM SCRIPT WHERE ID = ?",
                script_id,
            )
            row = cur.fetchone()
            if row is None or row[0] is None:
                print(f"{script_id}: 見つかりません")
                continue
            text = row[0]
            data = text.encode("utf-8")
            path = os.path.join(out_dir, f"{db_name}_script_{script_id}.xml")
            with open(path, "wb") as f:
                f.write(data)
            print(f"{script_id}: {len(text)} 文字 / md5={hashlib.md5(data).hexdigest()} / "
                  f"RowVer={row[1]} / 更新={row[2]} {row[3]} -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
