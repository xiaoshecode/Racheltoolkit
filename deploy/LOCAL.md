# 单机私有存储、域名与公网 IP HTTPS

本方案在持久化 Linux 主机上运行 `config.production_local`。Django 使用私有本地文件存储和 SQLite，Caddy 提供 HTTPS 及公共静态资源。它使用独立的 `compose.local.yml`；现有 Seafile 生产配置保持独立。

默认使用域名和现有 `Caddyfile`，由 Caddy 自动申请并续期公开受信任的 HTTPS 证书。将域名的 DNS A 记录解析到服务器公网 IPv4，`TEACH_HOST` 填写与之对应的完整域名。[Caddy 自动 HTTPS](https://caddyserver.com/docs/automatic-https)

公网 IP 访问是可选模式。Let's Encrypt 的 IP 证书使用 `shortlived` profile，有效期为 160 小时，Caddy 自动续期。[证书说明](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability)、[Caddy TLS 配置](https://caddyserver.com/docs/caddyfile/directives/tls)。选择 `Caddyfile.ip` 时，配置会显式使用该签发方式，并设置 `default_sni` 为未发送 SNI 的 IP 请求选择证书。

| 访问方式 | `TEACH_HOST` | `TEACH_CADDY_FILE` |
| --- | --- | --- |
| 域名（默认） | 实际完整域名，例如 `teach.example.org` | `./Caddyfile` |
| 公网 IPv4 | 实际稳定公网 IPv4 | `./Caddyfile.ip` |

Caddy 镜像固定为 2.11.8 及其摘要；更新时应重新核对官方版本、摘要和 HTTPS 验证结果。

**IP HTTPS 是技术访问方式，不替代网站备案。** 阿里云说明，托管于中国内地的网站无论通过域名还是 IP 对外提供服务，都需要完成 ICP 备案；其代备案系统只支持域名备案。公开上线条件应依服务器实际地域及服务商要求核对。[阿里云备案要求](https://help.aliyun.com/zh/icp-filing/basic-icp-service/product-overview/icp-filing-requirements-for-a-regular-website)。

## 路径与权限

| 项目 | 约定 |
| --- | --- |
| 源码 | `/srv/teaching-space`，固定已核验版本 |
| Compose 项目 | `racheltoolkit`；所有操作保持相同项目名 |
| 私有配置 | `/srv/teaching-space/deploy/.env`，root 所有、0600 |
| 数据 | `/srv/teaching-data`，UID/GID 10001 所有、0700；挂载到 web 的 `/data` |
| SQLite | `/srv/teaching-data/school.sqlite3` |
| 原件及派生文件 | `/srv/teaching-data/private/storage/`，仅应用和专用工作进程访问 |
| 公共静态文件 | `racheltoolkit_public-static` 命名卷；Caddy 仅只读挂载该卷 |
| TLS 状态 | `racheltoolkit_caddy-data`、`racheltoolkit_caddy-config`；保留证书、ACME 账号和配置 |
| 快照 | `/srv/teaching-backups` 或另选的私有位置；必须另留异机副本 |

web 以 UID 10001 运行，根文件系统只读，临时目录使用受限 tmpfs，内存上限 512 MiB、8 个请求线程。只有 Caddy 暴露 TCP 80/443；web 位于私有容器网络，无公开端口、Docker socket 或主机控制权限。Caddy 的内存上限为 256 MiB，拥有单独的出站网络用于签发和续期。两个服务的容器日志均轮换保存。

## 首次安装或迁移

1. 核对 Linux、Docker Compose、公网 IPv4、剩余磁盘及已有服务。使用根域名时，添加主机记录 `@` 的 A 记录，值为服务器公网 IPv4；使用子域名时添加对应主机记录。确认公共 DNS 已正确解析；如设置 AAAA 记录，其 IPv6 也必须实际到达这台服务器。确认安全组、主机防火墙和上游网络允许 TCP 80/443；证书验证和续期都依赖这些端口及访问 ACME 服务的出站网络。公网 IP 改变时更新 DNS；采用 IP 访问时还需同步修改 HTTPS、应用主机名和 Pages 配置。
2. 将核验过的源码放到固定目录。首次空站可创建 `/srv/teaching-data`；迁移时先恢复到新的隔离目录，验证后再确定数据位置。已有目录不得用空目录覆盖，也不能通过改变 Compose 项目名或数据挂载来跳过迁移。
3. 将 `local.env.example` 复制为私有 `deploy/.env`，填写版本、实际域名和随机 Django 密钥，保留 `TEACH_CADDY_FILE=./Caddyfile`。选择公网 IP 访问时，改填实际公网 IPv4，并设 `TEACH_CADDY_FILE=./Caddyfile.ip`。`TEACH_STORAGE_BACKEND` 必须显式为 `local`，web 的数据路径固定为 `/data`。密钥使用至少 50 字符的高熵随机值，建议在主机生成 96 位十六进制字符串并直接写入私有配置，不在终端或日志打印。
4. 从 Codespace 迁移时，停止旧站写入，生成**最新**完整快照并下载核验。使用 `restore_teaching` 恢复到新的隔离目录，保留 `restored-library.txt`、数据库及 `private/storage/`；管理员账号及密码哈希随数据库保留。旧站初始密码卡应独立私下保存。验证恢复目录后，将其所有权设为 10001:10001、目录权限设为 0700，再填写 `TEACH_PERSIST_DIR`。

在 `deploy/` 目录执行以下命令。首次启动及升级都显式运行迁移和静态资源收集；容器重启只做生产设置检查并启动网页服务，不自动迁移数据库。

```sh
docker compose --env-file .env -f compose.local.yml config --quiet
docker compose --env-file .env -f compose.local.yml build web
docker compose --env-file .env -f compose.local.yml run --rm --no-deps web \
  python app/manage.py check --deploy --fail-level WARNING
docker compose --env-file .env -f compose.local.yml run --rm --no-deps web \
  python app/manage.py migrate --plan
# 已有数据时，先完成并验证独立快照，再应用迁移。
docker compose --env-file .env -f compose.local.yml run --rm --no-deps web \
  python app/manage.py migrate --noinput
docker compose --env-file .env -f compose.local.yml run --rm --no-deps web \
  python app/manage.py collectstatic --noinput
docker compose --env-file .env -f compose.local.yml run --rm --no-deps https \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose --env-file .env -f compose.local.yml up -d
docker compose --env-file .env -f compose.local.yml ps
```

全新空站另行用交互式 `createsuperuser` 建立管理员；恢复已有数据库时保留原账号，不重复初始化。`config.production_local` 会拒绝缺失、权限过宽或服务用户无法写入的数据目录；应修正实际挂载和所有权，而非关闭检查。

## Office 预览与备份

文档转换由宿主的专用 `teacher` 用户（UID/GID 10001）运行。按 [systemd 运维说明](systemd/README.md) 准备该用户的 rootless Docker、固定镜像 ID、发布锁和工作进程；不要将普通 Docker socket 挂入 web，也不要把用户加入普通 Docker 管理组。共享主机 Python 环境和源码版本，宿主 `worker.env` 必须覆盖：

```dotenv
DJANGO_SETTINGS_MODULE=config.production_local
TEACH_STORAGE_BACKEND=local
TEACH_DATA_DIR=/srv/teaching-data
TEACH_DB=/srv/teaching-data/school.sqlite3
DOCKER_HOST=unix:///run/user/10001/docker.sock
XDG_RUNTIME_DIR=/run/user/10001
TEACH_OFFICE_IMAGE=sha256:replace-with-the-verified-rootless-image-id
```

systemd 读取主配置后再读取这些宿主覆盖值。使用虚构 Word/PPT 实际验证转换结果、原件下载及访问权限后，再启用预览定时器。空队列成功退出不能代替转换验收。

`deploy/.env` 中的 `COMPOSE_FILE=compose.local.yml` 和项目名允许既有 `backup.sh` 选择同一应用配置；运行时不得额外设置指向其他项目的 `COMPOSE_FILE` 或 `COMPOSE_PROJECT_NAME`。备份目录应预先存在且允许 UID 10001 写入。先执行一次快照、复制到异机私有位置，再用 `restore_teaching` 还原到全新隔离目录并检查文件哈希及数据库完整性。宿主目录或另一容器卷仍可能随同一服务器故障而丢失，不能将同机快照称为异机备份。

```sh
TEACH_BACKUP_DIR=/srv/teaching-backups sh deploy/backup.sh
```

此命令从仓库根目录执行。定时备份需按实际路径配置并验证后启用；定期检查退出状态、磁盘余量和最近异机副本。

## 对外验证与更新

从外网用系统信任链检查 `https://TEACH_HOST/health/`（替换为实际域名或 IP）、登录与退出、Secure Cookie、静态资源、私有文件拒绝访问、课程目录和跨域规则。证书必须覆盖所使用的域名或 IP，且由公开受信任 CA 签发；不能把跳过证书校验或安装 Caddy 内部 CA 当作公网验收。真实验证通过后才将实际 HTTPS 来源设置为仓库变量 `TEACH_ORIGIN`，并重新发布 Pages。

更新前停止预览及备份定时器、等待在途任务完成，并暂停旧 web 写入，保存独立快照后再迁移。使用同一 Compose 项目和数据路径，保留兼容旧镜像及快照；迁移失败时先保全数据并排查，不能直接覆盖旧库。验证新网页、文件预览、备份恢复与容器重启后持久数据，再恢复定时器。

现有 `deploy/release.py` 和 **Deploy teaching host** 工作流按 Seafile 生产路径设计；此单机配置尚未声明与其自动发布、回滚和计时器流程兼容。完成专项验证前，按本文的受控手工流程更新。
