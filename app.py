"""
ChatDesk - Local Network CRM + WhatsApp Campaign Manager
Flask Backend with SQLite + Playwright automation
"""

import os
import csv
import json
import uuid
import random
import threading
import time
import hashlib
import re
from datetime import datetime, date, timedelta
from functools import wraps
from io import StringIO

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, jsonify, flash, send_file, make_response
)
import sqlite3

# ─────────────────────────────────────────────
# App Setup
# ─────────────────────────────────────────────
def load_secret_key():
    env_key = os.environ.get('CHATDESK_SECRET_KEY')
    if env_key:
        return env_key

    key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.flask_secret_key')
    if os.path.exists(key_path):
        with open(key_path, 'r', encoding='utf-8') as fh:
            key = fh.read().strip()
            if key:
                return key

    key = uuid.uuid4().hex + uuid.uuid4().hex
    with open(key_path, 'w', encoding='utf-8') as fh:
        fh.write(key)
    return key


app = Flask(__name__)
app.secret_key = load_secret_key()
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['EXPORT_FOLDER'] = 'exports'
DB_PATH = 'database.db'
PLAYWRIGHT_SESSION_DIR = 'browser_session'
SESSION_TIMEOUT_MINUTES = 5

# ─────────────────────────────────────────────
# Safety Constants (FIXED - Do not change)
# ─────────────────────────────────────────────
MAX_CONTACTS_PER_CAMPAIGN = 100
DAILY_MAX = 200
DELAY_MIN = 5      # seconds
DELAY_MAX = 15     # seconds
BREAK_AFTER = 20   # contacts
BREAK_MIN = 120    # 2 min
BREAK_MAX = 300    # 5 min

# ─────────────────────────────────────────────
# Campaign State (in-memory)
# ─────────────────────────────────────────────
campaign_state = {
    'running': False,
    'paused': False,
    'campaign_id': None,
    'total': 0,
    'sent': 0,
    'failed': 0,
    'current_contact': None,
    'log': [],       # recent activity lines
    'thread': None,
}

# ─────────────────────────────────────────────
# Database Helpers
# ─────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def utc_now():
    return datetime.utcnow().replace(microsecond=0)


def utc_now_str():
    return utc_now().strftime('%Y-%m-%d %H:%M:%S')


def local_today_utc_bounds():
    now_local = datetime.now()
    now_utc = datetime.utcnow()
    local_offset = now_local - now_utc
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local - local_offset
    end_utc = start_utc + timedelta(days=1)
    return (
        start_utc.strftime('%Y-%m-%d %H:%M:%S'),
        end_utc.strftime('%Y-%m-%d %H:%M:%S')
    )


def ensure_column(conn, table_name, column_name, column_sql):
    columns = {row['name'] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
    if column_name not in columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")

def init_db():
    """Create all tables and default owner account."""
    conn = get_db()
    c = conn.cursor()

    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'staff',
            last_login TEXT,
            online INTEGER DEFAULT 0,
            session_token TEXT
        );

        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT UNIQUE NOT NULL,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            message TEXT NOT NULL,
            template_name TEXT,
            status TEXT DEFAULT 'draft',
            total INTEGER DEFAULT 0,
            sent INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            started_at TEXT,
            finished_at TEXT
        );

        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER,
            contact_id INTEGER,
            contact_name TEXT,
            contact_phone TEXT,
            status TEXT,
            message TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            token TEXT UNIQUE,
            created_at TEXT DEFAULT (datetime('now')),
            last_seen TEXT DEFAULT (datetime('now')),
            revoked_at TEXT,
            expires_at TEXT
        );

        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER,
            campaign_name TEXT,
            date TEXT,
            total_contacts INTEGER,
            sent INTEGER,
            failed INTEGER
        );

        CREATE TABLE IF NOT EXISTS templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS campaign_recipients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            contact_id INTEGER NOT NULL,
            contact_name TEXT NOT NULL,
            contact_phone TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            error_message TEXT,
            last_attempt_at TEXT,
            sent_at TEXT,
            UNIQUE(campaign_id, contact_id)
        );
    """)

    ensure_column(conn, 'sessions', 'last_seen', "TEXT DEFAULT (datetime('now'))")
    ensure_column(conn, 'sessions', 'revoked_at', "TEXT")
    ensure_column(conn, 'sessions', 'expires_at', "TEXT")
    ensure_column(conn, 'history', 'campaign_id', "INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_status_created_at ON logs(status, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_token ON sessions(user_id, token)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_last_seen ON sessions(last_seen)")

    # Create default owner if not exists
    owner_exists = c.execute("SELECT id FROM users WHERE role='owner'").fetchone()
    if not owner_exists:
        pw = hash_password('admin123')
        c.execute(
            "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
            ('owner', pw, 'owner')
        )

    conn.commit()
    conn.close()

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

# ─────────────────────────────────────────────
# Auth Decorators
# ─────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def owner_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('role') != 'owner':
            flash('Owner access required.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


@app.before_request
def validate_active_session():
    if request.endpoint == 'static':
        return None

    conn = get_db()
    cleanup_expired_sessions(conn)

    user_id = session.get('user_id')
    token = session.get('session_token')
    if not user_id:
        conn.commit()
        conn.close()
        return None

    active_session = conn.execute(
        """
        SELECT u.username, u.role, s.token
        FROM users u
        JOIN sessions s ON s.user_id = u.id
        WHERE u.id=? AND s.token=? AND s.revoked_at IS NULL
        LIMIT 1
        """,
        (user_id, token)
    ).fetchone()

    if not active_session:
        sync_user_online_status(conn, user_id)
        conn.commit()
        conn.close()
        session.clear()
        return redirect(url_for('login'))

    expires_at = (utc_now() + timedelta(hours=12)).strftime('%Y-%m-%d %H:%M:%S')
    conn.execute(
        "UPDATE sessions SET last_seen=?, expires_at=? WHERE user_id=? AND token=?",
        (utc_now_str(), expires_at, user_id, token)
    )
    conn.execute(
        "UPDATE users SET online=1, session_token=? WHERE id=?",
        (token, user_id)
    )
    conn.commit()
    conn.close()

    session.permanent = True
    session['username'] = active_session['username']
    session['role'] = active_session['role']
    return None

# ─────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────
def validate_phone(phone):
    """Basic phone validation."""
    cleaned = re.sub(r'[\s\-\(\)\+]', '', str(phone))
    return cleaned.isdigit() and 7 <= len(cleaned) <= 15

def normalize_phone(phone):
    return re.sub(r'[\s\-\(\)\+]', '', str(phone))

def get_daily_sent_count():
    """How many messages sent today across all campaigns."""
    conn = get_db()
    start_utc, end_utc = local_today_utc_bounds()
    row = conn.execute(
        """
        SELECT COUNT(*) as cnt
        FROM logs
        WHERE status='sent' AND created_at >= ? AND created_at < ?
        """,
        (start_utc, end_utc)
    ).fetchone()
    conn.close()
    return row['cnt'] if row else 0

def add_log_entry(icon, message):
    """Add to in-memory activity log (max 50 entries)."""
    entry = f"{icon} {message}"
    campaign_state['log'].insert(0, entry)
    campaign_state['log'] = campaign_state['log'][:50]

def sync_campaign_counts(conn, campaign_id):
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS sent,
            SUM(CASE WHEN status IN ('failed', 'skipped') THEN 1 ELSE 0 END) AS failed
        FROM campaign_recipients
        WHERE campaign_id=?
        """,
        (campaign_id,)
    ).fetchone()
    total = row['total'] or 0
    sent = row['sent'] or 0
    failed = row['failed'] or 0
    conn.execute(
        "UPDATE campaigns SET total=?, sent=?, failed=? WHERE id=?",
        (total, sent, failed, campaign_id)
    )
    return {'total': total, 'sent': sent, 'failed': failed}


def upsert_history_summary(conn, campaign_id, counts=None):
    campaign = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if not campaign:
        return None

    if counts is None:
        counts = sync_campaign_counts(conn, campaign_id)

    history_row = conn.execute(
        "SELECT id FROM history WHERE campaign_id=? ORDER BY id DESC LIMIT 1",
        (campaign_id,)
    ).fetchone()
    history_date = datetime.now().date().isoformat()
    params = (
        campaign['name'],
        history_date,
        counts['total'],
        counts['sent'],
        counts['failed']
    )
    if history_row:
        conn.execute(
            """
            UPDATE history
            SET campaign_name=?, date=?, total_contacts=?, sent=?, failed=?
            WHERE id=?
            """,
            params + (history_row['id'],)
        )
        return history_row['id']

    conn.execute(
        """
        INSERT INTO history (campaign_id, campaign_name, date, total_contacts, sent, failed)
        VALUES (?,?,?,?,?,?)
        """,
        (campaign_id,) + params
    )
    return conn.execute("SELECT last_insert_rowid() AS id").fetchone()['id']


def record_campaign_event(conn, campaign_id, recipient, status, message_text=None):
    conn.execute(
        """
        INSERT INTO logs (campaign_id, contact_id, contact_name, contact_phone, status, message)
        VALUES (?,?,?,?,?,?)
        """,
        (
            campaign_id,
            recipient['contact_id'],
            recipient['contact_name'],
            normalize_phone(recipient['contact_phone']),
            status,
            message_text
        )
    )
    counts = sync_campaign_counts(conn, campaign_id)
    upsert_history_summary(conn, campaign_id, counts)
    return counts


def sync_user_online_status(conn, user_id):
    active_session = conn.execute(
        """
        SELECT token
        FROM sessions
        WHERE user_id=? AND revoked_at IS NULL
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id,)
    ).fetchone()
    conn.execute(
        "UPDATE users SET online=?, session_token=? WHERE id=?",
        (1 if active_session else 0, active_session['token'] if active_session else None, user_id)
    )


def cleanup_expired_sessions(conn):
    cutoff = (utc_now() - timedelta(minutes=SESSION_TIMEOUT_MINUTES)).strftime('%Y-%m-%d %H:%M:%S')
    affected_users = [
        row['user_id'] for row in conn.execute(
            """
            SELECT DISTINCT user_id
            FROM sessions
            WHERE revoked_at IS NOT NULL
               OR last_seen < ?
               OR (expires_at IS NOT NULL AND expires_at < ?)
            """,
            (cutoff, utc_now_str())
        ).fetchall()
    ]
    conn.execute(
        """
        DELETE FROM sessions
        WHERE revoked_at IS NOT NULL
           OR last_seen < ?
           OR (expires_at IS NOT NULL AND expires_at < ?)
        """,
        (cutoff, utc_now_str())
    )
    for user_id in affected_users:
        sync_user_online_status(conn, user_id)
    return affected_users


def create_user_session(conn, user_id):
    token = str(uuid.uuid4())
    now = utc_now_str()
    expires_at = (utc_now() + timedelta(hours=12)).strftime('%Y-%m-%d %H:%M:%S')
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    conn.execute(
        """
        INSERT INTO sessions (user_id, token, created_at, last_seen, expires_at)
        VALUES (?,?,?,?,?)
        """,
        (user_id, token, now, now, expires_at)
    )
    conn.execute(
        "UPDATE users SET last_login=datetime('now'), online=1, session_token=? WHERE id=?",
        (token, user_id)
    )
    return token


def invalidate_session(conn, user_id, token=None):
    if token:
        conn.execute("DELETE FROM sessions WHERE user_id=? AND token=?", (user_id, token))
    else:
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    sync_user_online_status(conn, user_id)

def load_campaign_state(campaign_id=None):
    conn = get_db()
    if campaign_id is None:
        campaign = conn.execute(
            "SELECT * FROM campaigns WHERE status IN ('running','paused') ORDER BY id DESC LIMIT 1"
        ).fetchone()
    else:
        campaign = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()

    if not campaign:
        conn.close()
        campaign_state.update({
            'running': False,
            'paused': False,
            'campaign_id': None,
            'total': 0,
            'sent': 0,
            'failed': 0,
            'current_contact': None,
            'thread': None,
        })
        return

    counts = sync_campaign_counts(conn, campaign['id'])
    conn.commit()
    conn.close()
    campaign_state.update({
        'running': campaign['status'] == 'running',
        'paused': campaign['status'] == 'paused',
        'campaign_id': campaign['id'],
        'total': counts['total'],
        'sent': counts['sent'],
        'failed': counts['failed'],
        'current_contact': None,
        'thread': None,
    })

def restore_runtime_state():
    conn = get_db()
    cleanup_expired_sessions(conn)
    user_ids = [row['id'] for row in conn.execute("SELECT id FROM users").fetchall()]
    for user_id in user_ids:
        sync_user_online_status(conn, user_id)
    active = conn.execute(
        "SELECT id FROM campaigns WHERE status IN ('running','paused') ORDER BY id DESC"
    ).fetchall()

    for row in active:
        conn.execute("UPDATE campaigns SET status='paused' WHERE id=?", (row['id'],))
        conn.execute(
            """
            UPDATE campaign_recipients
            SET status='skipped',
                error_message=COALESCE(error_message, 'App restarted before send confirmation'),
                last_attempt_at=datetime('now')
            WHERE campaign_id=? AND status='sending'
            """,
            (row['id'],)
        )
        sync_campaign_counts(conn, row['id'])
        upsert_history_summary(conn, row['id'])

    conn.commit()
    conn.close()
    load_campaign_state()
    if campaign_state['campaign_id']:
        add_log_entry('INFO', 'Previous campaign restored in paused mode after restart.')

def pause_active_campaign(reason, current_contact=None):
    if not campaign_state['campaign_id']:
        return
    campaign_state['running'] = False
    campaign_state['paused'] = True
    if current_contact:
        campaign_state['current_contact'] = current_contact
    conn = get_db()
    conn.execute(
        "UPDATE campaigns SET status='paused' WHERE id=?",
        (campaign_state['campaign_id'],)
    )
    sync_campaign_counts(conn, campaign_state['campaign_id'])
    conn.commit()
    conn.close()
    add_log_entry('WARN', reason)

# ─────────────────────────────────────────────
# Campaign Runner (Playwright-based)
# ─────────────────────────────────────────────
def run_campaign(campaign_id):
    """
    Background thread: Opens WhatsApp Web via Playwright,
    sends messages to each contact with delays.
    """
    global campaign_state

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        add_log_entry('ERROR', 'Playwright not installed. Run: pip install playwright && playwright install chromium')
        campaign_state['running'] = False
        return
    conn = get_db()
    campaign = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if not campaign:
        conn.close()
        pause_active_campaign('Campaign record missing. Execution stopped.')
        return
    conn.close()

    sent_batch = 0
    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                PLAYWRIGHT_SESSION_DIR,
                headless=False
            )
            page = context.new_page()

            page.goto('https://web.whatsapp.com')
            add_log_entry('INFO', 'WhatsApp Web opened. Scan QR code if required.')

            try:
                page.wait_for_selector('div[data-testid="chat-list"]', timeout=60000)
                add_log_entry('OK', 'WhatsApp Web connected.')
            except Exception:
                context.close()
                pause_active_campaign('WhatsApp Web connection timeout. Campaign paused.')
                return

            while campaign_state['running']:
                while campaign_state['paused'] and campaign_state['running']:
                    time.sleep(2)

                if not campaign_state['running']:
                    break

                if get_daily_sent_count() >= DAILY_MAX:
                    pause_active_campaign(f'Daily limit of {DAILY_MAX} reached. Campaign paused.')
                    break

                conn = get_db()
                recipient = conn.execute(
                    """
                    SELECT * FROM campaign_recipients
                    WHERE campaign_id=? AND status='pending'
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    (campaign_id,)
                ).fetchone()

                if not recipient:
                    counts = sync_campaign_counts(conn, campaign_id)
                    conn.execute(
                        "UPDATE campaigns SET status='finished', finished_at=datetime('now') WHERE id=?",
                        (campaign_id,)
                    )
                    upsert_history_summary(conn, campaign_id, counts)
                    conn.commit()
                    conn.close()
                    campaign_state.update({
                        'running': False,
                        'paused': False,
                        'current_contact': None,
                    })
                    add_log_entry('OK', 'Campaign finished.')
                    break

                name = recipient['contact_name']
                phone = normalize_phone(recipient['contact_phone'])
                message = campaign['message'].replace('{name}', name)

                campaign_state['current_contact'] = name
                add_log_entry('INFO', f'Preparing {name} ({phone})')

                conn.execute(
                    """
                    UPDATE campaign_recipients
                    SET status='sending', last_attempt_at=datetime('now'), error_message=NULL
                    WHERE id=?
                    """,
                    (recipient['id'],)
                )
                conn.commit()
                conn.close()

                try:
                    url = f'https://web.whatsapp.com/send?phone={phone}&text={message}'
                    page.goto(url)
                    page.wait_for_selector('div[data-testid="conversation-compose-box-input"]', timeout=15000)
                    time.sleep(1)
                    page.keyboard.press('Enter')
                    time.sleep(1)

                    conn = get_db()
                    conn.execute(
                        """
                        UPDATE campaign_recipients
                        SET status='sent', sent_at=datetime('now'), last_attempt_at=datetime('now')
                        WHERE id=?
                        """,
                        (recipient['id'],)
                    )
                    counts = record_campaign_event(conn, campaign_id, recipient, 'sent')
                    conn.commit()
                    conn.close()

                    campaign_state['total'] = counts['total']
                    campaign_state['sent'] = counts['sent']
                    campaign_state['failed'] = counts['failed']
                    sent_batch += 1
                    add_log_entry('OK', f'Sent to {name}')

                except Exception as e:
                    error_text = str(e)
                    if 'Target page, context or browser has been closed' in error_text:
                        conn = get_db()
                        conn.execute(
                            """
                            UPDATE campaign_recipients
                            SET status='skipped', error_message=?, last_attempt_at=datetime('now')
                            WHERE id=?
                            """,
                            ('Browser closed unexpectedly before confirmation', recipient['id'])
                        )
                        counts = sync_campaign_counts(conn, campaign_id)
                        upsert_history_summary(conn, campaign_id, counts)
                        conn.commit()
                        conn.close()
                        campaign_state['total'] = counts['total']
                        campaign_state['sent'] = counts['sent']
                        campaign_state['failed'] = counts['failed']
                        pause_active_campaign('Browser closed unexpectedly. Campaign paused.', current_contact=name)
                        break

                    conn = get_db()
                    conn.execute(
                        """
                        UPDATE campaign_recipients
                        SET status='failed', error_message=?, last_attempt_at=datetime('now')
                        WHERE id=?
                        """,
                        (error_text, recipient['id'])
                    )
                    counts = record_campaign_event(conn, campaign_id, recipient, 'failed', error_text)
                    conn.commit()
                    conn.close()
                    campaign_state['total'] = counts['total']
                    campaign_state['sent'] = counts['sent']
                    campaign_state['failed'] = counts['failed']
                    add_log_entry('ERROR', f'Failed for {name}')

                if not campaign_state['running']:
                    break

                if sent_batch >= BREAK_AFTER:
                    break_time = random.randint(BREAK_MIN, BREAK_MAX)
                    add_log_entry('INFO', f'Auto break for {break_time // 60} min after {BREAK_AFTER} sends')
                    time.sleep(break_time)
                    sent_batch = 0
                else:
                    delay = random.randint(DELAY_MIN, DELAY_MAX)
                    add_log_entry('INFO', f'Next send in {delay} sec')
                    time.sleep(delay)

            context.close()
    except Exception:
        pause_active_campaign('Campaign interrupted unexpectedly. State restored in paused mode.')

# ─────────────────────────────────────────────
# Routes: Auth
# ─────────────────────────────────────────────
@app.route('/', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))

    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE username=? AND password=?",
            (username, hash_password(password))
        ).fetchone()

        if user:
            token = create_user_session(conn, user['id'])
            conn.commit()
            conn.close()
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            session['session_token'] = token
            session.permanent = True
            return redirect(url_for('dashboard'))
        else:
            error = 'Invalid username or password.'
            conn.close()

    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    if 'user_id' in session:
        conn = get_db()
        invalidate_session(conn, session['user_id'], session.get('session_token'))
        conn.commit()
        conn.close()
    session.clear()
    return redirect(url_for('login'))

# ─────────────────────────────────────────────
# Routes: Dashboard
# ─────────────────────────────────────────────
@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_db()
    total_contacts = conn.execute("SELECT COUNT(*) as c FROM contacts").fetchone()['c']
    sent_today = conn.execute(
        """
        SELECT COUNT(*) as c
        FROM logs
        WHERE status='sent' AND created_at >= ? AND created_at < ?
        """,
        local_today_utc_bounds()
    ).fetchone()['c']
    failed_today = conn.execute(
        """
        SELECT COUNT(*) as c
        FROM logs
        WHERE status='failed' AND created_at >= ? AND created_at < ?
        """,
        local_today_utc_bounds()
    ).fetchone()['c']
    online_users = conn.execute("SELECT COUNT(*) as c FROM users WHERE online=1").fetchone()['c']
    active_campaign = conn.execute(
        "SELECT * FROM campaigns WHERE status IN ('running','paused') ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    return render_template('dashboard.html',
        total_contacts=total_contacts,
        sent_today=sent_today,
        failed_today=failed_today,
        online_users=online_users,
        active_campaign=active_campaign,
        campaign_state=campaign_state,
        safety={
            'max_per_campaign': MAX_CONTACTS_PER_CAMPAIGN,
            'daily_max': DAILY_MAX,
            'delay': f'{DELAY_MIN}–{DELAY_MAX} sec',
        }
    )

@app.route('/how-it-works')
@login_required
def how_it_works():
    return render_template('how_it_works.html')

@app.route('/api/dashboard-stats')
@login_required
def api_dashboard_stats():
    conn = get_db()
    sent_today = conn.execute(
        """
        SELECT COUNT(*) as c
        FROM logs
        WHERE status='sent' AND created_at >= ? AND created_at < ?
        """,
        local_today_utc_bounds()
    ).fetchone()['c']
    online_users = conn.execute("SELECT COUNT(*) as c FROM users WHERE online=1").fetchone()['c']
    conn.close()

    return jsonify({
        'running': campaign_state['running'],
        'paused': campaign_state['paused'],
        'sent': campaign_state['sent'],
        'failed': campaign_state['failed'],
        'total': campaign_state['total'],
        'current_contact': campaign_state['current_contact'],
        'log': campaign_state['log'][:10],
        'sent_today': sent_today,
        'online_users': online_users,
    })

# ─────────────────────────────────────────────
# Routes: Contacts
# ─────────────────────────────────────────────
@app.route('/contacts')
@login_required
def contacts():
    conn = get_db()
    search = request.args.get('q', '').strip()
    if search:
        rows = conn.execute(
            "SELECT * FROM contacts WHERE name LIKE ? OR phone LIKE ? ORDER BY name",
            (f'%{search}%', f'%{search}%')
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM contacts ORDER BY name").fetchall()
    total = conn.execute("SELECT COUNT(*) as c FROM contacts").fetchone()['c']
    conn.close()
    return render_template('contacts.html', contacts=rows, total=total, search=search)

@app.route('/contacts/add', methods=['POST'])
@owner_required
def add_contact():
    name = request.form.get('name', '').strip()
    phone = request.form.get('phone', '').strip()
    notes = request.form.get('notes', '').strip()

    if not name or not phone:
        flash('Name and phone are required.', 'danger')
        return redirect(url_for('contacts'))

    if not validate_phone(phone):
        flash(f'Invalid phone number: {phone}', 'danger')
        return redirect(url_for('contacts'))

    phone = normalize_phone(phone)
    conn = get_db()
    try:
        conn.execute("INSERT INTO contacts (name, phone, notes) VALUES (?,?,?)", (name, phone, notes))
        conn.commit()
        flash(f'Contact {name} added.', 'success')
    except sqlite3.IntegrityError:
        flash(f'Phone {phone} already exists.', 'warning')
    conn.close()
    return redirect(url_for('contacts'))

@app.route('/contacts/upload', methods=['POST'])
@owner_required
def upload_contacts():
    file = request.files.get('csv_file')
    if not file or not file.filename.endswith('.csv'):
        flash('Please upload a valid CSV file.', 'danger')
        return redirect(url_for('contacts'))

    content = file.read().decode('utf-8-sig', errors='ignore')
    reader = csv.DictReader(StringIO(content))

    added = skipped_dup = invalid = 0
    conn = get_db()

    for row in reader:
        # Flexible column name matching
        name = (row.get('name') or row.get('Name') or row.get('NAME') or '').strip()
        phone = (row.get('phone') or row.get('Phone') or row.get('PHONE') or
                 row.get('mobile') or row.get('Mobile') or '').strip()
        notes = (row.get('notes') or row.get('Notes') or '').strip()

        if not name or not phone:
            invalid += 1
            continue

        if not validate_phone(phone):
            invalid += 1
            continue

        phone = normalize_phone(phone)
        try:
            conn.execute("INSERT INTO contacts (name, phone, notes) VALUES (?,?,?)", (name, phone, notes))
            conn.commit()
            added += 1
        except sqlite3.IntegrityError:
            skipped_dup += 1

    conn.close()
    flash(f'Upload complete: {added} added, {skipped_dup} duplicates skipped, {invalid} invalid.', 'success')
    return redirect(url_for('contacts'))

@app.route('/contacts/delete/<int:cid>', methods=['POST'])
@owner_required
def delete_contact(cid):
    conn = get_db()
    conn.execute("DELETE FROM contacts WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    flash('Contact deleted.', 'success')
    return redirect(url_for('contacts'))

@app.route('/contacts/export')
@owner_required
def export_contacts():
    conn = get_db()
    rows = conn.execute("SELECT name, phone, notes FROM contacts ORDER BY name").fetchall()
    conn.close()

    si = StringIO()
    writer = csv.writer(si)
    writer.writerow(['name', 'phone', 'notes'])
    for r in rows:
        writer.writerow([r['name'], r['phone'], r['notes']])

    output = make_response(si.getvalue())
    output.headers['Content-Disposition'] = 'attachment; filename=contacts_export.csv'
    output.headers['Content-type'] = 'text/csv'
    return output

@app.route('/contacts/sample-csv')
@login_required
def sample_csv():
    si = StringIO()
    writer = csv.writer(si)
    writer.writerow(['name', 'phone', 'notes'])
    writer.writerow(['Rahul Sharma', '919876543210', 'Class A student'])
    writer.writerow(['Aisha Khan', '919812345678', 'Class B student'])
    writer.writerow(['Priya Mehta', '919998887776', ''])
    output = make_response(si.getvalue())
    output.headers['Content-Disposition'] = 'attachment; filename=sample_contacts.csv'
    output.headers['Content-type'] = 'text/csv'
    return output

# ─────────────────────────────────────────────
# Routes: Campaigns
# ─────────────────────────────────────────────
@app.route('/campaigns')
@login_required
def campaigns():
    conn = get_db()
    camps = conn.execute("SELECT * FROM campaigns ORDER BY id DESC").fetchall()
    templates = conn.execute("SELECT * FROM templates ORDER BY id DESC").fetchall()
    total_contacts = conn.execute("SELECT COUNT(*) as c FROM contacts").fetchone()['c']
    conn.close()
    return render_template('campaigns.html',
        campaigns=camps,
        templates=templates,
        total_contacts=total_contacts,
        max_contacts=MAX_CONTACTS_PER_CAMPAIGN,
        campaign_state=campaign_state
    )

@app.route('/campaigns/create', methods=['POST'])
@owner_required
def create_campaign():
    name = request.form.get('name', '').strip()
    message = request.form.get('message', '').strip()
    template_name = request.form.get('template_name', '').strip()

    if not name or not message:
        flash('Campaign name and message are required.', 'danger')
        return redirect(url_for('campaigns'))

    conn = get_db()
    conn.execute(
        "INSERT INTO campaigns (name, message, template_name, status) VALUES (?,?,?,'draft')",
        (name, message, template_name)
    )

    # Save as template if name given
    if template_name:
        try:
            conn.execute("INSERT INTO templates (name, message) VALUES (?,?)", (template_name, message))
        except Exception:
            pass

    conn.commit()
    conn.close()
    flash(f'Campaign "{name}" created.', 'success')
    return redirect(url_for('campaigns'))

@app.route('/campaigns/start/<int:cid>', methods=['POST'])
@owner_required
def start_campaign(cid):
    global campaign_state

    if campaign_state['running'] or campaign_state['paused']:
        flash('Finish or resume the current paused/running campaign before starting a new one.', 'warning')
        return redirect(url_for('campaigns'))

    conn = get_db()
    camp = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not camp:
        flash('Campaign not found.', 'danger')
        conn.close()
        return redirect(url_for('campaigns'))

    # Get contacts (up to MAX)
    contacts = conn.execute(
        "SELECT * FROM contacts ORDER BY RANDOM() LIMIT ?",
        (MAX_CONTACTS_PER_CAMPAIGN,)
    ).fetchall()
    contacts = [dict(c) for c in contacts]

    if not contacts:
        flash('No contacts available.', 'warning')
        conn.close()
        return redirect(url_for('campaigns'))

    # Check daily limit
    if get_daily_sent_count() >= DAILY_MAX:
        flash(f'Daily limit of {DAILY_MAX} messages reached. Try tomorrow.', 'danger')
        conn.close()
        return redirect(url_for('campaigns'))

    conn.execute(
        "DELETE FROM campaign_recipients WHERE campaign_id=?",
        (cid,)
    )
    conn.execute("DELETE FROM history WHERE campaign_id=?", (cid,))
    for contact in contacts:
        conn.execute(
            """
            INSERT INTO campaign_recipients (campaign_id, contact_id, contact_name, contact_phone, status)
            VALUES (?,?,?,?, 'pending')
            """,
            (cid, contact['id'], contact['name'], normalize_phone(contact['phone']))
        )

    conn.execute(
        "UPDATE campaigns SET status='running', started_at=datetime('now'), finished_at=NULL, total=?, sent=0, failed=0 WHERE id=?",
        (len(contacts), cid)
    )
    upsert_history_summary(conn, cid, {'total': len(contacts), 'sent': 0, 'failed': 0})
    conn.commit()
    conn.close()

    campaign_state.update({
        'running': True,
        'paused': False,
        'campaign_id': cid,
        'total': len(contacts),
        'sent': 0,
        'failed': 0,
        'current_contact': None,
        'log': [],
    })

    t = threading.Thread(target=run_campaign, args=(cid,), daemon=True)
    campaign_state['thread'] = t
    t.start()

    flash(f'Campaign "{camp["name"]}" started with {len(contacts)} contacts.', 'success')
    return redirect(url_for('dashboard'))

@app.route('/campaigns/pause', methods=['POST'])
@login_required
def pause_campaign():
    campaign_state['running'] = False
    campaign_state['paused'] = True
    if campaign_state['campaign_id']:
        conn = get_db()
        conn.execute(
            "UPDATE campaigns SET status='paused' WHERE id=?",
            (campaign_state['campaign_id'],)
        )
        sync_campaign_counts(conn, campaign_state['campaign_id'])
        conn.commit()
        conn.close()
    add_log_entry('WARN', 'Campaign paused by user.')
    return jsonify({'status': 'paused'})

@app.route('/campaigns/resume', methods=['POST'])
@login_required
def resume_campaign():
    if not campaign_state['campaign_id']:
        load_campaign_state()
    if campaign_state['campaign_id']:
        conn = get_db()
        conn.execute(
            "UPDATE campaigns SET status='running' WHERE id=?",
            (campaign_state['campaign_id'],)
        )
        sync_campaign_counts(conn, campaign_state['campaign_id'])
        conn.commit()
        conn.close()
        campaign_state['running'] = True
        campaign_state['paused'] = False
        if not campaign_state.get('thread') or not campaign_state['thread'].is_alive():
            t = threading.Thread(target=run_campaign, args=(campaign_state['campaign_id'],), daemon=True)
            campaign_state['thread'] = t
            t.start()
    add_log_entry('OK', 'Campaign resumed by user.')
    return jsonify({'status': 'resumed'})

@app.route('/campaigns/stop', methods=['POST'])
@owner_required
def stop_campaign():
    campaign_state['running'] = False
    campaign_state['paused'] = False
    if campaign_state['campaign_id']:
        conn = get_db()
        conn.execute(
            "UPDATE campaigns SET status='stopped' WHERE id=?",
            (campaign_state['campaign_id'],)
        )
        conn.execute(
            """
            UPDATE campaign_recipients
            SET status='skipped', error_message=COALESCE(error_message, 'Stopped by user'), last_attempt_at=datetime('now')
            WHERE campaign_id=? AND status IN ('pending','sending')
            """,
            (campaign_state['campaign_id'],)
        )
        sync_campaign_counts(conn, campaign_state['campaign_id'])
        conn.commit()
        conn.close()
    campaign_state['thread'] = None
    add_log_entry('WARN', 'Campaign stopped by owner.')
    return jsonify({'status': 'stopped'})

@app.route('/campaigns/delete/<int:cid>', methods=['POST'])
@owner_required
def delete_campaign(cid):
    conn = get_db()
    conn.execute("DELETE FROM campaigns WHERE id=?", (cid,))
    conn.execute("DELETE FROM logs WHERE campaign_id=?", (cid,))
    conn.execute("DELETE FROM campaign_recipients WHERE campaign_id=?", (cid,))
    conn.commit()
    conn.close()
    if campaign_state.get('campaign_id') == cid:
        load_campaign_state()
    flash('Campaign deleted.', 'success')
    return redirect(url_for('campaigns'))

# ─────────────────────────────────────────────
# Routes: History
# ─────────────────────────────────────────────
@app.route('/history')
@login_required
def history():
    conn = get_db()
    rows = conn.execute("SELECT * FROM history ORDER BY id DESC").fetchall()
    camps = conn.execute(
        "SELECT * FROM campaigns WHERE status IN ('finished','stopped') ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return render_template('history.html', history=rows, campaigns=camps)

# ─────────────────────────────────────────────
# Routes: Settings
# ─────────────────────────────────────────────
@app.route('/settings')
@owner_required
def settings():
    conn = get_db()
    users = conn.execute("SELECT * FROM users ORDER BY role, username").fetchall()
    conn.close()
    return render_template('settings.html', users=users)

@app.route('/settings/change-password', methods=['POST'])
@owner_required
def change_password():
    current = request.form.get('current_password', '')
    new_pw = request.form.get('new_password', '')
    confirm = request.form.get('confirm_password', '')

    if new_pw != confirm:
        flash('New passwords do not match.', 'danger')
        return redirect(url_for('settings'))

    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE id=? AND password=?",
        (session['user_id'], hash_password(current))
    ).fetchone()

    if not user:
        flash('Current password is incorrect.', 'danger')
        conn.close()
        return redirect(url_for('settings'))

    conn.execute(
        "UPDATE users SET password=? WHERE id=?",
        (hash_password(new_pw), session['user_id'])
    )
    conn.commit()
    conn.close()
    flash('Password changed successfully.', 'success')
    return redirect(url_for('settings'))

@app.route('/settings/add-user', methods=['POST'])
@owner_required
def add_user():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    role = request.form.get('role', 'staff')

    if not username or not password:
        flash('Username and password required.', 'danger')
        return redirect(url_for('settings'))

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (username, password, role, online, session_token) VALUES (?,?,?,?,?)",
            (username, hash_password(password), role, 0, None)
        )
        conn.commit()
        flash(f'User "{username}" created.', 'success')
    except sqlite3.IntegrityError:
        flash(f'Username "{username}" already exists.', 'warning')
    conn.close()
    return redirect(url_for('settings'))

@app.route('/settings/remove-user/<int:uid>', methods=['POST'])
@owner_required
def remove_user(uid):
    if uid == session['user_id']:
        flash('Cannot delete your own account.', 'danger')
        return redirect(url_for('settings'))
    conn = get_db()
    conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    flash('User removed.', 'success')
    return redirect(url_for('settings'))

@app.route('/settings/force-logout/<int:uid>', methods=['POST'])
@owner_required
def force_logout(uid):
    conn = get_db()
    invalidate_session(conn, uid)
    conn.commit()
    conn.close()
    flash('User logged out.', 'success')
    return redirect(url_for('settings'))

@app.route('/settings/delete-contacts', methods=['POST'])
@owner_required
def delete_all_contacts():
    conn = get_db()
    conn.execute("DELETE FROM contacts")
    conn.commit()
    conn.close()
    flash('All contacts deleted.', 'success')
    return redirect(url_for('settings'))

@app.route('/settings/delete-campaigns', methods=['POST'])
@owner_required
def delete_all_campaigns():
    conn = get_db()
    conn.execute("DELETE FROM campaigns")
    conn.execute("DELETE FROM logs")
    conn.execute("DELETE FROM history")
    conn.execute("DELETE FROM campaign_recipients")
    conn.commit()
    conn.close()
    load_campaign_state()
    flash('All campaigns and history deleted.', 'success')
    return redirect(url_for('settings'))

@app.route('/settings/factory-reset', methods=['POST'])
@owner_required
def factory_reset():
    conn = get_db()
    conn.executescript("""
        DELETE FROM contacts;
        DELETE FROM campaigns;
        DELETE FROM campaign_recipients;
        DELETE FROM logs;
        DELETE FROM sessions;
        DELETE FROM history;
        DELETE FROM templates;
        DELETE FROM users WHERE role != 'owner';
    """)
    conn.execute("UPDATE users SET online=0, session_token=NULL")
    conn.commit()
    conn.close()
    load_campaign_state()
    flash('Factory reset complete. All data cleared (owner account kept).', 'warning')
    return redirect(url_for('settings'))

@app.route('/settings/backup')
@owner_required
def backup():
    """Export entire database as JSON."""
    conn = get_db()
    data = {
        'contacts': [dict(r) for r in conn.execute("SELECT * FROM contacts").fetchall()],
        'campaigns': [dict(r) for r in conn.execute("SELECT * FROM campaigns").fetchall()],
        'history': [dict(r) for r in conn.execute("SELECT * FROM history").fetchall()],
        'logs': [dict(r) for r in conn.execute("SELECT * FROM logs").fetchall()],
        'templates': [dict(r) for r in conn.execute("SELECT * FROM templates").fetchall()],
    }
    conn.close()
    output = make_response(json.dumps(data, indent=2))
    output.headers['Content-Disposition'] = f'attachment; filename=chatdesk_backup_{date.today()}.json'
    output.headers['Content-type'] = 'application/json'
    return output

# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
if __name__ == '__main__':
    os.makedirs('uploads', exist_ok=True)
    os.makedirs('exports', exist_ok=True)
    os.makedirs(PLAYWRIGHT_SESSION_DIR, exist_ok=True)
    init_db()
    restore_runtime_state()
    import socket
    hostname = socket.gethostname()
    try:
        local_ip = socket.gethostbyname(hostname)
    except Exception:
        local_ip = '127.0.0.1'
    print(f"""
╔══════════════════════════════════════╗
║         ChatDesk is running!         ║
╠══════════════════════════════════════╣
║  Local:   http://127.0.0.1:5000      ║
║  Network: http://{local_ip}:5000{'  ' if len(local_ip) < 13 else ''}║
╠══════════════════════════════════════╣
║  Default login:                      ║
║    Username: owner                   ║
║    Password: admin123                ║
╚══════════════════════════════════════╝
    """)
    app.run(host='0.0.0.0', port=5000, debug=False)
