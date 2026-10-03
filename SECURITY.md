# 安全策略

## 报告漏洞

请勿在公开 issue 中披露安全漏洞。请发送邮件至项目维护者（见仓库 Owner），并包含：

- 漏洞类型与影响范围
- 复现步骤
- 建议的修复方案（如有）

我们会在 72 小时内确认，修复后公开致谢（除非你要求匿名）。

## 部署者须知

| 项目 | 要求 |
|---|---|
| `PAPERPULSE_SECRET_KEY` | 生产环境必配。用于 session 与 HMAC token 签名，泄露会导致免登录反馈链接可被伪造 |
| `PAPERPULSE_ENCRYPTION_KEY` | 生产环境必配（Fernet）。**变更后已加密的 LLM Key / 邮件凭据不可解密，需重新配置** |
| 对外 URL | 必须为 HTTPS，邮件中的退订与反馈链接依赖它 |
| 数据库文件 | 含用户邮箱与加密凭据，权限应为 `600`，属主 `paperpulse` |

## 已实现的安全措施

- 密码：argon2id（time_cost=2, memory_cost=19MB）
- Session：签名 cookie，HttpOnly + SameSite=Lax
- CSRF：所有写操作校验 token（由 session 派生）
- 登录 / 注册：进程内令牌桶限流
- 凭据加密：Fernet，库中以 `enc:` 前缀标记
- SSRF：自定义 RSS 入库前校验（仅 http/https，拒绝内网 / 回环 / 链路本地 / 保留地址）
- 免登录链接：HMAC-SHA256 签名 + 180 天过期，payload 明文但不可篡改
- 一键退订：RFC 8058（`List-Unsubscribe` + `List-Unsubscribe-Post`）

## 已知风险

- 进程内限流在多实例部署下失效（Lite 模式为单进程，不受影响）
- 邮件退信的 webhook 需自行在 provider 侧配置；未配置时硬退信不会自动暂停用户
- 未做 IP 级别的暴力破解封禁，建议在前置反代（Caddy / Nginx）层加限制

## 不做的事

- 不在仓库中存储任何真实凭据
- 不在日志中打印 API Key 或密码（LLM / 邮件错误仅记录类型与截断信息）
