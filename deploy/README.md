# 恋屿云服务器部署

生产部署将代码、配置和用户数据分开保存：

- `/opt/love-moments/current`：可以随时替换的应用代码。
- `/etc/love-moments/love-moments.env`：权限为 `0600` 的服务端密钥与环境配置。
- `/srv/love-moments/data`：SQLite 数据库。
- `/srv/love-moments/media`：媒体本地副本及 Web Push 私钥。
- `/srv/love-moments/backups`：每天生成并完成恢复验证的备份。
- `/srv/love-moments/logs`、`/srv/love-moments/maintenance`：日志和巡检状态。

Docker 只把 `127.0.0.1:52013` 暴露给服务器本机。Nginx 是唯一公网入口，负责 HTTPS、上传大小限制和反向代理；因此腾讯云防火墙无需开放 `52013`。

当没有可用域名时，可直接为公网 IPv4 申请 Let's Encrypt 短期证书。IP 证书有效期约 6 天，必须安装 Certbot 5.4 或更高版本，并启用本目录的 `love-moments-cert-renew.timer` 每日检查两次。

## 首次部署顺序

1. 选择公网入口：将最终域名解析到服务器 IPv4，或直接使用公网 IPv4 证书；确认腾讯云防火墙已开放 80、443。
2. 创建上述持久目录并设为容器用户 `10001:10001` 可读写。
3. 把仓库发布到 `/opt/love-moments/current`，把真实 `.env` 保存为 `/etc/love-moments/love-moments.env`。
4. 生产环境必须覆盖以下值：

   ```ini
   APP_HOST=0.0.0.0
   APP_PORT=52013
   DATABASE_PATH=/srv/love-moments/data/love_moments.db
   MEDIA_ROOT=/srv/love-moments/media
   BACKUP_ROOT=/srv/love-moments/backups
   BACKUP_MIRROR_ROOT=
   MAINTENANCE_STATE_ROOT=/srv/love-moments/maintenance
   LOG_ROOT=/srv/love-moments/logs
   PUBLIC_BASE_URL=https://你的域名
   CORS_ALLOWED_ORIGINS=https://你的域名
   COOKIE_SECURE=true
   LOCAL_ADMIN_NOTIFICATIONS=false
   WATCHDOG_AUTO_RESTART=false
   ```

5. 域名入口使用 `love-moments-http.conf.template` 和 `love-moments-https.conf.template`；IP 入口使用对应的 `love-moments-ip-*.conf.template`。
6. IP 证书首次签发示例：

   ```bash
   /opt/certbot-ip/bin/certbot certonly \
     --preferred-profile shortlived \
     --webroot --webroot-path /var/www/html \
     --ip-address 203.0.113.10 \
     --non-interactive --agree-tos --register-unsafely-without-email
   ```

7. 构建并启动 `deploy/docker-compose.production.yml`，构建时用
   `APP_RELEASE_SHA="$(git rev-parse HEAD)" docker compose -f deploy/docker-compose.production.yml build`
   写入不可变版本号，再安装并启用 `deploy/systemd` 中的服务和定时器。若使用 IP 证书，必须同时启用 `love-moments-cert-renew.timer`。

## 验收

```bash
docker compose -f deploy/docker-compose.production.yml ps
curl -fsS http://127.0.0.1:52013/api/health
curl -fsS https://你的域名/api/health
systemctl list-timers 'love-moments-*'
systemctl status love-moments.service --no-pager
```

数据库与媒体不会写入镜像或代码目录，重建容器和更新代码不会覆盖 `/srv/love-moments`。不过系统盘仍不等于异地灾备：销毁实例或云盘会同时影响服务与本机备份，数据库还应定期复制到另一块云盘、NAS 或私有对象存储。
