#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小云雀 (XiaoYunque) Web API 服务器 v2.1
优化版本：
- SQLite 数据库持久化
- ThreadPoolExecutor 并发限制
- 线程安全锁
- 进度追踪
- 多 Cookies 管理
- 积分检查自动切换
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
from datetime import datetime
from enum import Enum
from concurrent.futures import ThreadPoolExecutor, Future
from flask import Flask, request, jsonify, send_file
from werkzeug.utils import secure_filename

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from xiaoyunque import main_wrapper as xiaoyunque_main, load_cookies, get_cookies_files, MODEL_CREDITS_PER_SEC

app = Flask(__name__, static_folder='static', static_url_path='')

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
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
PROGRESS_UPDATE_INTERVAL = 10
PROGRESS_MAX_RUNTIME = 3600
DEBUG_MODE = True
SAMPLE_VIDEO_PATH = os.path.join(BASE_DIR, 'downloads', '一位身穿华丽骨甲的战士站在古老_5s_20260326_165849.mp4')
DEBUG_MODE_LOCK = threading.Lock()

def set_debug_mode(enabled: bool):
    global DEBUG_MODE
    with DEBUG_MODE_LOCK:
        DEBUG_MODE = enabled
    return DEBUG_MODE

def get_debug_mode() -> bool:
    with DEBUG_MODE_LOCK:
        return DEBUG_MODE

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

class Task:
    def __init__(self, task_id: str, prompt: str, duration: int, ratio: str,
                 model: str, ref_images: list, output_dir: str):
        self.task_id = task_id
        self.prompt = prompt
        self.duration = duration
        self.ratio = ratio
        self.model = model
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

    def _load_pending_tasks(self):
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE status IN ('pending', 'running')")
        rows = cursor.fetchall()
        conn.close()

        for row in rows:
            task_id = row[0]
            task = Task(
                task_id=row[0], prompt=row[1], duration=row[2], ratio=row[3],
                model=row[4], ref_images=[], output_dir=row[6]
            )
            task.status = TaskStatus.PENDING
            task.progress = row[8] if row[8] else 0
            task.video_path = row[9]
            task.error_message = row[10]

            conn2 = sqlite3.connect(DB_PATH, check_same_thread=False)
            cursor2 = conn2.cursor()
            cursor2.execute("SELECT image_path FROM task_ref_images WHERE task_id = ?", (task_id,))
            for img_row in cursor2.fetchall():
                task.ref_images.append(img_row[0])
            conn2.close()

            with self._tasks_lock:
                self.tasks[task_id] = task

            if row[7] == 'pending':
                self.executor.submit(self._execute_task, task_id)

        print(f"[*] 从数据库加载了 {len(rows)} 个未完成任务")

    def _save_task_to_db(self, task: Task):
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO tasks
            (task_id, prompt, duration, ratio, model, ref_images, output_dir,
             status, progress, video_path, error_message, created_at, started_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            task.task_id, task.prompt, task.duration, task.ratio, task.model,
            json.dumps(task.ref_images), task.output_dir,
            task.status.value, task.progress, task.video_path, task.error_message,
            task.created_at.isoformat() if task.created_at else None,
            task.started_at.isoformat() if task.started_at else None,
            task.completed_at.isoformat() if task.completed_at else None
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

        self._save_task_to_db(task)
        self._save_task_ref_images(task_id, rel_images)

        self.executor.submit(self._execute_task, task_id)
        print(f"[>] 任务已提交: {task_id}")
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
            task = Task(
                task_id=row[0], prompt=row[1], duration=row[2], ratio=row[3],
                model=row[4], ref_images=[], output_dir=row[6]
            )
            task.status = TaskStatus(row[7])
            task.progress = row[8]
            task.video_path = row[9]
            task.error_message = row[10]
            return task
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

            ref_images = []
            cursor.execute("SELECT image_path FROM task_ref_images WHERE task_id = ?", (task_id,))
            for img_row in cursor.fetchall():
                ref_images.append(img_row[0])

            result.append({
                'task_id': row[0], 'prompt': row[1], 'duration': row[2],
                'ratio': row[3], 'model': row[4], 'status': row[7],
                'progress': row[8], 'video_path': row[9],
                'error_message': row[10], 'created_at': row[11],
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

            print(f"\n[*] 开始执行任务: {task_id}")
            print(f"   提示词: {task.prompt}")
            print(f"   时长: {task.duration}s, 比例: {task.ratio}, 模型: {task.model}")

            progress_thread = threading.Thread(
                target=self._update_progress, args=(task,), daemon=True
            )
            progress_thread.start()

            if DEBUG_MODE:
                print(f"[DEBUG] 调试模式，直接返回本地视频: {SAMPLE_VIDEO_PATH}")
                time.sleep(5)
                
                with task.lock:
                    task.status = TaskStatus.SUCCESS
                    task.video_path = os.path.relpath(SAMPLE_VIDEO_PATH, BASE_DIR)
                    task.progress = 100
                    task.completed_at = datetime.now()
                    print(f"[OK] 任务完成 (调试模式): {task_id}")
                
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
                model=task.model,
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
                    print(f"[OK] 任务完成: {task_id}")
                else:
                    task.status = TaskStatus.FAILED
                    task.error_message = result if isinstance(result, str) else "未找到生成的视频文件"
                    task.completed_at = datetime.now()
                    print(f"[ERROR] 任务失败: {task_id} - {task.error_message}")

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
                print(f"[WARN] 进度更新线程超时退出: {task.task_id}")
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
    try:
        name = request.form.get('name', '').strip()
        content = None

        if 'file' in request.files:
            file = request.files['file']
            if file:
                if name:
                    if not name.endswith('.json'):
                        name = name + '.json'
                else:
                    name = file.filename
                    if not name.endswith('.json'):
                        name = name + '.json'
                save_path = os.path.join(COOKIES_DIR, name)
                file.save(save_path)
                try:
                    with open(save_path, 'r', encoding='utf-8') as f:
                        content = json.load(f)
                except:
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

        if content is None:
            try:
                with open(save_path, 'r', encoding='utf-8') as f:
                    content = json.load(f)
            except:
                pass

        if content is not None and not isinstance(content, list):
            if os.path.exists(save_path):
                os.remove(save_path)
            return jsonify({'status': 'error', 'message': 'Cookie文件必须是数组格式'}), 400

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

        return jsonify({'status': 'success', 'message': 'Cookie已删除'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/cookies/<cookie_name>/test', methods=['POST'])
def test_cookie(cookie_name):
    try:
        if not cookie_name.endswith('.json'):
            cookie_name = cookie_name + '.json'

        cookie_path = os.path.join(COOKIES_DIR, cookie_name)
        if not os.path.exists(cookie_path):
            return jsonify({'status': 'error', 'message': 'Cookie文件不存在'}), 404

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
            return jsonify({'status': 'success', 'results': [], 'message': '没有找到Cookie文件'})

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
                print(f"[*] {cookie_name}: {credits} 积分")

            except Exception as e:
                results.append({
                    'name': cookie_name,
                    'filename': fname,
                    'credits': None,
                    'status': 'failed',
                    'error': str(e)
                })
                print(f"[!] {cookie_name}: 查询失败 - {e}")

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

        cookies_files = get_cookies_files()
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
                        img_bytes = base64.b64decode(img_bytes_b64)
                        filename = f"image_{i}_{int(time.time())}.png"
                        filepath = os.path.join(UPLOAD_FOLDER, filename)
                        with open(filepath, 'wb') as f:
                            f.write(img_bytes)
                        uploaded_files.append(filepath)
                    except Exception as e:
                        print(f"[WARN] 解析 base64 图片失败: {e}")

        if not uploaded_files:
            return jsonify({'status': 'error', 'message': '至少需要一张参考图片'}), 400

        required_credits = MODEL_CREDITS_PER_SEC.get(xiaoyunque_model, 5) * duration

        task_id = str(uuid.uuid4())
        output_dir = os.path.join(UPLOAD_FOLDER, task_id)
        os.makedirs(output_dir, exist_ok=True)

        for i, img_path in enumerate(uploaded_files):
            shutil.copy(img_path, os.path.join(output_dir, f"ref_{i}_{os.path.basename(img_path)}"))

        final_images = [os.path.join(output_dir, f"ref_{i}_{os.path.basename(p)}")
                       for i, p in enumerate(uploaded_files)]

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

    if not os.path.exists(video_path):
        return jsonify({'status': 'error', 'message': '视频文件不存在'}), 404

    return send_file(video_path, mimetype='video/mp4', as_attachment=True)


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
    print("小云雀 (XiaoYunque) Web API 服务器 v2.1")
    print("="*60)
    print(f"数据库: {DB_PATH}")
    print(f"上传目录: {UPLOAD_FOLDER}")
    print(f"Cookies目录: {COOKIES_DIR}")
    print(f"最大并发: {MAX_WORKERS}")
    print(f"服务地址: http://{host}:{port}")
    print("="*60 + "\n")

    app.run(host=host, port=port, debug=False, threaded=True)