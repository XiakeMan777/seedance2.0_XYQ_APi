#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 xiaoyunque Web API 服务器
"""

import requests
import json
import time
import sys

# API 基础 URL
BASE_URL = "http://localhost:6033"

def test_health():
    """测试健康检查端点"""
    print("测试健康检查端点...")
    try:
        response = requests.get(f"{BASE_URL}/api/health", timeout=5)
        print(f"状态码: {response.status_code}")
        print(f"响应: {response.json()}")
        return response.status_code == 200
    except requests.exceptions.ConnectionError:
        print("错误: 无法连接到服务器，请确保服务已启动")
        return False
    except Exception as e:
        print(f"错误: {e}")
        return False

def test_generate_video():
    """测试视频生成端点（模拟请求）"""
    print("\n测试视频生成端点...")
    
    # 准备测试数据
    test_data = {
        'prompt': '测试视频生成',
        'duration': 10,
        'ratio': '16:9',
        'model': 'seedance-2.0'
    }
    
    # 创建一个简单的测试图片文件
    import tempfile
    import base64
    
    # 创建一个 1x1 像素的 PNG 图片（base64）
    test_image_base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    
    test_data['images'] = [f'data:image/png;base64,{test_image_base64}']
    
    try:
        response = requests.post(f"{BASE_URL}/api/generate-video", data=test_data, timeout=10)
        print(f"状态码: {response.status_code}")
        
        if response.status_code == 200:
            result = response.json()
            print(f"响应: {json.dumps(result, indent=2, ensure_ascii=False)}")
            
            if result.get('status') == 'success':
                task_id = result.get('task_id')
                print(f"任务ID: {task_id}")
                
                # 等待几秒后检查任务状态
                time.sleep(2)
                return test_task_status(task_id)
            else:
                print(f"错误: {result.get('message')}")
                return False
        else:
            print(f"错误响应: {response.text}")
            return False
            
    except Exception as e:
        print(f"错误: {e}")
        return False

def test_task_status(task_id):
    """测试任务状态端点"""
    print(f"\n测试任务状态端点 (任务ID: {task_id})...")
    
    try:
        response = requests.get(f"{BASE_URL}/api/task/{task_id}", timeout=5)
        print(f"状态码: {response.status_code}")
        
        if response.status_code == 200:
            result = response.json()
            print(f"任务状态: {json.dumps(result, indent=2, ensure_ascii=False)}")
            return True
        elif response.status_code == 404:
            print("任务不存在")
            return False
        else:
            print(f"错误响应: {response.text}")
            return False
            
    except Exception as e:
        print(f"错误: {e}")
        return False

def test_list_tasks():
    """测试列出所有任务端点"""
    print("\n测试列出所有任务端点...")
    
    try:
        response = requests.get(f"{BASE_URL}/api/tasks", timeout=5)
        print(f"状态码: {response.status_code}")
        
        if response.status_code == 200:
            result = response.json()
            print(f"任务列表: {json.dumps(result, indent=2, ensure_ascii=False)}")
            return True
        else:
            print(f"错误响应: {response.text}")
            return False
            
    except Exception as e:
        print(f"错误: {e}")
        return False

def main():
    """主测试函数"""
    print("=" * 60)
    print("小云雀 Web API 服务器测试")
    print("=" * 60)
    
    # 测试健康检查
    if not test_health():
        print("\n❌ 健康检查失败，请先启动服务器")
        print(f"启动命令: python app.py")
        print(f"或使用 Docker: docker-compose up xiaoyunque")
        sys.exit(1)
    
    print("\n✅ 健康检查通过")
    
    # 测试列出任务
    if test_list_tasks():
        print("✅ 列出任务测试通过")
    else:
        print("⚠️ 列出任务测试失败（可能是服务器问题）")
    
    # 测试视频生成（由于需要真实的 cookies.json，可能会失败）
    print("\n注意：视频生成测试需要有效的 cookies.json 文件")
    print("如果没有有效的 cookies，测试可能会失败")
    
    try:
        import os
        if os.path.exists('cookies.json'):
            print("找到 cookies.json 文件，尝试视频生成测试...")
            if test_generate_video():
                print("✅ 视频生成测试通过")
            else:
                print("⚠️ 视频生成测试失败（可能是 cookies 无效）")
        else:
            print("未找到 cookies.json 文件，跳过视频生成测试")
            print("请将有效的 cookies.json 文件放在当前目录")
    except Exception as e:
        print(f"⚠️ 视频生成测试异常: {e}")
    
    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)
    
    # 显示 API 端点信息
    print("\n可用的 API 端点:")
    print(f"1. 健康检查: GET {BASE_URL}/api/health")
    print(f"2. 生成视频: POST {BASE_URL}/api/generate-video")
    print(f"3. 任务状态: GET {BASE_URL}/api/task/<task_id>")
    print(f"4. 列出任务: GET {BASE_URL}/api/tasks")
    print(f"5. 下载视频: GET {BASE_URL}/api/video/<task_id>")
    print(f"6. 清理任务: POST {BASE_URL}/api/cleanup")

if __name__ == '__main__':
    main()