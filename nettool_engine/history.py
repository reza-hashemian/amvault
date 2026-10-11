"""Keep what the monitor measured, for reports.

One row per sample in a SQLite file.  It holds device names, addresses and
measurements -- nothing that signs in to anything.
"""

from __future__ import annotations

import csv
import os
import sqlite3
import threading
import time

from .paths import data_dir

KEEP_DAYS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    device TEXT NOT NULL, title TEXT NOT NULL, address TEXT NOT NULL,
    t REAL NOT NULL, up INTEGER NOT NULL, ms REAL,
    cpu REAL, mem_pct REAL, rx_bps REAL, tx_bps REAL
);
CREATE INDEX IF NOT EXISTS samples_device_t ON samples (device, t);
"""


class History:
    def __init__(self, path: str | None = None):
        self._path = path or os.path.join(data_dir(), "history.db")
        self._lock = threading.Lock()
        # One connection shared across the monitor's threads, guarded by the lock.
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)
            self._db.execute("DELETE FROM samples WHERE t < ?", (time.time() - KEEP_DAYS * 86400,))

    def add(self, device: dict, sample: dict) -> None:
        used, total = sample.get("mem_used"), sample.get("mem_total")
        row = (
            device["id"], device.get("title") or "", device.get("address") or "",
            sample["t"], 1 if sample["up"] else 0, sample.get("ms"), sample.get("cpu"),
            round(100.0 * used / total, 1) if used is not None and total else None,
            sample.get("rx_bps"), sample.get("tx_bps"),
        )
        with self._lock, self._db:
            self._db.execute("INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?,?)", row)

    def summary(self, hours: float) -> list[dict]:
        """Per device over the last `hours`: availability and the averages."""
        since = time.time() - hours * 3600
        with self._lock:
            rows = self._db.execute(
                """SELECT device, MAX(title), MAX(address), COUNT(*), SUM(up),
                          AVG(ms), MAX(ms), AVG(cpu), MAX(cpu), AVG(mem_pct),
                          AVG(rx_bps), AVG(tx_bps), MIN(t), MAX(t)
                   FROM samples WHERE t >= ? GROUP BY device ORDER BY MAX(title) COLLATE NOCASE""",
                (since,),
            ).fetchall()
        keys = ("device", "title", "address", "samples", "answered", "ms_avg", "ms_max",
                "cpu_avg", "cpu_max", "mem_avg", "rx_avg", "tx_avg", "first", "last")
        report = []
        for row in rows:
            item = dict(zip(keys, row))
            item["availability"] = round(100.0 * item["answered"] / item["samples"], 2)
            report.append(item)
        return report

    def export_csv(self, hours: float, directory: str) -> str:
        since = time.time() - hours * 3600
        name = f"nettool-report-{time.strftime('%Y%m%d-%H%M%S')}.csv"
        path = os.path.join(directory, name)
        with self._lock:
            rows = self._db.execute(
                "SELECT title, address, t, up, ms, cpu, mem_pct, rx_bps, tx_bps "
                "FROM samples WHERE t >= ? ORDER BY title COLLATE NOCASE, t", (since,),
            ).fetchall()
        # utf-8-sig so Excel reads non-Latin device names correctly.
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(["device", "address", "time", "answered_ping", "ping_ms",
                             "cpu_percent", "memory_percent", "in_bits_per_s", "out_bits_per_s"])
            for title, address, t, up, *rest in rows:
                stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
                writer.writerow([title, address, stamp, "yes" if up else "no",
                                 *("" if v is None else round(v, 2) for v in rest)])
        return path

    def close(self) -> None:
        with self._lock:
            self._db.close()
