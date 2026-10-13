"""
SQLite storage engine for Signalpost historical company records.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path("signalpost.db")


def init_db(db_path: Path = DB_PATH) -> None:
    """Initialize the SQLite schema if it does not already exist."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS company_snapshots (
                orgnr TEXT PRIMARY KEY,
                company_name TEXT NOT NULL,
                profile_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def get_cached_profile(orgnr: str, db_path: Path = DB_PATH) -> dict | None:
    """Retrieve the previous profile snapshot for an organization number."""
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT profile_json FROM company_snapshots WHERE orgnr = ?",
            (orgnr,),
        )
        row = cursor.fetchone()
        if row and row[0]:
            return json.loads(row[0])
    finally:
        conn.close()
    return None


def save_company_snapshot(
    orgnr: str, company_name: str, profile_data: dict, db_path: Path = DB_PATH
) -> None:
    """Insert or update a company profile snapshot."""
    now_iso = datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO company_snapshots (orgnr, company_name, profile_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(orgnr) DO UPDATE SET
                company_name = excluded.company_name,
                profile_json = excluded.profile_json,
                updated_at = excluded.updated_at;
            """,
            (orgnr, company_name, json.dumps(profile_data, ensure_ascii=False), now_iso),
        )
        conn.commit()
    finally:
        conn.close()