"""Project-scoped Discord sessions. Only hashes of bearer credentials reach disk."""
import hashlib
import json
import secrets
import sqlite3
import time

import guild_store

SESSION_SECONDS = 30 * 24 * 3600


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def init_db():
    with guild_store._conn() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS discord_sessions (
            token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, username TEXT NOT NULL,
            project_id TEXT NOT NULL, project_name TEXT NOT NULL,
            guilds TEXT NOT NULL, expires REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS discord_projects (
            user_id TEXT NOT NULL, project_id TEXT NOT NULL, project_name TEXT NOT NULL,
            guild_id TEXT NOT NULL, channel_id TEXT NOT NULL, development INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(user_id, project_id), UNIQUE(user_id, guild_id, channel_id)
        );
        ''')


def issue(user, project_id, project_name, guilds):
    token = secrets.token_urlsafe(48)
    with guild_store._conn() as db:
        db.execute('DELETE FROM discord_sessions WHERE expires <= ? OR (user_id = ? AND project_id = ?)',
                   (time.time(), str(user['id']), project_id))
        db.execute('INSERT INTO discord_sessions VALUES (?, ?, ?, ?, ?, ?, ?)',
                   (digest(token), str(user['id']), user.get('global_name') or user['username'],
                    project_id, project_name, json.dumps(guilds), time.time() + SESSION_SECONDS))
    return token


def session(token):
    if not token:
        return None
    with guild_store._conn() as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM discord_sessions WHERE token_hash = ? AND expires > ?',
                         (digest(token), time.time())).fetchone()
    return dict(row) if row else None


def project(user_id, project_id):
    with guild_store._conn() as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM discord_projects WHERE user_id = ? AND project_id = ?',
                         (str(user_id), project_id)).fetchone()
    return dict(row) if row else None


def channel_project(user_id, guild_id, channel_id):
    # Include disabled bindings so ordinary messages cannot fall through to a legacy route.
    with guild_store._conn() as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM discord_projects WHERE user_id = ? AND guild_id = ? AND channel_id = ?',
                         (str(user_id), str(guild_id), str(channel_id))).fetchone()
    return dict(row) if row else None


def bind(s, guild_id, channel_id, development):
    with guild_store._conn() as db:
        db.execute('''INSERT INTO discord_projects VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, project_id) DO UPDATE SET project_name=excluded.project_name,
            guild_id=excluded.guild_id, channel_id=excluded.channel_id, development=excluded.development''',
            (s['user_id'], s['project_id'], s['project_name'], str(guild_id), str(channel_id), int(development)))


def revoke(s):
    with guild_store._conn() as db:
        db.execute('DELETE FROM discord_sessions WHERE user_id = ? AND project_id = ?', (s['user_id'], s['project_id']))
        db.execute('DELETE FROM discord_projects WHERE user_id = ? AND project_id = ?', (s['user_id'], s['project_id']))
