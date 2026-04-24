#!/usr/bin/env python3
# subnet_daemon.py — точка запуска для cron/systemd
# Запускать: python3 /path/to/subnet_daemon.py
# Cron пример: 0 4 * * * /usr/bin/python3 /opt/awg/subnet_daemon.py >> /var/log/awg-subnet.log 2>&1

import sys
import os
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from awg_core import run_subnet_daemon

if __name__ == "__main__":
    run_subnet_daemon()
