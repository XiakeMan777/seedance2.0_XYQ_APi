#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XiaoYunque Web API server v2.1.

Features:
- SQLite persistence
- ThreadPoolExecutor concurrency control
- Thread-safe task state
- Progress tracking
- Multi-cookie management
- Automatic credit checking
"""

import asyncio
import os
import sys
import json
import sqlite3
import threading
import uuid
import time
import shutil
import argparse
from datetime import datetime, timedelta
from enum import Enum
from math import gcd
from concurrent.futures import ThreadPoolExecutor, Future
from flask import Flask, request, jsonify, send_file, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

sys.path.append(os.path.dirname(os.path.abspath(__file__)))


def configure_runtime_encoding():
    os.environ.setdefault('PYTHONUTF8', '1')
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')

    for stream_name in ('stdin', 'stdout', 'stderr'):
        stream = getattr(sys, stream_name, None)
        if stream and hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8', errors='replace')
            except ValueError:
                pass

    if os.name != 'nt':
        return

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleCP(65001)
        kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass


configure_runtime_encoding()

from xiaoyunque import (
    main_wrapper as xiaoyunque_main,
    load_cookies,
    get_cookies_files,
    MODEL_CREDITS_PER_SEC,
    VIDEO_TIMEOUT_ERROR_MESSAGE,
    normalize_cookie_payload,
)

app = Flask(__name__, static_folder='static', static_url_path='')
app.json.ensure_ascii = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or f'xiaoyunque-{uuid.uuid4().hex}'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')

    if response.mimetype in {'application/json', 'text/html', 'text/plain'}:
        content_type = response.content_type or ''
        if 'charset=' not in content_type.lower():
            response.headers['Content-Type'] = f'{response.mimetype}; charset=utf-8'

    return response

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
COOKIES_DIR = os.path.join(BASE_DIR, 'cookies')
DB_PATH = os.path.join(DATA_DIR, 'xiaoyunque_tasks.db')
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(COOKIES_DIR, exist_ok=True)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

AUTH_SESSION_KEY = 'admin_authenticated'
AUTH_USERNAME_SESSION_KEY = 'admin_username'
DEFAULT_ADMIN_API_KEY = os.environ.get('DEFAULT_ADMIN_API_KEY', 'xiaoyunque-api-key')
PUBLIC_PATHS = {
    '/favicon.ico',
    '/login',
    '/api/auth/login',
    '/api/auth/status',
    '/api/health',
}

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'bmp', 'gif'}
MAX_CONTENT_LENGTH = 50 * 1024 * 1024
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

MAX_WORKERS = 3
SUPPORTED_DURATIONS = {5, 10, 15}
SUPPORTED_RATIOS = {'16:9', '9:16'}
DEFAULT_VIDEO_SIZE_BY_RATIO = {
    '16:9': '1280x720',
    '9:16': '720x1280',
}
MODEL_ALIASES = {
    'fast': 'fast',
    'seedance-2.0-fast': 'fast',
    'viduq3-turbo': 'fast',
    '2.0': '2.0',
    'seedance-2.0': '2.0',
}
DEFAULT_API_MODEL = 'seedance-2.0-fast'
PROGRESS_UPDATE_INTERVAL = 10
PROGRESS_MAX_RUNTIME = 3600

PROGRESS_STAGES = [
    {'time': 0, 'progress': 5},
    {'time': 60, 'progress': 15},
    {'time': 180, 'progress': 30},
    {'time': 300, 'progress': 50},
    {'time': 600, 'progress': 70},
    {'time': 900, 'progress': 85},
    {'time': 1200, 'progress': 90},
]

class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class APIError(Exception):
    def __init__(self, message: str, status_code: int = 400, param: str = None,
                 code: str = None, error_type: str = 'invalid_request_error'):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.param = param
        self.code = code
        self.error_type = error_type


def parse_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def to_unix_timestamp(value):
    dt = parse_datetime(value)
    if not dt:
        return None
    return int(dt.timestamp())


def default_size_for_ratio(ratio: str) -> str:
    return DEFAULT_VIDEO_SIZE_BY_RATIO.get(ratio, DEFAULT_VIDEO_SIZE_BY_RATIO['16:9'])


def normalize_ratio(ratio_value: str) -> str:
    ratio = str(ratio_value or '16:9').strip()
    if ratio not in SUPPORTED_RATIOS:
        raise APIError('ratio must be 16:9 or 9:16', param='ratio')
    return ratio


def normalize_size(size_value: str):
    if size_value is None or str(size_value).strip() == '':
        ratio = '16:9'
        return default_size_for_ratio(ratio), ratio

    size_text = str(size_value).strip().lower()
    parts = size_text.split('x')
    if len(parts) != 2:
        raise APIError('size must be in WIDTHxHEIGHT format', param='size')

    try:
        width = int(parts[0])
        height = int(parts[1])
    except ValueError as exc:
        raise APIError('size must be in WIDTHxHEIGHT format', param='size') from exc

    if width <= 0 or height <= 0:
        raise APIError('size must be greater than zero', param='size')

    divisor = gcd(width, height)
    ratio_key = f'{width // divisor}:{height // divisor}'
    if ratio_key not in SUPPORTED_RATIOS:
        raise APIError('only 16:9 and 9:16 sizes are supported', param='size')

    return f'{width}x{height}', ratio_key


def normalize_duration(seconds_value, field_name: str = 'seconds') -> int:
    try:
        duration = int(seconds_value)
    except (TypeError, ValueError) as exc:
        raise APIError(f'{field_name} must be an integer', param=field_name) from exc

    if duration not in SUPPORTED_DURATIONS:
        allowed = ', '.join(str(item) for item in sorted(SUPPORTED_DURATIONS))
        raise APIError(f'{field_name} must be one of: {allowed}', param=field_name)

    return duration


def resolve_backend_model(model_name: str) -> str:
    normalized = str(model_name or DEFAULT_API_MODEL).strip()
    return MODEL_ALIASES.get(normalized, 'fast')


def map_task_status_to_openai(status: TaskStatus) -> str:
    return {
        TaskStatus.PENDING: 'queued',
        TaskStatus.RUNNING: 'in_progress',
        TaskStatus.SUCCESS: 'completed',
        TaskStatus.FAILED: 'failed',
    }[status]


def openai_error_response(message: str, status_code: int = 400, param: str = None,
                          code: str = None, error_type: str = 'invalid_request_error'):
    return jsonify({
        'error': {
            'message': message,
            'type': error_type,
            'param': param,
            'code': code,
        }
    }), status_code


def build_openai_task_error(task: 'Task'):
    if not task.error_message:
        return None

    if task.error_message == VIDEO_TIMEOUT_ERROR_MESSAGE:
        return {
            'message': task.error_message,
            'type': 'timeout_error',
            'code': 'video_generation_timeout',
        }

    return {
        'message': task.error_message,
        'type': 'video_generation_error',
        'code': 'video_generation_failed',
    }

class Task:
    def __init__(self, task_id: str, prompt: str, duration: int, ratio: str,
                 model: str, ref_images: list, output_dir: str, size: str = None,
                 quality: str = 'standard'):
        self.task_id = task_id
        self.prompt = prompt
        self.duration = duration
        self.ratio = ratio
        self.model = model
        self.size = size or default_size_for_ratio(ratio)
        self.quality = quality or 'standard'
        self.ref_images = ref_images
        self.output_dir = output_dir
        self.status = TaskStatus.PENDING
        self.progress = 0
        self.video_path = None
        self.error_message = None
        self.created_at = datetime.now()
        self.started_at = None
        self.completed_at = None
        self.lock = threading.Lock()

    def to_dict(self):
        with self.lock:
            result = {
                'task_id': self.task_id,
                'prompt': self.prompt,
                'duration': self.duration,
                'ratio': self.ratio,
                'model': self.model,
                'size': self.size,
                'quality': self.quality,
                'status': self.status.value,
                'progress': self.progress,
                'ref_images_count': len(self.ref_images),
                'ref_images': self.ref_images,
                'created_at': self.created_at.isoformat() if self.created_at else None,
                'started_at': self.started_at.isoformat() if self.started_at else None,
                'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            }
            if self.video_path:
                result['video_path'] = self.video_path
            if self.error_message:
                result['error_message'] = self.error_message
            return result


def get_db_connection(row_factory: bool = False):
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    if row_factory:
        conn.row_factory = sqlite3.Row
    return conn


def resolve_cookie_file(cookie_name: str):
    filename = str(cookie_name or '').strip()
    if not filename:
        raise ValueError('Cookie 名称不能为空')

    if not filename.endswith('.json'):
        filename = filename + '.json'

    normalized_name = os.path.basename(filename)
    if normalized_name in {'', '.', '..'} or normalized_name != filename:
        raise ValueError('无效的 Cookie 名称')

    return normalized_name, os.path.join(COOKIES_DIR, normalized_name)


def save_cookie_payload(cookie_name: str, raw_content):
    content = normalize_cookie_payload(raw_content)
    filename, save_path = resolve_cookie_file(cookie_name)

    with open(save_path, 'w', encoding='utf-8') as f:
        json.dump(content, f, ensure_ascii=False, indent=2)

    cookie_record_name = filename.replace('.json', '')
    now = datetime.now().isoformat()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT created_at FROM cookies WHERE name = ?", (cookie_record_name,))
    existing_row = cursor.fetchone()
    created_at = existing_row[0] if existing_row and existing_row[0] else now

    if existing_row:
        cursor.execute('''
            UPDATE cookies
            SET file_path = ?, credits = NULL, last_used = NULL, status = ?, created_at = ?
            WHERE name = ?
        ''', (save_path, 'unknown', created_at, cookie_record_name))
    else:
        cursor.execute('''
            INSERT INTO cookies (name, file_path, credits, last_used, status, created_at)
            VALUES (?, ?, NULL, NULL, ?, ?)
        ''', (cookie_record_name, save_path, 'unknown', created_at))

    conn.commit()
    conn.close()
    return filename, save_path


def ensure_default_admin(cursor):
    cursor.execute("SELECT COUNT(*) FROM admin_users")
    admin_count = cursor.fetchone()[0]
    if admin_count > 0:
        cursor.execute('''
            SELECT username
            FROM admin_users
            WHERE api_key IS NULL OR TRIM(api_key) = ''
            ORDER BY created_at ASC, username ASC
            LIMIT 1
        ''')
        row = cursor.fetchone()
        if row:
            now = datetime.now().isoformat()
            cursor.execute('''
                UPDATE admin_users
                SET api_key = ?, updated_at = ?
                WHERE username = ?
            ''', (DEFAULT_ADMIN_API_KEY, now, row[0]))
        return

    now = datetime.now().isoformat()
    cursor.execute('''
        INSERT INTO admin_users (username, password_hash, api_key, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
    ''', ('admin', generate_password_hash('admin'), DEFAULT_ADMIN_API_KEY, now, now))


def init_database():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            prompt TEXT NOT NULL,
            duration INTEGER NOT NULL,
            ratio TEXT NOT NULL,
            model TEXT NOT NULL,
            ref_images TEXT NOT NULL,
            output_dir TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            progress INTEGER DEFAULT 0,
            video_path TEXT,
            error_message TEXT,
            created_at TEXT,
            started_at TEXT,
            completed_at TEXT,
            size TEXT,
            quality TEXT DEFAULT 'standard'
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS task_ref_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            image_path TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(task_id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS cookies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            file_path TEXT NOT NULL,
            credits INTEGER DEFAULT 0,
            last_used TEXT,
            status TEXT DEFAULT 'active',
            created_at TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS admin_users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            api_key TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    ''')
    cursor.execute("PRAGMA table_info(admin_users)")
    admin_columns = {row[1] for row in cursor.fetchall()}
    if 'api_key' not in admin_columns:
        cursor.execute("ALTER TABLE admin_users ADD COLUMN api_key TEXT")
    cursor.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_admin_users_api_key
        ON admin_users(api_key)
    ''')
    cursor.execute("PRAGMA table_info(tasks)")
    existing_columns = {row[1] for row in cursor.fetchall()}
    if 'size' not in existing_columns:
        cursor.execute("ALTER TABLE tasks ADD COLUMN size TEXT")
    if 'quality' not in existing_columns:
        cursor.execute("ALTER TABLE tasks ADD COLUMN quality TEXT DEFAULT 'standard'")
    ensure_default_admin(cursor)
    conn.commit()
    conn.close()


def get_admin_user(username: str):
    if not username:
        return None

    conn = get_db_connection(row_factory=True)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT username, password_hash, api_key, created_at, updated_at
        FROM admin_users
        WHERE username = ?
    ''', (username,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_admin_by_api_key(api_key: str):
    if not api_key:
        return None

    conn = get_db_connection(row_factory=True)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT username, password_hash, api_key, created_at, updated_at
        FROM admin_users
        WHERE api_key = ?
    ''', (api_key,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def is_admin_authenticated() -> bool:
    return bool(session.get(AUTH_SESSION_KEY) and session.get(AUTH_USERNAME_SESSION_KEY))


def get_current_admin_username():
    if not is_admin_authenticated():
        return None
    return session.get(AUTH_USERNAME_SESSION_KEY)


def login_admin(username: str):
    session.permanent = True
    session[AUTH_SESSION_KEY] = True
    session[AUTH_USERNAME_SESSION_KEY] = username


def logout_admin():
    session.pop(AUTH_SESSION_KEY, None)
    session.pop(AUTH_USERNAME_SESSION_KEY, None)


def get_request_bearer_token():
    authorization = request.headers.get('Authorization', '')
    if not authorization:
        return None

    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != 'bearer':
        return None
    return parts[1].strip() or None


def update_admin_credentials(current_username: str, current_password: str,
                             new_username: str = None, new_password: str = None,
                             new_api_key: str = None):
    current_username = str(current_username or '').strip()
    new_username = str(new_username or '').strip()
    new_password = str(new_password or '')
    new_api_key = str(new_api_key or '').strip()
    current_password = str(current_password or '')

    if not current_username:
        raise APIError('当前登录用户无效', status_code=401, code='unauthorized')
    if not current_password:
        raise APIError('请输入当前密码', param='current_password')

    admin = get_admin_user(current_username)
    if not admin or not check_password_hash(admin['password_hash'], current_password):
        raise APIError('当前密码错误', status_code=403, param='current_password', code='invalid_password')

    target_username = new_username or current_username
    target_api_key = new_api_key or admin.get('api_key') or DEFAULT_ADMIN_API_KEY
    if len(target_username) < 3:
        raise APIError('用户名至少需要 3 个字符', param='new_username')
    if len(target_api_key) < 8:
        raise APIError('API Key 至少需要 8 个字符', param='new_api_key')
    if (not new_password and target_username == current_username
            and target_api_key == (admin.get('api_key') or DEFAULT_ADMIN_API_KEY)):
        raise APIError('请至少修改用户名、密码或 API Key 其中一项')
    if new_password and len(new_password) < 4:
        raise APIError('新密码至少需要 4 个字符', param='new_password')

    existing_user = get_admin_user(target_username)
    if existing_user and target_username != current_username:
        raise APIError('该用户名已存在', status_code=409, param='new_username', code='username_exists')
    existing_api_key_owner = get_admin_by_api_key(target_api_key)
    if existing_api_key_owner and existing_api_key_owner['username'] != current_username:
        raise APIError('该 API Key 已被使用', status_code=409, param='new_api_key', code='api_key_exists')

    conn = get_db_connection()
    cursor = conn.cursor()
    now = datetime.now().isoformat()
    cursor.execute('''
        UPDATE admin_users
        SET username = ?, password_hash = ?, api_key = ?, updated_at = ?
        WHERE username = ?
    ''', (
        target_username,
        generate_password_hash(new_password) if new_password else admin['password_hash'],
        target_api_key,
        now,
        current_username,
    ))
    conn.commit()
    conn.close()
    return {
        'username': target_username,
        'api_key': target_api_key,
    }


def build_openai_video_object(task: Task):
    with task.lock:
        result = {
            'id': task.task_id,
            'object': 'video',
            'created_at': to_unix_timestamp(task.created_at),
            'completed_at': to_unix_timestamp(task.completed_at),
            'status': map_task_status_to_openai(task.status),
            'model': task.model,
            'progress': task.progress,
            'prompt': task.prompt,
            'seconds': task.duration,
            'size': task.size or default_size_for_ratio(task.ratio),
            'quality': task.quality or 'standard',
        }

        if task.status == TaskStatus.SUCCESS:
            result['content_path'] = f'/v1/videos/{task.task_id}/content'
            if task.completed_at:
                result['expires_at'] = to_unix_timestamp(task.completed_at + timedelta(days=7))

        task_error = build_openai_task_error(task)
        if task.status == TaskStatus.FAILED and task_error:
            result['error'] = task_error

        return result

def calculate_progress(elapsed: float) -> int:
    for i in range(len(PROGRESS_STAGES) - 1):
        current = PROGRESS_STAGES[i]
        next_stage = PROGRESS_STAGES[i + 1]
        if elapsed < next_stage['time']:
            time_ratio = (elapsed - current['time']) / (next_stage['time'] - current['time'])
            progress = current['progress'] + (next_stage['progress'] - current['progress']) * time_ratio
            return int(progress)
    return PROGRESS_STAGES[-1]['progress']

class TaskManager:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.tasks: dict = {}
        self.executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        self._tasks_lock = threading.Lock()
        init_database()
        self._load_pending_tasks()

    def _get_task_ref_images(self, task_id: str):
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("SELECT image_path FROM task_ref_images WHERE task_id = ?", (task_id,))
        images = [row[0] for row in cursor.fetchall()]
        conn.close()
        return images

    def _task_from_row(self, row):
        size = row[14] if len(row) > 14 and row[14] else default_size_for_ratio(row[3])
        quality = row[15] if len(row) > 15 and row[15] else 'standard'
        task = Task(
            task_id=row[0],
            prompt=row[1],
            duration=row[2],
            ratio=row[3],
            model=row[4],
            ref_images=self._get_task_ref_images(row[0]),
            output_dir=row[6],
            size=size,
            quality=quality,
        )
        task.status = TaskStatus(row[7])
        task.progress = row[8] if row[8] else 0
        task.video_path = row[9]
        task.error_message = row[10]
        task.created_at = parse_datetime(row[11]) or task.created_at
        task.started_at = parse_datetime(row[12])
        task.completed_at = parse_datetime(row[13])
        return task

    def _load_pending_tasks(self):
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE status IN ('pending', 'running')")
        rows = cursor.fetchall()
        conn.close()

        for row in rows:
            task_id = row[0]
            task = self._task_from_row(row)
            task.status = TaskStatus.PENDING

            with self._tasks_lock:
                self.tasks[task_id] = task

            if row[7] == 'pending':
                self.executor.submit(self._execute_task, task_id)

        print(f"[*] Loaded {len(rows)} unfinished tasks from database")

    def _save_task_to_db(self, task: Task):
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO tasks
            (task_id, prompt, duration, ratio, model, ref_images, output_dir,
             status, progress, video_path, error_message, created_at, started_at, completed_at,
             size, quality)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            task.task_id, task.prompt, task.duration, task.ratio, task.model,
            json.dumps(task.ref_images), task.output_dir,
            task.status.value, task.progress, task.video_path, task.error_message,
            task.created_at.isoformat() if task.created_at else None,
            task.started_at.isoformat() if task.started_at else None,
            task.completed_at.isoformat() if task.completed_at else None,
            task.size,
            task.quality,
        ))
        conn.commit()
        conn.close()

    def _save_task_ref_images(self, task_id: str, ref_images: list):
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM task_ref_images WHERE task_id = ?", (task_id,))
        for img_path in ref_images:
            cursor.execute("INSERT INTO task_ref_images (task_id, image_path) VALUES (?, ?)",
                         (task_id, img_path))
        conn.commit()
        conn.close()

    def add_task(self, prompt: str, duration: int, ratio: str, model: str,
                 ref_images: list, output_dir: str, size: str = None,
                 quality: str = 'standard') -> str:
        task_id = str(uuid.uuid4())
        
        rel_images = []
        for img_path in ref_images:
            if os.path.isabs(img_path):
                rel_path = os.path.relpath(img_path, BASE_DIR)
            else:
                rel_path = img_path
            rel_images.append(rel_path)
        
        task = Task(task_id, prompt, duration, ratio, model, rel_images, output_dir, size=size, quality=quality)

        with self._tasks_lock:
            self.tasks[task_id] = task

        self._save_task_to_db(task)
        self._save_task_ref_images(task_id, rel_images)

        self.executor.submit(self._execute_task, task_id)
        print(f"[>] Task submitted: {task_id}")
        return task_id

    def get_task(self, task_id: str):
        with self._tasks_lock:
            if task_id in self.tasks:
                return self.tasks[task_id]

        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        row = cursor.fetchone()
        conn.close()

        if row:
            return self._task_from_row(row)
        return None

    def get_all_tasks(self, limit: int = 100, offset: int = 0, status: str = None):
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()

        if status:
            cursor.execute("SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                         (status, limit, offset))
        else:
            cursor.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ? OFFSET ?",
                         (limit, offset))

        rows = cursor.fetchall()

        result = []
        for row in rows:
            task_id = row[0]
            with self._tasks_lock:
                if task_id in self.tasks:
                    result.append(self.tasks[task_id].to_dict())
                    continue

            ref_images = self._get_task_ref_images(task_id)
            size = row[14] if len(row) > 14 and row[14] else default_size_for_ratio(row[3])
            quality = row[15] if len(row) > 15 and row[15] else 'standard'

            result.append({
                'task_id': row[0], 'prompt': row[1], 'duration': row[2],
                'ratio': row[3], 'model': row[4], 'size': size, 'quality': quality, 'status': row[7],
                'progress': row[8], 'video_path': row[9],
                'error_message': row[10], 'created_at': row[11],
                'started_at': row[12], 'completed_at': row[13],
                'ref_images': ref_images, 'ref_images_count': len(ref_images)
            })

        conn.close()
        return result

    def get_running_count(self) -> int:
        with self._tasks_lock:
            return sum(1 for t in self.tasks.values() if t.status == TaskStatus.RUNNING)

    def _execute_task(self, task_id: str):
        task = self.get_task(task_id)
        if not task:
            return

        try:
            with task.lock:
                task.status = TaskStatus.RUNNING
                task.started_at = datetime.now()
                task.progress = 5

            self._save_task_to_db(task)
            backend_model = resolve_backend_model(task.model)

            print(f"\n[*] Starting task: {task_id}")
            print(f"   Prompt: {task.prompt}")
            print(f"   Duration: {task.duration}s, Ratio: {task.ratio}, Model: {task.model} -> {backend_model}")

            progress_thread = threading.Thread(
                target=self._update_progress, args=(task,), daemon=True
            )
            progress_thread.start()

            abs_ref_images = []
            for img_path in task.ref_images:
                if os.path.isabs(img_path):
                    abs_ref_images.append(img_path)
                else:
                    abs_ref_images.append(os.path.join(BASE_DIR, img_path))

            args = argparse.Namespace(
                prompt=task.prompt,
                ref_images=abs_ref_images,
                duration=task.duration,
                ratio=task.ratio,
                model=backend_model,
                cookies=COOKIES_DIR,
                output=task.output_dir,
                dry_run=False,
                cookie_index=None
            )

            result = xiaoyunque_main(args)

            video_files = []
            for file in os.listdir(task.output_dir):
                if file.endswith('.mp4'):
                    video_files.append(os.path.join(task.output_dir, file))

            with task.lock:
                if result and isinstance(result, str) and result.endswith('.mp4'):
                    task.status = TaskStatus.SUCCESS
                    task.video_path = os.path.relpath(result, BASE_DIR)
                    task.progress = 100
                    task.completed_at = datetime.now()
                    print(f"[OK] Task completed: {task_id}")
                else:
                    task.status = TaskStatus.FAILED
                    task.error_message = result if isinstance(result, str) else "Generated video file not found"
                    task.completed_at = datetime.now()
                    print(f"[ERROR] Task failed: {task_id} - {task.error_message}")

            self._save_task_to_db(task)

        except Exception as e:
            with task.lock:
                task.status = TaskStatus.FAILED
                task.error_message = str(e)
                task.completed_at = datetime.now()

            self._save_task_to_db(task)
            import traceback
            traceback.print_exc()

    def _update_progress(self, task: Task):
        start_time = time.time()

        while True:
            time.sleep(PROGRESS_UPDATE_INTERVAL)

            elapsed = time.time() - start_time
            if elapsed > PROGRESS_MAX_RUNTIME:
                print(f"[WARN] Progress updater timed out: {task.task_id}")
                break

            with task.lock:
                if task.status != TaskStatus.RUNNING:
                    break

                new_progress = calculate_progress(elapsed)
                if new_progress != task.progress:
                    task.progress = new_progress
                    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
                    cursor = conn.cursor()
                    cursor.execute("UPDATE tasks SET progress = ? WHERE task_id = ?",
                                 (task.progress, task.task_id))
                    conn.commit()
                    conn.close()

    def retry_task(self, task_id: str) -> bool:
        task = self.get_task(task_id)
        if not task or task.status != TaskStatus.FAILED:
            return False

        with task.lock:
            task.status = TaskStatus.PENDING
            task.progress = 0
            task.error_message = None
            task.started_at = None
            task.completed_at = None

        self._save_task_to_db(task)
        self.executor.submit(self._execute_task, task_id)
        return True

    def delete_task(self, task_id: str) -> bool:
        with self._tasks_lock:
            if task_id in self.tasks:
                task = self.tasks[task_id]
                if task.status == TaskStatus.RUNNING:
                    return False
                del self.tasks[task_id]

        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM task_ref_images WHERE task_id = ?", (task_id,))
        cursor.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        conn.commit()
        conn.close()

        task = self.get_task(task_id)
        if task and os.path.exists(task.output_dir):
            shutil.rmtree(task.output_dir, ignore_errors=True)

        return True

    def clear_all_tasks(self) -> dict:
        running = self.get_running_count()
        if running > 0:
            return {'status': 'error', 'message': f'{running} 个任务正在运行'}

        with self._tasks_lock:
            self.tasks.clear()

        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM task_ref_images")
        cursor.execute("DELETE FROM tasks")
        conn.commit()
        cursor.execute("SELECT COUNT(*) FROM tasks")
        total = cursor.fetchone()[0]
        conn.close()

        for item in os.listdir(UPLOAD_FOLDER):
            path = os.path.join(UPLOAD_FOLDER, item)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif item.endswith('.mp4'):
                os.remove(path)

        return {'status': 'success', 'deleted': total}


task_manager = TaskManager()


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_request_payload():
    if request.is_json:
        return request.get_json(silent=True) or {}
    if request.form:
        return request.form
    return {}


def collect_uploaded_images(data):
    uploaded_files = []

    file_fields = ['files', 'input_reference', 'input_reference[]']
    file_index = 0
    for field_name in file_fields:
        if field_name not in request.files:
            continue

        for file in request.files.getlist(field_name):
            if file and allowed_file(file.filename):
                filename = f"{int(time.time())}_{file_index}_{secure_filename(file.filename)}"
                filepath = os.path.join(UPLOAD_FOLDER, filename)
                file.save(filepath)
                uploaded_files.append(filepath)
                file_index += 1

    image_sources = data.get('images')
    if image_sources is None and 'input_reference' in data:
        image_sources = data.get('input_reference')
    if image_sources is None and 'input_reference[]' in data:
        image_sources = data.get('input_reference[]')

    if isinstance(image_sources, str):
        image_sources = [image_sources]

    if request.is_json and image_sources:
        for i, img_data in enumerate(image_sources):
            if not isinstance(img_data, str) or not img_data.startswith('data:image'):
                continue
            try:
                import base64
                _, img_bytes_b64 = img_data.split(',', 1)
                img_bytes = base64.b64decode(img_bytes_b64)
                filename = f"image_{i}_{int(time.time())}.png"
                filepath = os.path.join(UPLOAD_FOLDER, filename)
                with open(filepath, 'wb') as f:
                    f.write(img_bytes)
                uploaded_files.append(filepath)
            except Exception as exc:
                print(f"[WARN] Failed to decode base64 image: {exc}")

    return uploaded_files


def create_task_from_request(openai_compat: bool = False):
    data = get_request_payload()
    prompt = str(data.get('prompt', '')).strip()
    if not prompt:
        raise APIError('prompt is required' if openai_compat else '提示词不能为空', param='prompt')

    if openai_compat:
        duration = normalize_duration(data.get('seconds', data.get('duration', 10)), field_name='seconds')
        if data.get('size'):
            size, ratio = normalize_size(data.get('size'))
        else:
            ratio = normalize_ratio(data.get('ratio', '16:9'))
            size = default_size_for_ratio(ratio)
        model = str(data.get('model') or DEFAULT_API_MODEL).strip()
    else:
        duration = normalize_duration(data.get('duration', 10), field_name='duration')
        ratio = normalize_ratio(data.get('ratio', '16:9'))
        size = default_size_for_ratio(ratio)
        model = str(data.get('model') or 'fast').strip()

    cookies_files = get_cookies_files()
    if not cookies_files:
        raise APIError('请先上传 Cookie 文件', code='cookies_missing')

    uploaded_files = collect_uploaded_images(data)
    if not uploaded_files:
        param_name = 'input_reference[]' if openai_compat else 'files'
        message = 'at least one input_reference image is required' if openai_compat else '至少需要一张参考图片'
        raise APIError(message, param=param_name)

    backend_model = resolve_backend_model(model)
    required_credits = MODEL_CREDITS_PER_SEC.get(backend_model, 5) * duration

    output_dir = os.path.join(UPLOAD_FOLDER, str(uuid.uuid4()))
    os.makedirs(output_dir, exist_ok=True)

    for i, img_path in enumerate(uploaded_files):
        shutil.copy(img_path, os.path.join(output_dir, f"ref_{i}_{os.path.basename(img_path)}"))

    final_images = [
        os.path.join(output_dir, f"ref_{i}_{os.path.basename(path)}")
        for i, path in enumerate(uploaded_files)
    ]

    task_id = task_manager.add_task(
        prompt=prompt,
        duration=duration,
        ratio=ratio,
        model=model,
        ref_images=final_images,
        output_dir=output_dir,
        size=size,
        quality=str(data.get('quality') or 'standard').strip() or 'standard',
    )

    task = task_manager.get_task(task_id)
    return task, required_credits


def get_task_video_file(task: Task):
    if task.status != TaskStatus.SUCCESS or not task.video_path:
        raise APIError('视频尚未生成完成', status_code=404, code='video_not_ready')

    video_path = task.video_path
    if not os.path.isabs(video_path):
        video_path = os.path.join(BASE_DIR, video_path)

    if not os.path.exists(video_path):
        raise APIError('视频文件不存在', status_code=404, code='video_not_found')

    return video_path


@app.before_request
def require_admin_login():
    if request.method == 'OPTIONS':
        return None

    path = request.path or '/'

    if path == '/login' and is_admin_authenticated():
        return redirect(url_for('index'))

    if path in PUBLIC_PATHS:
        return None

    if path.startswith('/v1/'):
        api_key = get_request_bearer_token()
        if api_key and get_admin_by_api_key(api_key):
            return None
        return openai_error_response(
            'Invalid or missing Bearer token',
            status_code=401,
            code='authentication_required',
            error_type='authentication_error',
        )

    if is_admin_authenticated():
        return None

    if path.startswith('/api/'):
        return jsonify({'status': 'error', 'message': '请先登录管理员账号'}), 401

    return redirect(url_for('login_page'))


@app.route('/login')
def login_page():
    return send_file(os.path.join(app.static_folder, 'login.html'))


@app.route('/favicon.ico')
def favicon():
    favicon_path = os.path.join(app.static_folder, 'favicon.ico')
    if os.path.exists(favicon_path):
        return send_file(favicon_path, mimetype='image/x-icon')
    return '', 204


@app.route('/api/auth/status', methods=['GET'])
def auth_status():
    username = get_current_admin_username()
    admin = get_admin_user(username) if username else None
    return jsonify({
        'status': 'success',
        'authenticated': is_admin_authenticated(),
        'username': username,
        'api_key': admin.get('api_key') if admin else None,
    })


@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    data = request.get_json(silent=True) or request.form or {}
    username = str(data.get('username', '')).strip()
    password = str(data.get('password', ''))

    if not username or not password:
        return jsonify({'status': 'error', 'message': '请输入账号和密码'}), 400

    admin = get_admin_user(username)
    if not admin or not check_password_hash(admin['password_hash'], password):
        return jsonify({'status': 'error', 'message': '账号或密码错误'}), 401

    login_admin(username)
    return jsonify({
        'status': 'success',
        'message': '登录成功',
        'username': username,
    })


@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    logout_admin()
    return jsonify({'status': 'success', 'message': '已退出登录'})


@app.route('/api/auth/change-credentials', methods=['POST'])
def change_admin_credentials():
    try:
        data = request.get_json(silent=True) or {}
        current_password = data.get('current_password')
        new_username = data.get('new_username')
        new_password = data.get('new_password')
        new_api_key = data.get('new_api_key')
        updated_credentials = update_admin_credentials(
            current_username=get_current_admin_username(),
            current_password=current_password,
            new_username=new_username,
            new_password=new_password,
            new_api_key=new_api_key,
        )
        login_admin(updated_credentials['username'])
        return jsonify({
            'status': 'success',
            'message': '管理员账号信息已更新',
            'username': updated_credentials['username'],
            'api_key': updated_credentials['api_key'],
        })
    except APIError as e:
        return jsonify({'status': 'error', 'message': e.message}), e.status_code
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/')
def index():
    return send_file(os.path.join(app.static_folder, 'index.html'))


@app.route('/api/health', methods=['GET'])
def health_check():
    cookies_files = get_cookies_files()
    return jsonify({
        'status': 'healthy',
        'service': 'xiaoyunque-v2.1',
        'version': '2.1.0',
        'max_workers': MAX_WORKERS,
        'running_tasks': task_manager.get_running_count(),
        'cookies_count': len(cookies_files),
    })


@app.route('/api/cookies', methods=['GET'])
def list_cookies():
    cookies_files = get_cookies_files()
    cookies_list = []
    for i, fname in enumerate(cookies_files):
        fpath = os.path.join(COOKIES_DIR, fname)
        cookies_list.append({
            'id': i + 1,
            'name': fname.replace('.json', ''),
            'filename': fname,
            'path': fpath,
            'size': os.path.getsize(fpath) if os.path.exists(fpath) else 0
        })

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT name, credits, last_used, status FROM cookies")
    rows = cursor.fetchall()
    conn.close()

    credits_map = {}
    for name, credits, last_used, status in rows:
        credits_map[name] = {'credits': credits, 'last_used': last_used, 'status': status}

    for cookie in cookies_list:
        name = cookie['name']
        if name in credits_map:
            cookie['credits'] = credits_map[name]['credits']
            cookie['last_used'] = credits_map[name]['last_used']
            cookie['status'] = credits_map[name]['status']
        else:
            cookie['credits'] = None
            cookie['last_used'] = None
            cookie['status'] = 'unknown'

    return jsonify({
        'status': 'success',
        'cookies': cookies_list,
        'count': len(cookies_list)
    })


@app.route('/api/cookies', methods=['POST'])
def upload_cookie():
    save_path = None
    try:
        json_body = request.get_json(silent=True) if request.is_json else None
        name = request.form.get('name', '').strip()
        if not name and json_body:
            name = str(json_body.get('name', '')).strip()
        content = None

        if 'file' in request.files:
            file = request.files['file']
            if not file or not file.filename:
                return jsonify({'status': 'error', 'message': '请上传 Cookie JSON 文件'}), 400
            if name:
                if not name.endswith('.json'):
                    name = name + '.json'
            else:
                name = file.filename
                if not name.endswith('.json'):
                    name = name + '.json'
            _, save_path = resolve_cookie_file(name)
            file.save(save_path)
            with open(save_path, 'r', encoding='utf-8') as f:
                content = normalize_cookie_payload(json.load(f))
        elif json_body and 'content' in json_body:
            content = normalize_cookie_payload(json_body['content'])
            if not name:
                name = 'cookie_' + str(int(time.time()))
            if not name.endswith('.json'):
                name = name + '.json'
            _, save_path = resolve_cookie_file(name)
        else:
            return jsonify({'status': 'error', 'message': '请上传文件或提供 JSON 内容'}), 400

        name, save_path = save_cookie_payload(name, content)

        return jsonify({
            'status': 'success',
            'message': f'Cookie {name} 上传成功',
            'filename': name
        })

    except ValueError as e:
        if save_path and os.path.exists(save_path):
            os.remove(save_path)
        return jsonify({'status': 'error', 'message': str(e)}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/cookies/<cookie_name>', methods=['GET'])
def get_cookie(cookie_name):
    try:
        filename, cookie_path = resolve_cookie_file(cookie_name)
        if not os.path.exists(cookie_path):
            return jsonify({'status': 'error', 'message': 'Cookie 文件不存在'}), 404

        with open(cookie_path, 'r', encoding='utf-8') as f:
            content = f.read()

        return jsonify({
            'status': 'success',
            'cookie': {
                'name': filename.replace('.json', ''),
                'filename': filename,
                'content': content
            }
        })
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/cookies/<cookie_name>', methods=['PUT'])
def update_cookie(cookie_name):
    try:
        filename, cookie_path = resolve_cookie_file(cookie_name)
        if not os.path.exists(cookie_path):
            return jsonify({'status': 'error', 'message': 'Cookie 文件不存在'}), 404

        json_body = request.get_json(silent=True) or {}
        if 'content' not in json_body:
            return jsonify({'status': 'error', 'message': '请提供 Cookie JSON 内容'}), 400

        filename, _ = save_cookie_payload(filename, json_body['content'])

        return jsonify({
            'status': 'success',
            'message': f'Cookie {filename} 已更新',
            'filename': filename
        })
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/cookies/<cookie_name>', methods=['DELETE'])
def delete_cookie(cookie_name):
    try:
        cookie_name, fpath = resolve_cookie_file(cookie_name)
        if os.path.exists(fpath):
            os.remove(fpath)

        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM cookies WHERE name = ?", (cookie_name.replace('.json', ''),))
        conn.commit()
        conn.close()

        return jsonify({'status': 'success', 'message': 'Cookie 已删除'})
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/cookies/<cookie_name>/test', methods=['POST'])
def test_cookie(cookie_name):
    try:
        cookie_name, cookie_path = resolve_cookie_file(cookie_name)
        if not os.path.exists(cookie_path):
            return jsonify({'status': 'error', 'message': 'Cookie 文件不存在'}), 404

        from xiaoyunque import load_cookies, get_credits_info

        async def check_credits_async():
            cookies = load_cookies(cookie_path)
            from playwright.async_api import async_playwright
            p = await async_playwright().start()
            b = await p.chromium.launch(headless=True, args=['--no-sandbox'])
            ctx = await b.new_context(viewport={'width': 1920, 'height': 1080})
            await ctx.add_cookies(cookies)
            page = await ctx.new_page()
            try:
                await asyncio.wait_for(
                    page.goto('https://xyq.jianying.com/home', wait_until='domcontentloaded'),
                    timeout=30
                )
            except asyncio.TimeoutError:
                pass
            await page.wait_for_timeout(5000)
            credits = await get_credits_info(page)
            await b.close()
            await p.stop()
            return credits

        credits = asyncio.run(check_credits_async())

        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE cookies SET credits = ?, last_used = ?, status = ?
            WHERE name = ?
        ''', (credits, datetime.now().isoformat(), 'active', cookie_name.replace('.json', '')))
        conn.commit()
        conn.close()

        return jsonify({
            'status': 'success',
            'cookie_name': cookie_name,
            'credits': credits,
            'message': f'积分查询成功: {credits}'
        })

    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/cookies/check-all', methods=['POST'])
def check_all_cookies():
    try:
        cookies_files = get_cookies_files()
        if not cookies_files:
            return jsonify({'status': 'success', 'results': [], 'message': '没有找到 Cookie 文件'})

        results = []

        for fname in cookies_files:
            cookie_path = os.path.join(COOKIES_DIR, fname)
            cookie_name = fname.replace('.json', '')

            try:
                from xiaoyunque import load_cookies, get_credits_info

                async def check_credits_async():
                    cookies = load_cookies(cookie_path)
                    from playwright.async_api import async_playwright
                    p = await async_playwright().start()
                    b = await p.chromium.launch(headless=True, args=['--no-sandbox'])
                    ctx = await b.new_context(viewport={'width': 1920, 'height': 1080})
                    await ctx.add_cookies(cookies)
                    page = await ctx.new_page()
                    try:
                        await asyncio.wait_for(
                            page.goto('https://xyq.jianying.com/home', wait_until='domcontentloaded'),
                            timeout=30
                        )
                    except asyncio.TimeoutError:
                        pass
                    await page.wait_for_timeout(5000)
                    credits = await get_credits_info(page)
                    await b.close()
                    await p.stop()
                    return credits

                credits = asyncio.run(check_credits_async())

                conn = sqlite3.connect(DB_PATH, check_same_thread=False)
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE cookies SET credits = ?, last_used = ?, status = ?
                    WHERE name = ?
                ''', (credits, datetime.now().isoformat(), 'active', cookie_name))
                conn.commit()
                conn.close()

                results.append({
                    'name': cookie_name,
                    'filename': fname,
                    'credits': credits,
                    'status': 'success'
                })
                print(f"[*] {cookie_name}: {credits} credits")

            except Exception as e:
                results.append({
                    'name': cookie_name,
                    'filename': fname,
                    'credits': None,
                    'status': 'failed',
                    'error': str(e)
                })
                print(f"[!] {cookie_name}: query failed - {e}")

        return jsonify({
            'status': 'success',
            'results': results,
            'message': f'查询完成，成功 {sum(1 for r in results if r["status"] == "success")}/{len(results)}'
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/generate-video', methods=['POST'])
def generate_video():
    try:
        task, required_credits = create_task_from_request(openai_compat=False)
        return jsonify({
            'status': 'success',
            'task_id': task.task_id,
            'message': f'视频生成任务已提交（预计需要 {required_credits} 积分）',
            'required_credits': required_credits,
            'running_tasks': task_manager.get_running_count()
        })
    except APIError as e:
        return jsonify({'status': 'error', 'message': e.message}), e.status_code
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': f'服务器内部错误: {str(e)}'}), 500


@app.route('/v1/videos', methods=['POST'])
def create_video_openai():
    try:
        task, _ = create_task_from_request(openai_compat=True)
        return jsonify(build_openai_video_object(task))
    except APIError as e:
        return openai_error_response(
            e.message,
            status_code=e.status_code,
            param=e.param,
            code=e.code,
            error_type=e.error_type,
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        return openai_error_response(
            f'Internal server error: {str(e)}',
            status_code=500,
            code='internal_server_error',
        )


@app.route('/v1/videos/<task_id>', methods=['GET'])
def get_video_openai(task_id):
    task = task_manager.get_task(task_id)
    if not task:
        return openai_error_response(
            f'No video found with id {task_id}',
            status_code=404,
            param='id',
            code='video_not_found',
        )
    return jsonify(build_openai_video_object(task))


@app.route('/v1/videos/<task_id>', methods=['DELETE'])
def delete_video_openai(task_id):
    task = task_manager.get_task(task_id)
    if not task:
        return openai_error_response(
            f'No video found with id {task_id}',
            status_code=404,
            param='id',
            code='video_not_found',
        )

    if not task_manager.delete_task(task_id):
        return openai_error_response(
            f'Video {task_id} is still running',
            status_code=409,
            param='id',
            code='video_in_progress',
        )

    return jsonify({
        'id': task_id,
        'object': 'video.deleted',
        'deleted': True,
    })


@app.route('/v1/videos/<task_id>/content', methods=['GET'])
def get_video_content_openai(task_id):
    task = task_manager.get_task(task_id)
    if not task:
        return openai_error_response(
            f'No video found with id {task_id}',
            status_code=404,
            param='id',
            code='video_not_found',
        )

    variant = request.args.get('variant')
    if variant and variant not in {'video', 'mp4'}:
        return openai_error_response(
            'Only the default video variant is supported',
            status_code=400,
            param='variant',
            code='unsupported_variant',
        )

    try:
        video_path = get_task_video_file(task)
    except APIError as e:
        return openai_error_response(
            e.message,
            status_code=e.status_code,
            param='id',
            code=e.code,
            error_type=e.error_type,
        )

    return send_file(
        video_path,
        mimetype='video/mp4',
        as_attachment=False,
        download_name=os.path.basename(video_path),
    )


@app.route('/api/task/<task_id>', methods=['GET'])
def get_task_status(task_id):
    task = task_manager.get_task(task_id)
    if not task:
        return jsonify({'status': 'error', 'message': '任务不存在'}), 404

    result = task.to_dict()
    if task.status == TaskStatus.SUCCESS:
        result['video_url'] = f'/api/video/{task_id}'
    return jsonify(result)


@app.route('/api/task/<task_id>', methods=['DELETE'])
def delete_task(task_id):
    if task_manager.delete_task(task_id):
        return jsonify({'status': 'success', 'message': '任务已删除'})
    return jsonify({'status': 'error', 'message': '无法删除任务，可能正在运行'}), 400


@app.route('/api/task/<task_id>/retry', methods=['POST'])
def retry_task(task_id):
    if task_manager.retry_task(task_id):
        return jsonify({'status': 'success', 'message': '任务已重新提交'})
    return jsonify({'status': 'error', 'message': '只能重试失败的任务'}), 400


@app.route('/api/tasks', methods=['GET'])
def list_tasks():
    limit = request.args.get('limit', 100, type=int)
    offset = request.args.get('offset', 0, type=int)
    status = request.args.get('status', None)

    tasks = task_manager.get_all_tasks(limit, offset, status)
    running = task_manager.get_running_count()

    return jsonify({
        'status': 'success',
        'tasks': tasks,
        'total': len(tasks),
        'running_count': running
    })


@app.route('/api/tasks/clear', methods=['POST'])
def clear_all_tasks():
    result = task_manager.clear_all_tasks()
    if result['status'] == 'error':
        return jsonify(result), 400
    return jsonify(result)


@app.route('/api/video/<task_id>', methods=['GET'])
def get_video(task_id):
    task = task_manager.get_task(task_id)
    if not task:
        return jsonify({'status': 'error', 'message': '任务不存在'}), 404

    try:
        video_path = get_task_video_file(task)
    except APIError as e:
        return jsonify({'status': 'error', 'message': e.message}), e.status_code

    return send_file(
        video_path,
        mimetype='video/mp4',
        as_attachment=True,
        download_name=os.path.basename(video_path),
    )
@app.route('/api/image/<path:image_path>', methods=['GET'])
def get_image(image_path):
    if '..' in image_path:
        return jsonify({'status': 'error', 'message': '无效的路径'}), 400
    
    full_path = os.path.normpath(os.path.join(BASE_DIR, image_path))
    if not os.path.exists(full_path):
        return jsonify({'status': 'error', 'message': '图片不存在'}), 404
    
    mime_type = 'image/png'
    ext = os.path.splitext(full_path)[1].lower()
    if ext in ['.jpg', '.jpeg']:
        mime_type = 'image/jpeg'
    elif ext == '.gif':
        mime_type = 'image/gif'
    elif ext == '.webp':
        mime_type = 'image/webp'
    elif ext == '.bmp':
        mime_type = 'image/bmp'
    
    return send_file(full_path, mimetype=mime_type)


@app.route('/api/stats', methods=['GET'])
def get_stats():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT status, COUNT(*) FROM tasks GROUP BY status
    ''')
    rows = cursor.fetchall()
    conn.close()

    stats = {'pending': 0, 'running': 0, 'success': 0, 'failed': 0}
    for status, count in rows:
        if status in stats:
            stats[status] = count

    cookies_files = get_cookies_files()

    return jsonify({
        'status': 'success',
        'stats': stats,
        'total': sum(stats.values()),
        'running': task_manager.get_running_count(),
        'cookies_count': len(cookies_files)
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8033))
    host = os.environ.get('HOST', '0.0.0.0')

    print("\n" + "="*60)
    print("XiaoYunque Web API Server v2.1")
    print("="*60)
    print(f"Database: {DB_PATH}")
    print(f"Upload directory: {UPLOAD_FOLDER}")
    print(f"Cookies directory: {COOKIES_DIR}")
    print(f"Max workers: {MAX_WORKERS}")
    print(f"Service URL: http://{host}:{port}")
    print("="*60 + "\n")

    app.run(host=host, port=port, debug=False, threaded=True)
