# 邮件投递

## 通道决策（政策核实于 2026-10-03）

| 服务商 | 免费额度 | 日上限 | 品牌角标 | 永久 | 判定 |
|---|---|---|---|---|---|
| **Brevo** | 300 封/天，无时间限制、无需信用卡 | 300 | 有（"Sent with Brevo"） | ✅ | **主通道** |
| Mailjet | 6,000 封/月（≈200/天） | 200 | 有 | ✅ | 备选 |
| Resend | 3,000 封/月 | 100 | 无 | ✅ | 备选（品牌干净） |
| Mailgun | 100 封/天 | 100 | — | ✅ | 备选（日志仅留 1 天） |
| ZeptoMail | 注册送 10,000 封（1–6 个月有效） | 100 | — | ❌ | 排除 |
| SendGrid | 2025-05-27 起取消永久免费 | 100 | — | ❌ | 排除 |
| Amazon SES | 2026-07-21 起新账户不再享有 3,000 封/月免费额度 | — | 无 | ❌ | 付费扩容路径（$0.10/1000） |

**关键推论**：日发信量 = 活跃用户数（每人每天 1 封）。Brevo 300 封/天 → 约 **300 用户永久免费**。超过后 SES 按量约 3,000 封/月 ≈ $0.30，成本仍接近零。**无需服务器直发**。

## 通道类型

| kind | 实现 | 说明 |
|---|---|---|
| `brevo` | REST API `https://api.brevo.com/v3/smtp/email` | 无 API Key 时回落 SMTP `smtp-relay.brevo.com:587` STARTTLS |
| `resend` | REST API `https://api.resend.com/emails` | 无角标 |
| `ses` | SMTP `email-smtp.{region}.amazonaws.com:587` | 避免引入 boto3；额度在 AWS 控制台自管 |
| `smtp` | 通用 smtplib | 自建或第三方 |
| `postfix` | `127.0.0.1:25` 无认证 | 兜底，默认关闭 |

统一接口 `EmailProvider.send(msg) -> message_id`。

## 日配额治理（硬闸门）

- `daily_budget` 默认 **250**（低于 Brevo 的 300，为重试与告警预留 50 封缓冲）
- 发送前原子预占：

```sql
UPDATE send_quota SET sent_count = sent_count + 1
WHERE provider_key = ? AND quota_date = ? AND sent_count < ?
-- rowcount == 0 → 该封标 deferred，deferred_to_date = 次日
```

- 超限且配置了备用通道 → 自动切备；无备用 → 顺延并告警
- 后台仪表盘显示今日已发 / 额度 / 剩余 / 顺延队列长度
- **宁可顺延，不可突破免费额度**

## 可靠性

- **持久化队列**：先写 `deliveries(status=pending)` 再发送；进程重启不丢
- **幂等**：`UNIQUE(digest_id, to_email)`
- **重试**：3 次；4xx（除 429）不重试，直接标 `failed`
- **速率限制**：`max_per_minute`，新域名建议 ≤ 20/分钟预热
- **补发**：每 2 分钟扫 `pending` 与到期的 `deferred` 重投
- **超时**：连接 10s、发送 30s

## 邮件头（反垃圾关键项）

```
List-Unsubscribe: <mailto:unsub@...>, <https://{site_url}/u/{token}>
List-Unsubscribe-Post: List-Unsubscribe=One-Click
X-PaperPulse-Digest: {digest_id}
```

正文 `multipart/alternative`，必须同时有纯文本版本。

## 反垃圾清单（部署强制项）

1. 发信域名配置 **SPF**（`v=spf1 include:<provider> ~all`）
2. 配置 **DKIM**（provider 提供 CNAME / TXT）
3. 配置 **DMARC**（`v=DMARC1; p=quarantine; rua=mailto:dmarc@<domain>`）
4. `Return-Path` / `Envelope-From` 与 `From` 域名对齐
5. `List-Unsubscribe` + `List-Unsubscribe-Post`（RFC 8058）
6. `multipart/alternative`
7. 正文链接指向与发信域名同域的跳转地址（不堆外链、不用短链）
8. 固定发信节奏（同一时刻批量发送，而非随机刷）
9. 新域名先小规模预热
10. 邮件头加 `X-PaperPulse-Digest: <digest_id>`

## 服务器直发（兜底，默认关闭）

启用前必须**全部满足**：

1. 裸机**不在**阿里云 / 腾讯云 / 华为云 / AWS / GCP 等默认封禁 TCP 25 出方向的平台。⚠️ 阿里云与腾讯云的 25 端口解封协议明确要求"仅可用于连接第三方 SMTP 服务器"，若被发现直接用该 IP 通过 SMTP 对外发信，服务商有权永久封禁端口。**在这两家平台上禁止启用直发。**
2. 拥有静态公网 IPv4，且服务商允许自助设置 PTR / rDNS（指向 `mail.<domain>`）
3. 出方向 TCP 25 确实可达
4. IP 不在 Spamhaus PBL / SORBS / Barracuda 等黑名单中

```bash
nc -zv gmail-smtp-in.l.google.com 25                 # 端口 25 出方向可达
dig +short -x <your-ip>                              # 应返回 mail.<domain>
dig +short <ip-反写>.zen.spamhaus.org                # 无返回结果 = 未被列入
```

**明确风险**：新 IP 无历史声誉，Gmail / Outlook 投递率不可保证；需 IP 预热（首周日发 ≤ 20 封，逐周翻倍）、MTA-STS、持续解析 DMARC 报告。直发是兜底而非推荐路径。

## 退信处理

连续 2 次硬退信（invalid recipient）→ 自动暂停该用户推送。API 通道配 webhook（`/webhooks/bounce`）；SMTP 通道可选 IMAP 轮询（默认关，省资源）。
