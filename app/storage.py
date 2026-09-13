"""SQLite 方案版本库：不可变存储，同一输入重复保存幂等。"""
from __future__ import annotations

import hashlib
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

SEWING_SCHEMA = """
CREATE TABLE IF NOT EXISTS sewing_plans (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    uid TEXT NOT NULL UNIQUE,
    version INTEGER NOT NULL,
    name TEXT,
    request_json TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (plan_id, version)
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


class SewingStore:
    """锁线装订方案版本库：冻结来源拼版快照与锁线参数，不可变、幂等。"""

    def __init__(self, path: str = "imposition.db"):
        self.path = path
        with self._conn() as c:
            c.executescript(SEWING_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def save(
        self,
        *,
        name: str | None,
        plan_id: str,
        plan_version: int,
        input_hash: str,
        request_json: str,
        snapshot_json: str,
        result_json: str,
        metrics_json: str,
    ) -> dict:
        """保存为不可变版本。相同来源+参数+结果重复保存返回已有版本（幂等）。"""
        uid = f"{input_hash}:{hashlib.sha256(result_json.encode()).hexdigest()[:16]}"
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM sewing_plans WHERE uid = ?", (uid,)
            ).fetchone()
            if row:
                return {**self._meta(row), "created": False}
            version = (
                c.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM sewing_plans"
                    " WHERE plan_id = ?",
                    (plan_id,),
                ).fetchone()[0]
                + 1
            )
            sew_id = f"sew-{input_hash[:8]}-v{version}"
            created_at = datetime.now(timezone.utc).isoformat()
            c.execute(
                "INSERT INTO sewing_plans (id, plan_id, plan_version, uid, version,"
                " name, request_json, snapshot_json, result_json, input_hash,"
                " metrics_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sew_id,
                    plan_id,
                    plan_version,
                    uid,
                    version,
                    name,
                    request_json,
                    snapshot_json,
                    result_json,
                    input_hash,
                    metrics_json,
                    created_at,
                ),
            )
            row = c.execute(
                "SELECT * FROM sewing_plans WHERE id = ?", (sew_id,)
            ).fetchone()
            return {**self._meta(row), "created": True}

    @staticmethod
    def _meta(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "version": row["version"],
            "name": row["name"],
            "plan_id": row["plan_id"],
            "plan_version": row["plan_version"],
            "input_hash": row["input_hash"],
            "created_at": row["created_at"],
            "metrics": row["metrics_json"],
        }

    def get(self, sew_id: str) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM sewing_plans WHERE id = ?", (sew_id,)
            ).fetchone()

    def list(self) -> list[dict]:
        import json

        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM sewing_plans ORDER BY created_at, id"
            ).fetchall()
        out = []
        for r in rows:
            m = self._meta(r)
            m["metrics"] = json.loads(r["metrics_json"])
            out.append(m)
        return out
