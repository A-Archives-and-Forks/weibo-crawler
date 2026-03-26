from weibo import Weibo, handle_config_renaming, get_config as get_weibo_config
import const
import logging
import logging.config
import os
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import sqlite3
import json
from concurrent.futures import ThreadPoolExecutor
import threading
import uuid
import time
from datetime import datetime

# 1896820725 天津股侠 2024-12-09T16:47:04

# 如果日志文件夹不存在，则创建
if not os.path.isdir("log/"):
    os.makedirs("log/")
logging_path = os.path.split(os.path.realpath(__file__))[0] + os.sep + "logging.conf"
logging.config.fileConfig(logging_path)
logger = logging.getLogger("api")

# 从 config.json 加载配置（复用 weibo.py 中的 get_config 函数）
def load_config():
    """加载配置文件"""
    try:
        return get_weibo_config()
    except Exception as e:
        logger.error(f"加载配置文件失败: {e}")
        raise

# 初始化时加载配置
base_config = load_config()

def get_database_path():
    """获取SQLite数据库路径"""
    return base_config.get("sqlite_db_path", "weibodata.db")

app = Flask(__name__)
CORS(app)
app.config['JSON_AS_ASCII'] = False
app.config['JSONIFY_MIMETYPE'] = 'application/json;charset=utf-8'

schedule_config = {
    'enabled': False,
    'interval': 10,
    'next_run': None
}

# 添加线程池和任务状态跟踪
executor = ThreadPoolExecutor(max_workers=1)  # 限制只有1个worker避免并发爬取
tasks = {}  # 存储任务状态
MAX_TASKS = 100  # 最大保留任务数，防止内存泄漏

# 在executor定义后添加任务锁相关变量
current_task_id = None
task_lock = threading.Lock()

def cleanup_old_tasks():
    """清理旧任务，保留最近的MAX_TASKS个任务"""
    if len(tasks) <= MAX_TASKS:
        return
    
    # 按创建时间排序，删除最旧的任务
    sorted_tasks = sorted(
        tasks.items(),
        key=lambda x: x[1].get('created_at', ''),
        reverse=True
    )
    
    # 保留最近的MAX_TASKS个，删除其余的
    for task_id, _ in sorted_tasks[MAX_TASKS:]:
        if task_id != current_task_id:  # 不删除当前运行的任务
            del tasks[task_id]
    
    logger.info(f"已清理旧任务，当前任务数: {len(tasks)}")

def get_running_task():
    """获取当前运行的任务信息"""
    if current_task_id and current_task_id in tasks:
        task = tasks[current_task_id]
        if task['state'] in ['PENDING', 'PROGRESS']:
            return current_task_id, task
    return None, None

def get_config(user_id_list=None):
    """获取配置，允许动态设置user_id_list"""
    current_config = base_config.copy()
    if user_id_list:
        current_config['user_id_list'] = user_id_list
    handle_config_renaming(current_config, oldName="filter", newName="only_crawl_original")
    handle_config_renaming(current_config, oldName="result_dir_name", newName="user_id_as_folder_name")
    return current_config

def run_refresh_task(task_id, user_id_list=None):
    global current_task_id
    try:
        tasks[task_id]['state'] = 'PROGRESS'
        tasks[task_id]['progress'] = 0
        
        config = get_config(user_id_list)
        wb = Weibo(config)
        tasks[task_id]['progress'] = 50
        
        wb.start()  # 爬取微博信息
        tasks[task_id]['progress'] = 100
        tasks[task_id]['state'] = 'SUCCESS'
        tasks[task_id]['result'] = {"message": "微博列表已刷新"}
        
    except Exception as e:
        tasks[task_id]['state'] = 'FAILED'
        tasks[task_id]['error'] = str(e)
        logger.exception(e)
    finally:
        with task_lock:
            if current_task_id == task_id:
                current_task_id = None
            cleanup_old_tasks()  # 清理旧任务

@app.route('/')
def index():
    return send_from_directory('frontend', 'index.html')

@app.route('/refresh', methods=['POST'])
def refresh():
    global current_task_id
    
    # 获取请求参数
    data = request.get_json()
    user_id_list = data.get('user_id_list') if data else None
    
    # 验证参数
    if not user_id_list or not isinstance(user_id_list, list):
        return jsonify({
            'error': 'Invalid user_id_list parameter'
        }), 400
    
    # 检查是否有正在运行的任务
    with task_lock:
        running_task_id, running_task = get_running_task()
        if running_task:
            return jsonify({
                'task_id': running_task_id,
                'status': 'Task already running',
                'state': running_task['state'],
                'progress': running_task['progress']
            }), 409  # 409 Conflict
        
        # 创建新任务
        task_id = str(uuid.uuid4())
        tasks[task_id] = {
            'state': 'PENDING',
            'progress': 0,
            'created_at': datetime.now().isoformat(),
            'user_id_list': user_id_list
        }
        current_task_id = task_id
        
    executor.submit(run_refresh_task, task_id, user_id_list)
    return jsonify({
        'task_id': task_id,
        'status': 'Task started',
        'state': 'PENDING',
        'progress': 0,
        'user_id_list': user_id_list
    }), 202

@app.route('/task/<task_id>', methods=['GET'])
def get_task_status(task_id):
    task = tasks.get(task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
        
    response = {
        'state': task['state'],
        'progress': task['progress']
    }
    
    if task['state'] == 'SUCCESS':
        response['result'] = task.get('result')
    elif task['state'] == 'FAILED':
        response['error'] = task.get('error')
        
    return jsonify(response)

@app.route('/weibos', methods=['GET'])
def get_weibos():
    try:
        conn = sqlite3.connect(get_database_path())
        cursor = conn.cursor()
        # 按created_at倒序查询所有微博
        cursor.execute("SELECT * FROM weibo ORDER BY created_at DESC")
        columns = [column[0] for column in cursor.description]
        weibos = []
        for row in cursor.fetchall():
            weibo = dict(zip(columns, row))
            weibos.append(weibo)
        conn.close()
        res1 = json.dumps(weibos, ensure_ascii=False)
        print(res1)
        res = jsonify(weibos)
        print(res)
        return res, 200
    except Exception as e:
        logger.exception(e)
        return {"error": str(e)}, 500

@app.route('/weibos/<weibo_id>', methods=['GET'])
def get_weibo_detail(weibo_id):
    try:
        conn = sqlite3.connect(get_database_path())
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM weibo WHERE id=?", (weibo_id,))
        columns = [column[0] for column in cursor.description]
        row = cursor.fetchone()
        conn.close()
        
        if row:
            weibo = dict(zip(columns, row))
            return jsonify(weibo), 200
        else:
            return {"error": "Weibo not found"}, 404
    except Exception as e:
        logger.exception(e)
        return {"error": str(e)}, 500

@app.route('/config/users', methods=['GET'])
def get_user_list():
    try:
        user_id_list = base_config.get('user_id_list', [])
        if isinstance(user_id_list, str):
            if os.path.exists(user_id_list):
                with open(user_id_list, 'r') as f:
                    user_ids = [line.strip() for line in f if line.strip()]
            else:
                user_ids = []
        else:
            user_ids = user_id_list
        
        users = [{'id': uid, 'name': uid} for uid in user_ids]
        return jsonify({'users': users}), 200
    except Exception as e:
        logger.exception(e)
        return jsonify({'error': str(e)}), 500

@app.route('/config/users', methods=['POST'])
def add_user():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        if not user_id:
            return jsonify({'error': 'user_id is required'}), 400
        
        user_id_list = base_config.get('user_id_list', [])
        if isinstance(user_id_list, str):
            if os.path.exists(user_id_list):
                with open(user_id_list, 'r') as f:
                    user_ids = [line.strip() for line in f if line.strip()]
            else:
                user_ids = []
        else:
            user_ids = list(user_id_list)
        
        if user_id not in user_ids:
            user_ids.append(user_id)
            
            user_id_file = base_config.get('user_id_list')
            if isinstance(user_id_file, str) and os.path.exists(user_id_file):
                with open(user_id_file, 'a') as f:
                    f.write(f'\n{user_id}')
            else:
                base_config['user_id_list'] = user_ids
        
        return jsonify({'success': True, 'user_id': user_id}), 200
    except Exception as e:
        logger.exception(e)
        return jsonify({'error': str(e)}), 500

@app.route('/config/users/<user_id>', methods=['DELETE'])
def delete_user(user_id):
    try:
        user_id_list = base_config.get('user_id_list', [])
        if isinstance(user_id_list, str):
            if os.path.exists(user_id_list):
                with open(user_id_list, 'r') as f:
                    user_ids = [line.strip() for line in f if line.strip()]
            else:
                user_ids = []
        else:
            user_ids = list(user_id_list)
        
        if user_id in user_ids:
            user_ids.remove(user_id)
            
            user_id_file = base_config.get('user_id_list')
            if isinstance(user_id_file, str) and os.path.exists(user_id_file):
                with open(user_id_file, 'w') as f:
                    f.write('\n'.join(user_ids))
            else:
                base_config['user_id_list'] = user_ids
        
        return jsonify({'success': True}), 200
    except Exception as e:
        logger.exception(e)
        return jsonify({'error': str(e)}), 500

@app.route('/config/schedule', methods=['GET'])
def get_schedule():
    return jsonify(schedule_config), 200

@app.route('/config/schedule', methods=['POST'])
def update_schedule():
    try:
        data = request.get_json()
        
        if 'interval' in data:
            schedule_config['interval'] = max(1, min(1440, int(data['interval'])))
        
        if 'enabled' in data:
            schedule_config['enabled'] = bool(data['enabled'])
        
        if schedule_config['enabled']:
            next_run = datetime.now().timestamp() + schedule_config['interval'] * 60
            schedule_config['next_run'] = datetime.fromtimestamp(next_run).isoformat()
        else:
            schedule_config['next_run'] = None
        
        return jsonify(schedule_config), 200
    except Exception as e:
        logger.exception(e)
        return jsonify({'error': str(e)}), 500

@app.route('/stats', methods=['GET'])
def get_stats():
    try:
        conn = sqlite3.connect(get_database_path())
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM weibo")
        total_weibos = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(DISTINCT user_id) FROM weibo")
        total_users = cursor.fetchone()[0]
        
        today = datetime.now().strftime('%Y-%m-%d')
        cursor.execute("SELECT COUNT(*) FROM weibo WHERE created_at LIKE ?", (f'{today}%',))
        today_weibos = cursor.fetchone()[0]
        
        completed_tasks = sum(1 for t in tasks.values() if t['state'] == 'SUCCESS')
        
        conn.close()
        
        return jsonify({
            'totalWeibos': total_weibos,
            'totalUsers': total_users,
            'todayWeibos': today_weibos,
            'completedTasks': completed_tasks
        }), 200
    except Exception as e:
        logger.exception(e)
        return jsonify({
            'totalWeibos': 0,
            'totalUsers': 0,
            'todayWeibos': 0,
            'completedTasks': 0
        }), 200

def schedule_refresh():
    """定时刷新任务"""
    while True:
        try:
            if schedule_config['enabled']:
                running_task_id, running_task = get_running_task()
                if not running_task:
                    task_id = str(uuid.uuid4())
                    tasks[task_id] = {
                        'state': 'PENDING',
                        'progress': 0,
                        'created_at': datetime.now().isoformat(),
                        'user_id_list': base_config['user_id_list']
                    }
                    with task_lock:
                        global current_task_id
                        current_task_id = task_id
                    executor.submit(run_refresh_task, task_id, base_config['user_id_list'])
                    logger.info(f"Scheduled task {task_id} started")
                    
                    next_run = datetime.now().timestamp() + schedule_config['interval'] * 60
                    schedule_config['next_run'] = datetime.fromtimestamp(next_run).isoformat()
                
                time.sleep(schedule_config['interval'] * 60)
            else:
                time.sleep(60)
        except Exception as e:
            logger.exception("Schedule task error")
            time.sleep(60)

if __name__ == "__main__":
    # 启动定时任务线程
    scheduler_thread = threading.Thread(target=schedule_refresh, daemon=True)
    scheduler_thread.start()
    
    logger.info("服务启动")
    # 启动Flask应用
    app.run(debug=True, use_reloader=False, port=8888)