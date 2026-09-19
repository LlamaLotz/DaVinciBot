from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication

from davincibot.bootstrap import seed_defaults
from davincibot.database import Database
from davincibot.paths import AppPaths
from davincibot.ui.main_window import MainWindow


def run() -> int:
    paths = AppPaths.default()
    paths.ensure()
    logging.basicConfig(
        filename=paths.logs / "davincibot.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    database = Database(paths.database)
    seed_defaults(database)
    application = QApplication(sys.argv)
    application.setApplicationName("DaVinciBot")
    application.setOrganizationName("DaVinciBot")
    window = MainWindow(database, paths)
    window.show()
    result = application.exec()
    window.thread_pool.waitForDone()
    window.jobs.shutdown()
    database.close()
    return result
