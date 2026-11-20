"""ワーカースレッド（QThread）の起動ヘルパー

GUI スレッドで DB アクセスをしないため、時間のかかる処理は QThread 上で実行する。
完了・失敗・進捗の通知は、GUI スレッドに置いた中継オブジェクト（_Relay）の
スロット経由で呼び出し元のコールバックへ渡す（キュー接続になるため、
コールバックは必ず GUI スレッドで実行される）。
"""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal, Slot

logger = logging.getLogger(__name__)


@dataclass
class TaskError:
    """ワーカーで発生した例外。"""

    exception: BaseException
    traceback_text: str

    @property
    def message(self) -> str:
        return str(self.exception) or self.exception.__class__.__name__


class FunctionWorker(QObject):
    """任意の関数をワーカースレッドで実行する。"""

    progress = Signal(object)  # 進捗コールバックの引数（tuple）
    finished = Signal(object)  # 戻り値
    failed = Signal(object)    # TaskError

    def __init__(self, fn: Callable, args: tuple, kwargs: dict, progress_kw: str | None):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = dict(kwargs)
        if progress_kw:
            self._kwargs[progress_kw] = self._emit_progress

    def _emit_progress(self, *args) -> None:
        self.progress.emit(args)

    @Slot()
    def run(self) -> None:
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as e:
            logger.exception("ワーカースレッドでエラーが発生しました")
            self.failed.emit(TaskError(e, traceback.format_exc()))
            return
        self.finished.emit(result)


class _Relay(QObject):
    """GUI スレッドでコールバックを呼ぶための中継。"""

    def __init__(self, on_finished, on_failed, on_progress):
        super().__init__()
        self._on_finished = on_finished
        self._on_failed = on_failed
        self._on_progress = on_progress

    @Slot(object)
    def finished(self, result) -> None:
        if self._on_finished:
            self._on_finished(result)

    @Slot(object)
    def failed(self, error) -> None:
        if self._on_failed:
            self._on_failed(error)

    @Slot(object)
    def progress(self, args) -> None:
        if self._on_progress:
            self._on_progress(*args)


class TaskHandle:
    """実行中のタスク（スレッド・ワーカー・中継）への参照を保持する。"""

    def __init__(self, thread: QThread, worker: QObject, relay: _Relay | None):
        self.thread = thread
        self.worker = worker
        self.relay = relay

    def is_running(self) -> bool:
        try:
            return self.thread.isRunning()
        except RuntimeError:  # 既に破棄済み
            return False


# 実行中のタスク（GC で破棄されないよう参照を保持する）
_active: set[TaskHandle] = set()


def _start(worker: QObject, start_slot, relay: _Relay | None,
           done_signals: list) -> TaskHandle:
    thread = QThread()
    worker.moveToThread(thread)
    handle = TaskHandle(thread, worker, relay)
    _active.add(handle)

    thread.started.connect(start_slot)
    for sig in done_signals:
        sig.connect(thread.quit)

    def cleanup() -> None:
        _active.discard(handle)
        worker.deleteLater()
        thread.deleteLater()
        if relay is not None:
            relay.deleteLater()

    thread.finished.connect(cleanup)
    thread.start()
    return handle


def run_in_thread(
    fn: Callable,
    *args: Any,
    on_finished: Callable[[Any], None] | None = None,
    on_failed: Callable[[TaskError], None] | None = None,
    on_progress: Callable[..., None] | None = None,
    progress_kw: str | None = None,
    **kwargs: Any,
) -> TaskHandle:
    """fn(*args, **kwargs) をワーカースレッドで実行する。

    progress_kw を指定すると、その名前のキーワード引数に進捗コールバックを渡す。
    コールバックが呼ばれた引数は on_progress に GUI スレッドで渡される。
    """
    worker = FunctionWorker(fn, args, kwargs, progress_kw)
    relay = _Relay(on_finished, on_failed, on_progress)
    worker.finished.connect(relay.finished)
    worker.failed.connect(relay.failed)
    worker.progress.connect(relay.progress)
    return _start(worker, worker.run, relay, [worker.finished, worker.failed])


def start_worker(worker: QObject, start_slot, done_signals: list) -> TaskHandle:
    """独自の Signal を持つワーカー（例: ChangeSetExecutor）を起動する。

    呼び出し側は、ワーカーの Signal を GUI 側の QObject のメソッドへ接続してから呼ぶこと。
    """
    return _start(worker, start_slot, None, done_signals)


def any_running() -> bool:
    return any(h.is_running() for h in list(_active))


def wait_all(timeout_ms: int = 5000) -> None:
    """アプリ終了時に、実行中のスレッドの終了を待つ。"""
    for h in list(_active):
        try:
            if h.thread.isRunning():
                h.thread.quit()
                h.thread.wait(timeout_ms)
        except RuntimeError:
            pass
