"""調査用: SCRIPT.Script の生の XML をファイルへ書き出す（読み取りのみ）。

使い方（仮想環境を有効にして、リポジトリのルートで実行）:
    python tools\\dump_script_xml.py 100019210 100019225 100019157 100019382

- アプリの config.json の接続設定をそのまま使う（SQL Server 認証のパスワードは keyring から取得）。
- 実行するのは SELECT のみ。接続は READ UNCOMMITTED・自動コミット。
- 出力先: dump\\script_<ID>.xml（UTF-8、BOM なし、DB の文字列を一切加工しない）。
"""

from __future__ import annotations

import hashlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.config_manager import AUTH_SQL, ConfigManager, load_password  # noqa: E402
from core.db import Database  # noqa: E402


def main() -> int:
    ids = [int(a) for a in sys.argv[1:]]
    if not ids:
        print("SCRIPT.ID を1つ以上指定してください。")
        return 1

    cfg = ConfigManager().load().connection
    password = load_password(cfg.server, cfg.username) if cfg.auth == AUTH_SQL else ""
    out_dir = os.path.join(ROOT, "dump")
    os.makedirs(out_dir, exist_ok=True)

    with Database(cfg, password).open(readonly=True) as conn:
        cur = conn.cursor()
        for script_id in ids:
            cur.execute("SELECT Script FROM SCRIPT WHERE ID = ?", script_id)
            row = cur.fetchone()
            if row is None or row[0] is None:
                print(f"{script_id}: 見つかりません")
                continue
            text = row[0]
            data = text.encode("utf-8")
            path = os.path.join(out_dir, f"script_{script_id}.xml")
            with open(path, "wb") as f:
                f.write(data)
            print(f"{script_id}: {len(text)} 文字 / {len(data)} バイト / "
                  f"md5={hashlib.md5(data).hexdigest()} -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
