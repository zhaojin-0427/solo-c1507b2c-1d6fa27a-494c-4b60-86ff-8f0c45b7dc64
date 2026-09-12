"""SQLite 方案版本库：不可变存储，同一输入重复保存幂等。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    job_hash TEXT NOT NULL,
    selection_hash TEXT NOT NULL,
    uid TEXT NOT NULL UNIQUE,
    version INTEGER NOT NULL,
    name TEXT,
    input_json TEXT NOT NULL,
    selection_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    svg TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (job_hash, version)
);
"""


class PlanStore:
    def __init__(self, path: str = "imposition.db"):
        self.path = path
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def save(
        self,
        *,
        name: str | None,
        job_hash: str,
        selection_hash: str,
        input_json: str,
        selection_json: str,
        result_json: str,
        svg: str,
        metrics_json: str,
    ) -> dict:
        """保存为不可变版本。相同任务+相同选择重复保存返回已有版本（幂等）。"""
        uid = f"{job_hash}:{selection_hash}"
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM plans WHERE uid = ? AND result_json = ?",
                (uid, result_json),
            ).fetchone()
            if row:
                return {**self._meta(row), "created": False}
            version = (
                c.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM plans WHERE job_hash = ?",
                    (job_hash,),
                ).fetchone()[0]
                + 1
            )
            plan_id = f"plan-{job_hash[:8]}-v{version}"
            created_at = datetime.now(timezone.utc).isoformat()
            c.execute(
                "INSERT INTO plans (id, job_hash, selection_hash, uid, version, name,"
                " input_json, selection_json, result_json, svg, metrics_json, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    plan_id,
                    job_hash,
                    selection_hash,
                    uid,
                    version,
                    name,
                    input_json,
                    selection_json,
                    result_json,
                    svg,
                    metrics_json,
                    created_at,
                ),
            )
            row = c.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
            return {**self._meta(row), "created": True}

    @staticmethod
    def _meta(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "version": row["version"],
            "name": row["name"],
            "created_at": row["created_at"],
            "metrics": row["metrics_json"],
        }

    def get(self, plan_id: str) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()

    def list(self) -> list[dict]:
        import json

        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM plans ORDER BY created_at, id"
            ).fetchall()
        out = []
        for r in rows:
            m = self._meta(r)
            m["metrics"] = json.loads(r["metrics_json"])
            out.append(m)
        return out
