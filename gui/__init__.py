"""PySide6 による GUI

外観スタイル（QSS）は gui/styles.py に集約し、各ウィジェットでは objectName の
設定のみを行う。DB アクセスは必ずワーカースレッド（gui/worker.py）で行う。
"""
