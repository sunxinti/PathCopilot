# -*- coding: utf-8 -*-
"""
PathCopilot - 用户认证模块
SQLite + Flask-Login + werkzeug密码哈希 + 邮箱验证
"""

import os
import ssl
import random
import smtplib
import time
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from functools import wraps
from pathlib import Path
from werkzeug.security import generate_password_hash, check_password_hash
from flask import session, redirect, url_for, request, jsonify
import logging
import json

logger = logging.getLogger(__name__)

DB_PATH = 'pathcopilot_users.db'

# ==================== SMTP 配置 ====================
# 仅通过环境变量配置。公开仓库不提供邮箱账号或授权码。
SMTP_HOST     = os.environ.get('SMTP_HOST', '')
SMTP_PORT     = int(os.environ.get('SMTP_PORT', '465'))
SMTP_USER     = os.environ.get('SMTP_USER', '')
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', '')
SMTP_USE_SSL  = os.environ.get('SMTP_USE_SSL', 'true').lower() == 'true'
SMTP_SENDER_NAME = os.environ.get('SMTP_SENDER_NAME', 'PathCopilot')

VERIFY_CODE_EXPIRE_MINUTES = 10
VERIFY_CODE_LENGTH = 6
VERIFY_MAX_ATTEMPTS = 5
VERIFY_LOCKOUT_MINUTES = 15


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """初始化数据库，创建用户表和设置表"""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_verified INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            last_login TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id TEXT PRIMARY KEY,
            llm_provider TEXT DEFAULT 'openrouter',
            api_url TEXT DEFAULT '',
            llm_model TEXT DEFAULT '',
            api_key TEXT DEFAULT '',
            updated_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS verification_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            email TEXT NOT NULL,
            code TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            attempts INTEGER DEFAULT 0,
            locked_until TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')

    # 兼容升级：为旧表添加新列
    for col, tbl in [("api_url TEXT DEFAULT ''",        'user_settings'),
                     ("is_verified INTEGER DEFAULT 0",  'users'),
                     ("attempts INTEGER DEFAULT 0",     'verification_codes'),
                     ("locked_until TEXT",              'verification_codes')]:
        try:
            cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN {col}")
            conn.commit()
        except Exception:
            pass

    # ⭐ 关键：已有老用户（有 last_login 记录 = 曾经登录过的）自动标记为已验证
    # 避免老用户被邮箱验证拦截
    try:
        upgraded = cursor.execute(
            "UPDATE users SET is_verified = 1 WHERE is_verified = 0 AND last_login IS NOT NULL"
        ).rowcount
        if upgraded:
            logger.info(f"已将 {upgraded} 个老用户自动标记为已验证（已有登录记录）")
        conn.commit()
    except Exception:
        pass

    # 清理超过 24 小时的过期验证码
    try:
        cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
        deleted = cursor.execute(
            "DELETE FROM verification_codes WHERE expires_at < ? AND used = 1", (cutoff,)
        ).rowcount
        if deleted:
            logger.info(f"已清理 {deleted} 条过期验证码")
        conn.commit()
    except Exception:
        pass

    conn.commit()
    conn.close()

    _ensure_usage_table()

    logger.info("数据库初始化完成")


class User:
    def __init__(self, row):
        self.id = row['id']
        self.username = row['username']
        self.email = row['email']
        self.password_hash = row['password_hash']
        self.is_verified = bool(row['is_verified']) if 'is_verified' in row.keys() else True
        self.created_at = row['created_at']
        self.last_login = row['last_login']

    @staticmethod
    def get_by_id(user_id):
        conn = get_db()
        row = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        conn.close()
        return User(row) if row else None

    @staticmethod
    def get_by_username(username):
        conn = get_db()
        row = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        conn.close()
        return User(row) if row else None

    @staticmethod
    def get_by_email(email):
        conn = get_db()
        row = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
        conn.close()
        return User(row) if row else None

    @staticmethod
    def create(username, email, password):
        user_id = str(uuid.uuid4())
        password_hash = generate_password_hash(password)
        now = datetime.now().isoformat()

        conn = get_db()
        try:
            conn.execute(
                'INSERT INTO users (id, username, email, password_hash, created_at) VALUES (?, ?, ?, ?, ?)',
                (user_id, username, email, password_hash, now)
            )
            conn.execute(
                'INSERT INTO user_settings (user_id, updated_at) VALUES (?, ?)',
                (user_id, now)
            )
            conn.commit()
            logger.info(f"新用户注册: {username} ({email})")
            return user_id
        except sqlite3.IntegrityError as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def verify_password(self, password):
        return check_password_hash(self.password_hash, password)

    def update_last_login(self):
        conn = get_db()
        now = datetime.now().isoformat()
        conn.execute('UPDATE users SET last_login = ? WHERE id = ?', (now, self.id))
        conn.commit()
        conn.close()

    def mark_verified(self):
        conn = get_db()
        conn.execute('UPDATE users SET is_verified = 1 WHERE id = ?', (self.id,))
        conn.commit()
        conn.close()
        self.is_verified = True


# ==================== 邮箱验证 ====================

def generate_verification_code(user_id, email):
    """生成6位数字验证码并存入数据库，同时废弃该用户之前未使用的验证码"""
    code = ''.join(random.choices('0123456789', k=VERIFY_CODE_LENGTH))
    now = datetime.now()
    expires = now + timedelta(minutes=VERIFY_CODE_EXPIRE_MINUTES)

    conn = get_db()
    conn.execute('UPDATE verification_codes SET used = 1 WHERE user_id = ? AND used = 0', (user_id,))
    conn.execute(
        'INSERT INTO verification_codes (user_id, email, code, created_at, expires_at) VALUES (?, ?, ?, ?, ?)',
        (user_id, email, code, now.isoformat(), expires.isoformat())
    )
    conn.commit()
    conn.close()
    return code


def verify_email_code(user_id, code):
    """校验验证码（含暴力破解防护），成功则将用户标记为已验证"""
    conn = get_db()

    # 1. 找到该用户最新的未使用验证码
    row = conn.execute(
        'SELECT * FROM verification_codes WHERE user_id = ? AND used = 0 ORDER BY created_at DESC LIMIT 1',
        (user_id,)
    ).fetchone()

    if not row:
        conn.close()
        return False, 'No pending verification code. Please request a new one.'

    # 2. 检查是否被锁定
    locked_until = row['locked_until']
    if locked_until and datetime.fromisoformat(locked_until) > datetime.now():
        remaining = int((datetime.fromisoformat(locked_until) - datetime.now()).total_seconds() / 60) + 1
        conn.close()
        return False, f'Too many failed attempts. Please try again in {remaining} minutes.'

    # 3. 检查是否过期
    if datetime.fromisoformat(row['expires_at']) < datetime.now():
        conn.close()
        return False, 'Verification code has expired. Please resend.'

    # 4. 校验验证码
    if row['code'] != code:
        attempts = (row['attempts'] or 0) + 1
        if attempts >= VERIFY_MAX_ATTEMPTS:
            lock_time = (datetime.now() + timedelta(minutes=VERIFY_LOCKOUT_MINUTES)).isoformat()
            conn.execute(
                'UPDATE verification_codes SET attempts = ?, locked_until = ? WHERE id = ?',
                (attempts, lock_time, row['id'])
            )
            conn.commit()
            conn.close()
            return False, f'Too many failed attempts. Account locked for {VERIFY_LOCKOUT_MINUTES} minutes.'
        conn.execute('UPDATE verification_codes SET attempts = ? WHERE id = ?', (attempts, row['id']))
        conn.commit()
        conn.close()
        remaining = VERIFY_MAX_ATTEMPTS - attempts
        return False, f'Invalid code. {remaining} attempt(s) remaining.'

    # 5. 验证成功
    conn.execute('UPDATE verification_codes SET used = 1 WHERE id = ?', (row['id'],))
    conn.execute('UPDATE users SET is_verified = 1 WHERE id = ?', (user_id,))
    conn.commit()
    conn.close()
    return True, 'Email verified successfully'


def _build_verification_email(to_email, code, username=''):
    """构建验证码邮件 MIMEMultipart 对象"""
    html_body = f'''
    <div style="font-family: 'Inter', Arial, sans-serif; max-width: 480px; margin: 0 auto; padding: 32px;">
        <div style="text-align: center; margin-bottom: 24px;">
            <h2 style="color: #1a56db; margin: 0;">PathCopilot</h2>
            <p style="color: #64748b; font-size: 13px;">Email Verification</p>
        </div>
        <div style="background: #f8fafc; border-radius: 12px; padding: 28px; text-align: center;">
            <p style="color: #475569; margin: 0 0 8px;">Hi <strong>{username or "there"}</strong>, your verification code is:</p>
            <div style="font-size: 36px; font-weight: 700; letter-spacing: 8px; color: #1a56db;
                        background: #fff; border: 2px solid #e2e8f0; border-radius: 10px;
                        padding: 16px; margin: 16px 0;">{code}</div>
            <p style="color: #94a3b8; font-size: 13px; margin: 0;">
                Valid for {VERIFY_CODE_EXPIRE_MINUTES} minutes. Do not share this code with anyone.
            </p>
        </div>
        <p style="color: #cbd5e1; font-size: 11px; text-align: center; margin-top: 24px;">
            If you did not request this, please ignore this email.
        </p>
    </div>'''

    msg = MIMEMultipart('alternative')
    msg['From'] = f'{SMTP_SENDER_NAME} <{SMTP_USER}>'
    msg['To'] = to_email
    msg['Subject'] = 'PathCopilot - Email Verification Code'
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))
    return msg


def _smtp_send(to_email, msg):
    """底层 SMTP 发送（阻塞），返回 (success, error_message)"""
    try:
        if SMTP_USE_SSL:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=15) as server:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_USER, to_email, msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_USER, to_email, msg.as_string())
        logger.info(f"验证邮件已发送至 {to_email}")
        return True, ''
    except smtplib.SMTPAuthenticationError:
        logger.error(f"SMTP 认证失败: {SMTP_HOST}")
        return False, 'Email service authentication failed. Please check SMTP configuration.'
    except Exception as e:
        logger.error(f"发送验证邮件失败: {e}")
        return False, f'Failed to send email: {str(e)}'


def send_verification_email(to_email, code, username=''):
    """异步发送验证码邮件（不阻塞请求），返回 (True, '') 表示已提交发送"""
    if not SMTP_USER or not SMTP_PASSWORD:
        logger.warning("SMTP 未配置 (SMTP_USER / SMTP_PASSWORD)，跳过邮件发送")
        return False, 'Email service not configured. Please contact the administrator.'

    msg = _build_verification_email(to_email, code, username)
    thread = threading.Thread(target=_smtp_send, args=(to_email, msg), daemon=True)
    thread.start()
    return True, ''


def send_verification_email_sync(to_email, code, username=''):
    """同步发送验证码邮件（阻塞等待结果），用于 resend 等需要即时反馈的场景"""
    if not SMTP_USER or not SMTP_PASSWORD:
        logger.warning("SMTP 未配置 (SMTP_USER / SMTP_PASSWORD)，跳过邮件发送")
        return False, 'Email service not configured. Please contact the administrator.'

    msg = _build_verification_email(to_email, code, username)
    return _smtp_send(to_email, msg)


def can_resend_code(user_id, cooldown_seconds=60):
    """检查是否可以重新发送（防刷：60秒冷却）"""
    conn = get_db()
    row = conn.execute(
        'SELECT created_at FROM verification_codes WHERE user_id = ? ORDER BY created_at DESC LIMIT 1',
        (user_id,)
    ).fetchone()
    conn.close()
    if not row:
        return True
    last_sent = datetime.fromisoformat(row['created_at'])
    return (datetime.now() - last_sent).total_seconds() >= cooldown_seconds


def get_user_settings(user_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM user_settings WHERE user_id = ?', (user_id,)).fetchone()
    conn.close()
    if row:
        return {
            'llm_provider': row['llm_provider'],
            'api_url':      row['api_url'] if 'api_url' in row.keys() else '',
            'llm_model':    row['llm_model'],
            'api_key':      row['api_key'],
        }
    return {'llm_provider': 'openrouter', 'api_url': '', 'llm_model': '', 'api_key': ''}


def save_user_settings(user_id, settings):
    conn = get_db()
    now = datetime.now().isoformat()
    conn.execute('''
        INSERT INTO user_settings (user_id, llm_provider, api_url, llm_model, api_key, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            llm_provider = excluded.llm_provider,
            api_url      = excluded.api_url,
            llm_model    = excluded.llm_model,
            api_key      = excluded.api_key,
            updated_at   = excluded.updated_at
    ''', (user_id,
          settings.get('llm_provider', 'openrouter'),
          settings.get('api_url', ''),
          settings.get('llm_model', ''),
          settings.get('api_key', ''),
          now))
    conn.commit()
    conn.close()


def get_current_user():
    """从session中获取当前登录用户"""
    user_id = session.get('user_id')
    if user_id:
        return User.get_by_id(user_id)
    return None


def login_required(f):
    """要求登录的装饰器（本地桌面模式自动跳过）"""
    @wraps(f)
    def decorated(*args, **kwargs):
        # 本地桌面模式：自动创建/使用本地用户，无需登录
        if os.environ.get('PATHCOPILOT_LOCAL_MODE') == '1':
            if not session.get('user_id'):
                _ensure_local_user()
            return f(*args, **kwargs)
        if not session.get('user_id'):
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'success': False, 'error': 'Authentication required'}), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated


def _ensure_local_user():
    """确保本地桌面模式有一个默认用户"""
    LOCAL_USER_ID = 'local-desktop-user'
    conn = get_db()
    row = conn.execute('SELECT * FROM users WHERE id = ?', (LOCAL_USER_ID,)).fetchone()
    if not row:
        now = datetime.now().isoformat()
        conn.execute(
            'INSERT OR IGNORE INTO users (id, username, email, password_hash, is_verified, created_at) VALUES (?, ?, ?, ?, 1, ?)',
            (LOCAL_USER_ID, 'LocalUser', 'local@desktop', 'not-applicable', now)
        )
        conn.execute(
            'INSERT OR IGNORE INTO user_settings (user_id, updated_at) VALUES (?, ?)',
            (LOCAL_USER_ID, now)
        )
        conn.commit()
    conn.close()
    session['user_id'] = LOCAL_USER_ID


def get_user_upload_folder(user_id):
    """获取用户专属的上传文件夹"""
    folder = os.path.join('uploads', user_id)
    os.makedirs(folder, exist_ok=True)
    return folder


def get_user_files_json_path(user_id):
    """获取用户专属的文件索引路径"""
    folder = get_user_upload_folder(user_id)
    return os.path.join(folder, 'uploaded_files.json')


def load_user_files(user_id):
    """加载用户的文件列表"""
    path = get_user_files_json_path(user_id)
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_user_files(user_id, files_dict):
    """保存用户的文件列表"""
    path = get_user_files_json_path(user_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(files_dict, f, ensure_ascii=False, indent=2)


# ==================== 使用配额系统 ====================
# 配额总开关：True 启用每用户每日配额；False 不限制（本地/开源版本默认关闭）
QUOTA_ENABLED = False

DAILY_WSI_LIMIT  = 5    # 每用户每天 WSI 分析次数
DAILY_CHAT_LIMIT = 50   # 每用户每天聊天问答次数


def _ensure_usage_table():
    """确保 usage_log 表存在"""
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_usage_user_action_date
        ON usage_log (user_id, action, created_at)
    ''')
    conn.commit()
    conn.close()


def record_usage(user_id, action, detail=''):
    """记录一次使用（无论是否启用配额都记录，方便统计）"""
    conn = get_db()
    conn.execute(
        'INSERT INTO usage_log (user_id, action, detail, created_at) VALUES (?, ?, ?, ?)',
        (user_id, action, detail, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def get_today_usage_count(user_id, action):
    """获取用户今天某项操作的使用次数"""
    today_start = datetime.now().strftime('%Y-%m-%dT00:00:00')
    conn = get_db()
    row = conn.execute(
        'SELECT COUNT(*) as cnt FROM usage_log WHERE user_id = ? AND action = ? AND created_at >= ?',
        (user_id, action, today_start)
    ).fetchone()
    conn.close()
    return row['cnt'] if row else 0


def user_has_own_api_key(user_id):
    """检查用户是否已配置自己的 API Key"""
    settings = get_user_settings(user_id)
    api_key = (settings.get('api_key') or '').strip()
    if not api_key or api_key in ('[]', '[""]'):
        return False
    try:
        keys = json.loads(api_key)
        if isinstance(keys, list):
            return any(k.strip() for k in keys)
    except (json.JSONDecodeError, TypeError):
        pass
    return bool(api_key)


def check_quota(user_id, action):
    """
    检查用户是否还有配额。
    返回 (allowed: bool, remaining: int, limit: int)
    配置了自己 API Key 的用户不受限制。
    """
    if not QUOTA_ENABLED:
        return True, -1, -1

    if user_has_own_api_key(user_id):
        return True, -1, -1

    if action == 'wsi_analysis':
        limit = DAILY_WSI_LIMIT
    elif action == 'chat':
        limit = DAILY_CHAT_LIMIT
    else:
        return True, -1, -1

    used = get_today_usage_count(user_id, action)
    remaining = max(0, limit - used)
    return remaining > 0, remaining, limit


def get_user_usage_summary(user_id):
    """获取用户今日使用概览（供前端展示）"""
    wsi_used = get_today_usage_count(user_id, 'wsi_analysis')
    chat_used = get_today_usage_count(user_id, 'chat')
    return {
        'quota_enabled': QUOTA_ENABLED,
        'wsi': {
            'used': wsi_used,
            'limit': DAILY_WSI_LIMIT if QUOTA_ENABLED else None,
            'remaining': max(0, DAILY_WSI_LIMIT - wsi_used) if QUOTA_ENABLED else None,
        },
        'chat': {
            'used': chat_used,
            'limit': DAILY_CHAT_LIMIT if QUOTA_ENABLED else None,
            'remaining': max(0, DAILY_CHAT_LIMIT - chat_used) if QUOTA_ENABLED else None,
        }
    }


