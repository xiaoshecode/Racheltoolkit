# 文档预览工作进程

PDF 和照片在教学站校验原件后建立页序、宽高与方向清单。DOC、DOCX、PPT 和 PPTX 由独立工作进程转换为固定版式 PDF。网页先调用 `queue_preview(source)`；后台定期运行以下命令。教师从对应课程或已授权的提交版本查看结果，失败后仍可下载原件，或重新排队。

在有 Docker 的 Linux 工作机上，从仓库根目录构建专用镜像并记录实际镜像 ID。生产环境使用不可变 ID，转换机和 web 进程分开运行，仅转换机可调用受控容器运行时。

```sh
docker build -t teach-office:reviewed app/storage/office
export TEACH_OFFICE_IMAGE="$(docker image inspect --format '{{.Id}}' teach-office:reviewed)"
python app/manage.py render_previews --limit 20
python app/manage.py render_previews --asset 1 --retry-failed
```

转换子容器通过标准输入读取一份源件，标准输出返回 PDF，禁网，不挂载主机目录、不传入云盘凭证。根文件系统只读，临时区、进程数、CPU、内存、文件大小和总时长受限。宏安全等级设为最高；外链无法通过网络加载。限制 Docker CLI 权限只授予专用工作人员。镜像必须预先构建，运行时使用 `--pull=never`。部署时另验中文字体、DOC/DOCX/PPT 版式、失败重试和低资源超时。浏览器从原业务对象端点重新核权后读取派生件；未授权者不获得存储路径。

Seafile 库内预建 `公开课件`、`学生作业`、`教师备课`、`教学归档` 四个根目录。配置 `TEACH_STORAGE_BACKEND=seafile`、`SEAFILE_BASE_URL`、`SEAFILE_LIBRARY_ID` 与服务器专用 `SEAFILE_REPO_TOKEN`。仅在文件服务确实使用另一个 HTTPS 来源时设置逗号分隔的 `SEAFILE_TRANSFER_ORIGINS`。确认库根外链已撤销。上传后失联时，核对原路径的大小和哈希；重试绝不覆盖现有对象。只有原写入确已终止、对象缺失时，维护者才可释放对应的存储认领。
