# 部署（裸机 systemd + uv）

Docker 不作为生产部署形态（省 50–80MB 常驻内存，减少一层运维复杂度）。

## 目录

```
/opt/paperpulse/
  .venv/                  uv sync --frozen
  data/                   paperpulse.db, backup/
  config/                 config.yaml, sources.yaml
  .env                    SECRET_KEY, ENCRYPTION_KEY
/etc/systemd/system/paperpulse.service
/etc/caddy/Caddyfile      反代 127.0.0.1:8000，自动 HTTPS
```

## 1 前置检查清单

```bash
# 出网（采集全部依赖 HTTPS 出站）
curl -sS -o /dev/null -w '%{http_code}\n' 'https://export.arxiv.org/api/query?search_query=all:test'
curl -sS -o /dev/null -w '%{http_code}\n' 'https://api.openalex.org/works?per-page=1'

# 邮件：第三方通道走 587/465（不是 25）
nc -zv smtp-relay.brevo.com 587
nc -zv smtp.resend.com 465

# 时间同步（定时推送依赖准确时钟）
timedatectl status | grep -E 'NTP service|synchronized'

# 内存与 swap（目标：可用 ≥900MB，swap ≥512MB）
free -m && swapon --show
```

仅在启用服务器直发兜底时才检查 25 端口（见 email-delivery.md）。

## 2 初始化

```bash
useradd -r -m -d /opt/paperpulse paperpulse
cd /opt/paperpulse && git clone <repo> . && uv sync --frozen
cp config/config.example.yaml config/config.yaml
python -m app.cli init-db
python -m app.cli create-admin --email you@example.com
python -m app.cli verify-sources
python -m app.cli test-email --to you@example.com
systemctl enable --now paperpulse
```

## 3 生成密钥

```bash
python -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
# 分别写入 .env 的 PAPERPULSE_SECRET_KEY 与 PAPERPULSE_ENCRYPTION_KEY
```

`ENCRYPTION_KEY` 变更会导致已加密的 LLM Key / 邮件凭据无法解密，需重新配置。

### 3.1 不要设置 `PAPERPULSE_ENV`

应用默认按**生产模式**运行：缺 `PAPERPULSE_SECRET_KEY` 或
`PAPERPULSE_ENCRYPTION_KEY` 时**直接拒绝启动**，不会静默降级。

只有显式设置 `PAPERPULSE_ENV=development`（或 `test`）才会启用内置弱密钥回退。
生产环境误设这一项，会让会话 cookie 与 HMAC token 可被任意伪造 ——
等于没有签名。后台「系统自检 → 密钥」会把该状态标为异常并给出说明。

```bash
# 生产：不要出现这一行
# PAPERPULSE_ENV=development
```

### 3.2 反代与限流

限流按客户端 IP 分桶，而 IP 取自 `X-Forwarded-For`。该头**只在直连对端
命中受信列表时**才被采信，默认只信任回环地址（与 Caddy 同机的部署天然适用）。

如果 uvicorn 端口被直接暴露到公网，攻击者可以伪造该头轮换身份绕过限流。
务必经 Caddy / Nginx 访问；确需信任其他代理时：

```bash
PAPERPULSE_TRUSTED_PROXIES=10.0.0.0/8,192.168.1.1
```

## 4 首次部署引导（6 步）

> **安全提示**：引导完成前，`/admin/setup/*` 允许「库中还没有账号」的人自举
> 首位管理员。**部署完成后请立刻走完引导**，或直接关闭该端口。
> 走完后这些端点会永久失效；若引导中途放弃，务必确认 `/admin/setup` 已不可访问。

两条等价路径，随时可切换。

### 4.1 终端向导（SSH / 无浏览器环境，推荐首次部署用）

配好域名和 HTTPS 之前，Web 后台往往还访问不了。此时用终端向导：

```bash
sudo -u paperpulse -H /opt/paperpulse/.venv/bin/python -m app.cli setup
```

界面为 ANSI 绘制的进度条 + 编号菜单，密码输入不回显；非 TTY（管道 / CI）自动降级为普通输入，不会阻塞。

| 参数 | 作用 |
|---|---|
| （无） | 从未完成的步骤开始续配 |
| `--step N` | 从第 N 步开始（1–6） |
| `--check` | 只跑系统自检并打印报告，不进入向导 |

六个步骤：管理员账号 → 站点信息 → LLM 接入 → 邮件通道 → 数据源确认 → 系统自检。

每步都**先测试连通性再继续**：LLM 会发一个极小请求验证鉴权与模型可用；邮件会实际投递一封测试邮件并等待回执。Ctrl-C 随时退出，已完成步骤保存在 `system_settings.setup_step`，重跑自动续配。

### 4.2 浏览器引导

访问 `https://your.domain/admin/setup`。完成前应用处于未启用状态。

| Step | 内容 | 可跳过 | 校验 |
|---|---|---|---|
| 1 | 管理员邮箱、密码、时区 | 否 | 密码强度 |
| 2 | 站点名称、对外 URL、默认时区 | 否 | — |
| 3 | LLM：厂商 / base_url + key + model | **是**（选纯关键词模式） | 发极小请求验证鉴权 |
| 4 | 邮件通道：Brevo / Resend / SES / SMTP | **否** | 实际投递测试邮件 |
| 5 | 数据源确认（需 Key 的源置灰） | 是 | 逐源自检 |
| 6 | 系统自检报告 | — | 汇总判定 |

## 5 升级与回滚

```bash
sudo -u paperpulse -H git -C /opt/paperpulse pull --ff-only
sudo -u paperpulse -H /opt/paperpulse/.venv/bin/uv sync --frozen --directory /opt/paperpulse
systemctl restart paperpulse
# 失败回滚
sudo -u paperpulse -H git -C /opt/paperpulse checkout <上一个 tag>
systemctl restart paperpulse
```

> **若以 root 身份执行 `git pull` 报 `detected dubious ownership`**：仓库属主是 `paperpulse`，
> 而 git 拒绝在身份不匹配时操作。加上白名单即可：
>
> ```bash
> git config --global --add safe.directory /opt/paperpulse
> ```

## 6 备份

```bash
# /etc/cron.d/paperpulse
30 3 * * * paperpulse /opt/paperpulse/deploy/backup.sh >> /var/log/paperpulse-backup.log 2>&1
```

`VACUUM INTO` 为在线备份，不阻塞读写，保留 7 份。

## 7 日志

structlog JSON → journald。建议：

```bash
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/size.conf <<'EOF'
[Journal]
SystemMaxUse=200M
MaxRetentionSec=14day
EOF
```

## 8 守护与自愈

| 场景 | 动作 |
|---|---|
| 进程崩溃 | `Restart=always`，`RestartSec=10`，`StartLimitBurst=5/300s` |
| 进程假死 | `Type=notify` + `WatchdogSec=60`，应用每 30s `sd_notify("WATCHDOG=1")` |
| 内存失控 | `MemoryMax=700M` + `OOMPolicy=stop` |
| 任务卡死 | `reset_stuck_tasks` 每 5 分钟扫描 |
| 磁盘告警 | >80% 触发收紧到 7 天的清理；>90% 停止采集 |

## 9 巡检清单

- `/healthz` 进程存活
- 后台「概览」：任何周期任务超过 2 个周期未成功即需排查
- 每周检查：`verify-sources` 全绿、备份文件存在、磁盘 < 80%

---

## 附：设计文档

- [设计方案 v2.2](../PaperPulse-设计方案.md) —— 需求映射、运行模式、源总表、LLM 层、邮件策略、无人值守设计
- [开发计划](../PaperPulse-开发计划.md) —— W1–W15 施工序列、硬参数表、数据模型 DDL、施工记录
