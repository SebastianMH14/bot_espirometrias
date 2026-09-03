import logging
import os
import sys
from datetime import datetime

import config


_log_setup = False


def setup_logger():
    global _log_setup
    logger = logging.getLogger("bot_espirometrias")

    if _log_setup:
        return logger

    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(config.LOG_DIR, f"bot_{ts}.txt")

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)

    # En Windows, sys.stdout suele quedar en el codepage de la consola
    # (cp1252/850) en vez de UTF-8, lo que mostraba tildes/ñ como "�" en
    # logs volcados a stdout (ej. desde el Programador de tareas).
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)

    logger.addHandler(fh)
    logger.addHandler(ch)

    _log_setup = True
    logger.info("Log iniciado: %s", log_file)
    return logger
