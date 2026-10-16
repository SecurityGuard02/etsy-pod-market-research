from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
import uuid

from cryptography.fernet import Fernet

SECRET_FIELDS = {
    'telegram_bot_token', 'openai_api_key', 'etsy_key', 'etsy_secret',
    'etsy_access_token', 'etsy_refresh_token', 'instagram_access_token',
    'pinterest_access_token', 'pinterest_refresh_token', 'pinterest_client_secret',
    'printify_token',
    'instagram_client_secret',
}
DEFAULTS = {
    'niche': 'Özgün minimal tipografi ve doğadan esinlenen yetişkin tişörtleri',
    'target_country': 'US', 'currency': 'USD', 'language': 'en',
    'time_zone': 'Europe/Istanbul', 'hour': 9, 'retail_price': 29.9,
    'budget_per_run': 3.0, 'image_generation_limit': 5,
    'scheduling_enabled': False, 'paused': False, 'dry_run': True,
    'openai_text_model': 'gpt-4.1-mini', 'openai_image_model': 'gpt-image-1',
    'openai_tts_model': 'gpt-4o-mini-tts', 'voice': 'onyx',
    'printify_variant_ids': [], 'production_partner_ids': [],
}


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'assets').mkdir(exist_ok=True)
        key_path = self.root / 'encryption.key'
        if not key_path.exists():
            fd = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(Fernet.generate_key())
        self.key = key_path.read_bytes()
        self.fernet = Fernet(self.key)
        self.db_path = self.root / 'jarvis.sqlite3'
        with self.db() as c:
            c.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS campaigns (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, stage TEXT NOT NULL,
                    status TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS actions (
                    token TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, label TEXT NOT NULL, status TEXT NOT NULL,
                    expires REAL NOT NULL, consumed REAL);
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY, mime TEXT NOT NULL, filename TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, message TEXT NOT NULL,
                    level TEXT NOT NULL, campaign_id TEXT, timestamp REAL NOT NULL);
            ''')
        os.chmod(self.db_path, 0o600)
        if os.getenv('APP_PASSWORD') and not self.get_meta('password'):
            self.set_password(os.environ['APP_PASSWORD'])

    def db(self):
        c = sqlite3.connect(self.db_path, timeout=20)
        c.row_factory = sqlite3.Row
        return c

    def get_meta(self, key, default=None):
        with self.db() as c:
            row = c.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else default

    def set_meta(self, key, value):
        with self.db() as c:
            c.execute('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                      (key, json.dumps(value, ensure_ascii=False)))

    def get_settings(self):
        encrypted = self.get_meta('settings')
        saved = json.loads(self.fernet.decrypt(encrypted.encode())) if encrypted else {}
        return {**DEFAULTS, **saved, 'public_url': (os.getenv('APP_PUBLIC_URL') or
                os.getenv('RENDER_EXTERNAL_URL') or saved.get('public_url') or 'http://localhost:8080').rstrip('/')}

    def update_settings(self, updates):
        current = self.get_settings()
        for key, value in updates.items():
            if key in SECRET_FIELDS and not value:
                continue
            current[key] = value
        encrypted = self.fernet.encrypt(json.dumps(current, ensure_ascii=False).encode()).decode()
        self.set_meta('settings', encrypted)

    def public_settings(self):
        settings = self.get_settings()
        public = {k: v for k, v in settings.items() if k not in SECRET_FIELDS}
        public.pop('etsy_token_expires_at', None)
        return public, {k: bool(settings.get(k)) for k in SECRET_FIELDS}

    def set_password(self, password):
        if len(password) < 12:
            raise ValueError('Yönetim parolası en az 12 karakter olmalı.')
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 600_000)
        self.set_meta('password', base64.b64encode(salt + digest).decode())

    def check_password(self, password):
        saved = self.get_meta('password')
        if not saved:
            return False
        raw = base64.b64decode(saved)
        calculated = hashlib.pbkdf2_hmac('sha256', password.encode(), raw[:16], 600_000)
        return hmac.compare_digest(raw[16:], calculated)

    def new_session(self):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.db() as c:
            c.execute('DELETE FROM sessions WHERE expires < ?', (time.time(),))
            c.execute('INSERT INTO sessions VALUES (?,?,?)',
                      (hashlib.sha256(token.encode()).hexdigest(), csrf, time.time() + 86400))
        return token, csrf

    def session(self, token):
        if not token:
            return None
        with self.db() as c:
            row = c.execute('SELECT * FROM sessions WHERE token=? AND expires>?',
                (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        return dict(row) if row else None

    def delete_session(self, token):
        with self.db() as c:
            c.execute('DELETE FROM sessions WHERE token=?', (hashlib.sha256(token.encode()).hexdigest(),))

    def create_campaign(self, title, data):
        campaign_id = uuid.uuid4().hex[:16]
        now = time.time()
        with self.db() as c:
            c.execute('INSERT INTO campaigns VALUES (?,?,?,?,?,?,?)',
                      (campaign_id, title, 'research', 'processing', json.dumps(data), now, now))
        return campaign_id

    def get_campaign(self, campaign_id):
        with self.db() as c:
            row = c.execute('SELECT * FROM campaigns WHERE id=?', (campaign_id,)).fetchone()
        if not row:
            raise ValueError('Kampanya bulunamadı.')
        result = dict(row)
        result['data'] = json.loads(result['data'])
        result['actions'] = self.list_actions(campaign_id)
        return result

    def list_campaigns(self):
        with self.db() as c:
            ids = [r['id'] for r in c.execute('SELECT id FROM campaigns ORDER BY created DESC LIMIT 30')]
        return [self.get_campaign(i) for i in ids]

    def update_campaign(self, campaign_id, **updates):
        allowed = {k: v for k, v in updates.items() if k in ('title', 'stage', 'status', 'data')}
        if 'data' in allowed:
            allowed['data'] = json.dumps(allowed['data'], ensure_ascii=False)
        allowed['updated'] = time.time()
        with self.db() as c:
            c.execute('UPDATE campaigns SET ' + ','.join(k + '=?' for k in allowed) + ' WHERE id=?',
                      (*allowed.values(), campaign_id))

    def create_action(self, campaign_id, kind, payload, label):
        token = secrets.token_hex(16)
        with self.db() as c:
            c.execute('INSERT INTO actions VALUES (?,?,?,?,?,?,?,NULL)',
                      (token, campaign_id, kind, json.dumps(payload), label, 'pending', time.time() + 48 * 3600))
        return token

    def list_actions(self, campaign_id):
        with self.db() as c:
            rows = c.execute("SELECT * FROM actions WHERE campaign_id=? AND status='pending' AND expires>?",
                             (campaign_id, time.time())).fetchall()
        return [{**dict(r), 'payload': json.loads(r['payload'])} for r in rows]

    def get_action(self, token):
        with self.db() as c:
            row = c.execute('SELECT * FROM actions WHERE token=?', (token,)).fetchone()
        return {**dict(row), 'payload': json.loads(row['payload'])} if row else None

    def consume_action(self, token, decision, actor_chat=None):
        if decision not in ('approve', 'reject'):
            raise ValueError('Geçersiz karar.')
        config = self.get_settings()
        if config.get('paused'):
            raise ValueError('Asistan duraklatılmış. Önce devam ettirin.')
        if actor_chat is not None and str(actor_chat) != str(config.get('telegram_chat_id')):
            raise ValueError('Bu sohbet onay vermeye yetkili değil.')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM actions WHERE token=?', (token,)).fetchone()
            if not row or row['status'] != 'pending' or row['expires'] < time.time():
                raise ValueError('Onay kullanılmış, iptal edilmiş veya süresi dolmuş.')
            c.execute('UPDATE actions SET status=?, consumed=? WHERE token=?', (decision, time.time(), token))
        return {**dict(row), 'payload': json.loads(row['payload']), 'decision': decision}

    def invalidate_actions(self, campaign_id=None):
        with self.db() as c:
            if campaign_id:
                c.execute("UPDATE actions SET status='cancelled' WHERE status='pending' AND campaign_id=?", (campaign_id,))
            else:
                c.execute("UPDATE actions SET status='cancelled' WHERE status='pending'")

    def add_event(self, message, level='info', campaign_id=None):
        # Redact credential values if an upstream exception unexpectedly embeds them.
        message = str(message)
        for key, value in self.get_settings().items():
            if key in SECRET_FIELDS and value and len(str(value)) > 5:
                message = message.replace(str(value), '[gizli]')
        with self.db() as c:
            c.execute('INSERT INTO events(message,level,campaign_id,timestamp) VALUES (?,?,?,?)',
                      (message[:3000], level, campaign_id, time.time()))

    def events(self):
        with self.db() as c:
            return [dict(r) for r in c.execute('SELECT * FROM events ORDER BY id DESC LIMIT 100')]

    def save_asset(self, content, mime, filename):
        asset_id = uuid.uuid4().hex
        (self.root / 'assets' / asset_id).write_bytes(content)
        with self.db() as c:
            c.execute('INSERT INTO assets VALUES (?,?,?,?)',
                      (asset_id, mime, Path(filename).name, time.time()))
        return asset_id

    def asset_path(self, asset_id):
        if len(asset_id) != 32 or any(c not in '0123456789abcdef' for c in asset_id):
            raise ValueError('Dosya bulunamadı.')
        path = self.root / 'assets' / asset_id
        if not path.is_file():
            raise ValueError('Dosya bulunamadı.')
        return path

    def read_asset(self, asset_id):
        path = self.asset_path(asset_id)
        with self.db() as c:
            row = c.execute('SELECT * FROM assets WHERE id=?', (asset_id,)).fetchone()
        if not row:
            raise ValueError('Dosya bulunamadı.')
        return path.read_bytes(), row['mime'], row['filename']

    def asset_url(self, asset_id, ttl=86400):
        self.asset_path(asset_id)
        expires = int(time.time()) + min(max(int(ttl), 60), 7 * 86400)
        sig = hmac.new(self.key, f'{asset_id}:{expires}'.encode(), hashlib.sha256).hexdigest()
        return f'{self.get_settings()["public_url"]}/media/{asset_id}?expires={expires}&signature={sig}'

    def verify_asset(self, asset_id, expires, signature):
        expected = hmac.new(self.key, f'{asset_id}:{expires}'.encode(), hashlib.sha256).hexdigest()
        return time.time() < expires < time.time() + 7 * 86400 + 60 and hmac.compare_digest(expected, signature)
