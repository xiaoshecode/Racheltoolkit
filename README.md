# 吉老师个人教学空间

[公开课程入口](https://xiaoshecode.github.io/Racheltoolkit/)使用 GitHub Pages。登录、课程发布、整版回执、教师批阅、标准答案和个人错题由 Django 教学站提供。SQLite 保存业务记录，文件可使用服务器私有持久目录或通过服务器代理访问私有 Seafile 资料库。

## Codespaces 在线试用

从[创建 Racheltoolkit Codespace](https://codespaces.new/xiaoshecode/Racheltoolkit)启动，等待自动安装和启动完成，在 Ports 面板打开 8000 的地址。首次空站的管理员账号和随机密码只保存在私有工作区 `.local/codespaces/access.txt`，用它登录 `/admin/` 建课程和班级；以后启动保留账号与数据。需要学生访问时，将 8000 的 Port Visibility 设为 Public，重启 Codespace 后需重新分享。详细命令、Pages 接入和备份见[在线试用说明](deploy/README.md#codespaces-在线试用)。

这是按需开启的教学试用环境。GitHub 会在闲置超时或单次运行满 12 小时后停止 Codespace；全天候教学使用下方生产部署方案。[运行期限](https://docs.github.com/en/codespaces/about-codespaces/understanding-the-codespace-lifecycle)

## 本机验收

在 Windows 仓库根目录执行 `pwsh -NoProfile -File scripts/start-review.ps1`，打开 [本机教学空间](http://127.0.0.1:8765/)。脚本使用隔离的虚构班级和项目内的 Python 环境。吉老师、甲、乙的随机体验密码在本机 `.local/review/access.txt` 中。

- 访客从课程目录进入「基础数学 · 函数与图像」，查看第一章的两页讲义。
- 甲登录后在「课后练习」逐页选图、调序并提交，打开整版回执。主动再交 PDF 时会得到新版本。个人空间中已有一份已发布批阅，可以从错题回到第 2 页。
- 乙进入同一章能读相同讲义，也能读已截止作业的班级标准答案。甲的回执和批阅地址对乙返回 404。
- 教师从课程编辑和成绩册布置作业、查未交名单、代交、按版本批阅、发布成绩。到截止时，从作业的「管理标准答案」手动公布本班答案。

## 从干净环境启动

仓库根目录运行以下 PowerShell 命令，使用 Python 3.13。

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

## 部署

生产镜像、HTTPS 代理、云盘探测、备份和兼容版本回退见 [教学站部署手册](deploy/README.md)。Pages 使用仓库变量 `TEACH_ORIGIN` 接入已验证的教学主机。当前站点是否开放真实教学，以项目验收台账的 M7c 和 M8 记录为准。

自有云服务器使用本地私有文件存储时，按[单机部署与公网 IP HTTPS](deploy/LOCAL.md)准备持久化目录、文档预览和异机备份。GitHub Pages 保留为入口，后端地址通过 `TEACH_ORIGIN` 切换。

已准备 **Deploy teaching host** 手动工作流、迁移前快照和 HTTPS 健康检查，以及[后台预览与每日备份定时器](deploy/systemd/README.md)。工作流需要已有 Linux 主机和私有配置，不会自动提供服务器。师生实际操作见[使用指南](docs/user-guide.md)。

