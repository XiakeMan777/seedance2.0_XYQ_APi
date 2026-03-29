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
from flask import Flask, request, jsonify, send_file
from werkzeug.utils import secure_filename

sys.path.append(os.path.dirname(os.path.abspath(__file__)))


def configure_runtime_encoding():
    if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

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
    normalize_cookie_payload,
)

app = Flask(__name__, static_folder='static', static_url_path='')
app.json.ensure_ascii = False

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
DEBUG_MODE = True
SAMPLE_VIDEO_PATH = os.path.join(DATA_DIR, 'debug_sample.mp4')
SAMPLE_VIDEO_BYTES = bytes([
    0, 0, 0, 24, 102, 116, 121, 112,
    109, 112, 52, 50, 0, 0, 0, 0,
    109, 112, 52, 50, 105, 115, 111, 109,
    0, 0, 0, 8, 102, 114, 101, 101,
])
DEBUG_MODE_LOCK = threading.Lock()

def set_debug_mode(enabled: bool):
    global DEBUG_MODE
    with DEBUG_MODE_LOCK:
        DEBUG_MODE = enabled
    return DEBUG_MODE

def get_debug_mode() -> bool:
    with DEBUG_MODE_LOCK:
        return DEBUG_MODE


def ensure_debug_sample_video() -> str:
    if not os.path.exists(SAMPLE_VIDEO_PATH):
        os.makedirs(os.path.dirname(SAMPLE_VIDEO_PATH), exist_ok=True)
        with open(SAMPLE_VIDEO_PATH, 'wb') as f:
            f.write(SAMPLE_VIDEO_BYTES)
    return SAMPLE_VIDEO_PATH

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


def init_database():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
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
    cursor.execute("PRAGMA table_info(tasks)")
    existing_columns = {row[1] for row in cursor.fetchall()}
    if 'size' not in existing_columns:
        cursor.execute("ALTER TABLE tasks ADD COLUMN size TEXT")
    if 'quality' not in existing_columns:
        cursor.execute("ALTER TABLE tasks ADD COLUMN quality TEXT DEFAULT 'standard'")
    conn.commit()
    conn.close()


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

        if task.status == TaskStatus.FAILED and task.error_message:
            result['error'] = {
                'message': task.error_message,
                'type': 'video_generation_error',
                'code': 'video_generation_failed',
            }

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

            if DEBUG_MODE:
                sample_video_path = ensure_debug_sample_video()
                print(f"[DEBUG] Debug mode enabled, returning local sample video: {sample_video_path}")
                time.sleep(5)
                
                with task.lock:
                    task.status = TaskStatus.SUCCESS
                    task.video_path = os.path.relpath(sample_video_path, BASE_DIR)
                    task.progress = 100
                    task.completed_at = datetime.now()
                    print(f"[OK] Task completed (debug mode): {task_id}")
                
                self._save_task_to_db(task)
                return

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
        'debug_mode': get_debug_mode()
    })


@app.route('/api/debug-mode', methods=['GET'])
def get_debug_mode_api():
    return jsonify({
        'status': 'success',
        'debug_mode': get_debug_mode()
    })


@app.route('/api/debug-mode', methods=['POST'])
def set_debug_mode_api():
    try:
        data = request.json or {}
        enabled = data.get('enabled')
        if enabled is None:
            return jsonify({'status': 'error', 'message': '缺少 enabled 参数'}), 400
        new_mode = set_debug_mode(bool(enabled))
        return jsonify({
            'status': 'success',
            'debug_mode': new_mode,
            'message': f'调试模式已{"开启" if new_mode else "关闭"}'
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


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
            save_path = os.path.join(COOKIES_DIR, name)
            file.save(save_path)
            with open(save_path, 'r', encoding='utf-8') as f:
                content = normalize_cookie_payload(json.load(f))
        elif json_body and 'content' in json_body:
            content = normalize_cookie_payload(json_body['content'])
            if not name:
                name = 'cookie_' + str(int(time.time()))
            if not name.endswith('.json'):
                name = name + '.json'
            save_path = os.path.join(COOKIES_DIR, name)
        else:
            return jsonify({'status': 'error', 'message': '请上传文件或提供 JSON 内容'}), 400

        with open(save_path, 'w', encoding='utf-8') as f:
            json.dump(content, f, ensure_ascii=False, indent=2)


        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO cookies (name, file_path, created_at)
            VALUES (?, ?, ?)
        ''', (name.replace('.json', ''), save_path, datetime.now().isoformat()))
        conn.commit()
        conn.close()

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


@app.route('/api/cookies/<cookie_name>', methods=['DELETE'])
def delete_cookie(cookie_name):
    try:
        if not cookie_name.endswith('.json'):
            cookie_name = cookie_name + '.json'

        fpath = os.path.join(COOKIES_DIR, cookie_name)
        if os.path.exists(fpath):
            os.remove(fpath)

        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM cookies WHERE name = ?", (cookie_name.replace('.json', ''),))
        conn.commit()
        conn.close()

        return jsonify({'status': 'success', 'message': 'Cookie 已删除'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/cookies/<cookie_name>/test', methods=['POST'])
def test_cookie(cookie_name):
    try:
        if not cookie_name.endswith('.json'):
            cookie_name = cookie_name + '.json'

        cookie_path = os.path.join(COOKIES_DIR, cookie_name)
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
