# 教学站部署与恢复

## 公开入口和教学站

公开入口位于 [Racheltoolkit Pages](https://xiaoshecode.github.io/Racheltoolkit/)。工作流仅上传 `portal/`。教学站负责课程目录的当前发布修订、账号会话、提交和文件授权。仓库变量 `TEACH_ORIGIN` 留空时，Pages 显示“教学站地址尚未配置”。在教学站 HTTPS 验证通过后设置此变量并重新运行 Public portal 工作流即可连接。教师此后发布课件无需重建 Pages。

## 生产主机

使用有持久化磁盘的 Linux 主机、Docker Compose、指向主机的 HTTPS 域名和独立的私有备份目录。让域名解析指向服务器，开放 80、443 给 Caddy。应用容器只通过内部网络提供 WSGI。数据库保存在持久卷 `teaching-data`；反向代理只能读取单独的公共静态卷。

1. 检出已通过 CI 的 SHA，保存运行镜像的版本和回退版本。复制 `deploy/.env.example` 为 `deploy/.env`，使用私有权限填写真实域名、长随机 Django 密钥、库标识和专用 Seafile 库令牌。不要把真实值提交到 Git。
若预览工作进程运行在同一 Linux 主机上，在 deploy/.env 增加 `COMPOSE_FILE=compose.yml:compose.host-data.yml` 和 `TEACH_PERSIST_DIR=/srv/teaching-data`。先建立该私有目录，授予服务账号 UID 10001 读写权限；这会把该目录挂载到容器 /data。首次配置即确定数据位置，已有数据库时按恢复流程迁移并核对，不能直接切换到空目录。

2. 在一个私有 Seafile 资料库预建 `公开课件/`、`学生作业/`、`教师备课/`、`教学归档/`、`接口测试/`。库所有者撤销旧根分享及多余授权，并用全新匿名会话复核。若传输服务使用独立来源，经现场核对后填写 `SEAFILE_TRANSFER_ORIGINS`。
3. 在 `deploy/` 运行以下命令。已有数据时，先备份并核对迁移计划。启动命令会检查生产设置、应用迁移和收集静态文件。首次教学配置使用私有管理员账号从 `/admin/` 建学期、课程、班级和教师授权，再从教师工作台预检和导入名册。激活码通过已核验身份的私下途径发放。

```sh
docker compose --env-file .env build
docker compose --env-file .env run --rm web python app/manage.py migrate --plan
docker compose --env-file .env up -d
docker compose --env-file .env exec web python app/manage.py check --deploy --fail-level WARNING
docker compose --env-file .env exec web python app/manage.py createsuperuser
docker compose --env-file .env exec web python app/manage.py issue_school_code SCHOOL_ID activate
```

4. **预览工作进程。** 在同一持久磁盘的受控 Linux 主机上另起工作进程，使用相同代码版本、生产环境变量和库凭证。将 `TEACH_DB` 指向教学容器使用的同一 SQLite 文件，文件权限应允许专用服务账号读写。使用受限或 rootless 容器运行时，按 [Office 转换说明](../app/storage/office/README.md)构建镜像并固定 ID。定时执行 `python app/manage.py render_previews --limit 20`。转换子容器没有教学库凭证、网络或主机挂载。网页提供转换状态、原件下载和失败重试。
   例如将仓库安装在 /srv/teaching-space，让专用服务账号（UID 10001）使用项目内的 Linux Python 虚拟环境及 rootless Docker。由服务管理器注入 deploy/.env 中的服务设置，另外设置 `TEACH_DATA_DIR=/srv/teaching-data`、`TEACH_DB=/srv/teaching-data/school.sqlite3`、`DJANGO_SETTINGS_MODULE=config.production`、`TEACH_OFFICE_IMAGE` 和该账号的 `DOCKER_HOST`。可由 systemd timer 每分钟启动一次以下命令，服务的 WorkingDirectory 为 /srv/teaching-space。每次最多处理 20 项；进程退出或升级后未完成的任务可按租约重新认领。

   `/srv/teaching-space/.venv/bin/python app/manage.py render_previews --limit 20`

5. **真机复测。** 从教学主机注入与应用相同的专用库变量，在仓库根执行 `python deploy/verify_seafile.py --directory /接口测试` 和 `python deploy/verify_seafile.py --directory /接口测试 --roundtrip`。记录 read、upload、download、cleanup 和退出码。然后从校外网络验证 Pages 目录和同域课程页，使用虚构 A/B/J 完成收件、批阅、发布、越权拒绝；重启容器后复查原回执、成绩与附件。
6. **试教。** 校方确认资料保存范围后才使用真实名册。吉老师与 5–10 名学生分别走校园 Wi-Fi、手机流量和真机拍照。记录参与数、独立完成数、失败环节和复测。M7c 及小班试教通过后再扩到约 100 人。

### 从校外复核公开双入口

在教学站发布一个虚构课程和章节，核对 Pages 的 `TEACH_ORIGIN` 已更新。在能从校外访问两个域名的设备上，从仓库根目录运行：

```sh
python deploy/verify_entrypoints.py --teaching-origin https://已验证的教学站域名
```

脚本匿名读取真实 Pages 页面、资源和配置，并要求其中的教学站与操作者指定的地址一致。它用系统信任链校验 HTTPS，检查健康页、允许及拒绝的跨域来源、已发布章节链接，以及匿名访问个人页的登录跳转。退出码 0 表示公开入口检查通过；1 表示检查失败；2 表示尚缺教学站地址或已发布的合成章节。现场另行核对合成课件的原件哈希、登录后的 A/B/J 教学闭环、云盘读写及重启后数据。日志仅保留检查结果。

## 收件对账

`python app/manage.py reconcile_submissions` 列出未完成版本。加 `--repair` 检查云端大小及哈希，只在全部文件确认后签发回执。云端明确缺页时，学生可用原清单和原编号重试。只有操作员已核实旧执行者及远端上传均结束时，才对指定提交加 `--submission UUID --writer-stopped --repair`；冲突文件保持隔离。

## 备份、隔离恢复和回退

1. 从持有数据库和文件读取权限的服务进程，定期运行 `python app/manage.py snapshot_teaching /private-backup/NEW-SNAPSHOT`，目的地须为独立私有存储上尚不存在的目录。它使用 SQLite 在线备份，依该快照逐件读取原件／派生件并校验大小与 SHA-256。完整结束后交付原件树、数据库及清单。操作系统应限制目录权限并按校方政策加密、轮换。保存镜像 SHA 和验证时刻。
   生产 Compose 主机可将独立私有盘挂载到 /srv/teaching-backups，预先授权 UID 10001 写入。先运行 `TEACH_BACKUP_DIR=/srv/teaching-backups sh deploy/backup.sh` 并做一次还原。验证后，在能调用该项目 Compose 的受控账号上安装每日任务，例如 `15 2 * * * TEACH_BACKUP_DIR=/srv/teaching-backups /bin/sh /srv/teaching-space/deploy/backup.sh`。主机时区决定触发时刻，归档使用 UTC 命名。监控非零退出，另行核对备份盘配额与非现场副本。

2. 运行 `python app/manage.py restore_teaching /private-backup/NEW-SNAPSHOT /isolated/NEW-RESTORE`。恢复只能写到独立新目录。它复核清单、数据库完整性及外键、每件文件的摘要，在隔离目录创建本地文件替身并保留原库标识。用 `TEACH_DATA_DIR=/isolated/NEW-RESTORE TEACH_STORAGE_BACKEND=local DJANGO_SETTINGS_MODULE=config.settings` 在本机回环接口检验指定回执、批注页码、错题／答案修订。清除旧 `TEACH_DB` 覆盖，以恢复目录的数据库为准。
3. 程序回退优先切回兼容镜像，继续使用**当前**数据库及私有资料库。核对快照之后的提交仍可读。若迁移不兼容，先暂停新写入并为当前库另做保全快照，在隔离环境逐项核对和补录后恢复对外服务。保留旧镜像直至复验。
4. 在仓库根运行 `python app/manage.py test operations.test_concurrency operations.test_recovery operations.test_upgrade -v 2` 可复演并发、隔离还原和旧单文件迁移。可选设 `TEACH_ROLLBACK_APP` 为兼容旧版本的 `app` 目录；恢复测试会用旧代码实际读回快照后的新增版本，再用当前代码复核。
