# Linux 定时预览与备份

这些文件是待在教学主机安装的 systemd 模板。添加到仓库不会创建服务或定时任务。首次安装前先按[部署手册](../README.md)启动正式站，并确认同一主机的数据库与私有库连接有效。

## 固定路径与身份

| 项目 | 模板值 | 必须满足的条件 |
| --- | --- | --- |
| 稳定代码路径 | `/srv/teaching-space` | 已核验版本及其 `.venv/bin/python`，代码由部署账号管理，预览账号只读 |
| 预览账号 | `teacher`，UID/GID `10001` | 宿主机上创建的专用非特权用户；与 web 容器的 UID 一致 |
| 数据目录 | `/srv/teaching-data` | 首次部署就使用 `compose.host-data.yml` 绑定到 web 的 `/data`，目录和 SQLite 对 UID 10001 可写 |
| 预览容器运行时 | `unix:///run/user/10001/docker.sock` | `teacher` 自己的 rootless Docker；不使用 web 容器内的 Docker，也不把 socket 挂到 web |
| 备份目录 | `/srv/teaching-backups` | 已挂载独立的私有存储，目录对 UID 10001 可写 |
| 私有配置 | `deploy/.env`、`/etc/racheltoolkit/*.env` | root 可读；文件不得公开或加入 Git |
| 发布互斥锁 | `/srv/teaching-space/.local/deploy.lock` | 预先创建，`teacher:teacher`、0600；父目录 0700 且 teacher 可进入，发布账号也必须有访问权限 |

已有命名卷中的数据不能通过修改 Compose 配置直接切到空宿主目录。应先备份、隔离恢复并核对，再迁移到约定路径。模板中的路径、UID、用户名和运行时 socket 必须作为一组修改。

web 使用主机的普通 Docker daemon。只有预览账号使用 rootless daemon；两个 daemon 的镜像互不可见。备份 service 显式连接普通 daemon，并由 root 调用 Compose；这是一项主机 Docker 管理权限，不是非特权备份进程。

在首次部署时准备空的锁文件；已有文件时仅核对权限，不删除或替换正在使用的锁文件。root 执行发布和备份，预览进程以 UID 10001 执行，三者通过同一个 `flock` 文件互斥。若发布账号不是 root，应调整明确的共享组权限并同步模板，不能让发布与 worker 使用不同锁。

```sh
sudo install -d -o teacher -g teacher -m 0700 /srv/teaching-space/.local
sudo -u teacher touch /srv/teaching-space/.local/deploy.lock
sudo chmod 0600 /srv/teaching-space/.local/deploy.lock
```

## 准备预览进程

在宿主安装 Python 3.13、系统路径中的 Docker CLI 和该用户的 rootless Docker。预览用户的 Docker daemon 应作为其 **user service** 运行并开启 linger；不要用系统级 `docker.service` 的 `User=teacher` 来冒充 rootless daemon。参考 [Docker 官方 rootless 运维说明](https://docs.docker.com/engine/security/rootless/tips/)。

由部署管理员创建并锁定项目内环境；不使用正在运行的 web 容器内 Python：

```sh
cd /srv/teaching-space
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
```

使用已通过正常登录建立的 `teacher` 会话启用其 Docker user service，管理员再启用开机驻留。主机需提供 rootless Docker 所需的 subordinate UID/GID 和 cgroup v2；先确认其容器 CPU/内存/PID 限制可用。不要将该用户加入普通 Docker 的 `docker` 组。

```sh
# 在 teacher 登录会话中执行：
systemctl --user enable --now docker.service
DOCKER_HOST=unix:///run/user/10001/docker.sock docker info
DOCKER_HOST=unix:///run/user/10001/docker.sock docker build \
  -t teach-office:reviewed /srv/teaching-space/app/storage/office
DOCKER_HOST=unix:///run/user/10001/docker.sock docker image inspect \
  --format '{{.Id}}' teach-office:reviewed

# 由管理员执行：
sudo loginctl enable-linger teacher
```

将上一步的完整 `sha256:...` 镜像 ID 写入私有 `worker.env`。`run_renderer` 使用 `--pull=never`，因而 tag 或另一 daemon 中的镜像不能替代这一步。转换子容器仍沿用应用层的禁网、只读根目录、无主机挂载、无云盘凭证和资源限制。

先将示例复制到私有配置目录，再通过主机上的编辑器填写；命令行和日志中不输出部署密钥。`worker.env` 只放宿主路径及运行时覆盖，Seafile 和 Django 密钥继续从同一个 `deploy/.env` 读取。systemd 由服务管理器读配置后降权，因此无需给 `teacher` 开放这些 env 文件的读取权限。

```sh
sudo install -d -m 0700 /etc/racheltoolkit
sudo install -m 0600 deploy/systemd/worker.env.example /etc/racheltoolkit/worker.env
sudo install -m 0600 deploy/systemd/backup.env.example /etc/racheltoolkit/backup.env
sudoedit /etc/racheltoolkit/worker.env /etc/racheltoolkit/backup.env
```

上述复制用于首次安装；已有私有配置时直接编辑现有文件，不用示例覆盖。env 文件采用 `KEY=value`，不写 `export`、命令替换或 shell 语句。使用的 Django 密钥建议为生成的十六进制随机字符串，避免 Compose 与 systemd 对特殊字符解析不同。`worker.env` 后读入，保证预览进程与 web 操作同一个 SQLite 文件，而不是误建项目内开发库。

## 验证后启用定时器

检查目录、身份、venv、发布锁和 rootless socket；这些条件必须实际满足。模板先检查 socket 和既有数据库可写，缺条件会明确失败。`ProtectSystem=strict` 仅允许该 worker 写数据目录和约定的锁文件，权限模型见 [systemd 官方说明](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)。

```sh
sudo systemd-analyze verify deploy/systemd/racheltoolkit-previews.service \
  deploy/systemd/racheltoolkit-previews.timer \
  deploy/systemd/racheltoolkit-backup.service \
  deploy/systemd/racheltoolkit-backup.timer
sudo install -m 0644 deploy/systemd/racheltoolkit-previews.service \
  deploy/systemd/racheltoolkit-previews.timer \
  deploy/systemd/racheltoolkit-backup.service \
  deploy/systemd/racheltoolkit-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start racheltoolkit-previews.service
sudo systemctl start racheltoolkit-backup.service
sudo journalctl -u racheltoolkit-previews.service -u racheltoolkit-backup.service --since today
```

使用虚构课程上传真实 Word/PPT，确认预览由待处理变成就绪，原件和 PDF 可读；空队列退出成功不能代替转换验收。检查备份日志成功及新目录中的清单，再按部署手册恢复到独立目录并核对。完成这两项后才启用：

```sh
sudo systemctl enable --now racheltoolkit-previews.timer racheltoolkit-backup.timer
sudo systemctl list-timers 'racheltoolkit-*'
```

预览每批最多 20 项，前一批结束一分钟后再次执行；单一 service 不会并行叠加自身。预览与备份等待发布锁最多 55 秒，超时明确失败。批次失败会记为 failed，后续 timer 仍继续处理新队列；失败预览由教师明确重试，或维护者对指定 asset 使用 `--retry-failed`。备份按北京时间每日 02:15 加最多 5 分钟随机延迟执行，主机关机错过时补执行一次；若因锁超时失败，运维须在发布结束后手动补跑。服务失败需由运维查看 journal 和 `systemctl --failed`，这里没有擅自安装任何消息通知服务。

只要 `.local/deploy-incomplete` 存在，这两个 service 都会跳过执行。`guard-maintenance.sh` 在取得锁后再次检查，防止等待锁期间发布失败却继续运行。该标记表示发布未完成，不能把跳过当成预览或备份成功，也不能为恢复定时器直接删除标记；先排查迁移或健康检查失败，并由发布流程在验证完成后清除。已持有发布锁的部署脚本应直接调用 `deploy/backup.sh`，不能再通过这个带锁的 backup service 递归申请同一把锁。不要绕过 unit 单独运行 guard 脚本；它不自行申请锁。

## 更新与停止

更新应用及宿主 venv 时，先停预览 timer，并等待已有预览 service 自然结束，再切换代码和迁移。发布脚本若进入维护阶段，不应让新旧代码的 worker 同时运行。更新后重跑一份虚构 Word 的转换，再恢复 timer。备份运行中不要切换其代码目录；维护窗口先执行一次并核验备份。

```sh
sudo systemctl stop racheltoolkit-previews.timer racheltoolkit-backup.timer
systemctl is-active racheltoolkit-previews.service racheltoolkit-backup.service
# 如仍为 activating，等待批次结束，再更新。
# 更新完成且手动复测成功后：
sudo systemctl start racheltoolkit-previews.timer racheltoolkit-backup.timer
```

本目录提供的模板和静态检查不等同于生产运行证据。正式签收还需登记教学主机、实际 UID、同一数据库路径、rootless 镜像 ID、预览结果、备份还原结果及开机恢复后的定时器状态。
