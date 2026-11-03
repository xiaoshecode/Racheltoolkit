# 吉老师个人教学空间

Django 提供教学站，SQLite 保存教学记录。仓库根目录运行以下 PowerShell 命令，使用 Python 3.13。所有虚拟环境、下载缓存与本地数据库均位于项目内。

```powershell
New-Item -ItemType Directory -Force .local/tmp | Out-Null
$env:TEMP = (Resolve-Path .local/tmp).Path
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = "$PWD/.local/cache"
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python app/manage.py check
.venv/Scripts/python app/manage.py makemigrations --check --dry-run
.venv/Scripts/python app/manage.py migrate --plan
.venv/Scripts/python app/manage.py migrate
.venv/Scripts/python app/manage.py migrate
.venv/Scripts/python app/manage.py test
.venv/Scripts/python app/manage.py runserver 127.0.0.1:8000
```

健康检查地址为 `/health/`。开发库默认位于 `.local/school.sqlite3`。需要验证空库迁移时，在运行迁移前将 `TEACH_DB` 指向项目内一个尚不存在的 SQLite 文件；自动化测试另建文件型测试库，结束后由测试运行器清理。测试使用虚构学生和班级。

