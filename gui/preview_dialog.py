"""変更プレビュー（実行確認）ダイアログ

DB への書き込みはすべてこのダイアログを通す。
  - ChangeSet（追加・更新・削除）を「グループ名、操作、変更前、変更後」の表で示す。
  - 実行される SQL 文（表示用にパラメータを埋め込んだもの）を確認できる。
  - 対象外の行がある場合は別タブで理由とともに示す。警告（ローカル名の重複）も別タブ。
  - [実行] で、退避 → 1 トランザクションで実行 → ログ をワーカースレッドで行う。
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.change_executor import (
    DELETE_MODE,
    MI_REFRESH_NOTICE,
    ChangeSetExecutor,
    ExecutionResult,
    preview_sql,
    validate_updated_by,
)
from core.config_manager import ConfigManager
from core.db import Database
from core.models import on_off
from core.point_service import ChangeSet
from gui import worker
from gui.styles import COLORS
from gui.table_utils import FilterableTable, ReadOnlyTableModel

_CHANGE_COLUMNS = ["グループ名", "操作", "変更前", "変更後"]
_EXCLUDED_COLUMNS = ["グループ名", "エイリアス", "ローカル名", "理由"]


def _describe(alias: str, local_name: str | None, triggers: bool | None) -> str:
    return f"ローカル名={local_name or ''} / エイリアス={alias} / トリガー={on_off(triggers)}"


def show_error(parent: QWidget, title: str, message: str, detail: str = "") -> None:
    """要点をメッセージボックスで示し、スタックトレース等は詳細表示に入れる。"""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle(title)
    box.setText(message)
    if detail:
        box.setDetailedText(detail)
    box.exec()


class PreviewDialog(QDialog):
    def __init__(self, change_set: ChangeSet, database: Database | None,
                 config_manager: ConfigManager, parent=None):
        super().__init__(parent)
        self.change_set = change_set
        self.database = database
        self.config_manager = config_manager
        self.execution_result: ExecutionResult | None = None
        self._running = False
        self._handle = None
        self._executor: ChangeSetExecutor | None = None

        self.setWindowTitle(f"変更プレビュー（{change_set.operation}）")
        self.resize(1200, 760)
        self.setSizeGripEnabled(True)

        lay = QVBoxLayout(self)
        summary = QLabel(change_set.summary(), self)
        summary.setObjectName("headerLabel")
        lay.addWidget(summary)

        updated_by = config_manager.config.updated_by
        self._user_error = validate_updated_by(updated_by)
        info = QLabel(
            f"接続先: {config_manager.config.connection.display_name()}　"
            f"更新者: {updated_by or '(未設定)'}　"
            f"削除方式: {'物理削除' if DELETE_MODE == 'physical' else '論理削除（Active = 0）'}",
            self,
        )
        info.setObjectName("secondaryLabel")
        lay.addWidget(info)

        if self._user_error:
            err = QLabel(self._user_error, self)
            err.setObjectName("errorLabel")
            err.setWordWrap(True)
            lay.addWidget(err)

        if change_set.excluded:
            ex = QLabel(
                f"対象外の行が {len(change_set.excluded)} 件あります（「対象外」タブに理由を表示）。"
                "対象外の行は保存されず、保存後の再読込で、それらの未保存の変更は破棄されます。",
                self,
            )
            ex.setObjectName("warningLabel")
            ex.setWordWrap(True)
            lay.addWidget(ex)

        self.tabs = QTabWidget(self)
        lay.addWidget(self.tabs, 1)

        # 変更内容
        self.change_model = ReadOnlyTableModel(_CHANGE_COLUMNS, parent=self)
        rows, bg = self._change_rows()
        self.change_model.set_rows(rows, background=bg)
        change_table = FilterableTable(self.change_model, self)
        change_table.view.horizontalHeader().resizeSection(0, 260)
        change_table.view.horizontalHeader().resizeSection(1, 60)
        change_table.view.horizontalHeader().resizeSection(2, 420)
        self.tabs.addTab(change_table, f"変更内容（{change_set.total}）")

        # SQL
        self.sql_view = QPlainTextEdit(self)
        self.sql_view.setObjectName("sqlViewer")
        self.sql_view.setReadOnly(True)
        self.sql_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        sql_text = (
            "-- 表示用にパラメータを埋め込んだ SQL です。実行時はパラメータ化クエリを使用します。\n"
            "-- 1 つのトランザクションで DELETE → UPDATE → INSERT の順に実行します。\n\n"
            + "\n\n".join(preview_sql(change_set, updated_by))
        )
        self.sql_view.setPlainText(sql_text)
        self.tabs.addTab(self.sql_view, "SQL")

        # 対象外
        if change_set.excluded:
            ex_model = ReadOnlyTableModel(_EXCLUDED_COLUMNS, parent=self)
            ex_model.set_rows([[e.group_name, e.alias, e.local_name, e.reason]
                               for e in change_set.excluded])
            ex_table = FilterableTable(ex_model, self)
            ex_table.view.horizontalHeader().resizeSection(1, 420)
            self.tabs.addTab(ex_table, f"対象外（{len(change_set.excluded)}）")

        # 警告
        if change_set.warnings:
            warn_view = QPlainTextEdit(self)
            warn_view.setObjectName("detailText")
            warn_view.setReadOnly(True)
            warn_view.setPlainText("\n".join(change_set.warnings))
            self.tabs.addTab(warn_view, f"警告（{len(change_set.warnings)}）")

        if change_set.is_empty and change_set.excluded:
            self.tabs.setCurrentIndex(self.tabs.count() - (2 if change_set.warnings else 1))

        # 進捗・ボタン
        bottom = QHBoxLayout()
        self.progress = QProgressBar(self)
        self.progress.setVisible(False)
        bottom.addWidget(self.progress, 1)
        self.status_label = QLabel("", self)
        self.status_label.setObjectName("secondaryLabel")
        bottom.addWidget(self.status_label)
        bottom.addStretch(1)
        self.exec_btn = QPushButton("実行", self)
        self.cancel_btn = QPushButton("キャンセル", self)
        bottom.addWidget(self.exec_btn)
        bottom.addWidget(self.cancel_btn)
        lay.addLayout(bottom)

        self.exec_btn.setEnabled(not change_set.is_empty and not self._user_error
                                 and database is not None)
        if change_set.is_empty:
            self.status_label.setText("実行する変更がありません。")
        self.exec_btn.clicked.connect(self._on_execute)
        self.cancel_btn.clicked.connect(self.reject)

    # ------------------------------------------------------------------
    def _change_rows(self) -> tuple[list[list[str]], dict[int, QColor]]:
        cs = self.change_set
        rows: list[list[str]] = []
        bg: dict[int, QColor] = {}
        c_del = QColor(COLORS["deleted_row"])
        c_upd = QColor(COLORS["changed_cell"])
        c_ins = QColor(COLORS["added_row"])
        for op in cs.deletes:
            bg[len(rows)] = c_del
            rows.append([op.group_name, "削除",
                         _describe(op.alias, op.before.local_name, op.before.triggers_execution), ""])
        for op in cs.updates:
            bg[len(rows)] = c_upd
            rows.append([
                op.group_name, "更新",
                _describe(op.before_alias, op.before.local_name, op.before.triggers_execution),
                _describe(op.alias, op.local_name, op.triggers_execution),
            ])
        for op in cs.inserts:
            bg[len(rows)] = c_ins
            rows.append([op.group_name, "追加", "",
                         _describe(op.alias, op.local_name, op.triggers_execution)])
        return rows, bg

    def _on_execute(self) -> None:
        if self._running or self.database is None:
            return
        cs = self.change_set
        answer = QMessageBox.question(
            self, "実行の確認",
            f"{cs.summary()}\n\n接続先 {self.config_manager.config.connection.display_name()} の "
            "MI_ACTION_GROUP_POINT に書き込みます。実行しますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self._running = True
        self.exec_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.tabs.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, max(1, cs.total))
        self.progress.setValue(0)
        self.status_label.setText("退避ファイルを書き出しています…")

        self._executor = ChangeSetExecutor(
            cs, self.database, self.config_manager.config.updated_by,
            self.config_manager.backup_dir, self.config_manager.log_dir,
        )
        self._executor.progress.connect(self._on_progress)
        self._executor.finished.connect(self._on_finished)
        self._handle = worker.start_worker(self._executor, self._executor.run,
                                           [self._executor.finished])

    @Slot(int, int)
    def _on_progress(self, done: int, total: int) -> None:
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(done)
        self.status_label.setText(f"実行中… {done} / {total}")

    @Slot(object)
    def _on_finished(self, result: ExecutionResult) -> None:
        self._running = False
        self._executor = None
        self.execution_result = result
        self.progress.setVisible(False)
        if result.success:
            self.status_label.setText("完了しました。")
            backup = os.path.basename(result.backup_json) if result.backup_json else "-"
            QMessageBox.information(
                self, "保存完了",
                f"{result.message}\n\n退避ファイル: {backup}\n\n{MI_REFRESH_NOTICE}",
            )
            self.accept()
            return
        self.status_label.setText("失敗しました。")
        msg = result.message
        if result.backup_json:
            msg += f"\n\n退避ファイル: {os.path.basename(result.backup_json)}"
        show_error(self, "保存エラー", msg, result.detail)
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("閉じる")
        self.tabs.setEnabled(True)

    # 実行中は閉じさせない
    def reject(self) -> None:
        if self._running:
            return
        super().reject()

    def closeEvent(self, event) -> None:
        if self._running:
            event.ignore()
            return
        super().closeEvent(event)
