#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小云雀 (XiaoYunque) Web API 服务器 v3.0
优化版本：
- OpenAI 兼容 API 格式
- asyncio + aiohttp 异步架构
- 异步任务队列（提交/查询分离）
- 浏览器会话复用（单例 + 多 Context + 空闲超时）
- 指数退避轮询
- 结构化错误码
- 多层配置系统
- 多账号 Token 管理
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
import random
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, List, Any
from pathlib import Path
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures
from flask import Flask, request, jsonify, send_file
from werkzeug.utils import secure_filename

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from xiaoyunque_v3 import (
    run as xiaoyunque_run,
    browser_session,
    get_token_files,
    load_cookies,
    parse_tokens,
    config,
    APIException,
    ErrorCode,
    MODEL_CREDITS_PER_SEC,
    log
)

app = Flask(__name__, static_folder='static', static_url_path='')

# ==================== 配置 ====================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
COOKIES_DIR = os.environ.get('COOKIES_DIR', os.path.join(BASE_DIR, 'cookies'))
DB_PATH = os.path.join(DATA_DIR, 'xiaoyunque_tasks.db')
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
ASYNC_TASKS_DIR = os.path.join(DATA_DIR, 'async-tasks')

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(COOKIES_DIR, exist_ok=True)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(ASYNC_TASKS_DIR, exist_ok=True)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'bmp', 'gif'}
MAX_CONTENT_LENGTH = int(os.environ.get('MAX_CONTENT_LENGTH', 50 * 1024 * 1024))
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

MAX_WORKERS = int(os.environ.get('MAX_WORKERS', 1))
TASK_TIMEOUT = int(os.environ.get('TASK_TIMEOUT', 1200))  # 单任务最大执行时间（秒），默认 20 分钟
DEBUG_MODE = os.environ.get('DEBUG_MODE', 'false').lower() == 'true'
DEBUG_MODE_LOCK = threading.Lock()

# ==================== 运行时设置 ====================
# 允许通过 API 运行时修改的超时设置（线程安全）
_runtime_settings = {
    'task_timeout': TASK_TIMEOUT,  # 单任务最大执行时间（秒）
}
_runtime_settings_lock = threading.Lock()

def get_task_timeout() -> int:
    """获取当前任务超时时间（秒）"""
    with _runtime_settings_lock:
        return _runtime_settings['task_timeout']

def set_task_timeout(timeout: int) -> int:
    """设置任务超时时间（秒），返回设置后的值"""
    # 限制范围：60秒 ~ 7200秒（2小时）
    timeout = max(60, min(7200, int(timeout)))
    with _runtime_settings_lock:
        _runtime_settings['task_timeout'] = timeout
    return timeout

# ==================== 全局持久化 Event Loop ====================
# Playwright 的 Browser/Context/Page 对象绑定到创建它们的 event loop，
# 关闭 loop 后会导致回调失败 (RuntimeError: Event loop is closed)。
# 因此所有 Playwright 操作必须在同一个持久化的 event loop 中执行。

_pw_loop: asyncio.AbstractEventLoop = None
_pw_loop_thread: threading.Thread = None
_pw_loop_ready = threading.Event()

def _start_pw_event_loop():
    """在后台线程中运行一个永不关闭的 event loop，专门用于 Playwright 操作"""
    global _pw_loop
    _pw_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_pw_loop)
    _pw_loop_ready.set()  # 通知主线程 loop 已就绪
    _pw_loop.run_forever()

def get_pw_loop() -> asyncio.AbstractEventLoop:
    """获取全局 Playwright event loop（懒启动）"""
    global _pw_loop_thread
    if _pw_loop is None or _pw_loop.is_closed():
        _pw_loop_thread = threading.Thread(target=_start_pw_event_loop, daemon=True)
        _pw_loop_thread.start()
        _pw_loop_ready.wait(timeout=30)
    return _pw_loop

def run_async(coro, timeout: float = None):
    """在全局 Playwright event loop 上运行协程，支持超时。
    
    与 asyncio.run_coroutine_threadsafe 不同，此函数支持 timeout。
    超时后会取消协程并抛出 TimeoutError。
    """
    loop = get_pw_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise TimeoutError(f'协程执行超时 ({timeout}s)')

def set_debug_mode(enabled: bool):
    global DEBUG_MODE
    with DEBUG_MODE_LOCK:
        DEBUG_MODE = enabled
    return DEBUG_MODE

def get_debug_mode() -> bool:
    with DEBUG_MODE_LOCK:
        return DEBUG_MODE

# ==================== CORS ====================

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

# ==================== 错误处理 ====================

class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"

@dataclass
class Task:
    task_id: str
    prompt: str
    duration: int
    ratio: str
    model: str
    ref_images: list
    output_dir: str
    status: TaskStatus = TaskStatus.PENDING
    progress: int = 0
    video_path: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    history_id: Optional[str] = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def to_dict(self):
        with self.lock:
            result = {
                'task_id': self.task_id,
                'prompt': self.prompt,
                'duration': self.duration,
                'ratio': self.ratio,
                'model': self.model,
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

    def to_openai_dict(self):
        """OpenAI 兼容格式"""
        d = {
            'created': int(self.created_at.timestamp()) if self.created_at else int(time.time()),
            'task_id': self.task_id,
            'status': self.status.value,
        }
        if self.status == TaskStatus.SUCCESS:
            d['data'] = [{'url': f'/api/video/{self.task_id}', 'revised_prompt': self.prompt}]
        elif self.status == TaskStatus.FAILED:
            d['error'] = self.error_message or '未知错误'
        return d

# ==================== 数据库 ====================

def get_db_connection():
    """获取数据库连接（启用 WAL 模式防止多进程写锁死）"""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    return conn

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
            history_id TEXT,
            created_at TEXT,
            started_at TEXT,
            completed_at TEXT
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
    conn.commit()
    conn.close()

def save_task_to_db(task: Task):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO tasks
        (task_id, prompt, duration, ratio, model, ref_images, output_dir,
         status, progress, video_path, error_message, history_id, created_at, started_at, completed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        task.task_id, task.prompt, task.duration, task.ratio, task.model,
        json.dumps(task.ref_images), task.output_dir,
        task.status.value, task.progress, task.video_path, task.error_message,
        task.history_id,
        task.created_at.isoformat() if task.created_at else None,
        task.started_at.isoformat() if task.started_at else None,
        task.completed_at.isoformat() if task.completed_at else None
    ))
    conn.commit()
    conn.close()

def save_task_ref_images(task_id: str, ref_images: list):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM task_ref_images WHERE task_id = ?", (task_id,))
    for img_path in ref_images:
        cursor.execute("INSERT INTO task_ref_images (task_id, image_path) VALUES (?, ?)",
                     (task_id, img_path))
    conn.commit()
    conn.close()

# ==================== 异步任务管理器 ====================

class AsyncTaskManager:
    """异步任务管理器：内存 + 文件持久化 + 事件等待"""
    
    def __init__(self):
        self.tasks: Dict[str, Task] = {}
        self._tasks_lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        self._events: Dict[str, threading.Event] = {}
        init_database()
        # v4: 取消自动恢复，防止重启后重复提交
        # 启动时将所有孤立的 running/pending 任务标记为 failed（重启后不可能还在执行）
        self._mark_orphan_tasks_failed()
    
    def _mark_orphan_tasks_failed(self):
        """启动时将所有 running/pending 的孤立任务标记为 failed"""
        conn = get_db_connection()
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.execute(
            "UPDATE tasks SET status = 'failed', error_message = '服务重启，任务中断', completed_at = ? WHERE status IN ('pending', 'running')",
            (now,)
        )
        updated = cursor.rowcount
        conn.commit()
        conn.close()
        if updated > 0:
            log(f"[*] 已将 {updated} 个孤立任务标记为 failed")

    def _load_pending_tasks(self):
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE status IN ('pending', 'running')")
        rows = cursor.fetchall()
        conn.close()

        for row in rows:
            task_id = row['task_id']
            task = Task(
                task_id=row['task_id'], prompt=row['prompt'], duration=row['duration'], ratio=row['ratio'],
                model=row['model'], ref_images=[], output_dir=row['output_dir'],
                history_id=row['history_id']
            )
            task.status = TaskStatus.PENDING
            task.progress = row['progress'] or 0
            task.video_path = row['video_path']
            task.error_message = row['error_message']

            conn2 = get_db_connection()
            cursor2 = conn2.cursor()
            cursor2.execute("SELECT image_path FROM task_ref_images WHERE task_id = ?", (task_id,))
            for img_row in cursor2.fetchall():
                task.ref_images.append(img_row[0])
            conn2.close()

            with self._tasks_lock:
                self.tasks[task_id] = task

            # 重新提交所有未完成的任务（包括 running 状态，防止僵尸任务）
            if task.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
                self._submit_to_executor(task_id)

        log(f"[*] 从数据库加载了 {len(rows)} 个未完成任务")

    def _save_task_file(self, task: Task):
        """持久化任务到 JSON 文件（崩溃恢复）"""
        task_file = os.path.join(ASYNC_TASKS_DIR, f'{task.task_id}.json')
        data = task.to_dict()
        with open(task_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _load_task_file(self, task_id: str) -> Optional[Task]:
        task_file = os.path.join(ASYNC_TASKS_DIR, f'{task_id}.json')
        if not os.path.exists(task_file):
            return None
        with open(task_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        task = Task(
            task_id=data['task_id'], prompt=data['prompt'], duration=data['duration'],
            ratio=data['ratio'], model=data['model'], ref_images=data.get('ref_images', []),
            output_dir=data.get('output_dir', ''),
            status=TaskStatus(data.get('status', 'pending')),
            progress=data.get('progress', 0),
            video_path=data.get('video_path'),
            error_message=data.get('error_message'),
            history_id=data.get('history_id')
        )
        return task

    def _cleanup_expired_tasks(self):
        """清理 24 小时前已完成的任务（仅清理 SUCCESS 和 FAILED，不影响 RUNNING/PENDING）"""
        now = time.time()
        to_remove = []
        with self._tasks_lock:
            for task_id, task in self.tasks.items():
                if task.status in (TaskStatus.SUCCESS, TaskStatus.FAILED):
                    if task.completed_at:
                        completed_ts = task.completed_at.timestamp()
                        if now - completed_ts > 86400:
                            to_remove.append(task_id)
        
        for task_id in to_remove:
            self.delete_task(task_id)
            task_file = os.path.join(ASYNC_TASKS_DIR, f'{task_id}.json')
            if os.path.exists(task_file):
                os.remove(task_file)

    def _cleanup_zombie_tasks(self):
        """检测并清理僵尸任务：running 超过超时阈值的任务标记为失败"""
        now = time.time()
        current_timeout = get_task_timeout()
        zombie_timeout = current_timeout + 60  # 比任务超时多 60 秒，确保 _execute_task 的超时先触发
        with self._tasks_lock:
            for task_id, task in list(self.tasks.items()):
                if task.status == TaskStatus.RUNNING and task.started_at:
                    elapsed = now - task.started_at.timestamp()
                    if elapsed > zombie_timeout:
                        log(f'[WARN] 发现僵尸任务: {task_id} (运行 {elapsed:.0f}s)，强制标记失败')
                        with task.lock:
                            task.status = TaskStatus.FAILED
                            task.error_message = f'任务执行超时 (运行 {elapsed:.0f}s，超时阈值 {zombie_timeout}s)'
                            task.completed_at = datetime.now()
                        save_task_to_db(task)
                        self._save_task_file(task)
                        self._signal_task_complete(task_id)

    def add_task(self, prompt: str, duration: int, ratio: str, model: str,
                 ref_images: list, output_dir: str) -> str:
        task_id = str(uuid.uuid4())
        
        rel_images = []
        for img_path in ref_images:
            if os.path.isabs(img_path):
                rel_path = os.path.relpath(img_path, BASE_DIR)
            else:
                rel_path = img_path
            rel_images.append(rel_path)
        
        task = Task(task_id, prompt, duration, ratio, model, rel_images, output_dir)

        with self._tasks_lock:
            self.tasks[task_id] = task
            self._events[task_id] = threading.Event()

        save_task_to_db(task)
        save_task_ref_images(task_id, rel_images)
        self._save_task_file(task)

        self._submit_to_executor(task_id)
        log(f"[>] 任务已提交: {task_id}")
        return task_id

    def _submit_to_executor(self, task_id: str):
        self.executor.submit(self._execute_task, task_id)

    def get_task(self, task_id: str) -> Optional[Task]:
        with self._tasks_lock:
            if task_id in self.tasks:
                return self.tasks[task_id]

        # 从文件恢复
        task = self._load_task_file(task_id)
        if task:
            with self._tasks_lock:
                self.tasks[task_id] = task
            return task

        # 从数据库查询
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        row = cursor.fetchone()
        conn.close()

        if row:
            task = Task(
                task_id=row['task_id'], prompt=row['prompt'], duration=row['duration'], ratio=row['ratio'],
                model=row['model'], ref_images=[], output_dir=row['output_dir'],
                history_id=row['history_id']
            )
            task.status = TaskStatus(row['status'])
            task.progress = row['progress'] or 0
            task.video_path = row['video_path']
            task.error_message = row['error_message']
            return task
        return None

    def get_all_tasks(self, limit: int = 100, offset: int = 0, status: str = None):
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        if status:
            cursor.execute("SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                         (status, limit, offset))
        else:
            cursor.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ? OFFSET ?",
                         (limit, offset))

        rows = cursor.fetchall()
        conn.close()

        result = []
        for row in rows:
            task_id = row['task_id']
            with self._tasks_lock:
                if task_id in self.tasks:
                    result.append(self.tasks[task_id].to_dict())
                    continue

            ref_images = []
            conn2 = get_db_connection()
            cursor2 = conn2.cursor()
            cursor2.execute("SELECT image_path FROM task_ref_images WHERE task_id = ?", (task_id,))
            for img_row in cursor2.fetchall():
                ref_images.append(img_row[0])
            conn2.close()

            result.append({
                'task_id': row['task_id'], 'prompt': row['prompt'], 'duration': row['duration'],
                'ratio': row['ratio'], 'model': row['model'], 'status': row['status'],
                'progress': row['progress'] or 0, 'video_path': row['video_path'],
                'error_message': row['error_message'], 'created_at': row['created_at'],
                'ref_images': ref_images, 'ref_images_count': len(ref_images)
            })

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

            save_task_to_db(task)
            self._save_task_file(task)

            log(f"\n[*] 开始执行任务: {task_id}")
            log(f"   提示词: {task.prompt}")
            log(f"   时长: {task.duration}s, 比例: {task.ratio}, 模型: {task.model}")

            # 后台进度更新：每30秒根据 elapsed/timeout 比例更新中间进度
            progress_stop = threading.Event()
            def _update_progress():
                while not progress_stop.is_set():
                    progress_stop.wait(30)
                    if progress_stop.is_set():
                        break
                    t = self.get_task(task_id)
                    if not t or t.status != TaskStatus.RUNNING:
                        break
                    current_timeout = get_task_timeout()
                    if t.started_at:
                        elapsed = (datetime.now() - t.started_at).total_seconds()
                        # 进度从 5 线性增长到 90，留 90~100 给完成阶段
                        est_progress = min(90, int(5 + 85 * (elapsed / current_timeout)))
                        with t.lock:
                            if t.progress < est_progress:
                                t.progress = est_progress
                        save_task_to_db(t)

            progress_thread = threading.Thread(target=_update_progress, daemon=True)
            progress_thread.start()

            if DEBUG_MODE:
                log(f"[DEBUG] 调试模式，直接返回本地视频")
                time.sleep(5)
                
                sample_video = os.path.join(BASE_DIR, 'downloads', 'debug_test.mp4')
                with task.lock:
                    task.status = TaskStatus.SUCCESS
                    task.video_path = os.path.relpath(sample_video, BASE_DIR) if os.path.exists(sample_video) else None
                    task.progress = 100
                    task.completed_at = datetime.now()
                    log(f"[OK] 任务完成 (调试模式): {task_id}")
                
                save_task_to_db(task)
                self._save_task_file(task)
                self._signal_task_complete(task_id)
                return

            abs_ref_images = []
            for img_path in task.ref_images:
                if os.path.isabs(img_path):
                    abs_ref_images.append(img_path)
                else:
                    abs_ref_images.append(os.path.join(BASE_DIR, img_path))

            # 异步执行核心任务（带全局超时，使用持久化 event loop）
            current_timeout = get_task_timeout()
            try:
                result = run_async(
                    xiaoyunque_run(
                        prompt=task.prompt,
                        duration=task.duration,
                        ratio=task.ratio,
                        model=task.model,
                        ref_images=abs_ref_images,
                        output_dir=task.output_dir
                    ),
                    timeout=current_timeout
                )
            except TimeoutError:
                log(f"[ERROR] 任务执行超时 ({current_timeout}s): {task_id}")
                raise APIException(ErrorCode.TIMEOUT, f'任务执行超时 ({current_timeout}s)')

            with task.lock:
                if result and isinstance(result, str) and result.endswith('.mp4'):
                    task.status = TaskStatus.SUCCESS
                    task.video_path = os.path.relpath(result, BASE_DIR)
                    task.progress = 100
                    task.completed_at = datetime.now()
                    log(f"[OK] 任务完成: {task_id}")
                else:
                    task.status = TaskStatus.FAILED
                    task.error_message = result if isinstance(result, str) else "未找到生成的视频文件"
                    task.completed_at = datetime.now()
                    log(f"[ERROR] 任务失败: {task_id} - {task.error_message}")

            save_task_to_db(task)
            self._save_task_file(task)

        except APIException as e:
            with task.lock:
                task.status = TaskStatus.FAILED
                task.error_message = e.message
                task.completed_at = datetime.now()
            save_task_to_db(task)
            self._save_task_file(task)
            log(f"[ERROR] API错误: {task_id} - {e.message}")
        except Exception as e:
            with task.lock:
                task.status = TaskStatus.FAILED
                task.error_message = str(e)
                task.completed_at = datetime.now()
            save_task_to_db(task)
            self._save_task_file(task)
            import traceback
            traceback.print_exc()
        finally:
            progress_stop.set()
            self._signal_task_complete(task_id)

    def _signal_task_complete(self, task_id: str):
        """通知等待的查询请求，并延迟清理事件"""
        with self._tasks_lock:
            if task_id in self._events:
                self._events[task_id].set()
        # 延迟清理 _events，给等待者足够时间读取结果
        def _cleanup_event():
            time.sleep(30)  # 等待 30 秒让所有等待者获取结果
            with self._tasks_lock:
                self._events.pop(task_id, None)
        threading.Thread(target=_cleanup_event, daemon=True).start()

    def wait_for_task(self, task_id: str, timeout: int = 1200) -> Optional[Task]:
        """阻塞等待任务完成"""
        task = self.get_task(task_id)
        if not task:
            return None
        
        if task.status in (TaskStatus.SUCCESS, TaskStatus.FAILED):
            return task
        
        with self._tasks_lock:
            if task_id not in self._events:
                self._events[task_id] = threading.Event()
            event = self._events[task_id]
        
        event.wait(timeout=timeout)
        return self.get_task(task_id)

    def retry_task(self, task_id: str) -> bool:
        task = self.get_task(task_id)
        if not task or task.status != TaskStatus.FAILED:
            return False

        with task.lock:
            task.status = TaskStatus.PENDING
            task.progress = 0
            task.error_message = None
            task.video_path = None
            task.started_at = None
            task.completed_at = None
            task.history_id = None

        save_task_to_db(task)
        self._save_task_file(task)
        
        with self._tasks_lock:
            if task_id in self._events:
                self._events[task_id].clear()
            else:
                self._events[task_id] = threading.Event()
        
        self._submit_to_executor(task_id)
        return True

    def cancel_task(self, task_id: str) -> bool:
        """取消正在运行的任务（标记为 failed，前端可立即感知）"""
        task = self.get_task(task_id)
        if not task:
            return False
        # 只有 pending 和 running 状态可取消
        if task.status not in (TaskStatus.PENDING, TaskStatus.RUNNING):
            return False
        with task.lock:
            task.status = TaskStatus.FAILED
            task.error_message = '任务已被用户取消'
            task.completed_at = datetime.now()
        save_task_to_db(task)
        self._save_task_file(task)
        self._signal_task_complete(task_id)
        log(f'[*] 任务已取消: {task_id}')
        return True

    def delete_task(self, task_id: str) -> bool:
        task = self.get_task(task_id)
        
        with self._tasks_lock:
            if task_id in self.tasks:
                task = self.tasks[task_id]
                if task.status == TaskStatus.RUNNING:
                    return False
                del self.tasks[task_id]
            if task_id in self._events:
                del self._events[task_id]

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM task_ref_images WHERE task_id = ?", (task_id,))
        cursor.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        conn.commit()
        conn.close()

        task_file = os.path.join(ASYNC_TASKS_DIR, f'{task_id}.json')
        if os.path.exists(task_file):
            os.remove(task_file)

        if task and task.output_dir and os.path.exists(task.output_dir):
            shutil.rmtree(task.output_dir, ignore_errors=True)

        return True

    def clear_all_tasks(self) -> dict:
        running = self.get_running_count()
        if running > 0:
            return {'status': 'error', 'message': f'{running} 个任务正在运行'}

        # Count tasks before clearing
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM tasks")
        total = cursor.fetchone()[0]
        cursor.execute("DELETE FROM task_ref_images")
        cursor.execute("DELETE FROM tasks")
        conn.commit()
        conn.close()

        with self._tasks_lock:
            self.tasks.clear()
            self._events.clear()

        for item in os.listdir(ASYNC_TASKS_DIR):
            path = os.path.join(ASYNC_TASKS_DIR, item)
            if os.path.isfile(path):
                os.remove(path)

        for item in os.listdir(UPLOAD_FOLDER):
            path = os.path.join(UPLOAD_FOLDER, item)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif item.endswith('.mp4'):
                os.remove(path)

        return {'status': 'success', 'deleted': total}

task_manager = AsyncTaskManager()

# ==================== 工具函数 ====================

def allowed_file(filename):
    return filename and '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def error_response(code: ErrorCode, detail: str = ''):
    """标准化错误响应"""
    return jsonify({
        'error': {
            'code': code.value[0],
            'message': f'{code.value[1]}: {detail}' if detail else code.value[1],
            'type': code.name
        }
    }), code.value[2]

def success_response(data: Any = None, message: str = '成功'):
    """标准化成功响应"""
    return jsonify({
        'status': 'success',
        'message': message,
        'data': data
    })

# ==================== 健康检查 ====================

@app.route('/')
def index():
    return send_file(os.path.join(app.static_folder, 'index.html'))

@app.route('/api/health', methods=['GET'])
def health_check():
    cookies_files = get_token_files()
    return jsonify({
        'status': 'healthy',
        'service': 'xiaoyunque-v3.0',
        'version': '3.0.0',
        'max_workers': MAX_WORKERS,
        'running_tasks': task_manager.get_running_count(),
        'cookies_count': len(cookies_files),
        'debug_mode': get_debug_mode(),
        'task_timeout': get_task_timeout()
    })

@app.route('/api/debug-mode', methods=['GET'])
def get_debug_mode_api():
    return jsonify({'status': 'success', 'debug_mode': get_debug_mode()})

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

@app.route('/api/settings', methods=['GET'])
def get_settings():
    """获取运行时设置"""
    return jsonify({
        'status': 'success',
        'settings': {
            'task_timeout': get_task_timeout(),
            'max_workers': MAX_WORKERS,
            'debug_mode': get_debug_mode(),
        }
    })

@app.route('/api/settings', methods=['POST'])
def update_settings():
    """更新运行时设置"""
    try:
        data = request.json or {}
        changes = {}

        if 'task_timeout' in data:
            new_timeout = int(data['task_timeout'])
            new_timeout = set_task_timeout(new_timeout)
            changes['task_timeout'] = new_timeout

        if 'debug_mode' in data:
            new_mode = set_debug_mode(bool(data['debug_mode']))
            changes['debug_mode'] = new_mode

        if not changes:
            return jsonify({'status': 'error', 'message': '没有可更新的设置'}), 400

        return jsonify({
            'status': 'success',
            'message': f'设置已更新: {", ".join(f"{k}={v}" for k, v in changes.items())}',
            'settings': {
                'task_timeout': get_task_timeout(),
                'max_workers': MAX_WORKERS,
                'debug_mode': get_debug_mode(),
            }
        })
    except (ValueError, TypeError) as e:
        return jsonify({'status': 'error', 'message': f'参数错误: {e}'}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ==================== Cookie 管理 ====================

@app.route('/api/cookies', methods=['GET'])
def list_cookies():
    cookies_files = get_token_files()
    cookies_list = []
    for i, fname in enumerate(cookies_files):
        fpath = os.path.join(COOKIES_DIR, fname)
        cookies_list.append({
            'id': i + 1,
            'name': fname.replace('.json', ''),
            'filename': fname,
            'path': fpath,
            'size': os.path.getsize(fpath) if os.path.exists(fpath) else 0,
            'credits': None,
            'last_used': None,
            'status': 'unknown'
        })

    # 从数据库读取积分信息
    if os.path.exists(DB_PATH):
        try:
            conn = get_db_connection()
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
        except Exception:
            pass

    return jsonify({
        'cookies': cookies_list,
        'count': len(cookies_list)
    })

@app.route('/api/cookies', methods=['POST'])
def upload_cookie():
    try:
        name = request.form.get('name', '').strip()
        content = None
        save_path = None

        if 'file' in request.files:
            file = request.files['file']
            if file and file.filename:
                if name:
                    name = secure_filename(name)
                    if not name.endswith('.json'):
                        name = name + '.json'
                else:
                    name = secure_filename(file.filename)
                    if not name.endswith('.json'):
                        name = name + '.json'
                save_path = os.path.join(COOKIES_DIR, name)
                file.save(save_path)
                try:
                    with open(save_path, 'r', encoding='utf-8') as f:
                        content = json.load(f)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
        elif request.is_json and request.json and 'content' in request.json:
            raw_content = request.json['content']
            if isinstance(raw_content, str):
                content = raw_content
            elif isinstance(raw_content, (list, dict)):
                content = json.dumps(raw_content, ensure_ascii=False)
            else:
                content = str(raw_content)
            if not name:
                name = 'cookie_' + str(int(time.time()))
            if not name.endswith('.json'):
                name = name + '.json'
            save_path = os.path.join(COOKIES_DIR, name)
            with open(save_path, 'w', encoding='utf-8') as f:
                f.write(content)
        else:
            return jsonify({'status': 'error', 'message': '请上传文件或提供JSON内容'}), 400

        if content is not None and not isinstance(content, list):
            if save_path and os.path.exists(save_path):
                os.remove(save_path)
            return jsonify({'status': 'error', 'message': 'Cookie文件必须是数组格式'}), 400

        return jsonify({
            'status': 'success',
            'message': f'Cookie {name} 上传成功',
            'filename': name
        })

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

        return jsonify({'status': 'success', 'message': 'Cookie已删除'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ==================== Cookie 测试端点 ====================

@app.route('/api/cookies/<cookie_name>/test', methods=['POST'])
def test_cookie(cookie_name):
    """测试单个 Cookie 的积分"""
    try:
        if not cookie_name.endswith('.json'):
            cookie_name = cookie_name + '.json'

        cookie_path = os.path.join(COOKIES_DIR, cookie_name)
        if not os.path.exists(cookie_path):
            return jsonify({'status': 'error', 'message': 'Cookie文件不存在'}), 404

        from xiaoyunque_v3 import load_cookies, get_credits_info, browser_session

        async def check_credits_async():
            # 复用已有的 browser_session 单例，避免每次启动新浏览器
            cookies = load_cookies(cookie_path)
            ctx = None
            page = None
            try:
                ctx = await browser_session.get_context(cookie_path)
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
                return credits
            finally:
                if page:
                    try:
                        await asyncio.wait_for(page.close(), timeout=10)
                    except Exception:
                        pass

        credits = run_async(check_credits_async(), timeout=60)

        # 保存到数据库
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO cookies (name, file_path, credits, last_used, status, created_at)
            VALUES (?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM cookies WHERE name = ?), ?))
        ''', (cookie_name, cookie_path, credits, datetime.now().isoformat(), 'active', cookie_name, datetime.now().isoformat()))
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
    """批量查询所有 Cookie 积分"""
    try:
        cookies_files = get_token_files()
        if not cookies_files:
            return jsonify({'status': 'success', 'results': [], 'message': '没有找到Cookie文件'})

        results = []

        for fname in cookies_files:
            cookie_path = os.path.join(COOKIES_DIR, fname)
            cookie_name = fname.replace('.json', '')

            try:
                from xiaoyunque_v3 import load_cookies, get_credits_info

                async def check_credits_async():
                    # 复用已有的 browser_session 单例
                    cookies = load_cookies(cookie_path)
                    ctx = None
                    page = None
                    try:
                        ctx = await browser_session.get_context(cookie_path)
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
                        return credits
                    finally:
                        if page:
                            try:
                                await asyncio.wait_for(page.close(), timeout=10)
                            except Exception:
                                pass

                credits = run_async(check_credits_async(), timeout=60)

                # 保存到数据库
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO cookies (name, file_path, credits, last_used, status, created_at)
                    VALUES (?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM cookies WHERE name = ?), ?))
                ''', (cookie_name, cookie_path, credits, datetime.now().isoformat(), 'active', cookie_name, datetime.now().isoformat()))
                conn.commit()
                conn.close()

                results.append({
                    'name': cookie_name,
                    'filename': fname,
                    'credits': credits,
                    'status': 'success'
                })
                log(f"[*] {cookie_name}: {credits} 积分")

            except Exception as e:
                results.append({
                    'name': cookie_name,
                    'filename': fname,
                    'credits': None,
                    'status': 'failed',
                    'error': str(e)
                })
                log(f"[!] {cookie_name}: 查询失败 - {e}")

        return jsonify({
            'status': 'success',
            'results': results,
            'message': f'查询完成，成功 {sum(1 for r in results if r["status"] == "success")}/{len(results)}'
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500


# ==================== OpenAI 兼容 API ====================

@app.route('/v1/models', methods=['GET'])
def list_models():
    """获取可用模型列表"""
    models = [
        {'id': 'seedance-2.0-fast', 'object': 'model', 'created': 1700000000, 'owned_by': 'xiaoyunque'},
        {'id': 'seedance-2.0', 'object': 'model', 'created': 1700000000, 'owned_by': 'xiaoyunque'},
    ]
    return jsonify({'object': 'list', 'data': models})

@app.route('/v1/videos/generations', methods=['POST'])
def generate_video_openai():
    """OpenAI 兼容视频生成接口"""
    try:
        # 支持 multipart/form-data 和 application/json
        if request.content_type and 'multipart' in request.content_type:
            prompt = request.form.get('prompt', '').strip()
            duration = int(request.form.get('duration', 10))
            ratio = request.form.get('ratio', '16:9')
            model = request.form.get('model', 'seedance-2.0-fast')
        else:
            data = request.json or {}
            prompt = data.get('prompt', '').strip()
            duration = int(data.get('duration', 10))
            ratio = data.get('ratio', '16:9')
            model = data.get('model', 'seedance-2.0-fast')

        if not prompt:
            return error_response(ErrorCode.PARAMS_INVALID, '提示词不能为空')

        if duration not in [5, 10, 15]:
            return error_response(ErrorCode.PARAMS_INVALID, '时长必须是 5、10 或 15 秒')

        if ratio not in ['16:9', '9:16']:
            return error_response(ErrorCode.PARAMS_INVALID, '比例必须是 16:9 或 9:16')

        cookies_files = get_token_files()
        if not cookies_files:
            return error_response(ErrorCode.PARAMS_INVALID, '请先上传 Cookie 文件')

        model_map = {'seedance-2.0-fast': 'fast', 'seedance-2.0': '2.0'}
        xiaoyunque_model = model_map.get(model, 'fast')

        uploaded_files = []

        if 'files' in request.files:
            files = request.files.getlist('files')
            for i, file in enumerate(files):
                if file and allowed_file(file.filename):
                    filename = f"{int(time.time())}_{i}_{secure_filename(file.filename)}"
                    filepath = os.path.join(UPLOAD_FOLDER, filename)
                    file.save(filepath)
                    uploaded_files.append(filepath)

        if request.is_json and 'file_paths' in data:
            uploaded_files = data.get('file_paths', [])

        if not uploaded_files:
            return error_response(ErrorCode.PARAMS_INVALID, '至少需要一张参考图片')

        required_credits = MODEL_CREDITS_PER_SEC.get(xiaoyunque_model, 5) * duration

        task_id = str(uuid.uuid4())
        output_dir = os.path.join(UPLOAD_FOLDER, task_id)
        os.makedirs(output_dir, exist_ok=True)

        for i, img_path in enumerate(uploaded_files):
            if os.path.exists(img_path):
                shutil.copy(img_path, os.path.join(output_dir, f"ref_{i}_{os.path.basename(img_path)}"))

        final_images = [os.path.join(output_dir, f"ref_{i}_{os.path.basename(p)}")
                       for i, p in enumerate(uploaded_files) if os.path.exists(p)]

        result_task_id = task_manager.add_task(
            prompt=prompt,
            duration=duration,
            ratio=ratio,
            model=xiaoyunque_model,
            ref_images=final_images,
            output_dir=output_dir
        )

        return jsonify({
            'created': int(time.time()),
            'task_id': result_task_id,
            'status': 'processing',
            'message': f'任务已提交，请使用 GET /v1/videos/generations/{result_task_id} 查询结果',
            'required_credits': required_credits
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return error_response(ErrorCode.REQUEST_FAILED, str(e))

@app.route('/v1/videos/generations/<task_id>', methods=['GET'])
def get_video_status_openai(task_id):
    """查询视频生成状态（OpenAI 兼容格式）"""
    # 防止 "async" 被当作 task_id
    if task_id == 'async':
        return error_response(ErrorCode.PARAMS_INVALID, '请使用 POST /v1/videos/generations/async 提交任务')
    task = task_manager.get_task(task_id)
    if not task:
        return error_response(ErrorCode.PARAMS_INVALID, '任务不存在')
    
    return jsonify(task.to_openai_dict())

@app.route('/v1/videos/generations/async', methods=['POST'])
def generate_video_async():
    """异步视频生成：提交任务立即返回"""
    return generate_video_openai()

@app.route('/v1/videos/generations/async/<task_id>', methods=['GET'])
def get_async_video_result(task_id):
    """查询异步任务结果（服务端阻塞等待）"""
    task = task_manager.get_task(task_id)
    if not task:
        return error_response(ErrorCode.PARAMS_INVALID, '任务不存在')
    
    if task.status in (TaskStatus.SUCCESS, TaskStatus.FAILED):
        return jsonify(task.to_openai_dict())
    
    # 阻塞等待
    timeout = int(request.args.get('timeout', get_task_timeout()))
    task = task_manager.wait_for_task(task_id, timeout=timeout)
    
    if not task:
        return error_response(ErrorCode.TIMEOUT, '查询超时')
    
    return jsonify(task.to_openai_dict())

# ==================== 兼容旧 API ====================

@app.route('/api/generate-video', methods=['POST'])
def generate_video():
    try:
        data = request.form if request.form else request.json

        prompt = data.get('prompt', '').strip()
        duration = int(data.get('duration', 10))
        ratio = data.get('ratio', '16:9')
        model = data.get('model', 'fast')

        if not prompt:
            return jsonify({'status': 'error', 'message': '提示词不能为空'}), 400

        if duration not in [5, 10, 15]:
            return jsonify({'status': 'error', 'message': '时长必须是 5、10 或 15 秒'}), 400

        if ratio not in ['16:9', '9:16']:
            return jsonify({'status': 'error', 'message': '比例必须是 16:9 或 9:16'}), 400

        cookies_files = get_token_files()
        if not cookies_files:
            return jsonify({'status': 'error', 'message': '请先上传 Cookie 文件'}), 400

        model_map = {'seedance-2.0-fast': 'fast', 'seedance-2.0': '2.0'}
        xiaoyunque_model = model_map.get(model, 'fast')

        uploaded_files = []

        if 'files' in request.files:
            files = request.files.getlist('files')
            for i, file in enumerate(files):
                if file and allowed_file(file.filename):
                    filename = f"{int(time.time())}_{i}_{secure_filename(file.filename)}"
                    filepath = os.path.join(UPLOAD_FOLDER, filename)
                    file.save(filepath)
                    uploaded_files.append(filepath)

        if request.is_json and 'images' in data:
            images_data = data.get('images', [])
            if isinstance(images_data, str):
                images_data = [images_data]
            for i, img_data in enumerate(images_data):
                if img_data.startswith('data:image'):
                    import base64
                    try:
                        header, img_bytes_b64 = img_data.split(',', 1)
                        filename = f"image_{i}_{int(time.time())}.png"
                        filepath = os.path.join(UPLOAD_FOLDER, filename)
                        # 流式解码写入，避免大文件全部加载到内存
                        with open(filepath, 'wb') as f:
                            # 分块解码
                            chunk_size = 65536
                            for j in range(0, len(img_bytes_b64), chunk_size):
                                chunk = img_bytes_b64[j:j+chunk_size]
                                # 补齐 padding
                                padding = 4 - len(chunk) % 4
                                if padding != 4:
                                    chunk += '=' * padding
                                f.write(base64.b64decode(chunk))
                        uploaded_files.append(filepath)
                    except Exception as e:
                        log(f"[WARN] 解析 base64 图片失败: {e}")

        if not uploaded_files:
            return jsonify({'status': 'error', 'message': '至少需要一张参考图片'}), 400

        required_credits = MODEL_CREDITS_PER_SEC.get(xiaoyunque_model, 5) * duration

        task_id = str(uuid.uuid4())
        output_dir = os.path.join(UPLOAD_FOLDER, task_id)
        os.makedirs(output_dir, exist_ok=True)

        for i, img_path in enumerate(uploaded_files):
            shutil.copy(img_path, os.path.join(output_dir, f"ref_{i}_{os.path.basename(img_path)}"))

        final_images = [os.path.join(output_dir, f"ref_{i}_{os.path.basename(p)}")
                       for i, p in enumerate(uploaded_files) if os.path.exists(p)]

        result_task_id = task_manager.add_task(
            prompt=prompt,
            duration=duration,
            ratio=ratio,
            model=xiaoyunque_model,
            ref_images=final_images,
            output_dir=output_dir
        )

        return jsonify({
            'status': 'success',
            'task_id': result_task_id,
            'message': f'视频生成任务已提交 (预计需要 {required_credits} 积分)',
            'required_credits': required_credits,
            'running_tasks': task_manager.get_running_count()
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': f'服务器内部错误: {str(e)}'}), 500

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

@app.route('/api/task/<task_id>/cancel', methods=['POST'])
def cancel_task(task_id):
    if task_manager.cancel_task(task_id):
        return jsonify({'status': 'success', 'message': '任务已取消'})
    return jsonify({'status': 'error', 'message': '无法取消任务，可能已完成或不存在'}), 400

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

    if task.status != TaskStatus.SUCCESS or not task.video_path:
        return jsonify({'status': 'error', 'message': '视频尚未生成完成'}), 404

    video_path = task.video_path
    if not os.path.isabs(video_path):
        video_path = os.path.join(BASE_DIR, video_path)
    video_path = os.path.normpath(video_path)
    if not video_path.startswith(BASE_DIR):
        return jsonify({'status': 'error', 'message': '无效的路径'}), 400
    if not os.path.exists(video_path):
        return jsonify({'status': 'error', 'message': '视频文件不存在'}), 404

    return send_file(video_path, mimetype='video/mp4', as_attachment=True)

@app.route('/api/image/<path:image_path>', methods=['GET'])
def get_image(image_path):
    if '..' in image_path:
        return jsonify({'status': 'error', 'message': '无效的路径'}), 400
    
    full_path = os.path.normpath(os.path.join(BASE_DIR, image_path))
    if not full_path.startswith(BASE_DIR):
        return jsonify({'status': 'error', 'message': '无效的路径'}), 400
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
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT status, COUNT(*) FROM tasks GROUP BY status')
    rows = cursor.fetchall()
    conn.close()

    stats = {'pending': 0, 'running': 0, 'success': 0, 'failed': 0}
    for status, count in rows:
        if status in stats:
            stats[status] = count

    cookies_files = get_token_files()

    return jsonify({
        'status': 'success',
        'stats': stats,
        'total': sum(stats.values()),
        'running': task_manager.get_running_count(),
        'cookies_count': len(cookies_files)
    })

# ==================== 后台任务 ====================

def cleanup_loop():
    """定期清理空闲浏览器会话、过期任务和僵尸任务"""
    while True:
        time.sleep(60)  # 每 60 秒执行一次（从 5 分钟改为 1 分钟，更快发现僵尸）
        try:
            task_manager._cleanup_expired_tasks()
        except Exception as e:
            log(f"[WARN] 清理过期任务失败: {e}")
        try:
            task_manager._cleanup_zombie_tasks()
        except Exception as e:
            log(f"[WARN] 清理僵尸任务失败: {e}")
        try:
            run_async(browser_session.cleanup_idle(), timeout=30)
        except Exception as e:
            log(f"[WARN] 清理空闲浏览器失败: {e}")

# ==================== 启动 ====================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8033))
    host = os.environ.get('HOST', '0.0.0.0')

    print("\n" + "="*60)
    print("小云雀 (XiaoYunque) Web API 服务器 v3.0")
    print("="*60)
    print(f"数据库: {DB_PATH}")
    print(f"上传目录: {UPLOAD_FOLDER}")
    print(f"Cookies目录: {COOKIES_DIR}")
    print(f"最大并发: {MAX_WORKERS}")
    print(f"任务超时: {get_task_timeout()}秒 ({get_task_timeout() // 60}分钟)")
    print(f"调试模式: {'开启' if DEBUG_MODE else '关闭'}")
    print(f"服务地址: http://{host}:{port}")
    print("="*60 + "\n")

    # 启动清理线程
    cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
    cleanup_thread.start()

    app.run(host=host, port=port, debug=False, threaded=True)
