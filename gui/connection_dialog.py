"""接続設定ダイアログ（モーダル）

- サーバー / データベース / ODBC ドライバー / 認証方式 / ユーザー名・パスワード /
  サーバー証明書を信頼する / 更新者名
- [接続テスト] は SELECT 1 をワーカースレッドで実行し、結果を表示する。
- パスワードは config.json に書かず、keyring に保存する
  （サービス名 AprisoMIToolEx、キーは <サーバー>/<ユーザー名>）。
"""

from __future__ import annotations

import copy

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from core.config_manager import (
    AUTH_SQL,
    AUTH_WINDOWS,
    UPDATED_BY_MAX_LENGTH,
    ConfigManager,
    ConnectionConfig,
    load_password,
    save_password,
)
from core.db import Database, list_sql_server_drivers
from gui import worker
from gui.styles import refresh_style


class ConnectionDialog(QDialog):
    def __init__(self, config_manager: ConfigManager, parent=None, message: str = ""):
        super().__init__(parent)
        self.config_manager = config_manager
        self.setWindowTitle("接続設定")
        self.setModal(True)
        self.resize(560, 0)
        self._testing = False
        self._handle = None
        cfg = config_manager.config
        conn = cfg.connection

        lay = QVBoxLayout(self)
        if message:
            msg = QLabel(message, self)
            msg.setObjectName("warningLabel")
            msg.setWordWrap(True)
            lay.addWidget(msg)

        db_box = QGroupBox("接続先", self)
        form = QFormLayout(db_box)
        self.server_edit = QLineEdit(conn.server, db_box)
        self.server_edit.setPlaceholderText("例: MESDB01 または MESDB01\\INSTANCE")
        form.addRow("サーバー:", self.server_edit)
        self.database_edit = QLineEdit(conn.database or "FLX24M", db_box)
        form.addRow("データベース:", self.database_edit)
        self.driver_combo = QComboBox(db_box)
        self.driver_combo.setEditable(True)
        drivers = list_sql_server_drivers()
        self.driver_combo.addItems(drivers)
        if conn.driver and conn.driver not in drivers:
            self.driver_combo.addItem(conn.driver)
        self.driver_combo.setCurrentText(conn.driver or (drivers[0] if drivers else ""))
        form.addRow("ODBC ドライバー:", self.driver_combo)
        if not drivers:
            warn = QLabel("SQL Server 用の ODBC ドライバーが見つかりません。"
                          "ODBC Driver 17 / 18 for SQL Server をインストールしてください。", db_box)
            warn.setObjectName("errorLabel")
            warn.setWordWrap(True)
            form.addRow("", warn)
        self.trust_cb = QCheckBox("サーバー証明書を信頼する（TrustServerCertificate=yes）", db_box)
        self.trust_cb.setChecked(conn.trust_server_certificate)
        form.addRow("", self.trust_cb)
        lay.addWidget(db_box)

        auth_box = QGroupBox("認証", self)
        aform = QFormLayout(auth_box)
        radio_host = QWidget(auth_box)
        rlay = QHBoxLayout(radio_host)
        rlay.setContentsMargins(0, 0, 0, 0)
        self.win_radio = QRadioButton("Windows 認証", radio_host)
        self.sql_radio = QRadioButton("SQL Server 認証", radio_host)
        self._auth_group = QButtonGroup(self)
        self._auth_group.addButton(self.win_radio)
        self._auth_group.addButton(self.sql_radio)
        rlay.addWidget(self.win_radio)
        rlay.addWidget(self.sql_radio)
        rlay.addStretch(1)
        aform.addRow("認証方式:", radio_host)
        self.user_edit = QLineEdit(conn.username, auth_box)
        aform.addRow("ユーザー名:", self.user_edit)
        self.password_edit = QLineEdit(auth_box)
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("Windows 資格情報マネージャーに保存されます")
        aform.addRow("パスワード:", self.password_edit)
        lay.addWidget(auth_box)
        if conn.auth == AUTH_SQL:
            self.sql_radio.setChecked(True)
            self.password_edit.setText(load_password(conn.server, conn.username))
        else:
            self.win_radio.setChecked(True)

        audit_box = QGroupBox("監査", self)
        uform = QFormLayout(audit_box)
        self.updated_by_edit = QLineEdit(cfg.updated_by, audit_box)
        self.updated_by_edit.setMaxLength(UPDATED_BY_MAX_LENGTH)
        self.updated_by_edit.setPlaceholderText("例: 岸 隼人(ALSI)")
        uform.addRow("更新者名:", self.updated_by_edit)
        note = QLabel(f"CreatedBy / LastUpdatedBy に記録する表示名（{UPDATED_BY_MAX_LENGTH} 文字以内）。"
                      "未設定の場合は保存（DB への書き込み）を実行できません。", audit_box)
        note.setObjectName("secondaryLabel")
        note.setWordWrap(True)
        uform.addRow("", note)
        lay.addWidget(audit_box)

        self.result_label = QLabel("", self)
        self.result_label.setObjectName("secondaryLabel")
        self.result_label.setWordWrap(True)
        self.result_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.result_label)

        btns = QHBoxLayout()
        self.test_btn = QPushButton("接続テスト", self)
        self.ok_btn = QPushButton("OK", self)
        self.cancel_btn = QPushButton("キャンセル", self)
        self.ok_btn.setDefault(True)
        btns.addWidget(self.test_btn)
        btns.addStretch(1)
        btns.addWidget(self.ok_btn)
        btns.addWidget(self.cancel_btn)
        lay.addLayout(btns)

        self.win_radio.toggled.connect(self._update_auth_fields)
        self.test_btn.clicked.connect(self._on_test)
        self.ok_btn.clicked.connect(self._on_ok)
        self.cancel_btn.clicked.connect(self.reject)
        self._update_auth_fields()

    # ------------------------------------------------------------------
    def _update_auth_fields(self, *args) -> None:
        sql = self.sql_radio.isChecked()
        self.user_edit.setEnabled(sql and not self._testing)
        self.password_edit.setEnabled(sql and not self._testing)

    def _connection_from_form(self) -> ConnectionConfig:
        conn = copy.deepcopy(self.config_manager.config.connection)
        conn.server = self.server_edit.text().strip()
        conn.database = self.database_edit.text().strip()
        conn.driver = self.driver_combo.currentText().strip()
        conn.auth = AUTH_SQL if self.sql_radio.isChecked() else AUTH_WINDOWS
        conn.username = self.user_edit.text().strip() if conn.auth == AUTH_SQL else conn.username
        conn.trust_server_certificate = self.trust_cb.isChecked()
        return conn

    def _validate(self) -> str:
        if not self.server_edit.text().strip():
            return "サーバーを入力してください。"
        if not self.database_edit.text().strip():
            return "データベースを入力してください。"
        if not self.driver_combo.currentText().strip():
            return "ODBC ドライバーを選択してください。"
        if self.sql_radio.isChecked() and not self.user_edit.text().strip():
            return "ユーザー名を入力してください。"
        return ""

    def _set_result(self, text: str, kind: str) -> None:
        self.result_label.setObjectName(kind)
        self.result_label.setText(text)
        refresh_style(self.result_label)

    def _set_busy(self, busy: bool) -> None:
        self._testing = busy
        for w in (self.server_edit, self.database_edit, self.driver_combo, self.trust_cb,
                  self.win_radio, self.sql_radio, self.updated_by_edit, self.test_btn,
                  self.ok_btn, self.cancel_btn):
            w.setEnabled(not busy)
        self._update_auth_fields()

    def _on_test(self) -> None:
        err = self._validate()
        if err:
            self._set_result(err, "errorLabel")
            return
        conn = self._connection_from_form()
        password = self.password_edit.text() if conn.auth == AUTH_SQL else ""
        db = Database(conn, password)
        self._set_busy(True)
        self._set_result("接続テスト中…（SELECT 1）", "secondaryLabel")
        self._handle = worker.run_in_thread(
            db.test_connection,
            on_finished=self._on_test_finished,
            on_failed=self._on_test_failed,
        )

    def _on_test_finished(self, message: str) -> None:
        self._set_busy(False)
        self._set_result(message, "successLabel")

    def _on_test_failed(self, error: worker.TaskError) -> None:
        self._set_busy(False)
        self._set_result(f"接続に失敗しました。\n{error.message}", "errorLabel")

    def _on_ok(self) -> None:
        err = self._validate()
        if err:
            self._set_result(err, "errorLabel")
            return
        conn = self._connection_from_form()
        cfg = self.config_manager.config
        if conn.auth == AUTH_SQL:
            try:
                save_password(conn.server, conn.username, self.password_edit.text())
            except Exception as e:
                QMessageBox.warning(
                    self, "接続設定",
                    f"パスワードを Windows 資格情報マネージャーに保存できませんでした。\n{e}",
                )
                return
        cfg.connection = conn
        cfg.updated_by = self.updated_by_edit.text().strip()
        try:
            self.config_manager.save()
        except OSError as e:
            QMessageBox.warning(self, "接続設定", f"config.json を保存できませんでした。\n{e}")
            return
        self.accept()

    def reject(self) -> None:
        if self._testing:
            return
        super().reject()

    def closeEvent(self, event) -> None:
        if self._testing:
            event.ignore()
            return
        super().closeEvent(event)
