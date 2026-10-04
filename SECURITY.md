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

**认证与会话**

- 密码：argon2id（time_cost=2, memory_cost=19MB）
- Session：HMAC-SHA256 签名 cookie，HttpOnly + SameSite=Lax
- 会话 cookie 仅在**能确定 HTTPS 时**才加 `Secure`（内网 HTTP 部署下加了会导致登录后立即掉线）
- 会话可吊销：`password_changed_at` 编入 token，改密 / 重置密码后旧会话立即失效
- 免登录链接有效期 30 天（`PAPERPULSE_TOKEN_TTL_DAYS`）；**重置密码链接单独收紧到 1 小时**
- CSRF：所有写操作校验 token（由 session 派生），含 `/logout`
- 忘记密码：全程返回中性提示，不泄露邮箱是否存在
- 注册用户名仅允许 ASCII 字母数字与 `_ - .`（拒绝全角/同形字，避免仿冒冒名）
- 登录 / 注册 / 找回密码：进程内令牌桶限流

**访问控制与资源治理**

- 首次部署引导（`/admin/setup/*`）完成后即失效；库中已有账号时，需管理员会话才能访问
- 自助注册开关（`/admin/system`），关闭后仅管理员可建号；库内无账号时保持自举可用
- 每用户日配额：AI 调用、推送邮件、订阅数量（`quota` 配置段，0 表示不限）
- LLM 配额闸门置于 `llm/client.py` 唯一出口，一次拦截覆盖打分 / 解析 / 修订 / 连接测试
- 用户数据管理：活跃度识别（30 天）、级联删除；管理员不可删除或停用自己

**输入与网络**

- 凭据加密：Fernet，库中以 `enc:` 前缀标记
- SSRF 防护（`app/core/urlguard.py`）：解析**全部** A/AAAA 记录逐一判定、限制端口 80/443、逐跳校验重定向
- 内容过滤：管理员正则 + 通道自适应词库 + 订阅名上下文匹配；正则含长度上限与 ReDoS 构造检测
- 订阅参数钳制（`min_score` / 篇数 / 回溯天数）；`queries` 走键名白名单与长度限制
- 订阅回滚、召回预览均校验属主（IDOR）

**密钥与响应头**

- 生产环境缺失 `PAPERPULSE_SECRET_KEY` / `PAPERPULSE_ENCRYPTION_KEY` 时**拒绝启动**，不静默降级到弱密钥
  （判定依据是显式的 `PAPERPULSE_ENV=development|test`，`run_mode: lite` 本身也可能是生产形态）
- 安全响应头由**应用层**统一下发（不依赖反代）：CSP、`X-Content-Type-Options`、
  `X-Frame-Options`、`Referrer-Policy`、`Permissions-Policy`、HTTPS 下的 HSTS
- CSP 的 `script-src 'self'` 为严格模式；`style-src` 含 `'unsafe-inline'`（模板有约 145 处动态内联样式，
  内联样式不能执行脚本，不构成 XSS 矢量）

## 已知风险

- 进程内限流在多实例部署下失效（Lite 模式为单进程，不受影响）
- `X-Forwarded-For` 仅在直连对端属于受信代理时才采信，默认只信回环地址；
  若把 uvicorn 端口直接暴露到公网，攻击者可伪造该头绕过限流 —— 务必经反代访问
- 邮件退信的 webhook 需自行在 provider 侧配置；未配置时硬退信不会自动暂停用户
- 未做 IP 级别的暴力破解封禁，建议在前置反代（Caddy / Nginx）层加限制
- 通道自适应词库基于**逐词最小探测**。若邮件通道判定的是「组合语义」而非单词黑名单
  （部分国内云邮服务即如此），逐词探测会全部放行而整封仍被拒收 —— 此时系统会如实报告，
  不会假装成功

## 不做的事

- 不在仓库中存储任何真实凭据
- 不在日志中打印 API Key 或密码（LLM / 邮件错误仅记录类型与截断信息）
