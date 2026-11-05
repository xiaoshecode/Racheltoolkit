# 教学站部署与恢复

## 公开入口和教学站

公开入口位于 [Racheltoolkit Pages](https://xiaoshecode.github.io/Racheltoolkit/)。工作流仅上传 `portal/`。教学站负责课程目录的当前发布修订、账号会话、提交和文件授权。仓库变量 `TEACH_ORIGIN` 留空时，Pages 显示“教学站地址尚未配置”。在教学站 HTTPS 验证通过后设置此变量并重新运行 Public portal 工作流即可连接。教师此后发布课件无需重建 Pages。

## Codespaces 在线试用

Codespaces 可以按需运行完整 Django 教学站、SQLite、私有文件存储及隔离的 Office 预览，适合演示和试用。它会闲置停止，单次运行最多 12 小时，不能保证 24 小时在线；停止后 Pages 仍在，但教学站不可访问。[GitHub 生命周期与数据保存](https://docs.github.com/en/codespaces/about-codespaces/understanding-the-codespace-lifecycle)

### 首次打开和分享

1. 打开[创建 Racheltoolkit Codespace](https://codespaces.new/xiaoshecode/Racheltoolkit)，选择 `main` 和 2 核机型，按 GitHub 页面显示核对账号用量和预算。仓库配置会自动安装 Python 依赖、构建 Office 镜像、迁移空库、收集静态资源并启动服务。
2. 在编辑器中私下打开 `.local/codespaces/access.txt`。空站只创建一次 `rachel-admin` 随机密码，用它登录 Ports 面板中 8000 地址的 `/admin/`，首次登录后更换密码并建立课程、班级和教师授权。后续 setup、重启不会重置账号或密码；不要把该文件提交到 Git、贴入聊天或分享给学生。
3. 8000 默认 Private。需要师生在线访问时，在 Ports 面板将该端口的 **Port Visibility → Public**，再复制 HTTPS 地址。公开端口允许访客打开站点，个人教学数据仍由应用登录和权限控制。Codespace 重启或移除再添加端口后会恢复 Private，每次分享前重新核对。[GitHub 端口安全规则](https://docs.github.com/en/codespaces/reference/security-in-github-codespaces)
4. **端口协议保留 HTTP。** 浏览器使用 `https://CODESPACE_NAME-8000.app.github.dev`，外层 TLS 由 GitHub 处理；内部 Waitress 接收 HTTP 并按 HTTPS 语义生成链接和安全 Cookie。不要将 Ports 的 Change Port Protocol 改成 HTTPS，应用内部没有 TLS 监听器。[端口与 HTTPS 转发说明](https://docs.github.com/en/codespaces/developing-in-a-codespace/forwarding-ports-in-your-codespace)

在匿名窗口确认公开地址的 `/health/` 和已发布课程可访问后，可将该 HTTPS 地址设为仓库变量 `TEACH_ORIGIN`，重新运行 **Public portal**，保持 Pages 为统一入口。创建另一个 Codespace 后地址会改变，需要重新核验和更新变量。

### 启停、更新和备份

以下命令在 Codespace 的仓库根目录执行。首次创建自动运行 setup，每次启动自动运行 start；手动更新前先 stop。

| 操作 | 命令 | 作用 |
| --- | --- | --- |
| 准备或更新 | `python3 scripts/codespaces.py setup` | 安装依赖、准备预览镜像；已有库先生成快照，再迁移和收集静态文件 |
| 启动服务 | `python3 scripts/codespaces.py start` | 启动网页和预览工作进程，输出试用站地址 |
| 停止服务 | `python3 scripts/codespaces.py stop` | 停止本项目网页和预览进程，保留数据 |
| 生成备份 | `python3 scripts/codespaces.py backup` | 校验并保存数据库、文件及清单，输出完整快照目录 |

`stop` 只停止应用。用完后还需在 [Your Codespaces](https://github.com/codespaces) 对该环境选择 **Stop codespace**，才停止计算用量；关闭浏览器标签不会立即停止环境。默认闲置期限为 30 分钟，可在个人设置中调整为 5–240 分钟，单次最长运行期限仍为 12 小时。[停止方式](https://docs.github.com/en/codespaces/developing-in-a-codespace/stopping-and-starting-a-codespace)、[闲置设置](https://docs.github.com/en/codespaces/setting-your-user-preferences/setting-your-timeout-period-for-github-codespaces)

SQLite、私有上传、密钥和日志放在工作区 `.local/codespaces/`；备份位于其 `backups/`。这些目录被 Git 忽略，在同一 Codespace 停止、恢复和重建后保留。**工作区内快照不是独立备份：必须把完整快照目录私下下载到本机或另一处私有存储。删除 Codespace（包括到期自动删除）会同时删除原数据和其中的备份，公开 GitHub 仓库不会保存它们。** 恢复时使用本文后面的隔离恢复流程，先验证快照，不直接覆盖运行中的数据库。[GitHub 数据生命周期](https://docs.github.com/en/codespaces/about-codespaces/understanding-the-codespace-lifecycle)

### Pro 用量和环境边界

按 GitHub 当前说明，个人 Pro 每月包含 **180 核时和 20 GB-month 存储**，由该账号下的 Codespaces 共享；若计算额度全部用于一个 2 核环境，约能运行 90 小时。停止的环境仍占存储用量，实际剩余额度、付款方式和预算以个人 Billing 页面为准。本项目不假定账号已开通 Pro、还有免费余额或允许超额付费，也不会调整预算或自动续开环境。[GitHub Codespaces 计费说明](https://docs.github.com/en/billing/concepts/product-billing/github-codespaces)

试用使用独立的 `config.codespaces`：关闭 DEBUG，只允许本环境精确主机名，默认把文件保存在私有本地目录。确有私有 Seafile 时，才通过 Codespaces secrets 显式设置 `TEACH_STORAGE_BACKEND=seafile` 和完整的库配置。Office 转换仍在无网络、无主机挂载且有资源限制的子容器内运行；网页和工作进程不继承 GitHub 访问令牌。`config.production` 仍要求私有 Seafile，以下生产主机流程保持独立。

## 生产主机

使用自有云服务器、私有本地文件目录和公网 IP HTTPS 时，参见[单机部署说明](LOCAL.md)。以下流程适用于私有 Seafile 存储；两种配置应分别选择，不能直接更换已有数据库的文件存储位置。

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

## 从 GitHub 发起后端发布

仓库的 **Deploy teaching host** 工作流可从 Actions 手动运行，只接受 `main`。它先运行应用与部署回归，再通过校验主机公钥的 SSH 发布本次完整 SHA。推送 `main` 仍只自动更新 Pages；新增工作流不会创建或购买服务器。

主机首次准备按前述章节完成：稳定检出到 `/srv/teaching-space`，安装 Python 3.13、Git、Docker Compose；配置域名与私有 `deploy/.env`，建立独立备份目录。`.env` 的 `TEACH_BACKUP_DIR` 指向该目录。程序默认使用 `/data/school.sqlite3`，已有数据必须继续使用原 Compose 项目名及数据卷。采用宿主预览进程时，在第一次部署就选择 `compose.host-data.yml`，再按 [systemd 运维说明](systemd/README.md) 配置专用账号、rootless 转换、共同发布锁及定时器。Django 密钥、云盘令牌留在主机，不上传到 Actions。

在 GitHub 仓库的 `teaching-production` Environment 中配置：

| 类型 | 名称 | 内容 |
| --- | --- | --- |
| Variable | `DEPLOY_HOST` | 教学主机 DNS 名称或 IPv4，SSH 使用 22 端口 |
| Variable | `DEPLOY_USER` | 已有部署账号，具有源码、Compose 及运维定时器的管理权限；随仓库模板使用 root |
| Variable | `DEPLOY_PATH` | 主机固定检出根目录，如 `/srv/teaching-space` |
| Variable | `TEACH_ORIGIN` | 教学站的 HTTPS 地址，仅作本次部署链接 |
| Secret | `DEPLOY_SSH_KEY` | 上述账号的专用 SSH 私钥 |
| Secret | `DEPLOY_KNOWN_HOSTS` | 经主机控制台核对指纹的完整 OpenSSH known_hosts 条目 |

不要在工作流里用未经核对的 `ssh-keyscan` 输出代替主机身份验证。源码目录必须干净；私有 `.env`、数据和 `.local` 均保持 Git 忽略。部署不会覆盖本地改动。工作流通过环境变量接收配置，并限制 SSH 目标和路径字符，避免将输入当作脚本执行。参见 [GitHub 脚本输入说明](https://docs.github.com/en/actions/concepts/security/script-injections) 和 [Compose 项目名与卷隔离](https://docs.docker.com/compose/how-tos/project-name/)。

`release.py` 与预览、定时备份共用 `.local/deploy.lock`：确认 SHA 属于远端 main，构建候选镜像并运行生产检查，停止旧 web，用**旧镜像、旧环境、旧数据卷**写出独立快照后才迁移。它拒绝更换数据卷、数据库位置或私有资料库；仅更换令牌可以正常发布。如果找不到旧容器却发现已有数据库，必须先按人工恢复流程接回旧容器，不能跳过快照。

发布通过可信 HTTPS 健康检查后，原子更新私有 `.env` 的 `TEACH_RELEASE`、记录 `.local/current-release`，恢复已启用的定时器并清除维护标记。后续备份和手工 Compose 操作会继续使用同一镜像。全新服务器仍需建立管理员、班级、课程和名册，并完成专用云盘读写及真实双入口检查；健康页成功不等于试教签收。服务器上也可从检出根目录直接运行 `python3 deploy/release.py 完整40位SHA`。

**失败处理：** 迁移开始之前失败，程序恢复原代码并重启已停止的旧容器。迁移开始之后失败，保持 web 停止及 `.local/deploy-incomplete` 标记，预览和定时备份不会继续写入。它不会自动恢复旧数据库或丢弃新提交。此时先从主机私下检查失败命令，保全当前数据库和文件；按后文的隔离恢复流程确认迁移状态，修复后在同一锁下完成剩余迁移、启动及 HTTPS 检查。确认库版本、回执、附件、生产镜像与 `.env` 一致后，才由维护者移除标记并重启定时器。不要用删除标记来绕过未查明的失败。

后端验收通过后，把 HTTPS 地址同时设置为**仓库级** `TEACH_ORIGIN`（Pages 使用仓库变量，不读取 teaching-production 的环境变量），重新运行 **Public portal**，再执行 `verify_entrypoints.py`。账户、成绩和文件始终保存在后端，公开发布包仍只含 `portal/`。

## 收件对账

`python app/manage.py reconcile_submissions` 列出未完成版本。加 `--repair` 检查云端大小及哈希，只在全部文件确认后签发回执。云端明确缺页时，学生可用原清单和原编号重试。只有操作员已核实旧执行者及远端上传均结束时，才对指定提交加 `--submission UUID --writer-stopped --repair`；冲突文件保持隔离。

## 备份、隔离恢复和回退

1. 从持有数据库和文件读取权限的服务进程，定期运行 `python app/manage.py snapshot_teaching /private-backup/NEW-SNAPSHOT`，目的地须为独立私有存储上尚不存在的目录。它使用 SQLite 在线备份，依该快照逐件读取原件／派生件并校验大小与 SHA-256。完整结束后交付原件树、数据库及清单。操作系统应限制目录权限并按校方政策加密、轮换。保存镜像 SHA 和验证时刻。
   生产 Compose 主机可将独立私有盘挂载到 /srv/teaching-backups，预先授权 UID 10001 写入。先运行 `TEACH_BACKUP_DIR=/srv/teaching-backups sh deploy/backup.sh` 并做一次还原。验证后，在能调用该项目 Compose 的受控账号上安装每日任务，例如 `15 2 * * * TEACH_BACKUP_DIR=/srv/teaching-backups /bin/sh /srv/teaching-space/deploy/backup.sh`。主机时区决定触发时刻，归档使用 UTC 命名。监控非零退出，另行核对备份盘配额与非现场副本。

2. 运行 `python app/manage.py restore_teaching /private-backup/NEW-SNAPSHOT /isolated/NEW-RESTORE`。恢复只能写到独立新目录。它复核清单、数据库完整性及外键、每件文件的摘要，在隔离目录创建本地文件替身并保留原库标识。用 `TEACH_DATA_DIR=/isolated/NEW-RESTORE TEACH_STORAGE_BACKEND=local DJANGO_SETTINGS_MODULE=config.settings` 在本机回环接口检验指定回执、批注页码、错题／答案修订。清除旧 `TEACH_DB` 覆盖，以恢复目录的数据库为准。
3. 程序回退优先切回兼容镜像，继续使用**当前**数据库及私有资料库。核对快照之后的提交仍可读。若迁移不兼容，先暂停新写入并为当前库另做保全快照，在隔离环境逐项核对和补录后恢复对外服务。保留旧镜像直至复验。
4. 在仓库根运行 `python app/manage.py test operations.test_concurrency operations.test_recovery operations.test_upgrade -v 2` 可复演并发、隔离还原和旧单文件迁移。可选设 `TEACH_ROLLBACK_APP` 为兼容旧版本的 `app` 目录；恢复测试会用旧代码实际读回快照后的新增版本，再用当前代码复核。
