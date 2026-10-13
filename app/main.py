"""萬能字幕產生器 程式入口(GUI)。

用 啟動字幕產生器.bat 開啟;也可以把影片拖到 bat 上,啟動後直接加入佇列。
"""
from __future__ import annotations

from app.paths import setup_env

setup_env()

import logging  # noqa: E402
import multiprocessing as mp  # noqa: E402
import sys  # noqa: E402
import traceback  # noqa: E402

from app.paths import LOGS_DIR, ROOT  # noqa: E402

log = logging.getLogger("app")


def main() -> int:
    logging.basicConfig(
        filename=LOGS_DIR / "app.log", level=logging.INFO, encoding="utf-8",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    def excepthook(exc_type, exc, tb):
        log.error("未處理的例外:\n%s", "".join(traceback.format_exception(exc_type, exc, tb)))
        try:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.critical(None, "萬能字幕產生器", f"發生錯誤:{exc}\n\n詳細內容在 logs\\app.log")
        except Exception:
            pass

    sys.excepthook = excepthook
    log.info("程式啟動,資料夾 %s", ROOT)

    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    from app.gui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("萬能字幕產生器")
    if sys.platform == "win32":
        app.setFont(QFont("Microsoft JhengHei UI", 10))
    win = MainWindow()
    win.show()
    if len(sys.argv) > 1:
        win.add_files(sys.argv[1:])
    return app.exec()


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
