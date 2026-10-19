import os
import sqlite3
import subprocess
import sys

from app.db.models import Base


def test_upgrade_preserves_existing_bot_context(tmp_path):
    database = tmp_path / "migration.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{database}"}

    def migrate(target):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", target],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    migrate("0001")
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO users VALUES (?, ?, ?, ?, ?)",
            ("old@example.com", "hash", "Old User", "a" * 32, "2026-10-13"),
        )
        conn.execute(
            "INSERT INTO organizations VALUES (?, ?, ?, ?)",
            ("Old Org", "a" * 32, "b" * 32, "2026-10-13"),
        )
        conn.execute(
            "INSERT INTO bots VALUES (?, ?, ?, ?, ?, ?)",
            ("b" * 32, "Old Bot", "Existing company information", "hi-IN", "c" * 32, "2026-10-13"),
        )
    # The clinic/platform migrations remain reversible.
    migrate("0004")
    with sqlite3.connect(database) as conn:
        row = conn.execute("SELECT company_context, template_id FROM bots").fetchone()
        assert row == ("Existing company information", "00000000000040008000000000000001")
        assert conn.execute("SELECT count(*) FROM bot_templates").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM clinic_doctors").fetchone()[0] == 3
        assert (
            conn.execute("SELECT count(*) FROM clinic_doctors WHERE bot_id IS NULL").fetchone()[0]
            == 3
        )
        assert (
            conn.execute(
                "SELECT service_type FROM bot_templates WHERE slug = 'clinic-booking'"
            ).fetchone()[0]
            == "clinic"
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0001"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database) as conn:
        assert (
            conn.execute("SELECT system_prompt FROM bots").fetchone()[0]
            == "Existing company information"
        )


def test_head_upgrade_and_intentional_downgrade_guard(tmp_path):
    database = tmp_path / "head.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{database}"}
    upgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert upgrade.returncode == 0, upgrade.stderr
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("0010",)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        for table in Base.metadata.sorted_tables:
            columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table.name}")')}
            assert columns == set(table.columns.keys()), table.name
    downgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0006"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert downgrade.returncode != 0
    assert "restore a backup to downgrade" in downgrade.stderr


def test_calendar_cleanup_preserves_existing_rows(tmp_path):
    database = tmp_path / "calendar.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{database}"}

    def migrate(direction, target):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", direction, target],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    migrate("upgrade", "0007")
    connection_id, calendar_id, event_id = "a" * 32, "b" * 32, "c" * 32
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO meeting_google_connection "
            "(id, created_at, google_subject, email, refresh_token_ciphertext, "
            "granted_scopes, status, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (connection_id, "2026-10-19", "subject", "bot@example.com", "encrypted",
             "calendar.events.readonly", "connected", "2026-10-19"),
        )
        conn.execute(
            "INSERT INTO meeting_calendars "
            "(id, created_at, connection_id, google_calendar_id, enabled, sync_token) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (calendar_id, "2026-10-19", connection_id, "primary", True, "obsolete"),
        )
        conn.execute(
            "INSERT INTO meeting_calendar_events "
            "(id, created_at, calendar_id, provider_event_id, title, cancelled, "
            "provider_updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, "2026-10-19", calendar_id, "event", "Team meeting", False, "2026-10-19"),
        )

    migrate("upgrade", "head")
    with sqlite3.connect(database) as conn:
        assert conn.execute(
            "SELECT id, connection_id, google_calendar_id, enabled FROM meeting_calendars"
        ).fetchone() == (calendar_id, connection_id, "primary", 1)
        assert conn.execute(
            "SELECT id, calendar_id, provider_event_id, title FROM meeting_calendar_events"
        ).fetchone() == (event_id, calendar_id, "event", "Team meeting")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    migrate("downgrade", "0007")
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT sync_token FROM meeting_calendars").fetchone() == (None,)
        assert conn.execute(
            "SELECT provider_updated_at FROM meeting_calendar_events"
        ).fetchone() == (None,)
    migrate("upgrade", "head")
