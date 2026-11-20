"""設定管理モジュール

config.json の読み書きと、アプリ設定を表すデータクラスを提供する。

- ファイルがなければ既定値で生成する（初回起動時）。
- 未知のキーは無視し、欠けたキーは既定値で補う。
- 相対パスはアプリのルート（exe 化時は exe のあるフォルダ）を基準に解決する。
- SQL Server 認証のパスワードは config.json に書かず、keyring
  （Windows 資格情報マネージャー）に保存する。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from typing import Any

logger = logging.getLogger(__name__)


def _app_root() -> str:
    """アプリのルートディレクトリを返す。

    PyInstaller で exe 化した場合は exe のあるフォルダ、
    ソース実行時はプロジェクト直下（このファイルは core/ 配下にあるためひとつ上）。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


APP_ROOT = _app_root()
DEFAULT_CONFIG_PATH = os.path.join(APP_ROOT, "config.json")

# keyring のサービス名（キーは "<サーバー>/<ユーザー名>"）
KEYRING_SERVICE = "AprisoMIToolEx"

# 監査列（CreatedBy / LastUpdatedBy）は nvarchar(50)
UPDATED_BY_MAX_LENGTH = 50

AUTH_WINDOWS = "windows"
AUTH_SQL = "sql"


# ----------------------------------------------------------------------
# 設定データクラス
# ----------------------------------------------------------------------
@dataclass
class ConnectionConfig:
    """DB 接続設定。"""

    server: str = ""
    database: str = "FLX24M"
    driver: str = "ODBC Driver 17 for SQL Server"
    auth: str = AUTH_WINDOWS  # "windows" または "sql"
    username: str = ""
    trust_server_certificate: bool = True
    timeout_seconds: int = 15

    def display_name(self) -> str:
        """ステータスバー表示用の「<サーバー>/<DB>」。"""
        return f"{self.server or '(未設定)'}/{self.database}"


@dataclass
class ViewerConfig:
    """コードビューアの設定。"""

    font_family: str = "Consolas"
    font_size: int = 10
    word_wrap: bool = False


@dataclass
class WindowConfig:
    """ウィンドウ状態（位置・サイズ、スプリッター位置、表の列幅）。

    geometry / splitter / table_headers の値は Qt の saveGeometry() /
    saveState() のバイト列を Base64 文字列にしたもの。
    """

    geometry: str = ""
    splitter: str = ""
    group_by_equipment: bool = False
    # 表の列幅（ヘッダー状態）。キーは表の識別名（例: "point_tab"）
    table_headers: dict = field(default_factory=dict)


@dataclass
class PathsConfig:
    """フォルダ設定（相対パスはアプリのルート基準）。"""

    backup_dir: str = "backup"
    log_dir: str = "log"
    last_export_dir: str = ""


@dataclass
class AppConfig:
    """アプリ全体の設定。"""

    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    updated_by: str = ""
    viewer: ViewerConfig = field(default_factory=ViewerConfig)
    window: WindowConfig = field(default_factory=WindowConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> "AppConfig":
        """dict から生成する。未知のキーは無視し、欠けたキーは既定値で補う。"""
        cfg = cls()
        if not isinstance(data, dict):
            return cfg
        cfg.connection = _load_section(ConnectionConfig, data.get("connection"))
        cfg.updated_by = _coerce(cfg.updated_by, data.get("updated_by", cfg.updated_by))
        cfg.viewer = _load_section(ViewerConfig, data.get("viewer"))
        cfg.window = _load_section(WindowConfig, data.get("window"))
        cfg.paths = _load_section(PathsConfig, data.get("paths"))

        # 値の正規化
        if cfg.connection.auth not in (AUTH_WINDOWS, AUTH_SQL):
            cfg.connection.auth = AUTH_WINDOWS
        if cfg.connection.timeout_seconds <= 0:
            cfg.connection.timeout_seconds = 15
        if cfg.viewer.font_size <= 0:
            cfg.viewer.font_size = 10
        cfg.window.table_headers = {
            str(k): v for k, v in cfg.window.table_headers.items() if isinstance(v, str)
        }
        return cfg


def _coerce(default: Any, value: Any) -> Any:
    """既定値の型に合わせて値を変換する。変換できなければ既定値を返す。"""
    # bool は int のサブクラスのため先に判定する
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return default
    if isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, str):
        if isinstance(value, str):
            return value
        return default if value is None else str(value)
    if isinstance(default, dict):
        return dict(value) if isinstance(value, dict) else default
    return value


def _load_section(cls, data: Any):
    """データクラス 1 セクション分を dict から読み込む。"""
    obj = cls()
    if not isinstance(data, dict):
        return obj
    for f in fields(cls):
        if f.name in data:
            setattr(obj, f.name, _coerce(getattr(obj, f.name), data[f.name]))
    return obj


# ----------------------------------------------------------------------
# 設定ファイルの読み書き
# ----------------------------------------------------------------------
def resolve_path(path: str) -> str:
    """相対パスをアプリのルート基準の絶対パスへ解決する。"""
    if not path:
        return APP_ROOT
    path = os.path.expandvars(os.path.expanduser(path))
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(APP_ROOT, path))


class ConfigManager:
    """config.json の読み書きを担う。"""

    def __init__(self, config_path: str | None = None):
        self.config_path = config_path or DEFAULT_CONFIG_PATH
        self.config: AppConfig = AppConfig()

    def load(self) -> AppConfig:
        """設定を読み込む。ファイルがない場合は既定値で生成して保存する。"""
        if not os.path.exists(self.config_path):
            self.config = AppConfig()
            try:
                self.save()
            except OSError:
                logger.exception("config.json の生成に失敗しました: %s", self.config_path)
            return self.config

        try:
            with open(self.config_path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            self.config = AppConfig.from_dict(data)
        except (OSError, ValueError):
            # 破損時などは既定値にフォールバックする（既存ファイルはこの時点では上書きしない）
            logger.exception("config.json の読み込みに失敗しました。既定値を使用します: %s",
                             self.config_path)
            self.config = AppConfig()
        return self.config

    def save(self) -> None:
        """現在の設定を config.json に保存する（一時ファイル経由で置き換える）。"""
        tmp_path = self.config_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.config.to_dict(), f, ensure_ascii=False, indent=4)
        os.replace(tmp_path, self.config_path)

    # --- パス ---------------------------------------------------------
    @property
    def backup_dir(self) -> str:
        return resolve_path(self.config.paths.backup_dir or "backup")

    @property
    def log_dir(self) -> str:
        return resolve_path(self.config.paths.log_dir or "log")

    def is_connection_configured(self) -> bool:
        """接続設定が入力済みか（サーバー・DB・ドライバー、SQL 認証ならユーザー名）。"""
        c = self.config.connection
        if not (c.server.strip() and c.database.strip() and c.driver.strip()):
            return False
        if c.auth == AUTH_SQL and not c.username.strip():
            return False
        return True


# ----------------------------------------------------------------------
# パスワード（keyring）
# ----------------------------------------------------------------------
def keyring_key(server: str, username: str) -> str:
    return f"{server}/{username}"


def load_password(server: str, username: str) -> str:
    """keyring からパスワードを取得する。取得できなければ空文字を返す。"""
    if not server or not username:
        return ""
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, keyring_key(server, username)) or ""
    except Exception:  # keyring のバックエンド不在など
        logger.exception("keyring からのパスワード取得に失敗しました")
        return ""


def save_password(server: str, username: str, password: str) -> None:
    """keyring にパスワードを保存する。失敗時は例外を送出する。"""
    import keyring

    keyring.set_password(KEYRING_SERVICE, keyring_key(server, username), password)


def delete_password(server: str, username: str) -> None:
    """keyring からパスワードを削除する（存在しない場合は何もしない）。"""
    if not server or not username:
        return
    try:
        import keyring
        from keyring.errors import PasswordDeleteError

        try:
            keyring.delete_password(KEYRING_SERVICE, keyring_key(server, username))
        except PasswordDeleteError:
            pass
    except Exception:
        logger.exception("keyring からのパスワード削除に失敗しました")
