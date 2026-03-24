#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小云雀 (XiaoYunque) Web API 服务器
将 xiaoyunque.py 包装为 Flask Web 服务，提供 REST API 接口
"""

import os
import sys
import json
import asyncio
import tempfile
import shutil
from flask import Flask, request, jsonify
from werkzeug.utils import secure_filename
import threading
import uuid
import time

# 添加当前目录到 Python 路径
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# 导入 xiaoyunque 模块
from xiaoyunque import main as xiaoyunque_main
import argparse

app = Flask(__name__)

# 配置
UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'bmp', 'gif'}
MAX_CONTENT_LENGTH = 20 * 1024 * 1024  # 20MB

# 确保上传目录存在
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# 任务状态存储
tasks = {}

def allowed_file(filename):
    """检查文件扩展名是否允许"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def run_xiaoyunque_task(task_id, prompt, duration, ratio, model, image_paths, output_dir):
    """运行 xiaoyunque 任务的线程函数"""
    try:
        # 构建命令行参数
        args = argparse.Namespace(
            prompt=prompt,
            ref_images=image_paths,
            duration=duration,
            ratio=ratio,
            model=model,
            cookies='cookies.json',
            output=output_dir,
            dry_run=False
        )
        
        # 运行 xiaoyunque
        tasks[task_id]['status'] = 'running'
        tasks[task_id]['start_time'] = time.time()
        
        # 由于 xiaoyunque_main 是同步函数，直接调用
        xiaoyunque_main(args)
        
        # 查找生成的视频文件
        video_files = []
        for file in os.listdir(output_dir):
            if file.endswith('.mp4'):
                video_files.append(os.path.join(output_dir, file))
        
        if video_files:
            # 取最新的视频文件
            latest_video = max(video_files, key=os.path.getctime)
            tasks[task_id]['status'] = 'completed'
            tasks[task_id]['video_path'] = latest_video
            tasks[task_id]['end_time'] = time.time()
            tasks[task_id]['message'] = '视频生成成功'
        else:
            tasks[task_id]['status'] = 'failed'
            tasks[task_id]['end_time'] = time.time()
            tasks[task_id]['message'] = '视频生成失败：未找到生成的视频文件'
            
    except Exception as e:
        tasks[task_id]['status'] = 'failed'
        tasks[task_id]['end_time'] = time.time()
        tasks[task_id]['message'] = f'视频生成失败：{str(e)}'
        import traceback
        tasks[task_id]['error'] = traceback.format_exc()
    finally:
        # 清理临时目录（保留上传的文件用于调试）
        pass

@app.route('/api/health', methods=['GET'])
def health_check():
    """健康检查端点"""
    return jsonify({
        'status': 'healthy',
        'service': 'xiaoyunque',
        'timestamp': time.time()
    })

@app.route('/api/generate-video', methods=['POST'])
def generate_video():
    """生成视频 API 端点"""
    try:
        # 获取参数
        prompt = request.form.get('prompt', '').strip()
        duration = int(request.form.get('duration', 10))
        ratio = request.form.get('ratio', '16:9')
        model = request.form.get('model', 'seedance-2.0')
        
        if not prompt:
            return jsonify({
                'status': 'error',
                'message': '提示词不能为空'
            }), 400
        
        # 验证时长
        if duration not in [5, 10, 15]:
            return jsonify({
                'status': 'error',
                'message': '时长必须是 5、10 或 15 秒'
            }), 400
        
        # 验证比例
        if ratio not in ['16:9', '9:16', '1:1']:
            return jsonify({
                'status': 'error',
                'message': '比例必须是 16:9、9:16 或 1:1'
            }), 400
        
        # 验证模型
        model_map = {
            'seedance-2.0': '2.0',
            'seedance-2.0-fast': 'fast',
            'seedance-1.5': '1.5'
        }
        xiaoyunque_model = model_map.get(model, 'fast')
        
        # 处理上传的文件
        uploaded_files = []
        if 'files' in request.files:
            files = request.files.getlist('files')
            for file in files:
                if file and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    filepath = os.path.join(UPLOAD_FOLDER, filename)
                    file.save(filepath)
                    uploaded_files.append(filepath)
        
        # 如果没有上传文件，检查是否有 base64 图片
        if not uploaded_files and 'images' in request.form:
            images_data = request.form.getlist('images')
            for i, img_data in enumerate(images_data):
                if img_data.startswith('data:image'):
                    # 处理 base64 图片
                    import base64
                    try:
                        # 提取 base64 数据
                        header, data = img_data.split(',', 1)
                        img_bytes = base64.b64decode(data)
                        
                        # 生成文件名
                        filename = f'image_{i}_{int(time.time())}.png'
                        filepath = os.path.join(UPLOAD_FOLDER, filename)
                        
                        with open(filepath, 'wb') as f:
                            f.write(img_bytes)
                        
                        uploaded_files.append(filepath)
                    except Exception as e:
                        app.logger.error(f"解析 base64 图片失败: {e}")
        
        if not uploaded_files:
            return jsonify({
                'status': 'error',
                'message': '至少需要一张参考图片'
            }), 400
        
        # 创建任务
        task_id = str(uuid.uuid4())
        output_dir = os.path.join(UPLOAD_FOLDER, task_id)
        os.makedirs(output_dir, exist_ok=True)
        
        # 初始化任务状态
        tasks[task_id] = {
            'status': 'pending',
            'prompt': prompt,
            'duration': duration,
            'ratio': ratio,
            'model': model,
            'image_count': len(uploaded_files),
            'created_time': time.time(),
            'output_dir': output_dir
        }
        
        # 启动后台任务
        thread = threading.Thread(
            target=run_xiaoyunque_task,
            args=(task_id, prompt, duration, ratio, xiaoyunque_model, uploaded_files, output_dir)
        )
        thread.daemon = True
        thread.start()
        
        return jsonify({
            'status': 'success',
            'task_id': task_id,
            'message': '视频生成任务已提交',
            'task_status': 'pending'
        })
        
    except Exception as e:
        app.logger.error(f"生成视频 API 错误: {e}")
        return jsonify({
            'status': 'error',
            'message': f'服务器内部错误: {str(e)}'
        }), 500

@app.route('/api/task/<task_id>', methods=['GET'])
def get_task_status(task_id):
    """获取任务状态"""
    if task_id not in tasks:
        return jsonify({
            'status': 'error',
            'message': '任务不存在'
        }), 404
    
    task = tasks[task_id]
    response = {
        'task_id': task_id,
        'status': task['status'],
        'prompt': task.get('prompt', ''),
        'duration': task.get('duration', 0),
        'ratio': task.get('ratio', ''),
        'model': task.get('model', ''),
        'image_count': task.get('image_count', 0),
        'created_time': task.get('created_time', 0),
        'message': task.get('message', '')
    }
    
    if 'start_time' in task:
        response['start_time'] = task['start_time']
    
    if 'end_time' in task:
        response['end_time'] = task['end_time']
        if task['status'] == 'completed':
            response['video_url'] = f'/api/video/{task_id}'
    
    if 'error' in task:
        response['error'] = task['error']
    
    return jsonify(response)

@app.route('/api/video/<task_id>', methods=['GET'])
def get_video(task_id):
    """获取生成的视频文件"""
    if task_id not in tasks:
        return jsonify({
            'status': 'error',
            'message': '任务不存在'
        }), 404
    
    task = tasks[task_id]
    if task['status'] != 'completed' or 'video_path' not in task:
        return jsonify({
            'status': 'error',
            'message': '视频尚未生成完成'
        }), 404
    
    video_path = task['video_path']
    if not os.path.exists(video_path):
        return jsonify({
            'status': 'error',
            'message': '视频文件不存在'
        }), 404
    
    # 返回视频文件
    from flask import send_file
    return send_file(video_path, mimetype='video/mp4', as_attachment=True)

@app.route('/api/tasks', methods=['GET'])
def list_tasks():
    """列出所有任务"""
    task_list = []
    for task_id, task in tasks.items():
        task_list.append({
            'task_id': task_id,
            'status': task['status'],
            'prompt': task.get('prompt', '')[:50] + '...' if len(task.get('prompt', '')) > 50 else task.get('prompt', ''),
            'created_time': task.get('created_time', 0),
            'duration': task.get('duration', 0)
        })
    
    # 按创建时间排序
    task_list.sort(key=lambda x: x['created_time'], reverse=True)
    
    return jsonify({
        'status': 'success',
        'tasks': task_list,
        'total': len(task_list)
    })

@app.route('/api/cleanup', methods=['POST'])
def cleanup():
    """清理旧任务和文件"""
    try:
        max_age = int(request.args.get('max_age', 86400))  # 默认24小时
        current_time = time.time()
        
        deleted_tasks = []
        for task_id, task in list(tasks.items()):
            if 'created_time' in task and current_time - task['created_time'] > max_age:
                # 清理任务数据
                if 'output_dir' in task and os.path.exists(task['output_dir']):
                    shutil.rmtree(task['output_dir'], ignore_errors=True)
                
                # 清理上传的文件
                for key in ['video_path']:
                    if key in task and os.path.exists(task[key]):
                        try:
                            os.remove(task[key])
                        except:
                            pass
                
                del tasks[task_id]
                deleted_tasks.append(task_id)
        
        return jsonify({
            'status': 'success',
            'message': f'清理了 {len(deleted_tasks)} 个旧任务',
            'deleted_tasks': deleted_tasks
        })
        
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': f'清理失败: {str(e)}'
        }), 500

if __name__ == '__main__':
    # 从环境变量获取端口，默认为 6033
    port = int(os.environ.get('PORT', 6033))
    host = os.environ.get('HOST', '0.0.0.0')
    
    print(f"启动 xiaoyunque Web API 服务器在 {host}:{port}")
    print(f"上传目录: {UPLOAD_FOLDER}")
    print(f"健康检查: http://{host}:{port}/api/health")
    print(f"API 文档:")
    print(f"  POST /api/generate-video - 生成视频")
    print(f"  GET  /api/task/<task_id> - 获取任务状态")
    print(f"  GET  /api/video/<task_id> - 下载视频")
    print(f"  GET  /api/tasks - 列出所有任务")
    print(f"  POST /api/cleanup - 清理旧任务")
    
    app.run(host=host, port=port, debug=False, threaded=True)