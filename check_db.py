import sqlite3, os, sys

# 自动检测数据库路径：优先从环境变量/当前目录，回退到 data/ 子目录
script_dir = os.path.dirname(os.path.abspath(__file__))
db_path = os.environ.get('DB_PATH', os.path.join(script_dir, 'data', 'xiaoyunque_tasks.db'))

print(f'DB path: {db_path}')
print(f'DB exists: {os.path.exists(db_path)}')

if not os.path.exists(db_path):
    print('ERROR: 数据库文件不存在')
    sys.exit(1)

conn = sqlite3.connect(db_path)
cur = conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cur.fetchall()
print(f'Tables: {tables}')

for t in tables:
    print(f'\nTable: {t[0]}')
    cur.execute(f'SELECT * FROM "{t[0]}" LIMIT 5')
    rows = cur.fetchall()
    for r in rows:
        print(f'  {r}')

conn.close()
