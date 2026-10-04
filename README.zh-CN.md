<div align="center">

# PaperPulse

**让论文来找你，而不是你去找论文。**

[English](README.md) · [简体中文](README.zh-CN.md)

[![CI](https://github.com/yuyanfeixue123/paperpulse/actions/workflows/ci.yml/badge.svg)](https://github.com/yuyanfeixue123/paperpulse/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Sources](https://img.shields.io/badge/sources-45%20built--in-informational.svg)](config/sources.yaml)
[![No API key for users](https://img.shields.io/badge/users-无需%20API%20Key-brightgreen.svg)](docs/llm-providers.md)

</div>

---

## 它做什么

你用一句话描述研究方向。PaperPulse 每 30 分钟从 45 个源采集新论文，用 LLM 按你的画像筛选，在你设定的时间把值得读的推送到邮箱。你在邮件里打的分，会反过来重塑下一次的画像。

```
「我在读城市规划硕士，关心用街景图像和深度学习做建成环境评估，
  也想看 15 分钟生活圈和公共交通可达性的研究。
  不想看纯算法理论推导，也不想看医学图像分割。」
                          │
                          ▼
                   结构化兴趣画像
      中英双语关键词 · 排除项 · arXiv 分类 · 检索式 · 阈值
                          │
                          ▼
  arXiv  OpenAlex  DOAJ  Europe PMC  bioRxiv  OSF  Nature  Science  PLOS  NBER …
                          │  每 30 分钟 · 3 天重叠窗口 · 按 DOI/arXiv/标题去重
                          ▼
              硬过滤 → FTS5 BM25 召回 → LLM 批量精排
                          │                    （每次 20 篇）
                          ▼
  final = 0.7·LLM + 0.2·口味相似度 + 0.1·新鲜度 − 0.5·已推过
                          │
                          ▼
              每日邮件 ──★1–5 / 不感兴趣──▶ 画像进化
```

## 为什么这么做

现有工具要么让你写布尔检索式，要么让你勾选期刊，或者干脆停在推荐面板前。真正的瓶颈不是「找到论文」，而是**筛掉论文**。PaperPulse 把 LLM 的开销全部花在这一件事上，同时把整个系统压到 1 核 1GB 跑得动的规模；凭据由部署者**一次性**配置好，终端用户不需要注册任何服务、不需要申请任何 API Key。

## 设计约束

| | |
|---|---|
| **运行形态** | 单进程 —— uvicorn + APScheduler + 4 线程池。无 Redis、无 Celery、无 Postgres |
| **数据库** | SQLite WAL 模式。默认源下常驻内存约 300 MB |
| **任务队列** | 用 `task_runs` 表而非 Redis。进程崩溃后未完成任务仍在表里，重启自动续跑 |
| **安装方式** | systemd + `uv`；Docker 仅供试用。生产目标是裸机 |
| **用户上手** | 只写自然语言。凭据归部署者 |
| **结构化输出** | 三级降级 —— `json_schema` → `json_object` → 纯文本 + 服务端容错解析，几乎任何模型都能跑 |
| **邮件** | Brevo 免费 300 封/天约可支撑 300 名活跃用户。配额是硬闸门：超限顺延次日，绝不静默丢弃 |

## 快速开始

最低要求：**1 核 / 1GB 内存 / 20GB SSD / 公网 IP + 一个域名**。

### A · 一键安装（生产）

```bash
git clone https://github.com/yuyanfeixue123/paperpulse.git && cd paperpulse
sudo ./scripts/install.sh --repo https://github.com/yuyanfeixue123/paperpulse.git --domain papers.example.com
```

脚本会安装依赖与 `uv`、创建 `paperpulse` 用户、生成 `SECRET_KEY` / `ENCRYPTION_KEY`（权限 600）、初始化数据库与内置源、写入 systemd unit 与 Caddyfile、开机自启、配置每日 03:30 备份，最后进入配置向导。可重复执行：不会覆盖已有的 `.env`、`config/config.yaml` 与 `data/`。

### B · 没有浏览器？用终端向导

通过 SSH 部署时域名与 HTTPS 往往还没就绪、Web 后台访问不了。终端向导用 ANSI 进度条 + 编号菜单完成全部六步配置，密码输入不回显：

```bash
python -m app.cli setup            # 交互式；随时 Ctrl-C 退出，进度自动保存
python -m app.cli setup --check    # 只看系统自检报告
```

LLM 与邮件步骤会**先测试连通性再允许继续**。非 TTY 环境（管道、CI）自动降级为普通输入，不会阻塞。

### C · 手动五步

```bash
useradd -r -m -d /opt/paperpulse paperpulse
cd /opt/paperpulse && git clone https://github.com/yuyanfeixue123/paperpulse.git . && uv sync --frozen
cp config/config.example.yaml config/config.yaml
python -m app.cli init-db && python -m app.cli create-admin --email you@example.com
sudo cp deploy/paperpulse.service /etc/systemd/system/ && systemctl enable --now paperpulse
```

随后用 `python -m app.cli setup` 完成配置，或等 DNS 与 TLS 就绪后访问 `https://your.domain/admin/setup`。

### D · Docker（仅供试用）

```bash
cp .env.example .env   # 填入两个密钥
docker compose up -d
```

**Docker 不是生产形态** —— 会多占 50–80MB 常驻内存。

## 内置数据源

源链接核实日期：**2026-10-03**。🟢 已核实 · 🟡 按官方模板推导，部署时须现场验证 · ⚪ 待验证。

| 类别 | 源 |
|---|---|
| **综合 API** | arXiv（9 个学科源）🟢 · OpenAlex 🟢 · Crossref 🟢 · DOAJ 🟢 · Europe PMC 🟢 · OSF Preprints（5 个 provider）🟢 |
| **专业 API** | bioRxiv 🟢 · medRxiv 🟢 · ChemRxiv 🟡 · Zenodo 🟡 · HAL 🟡 |
| **需 Key** | Semantic Scholar 🟡 · PubMed 🟡 —— 默认关闭，未填凭据前开关置灰 |
| **RSS / Atom** | Nature ×3 · Science ×2 · PNAS · eLife · PLOS ONE · NBER 🟢 · Cell · Lancet · JAMA · BMJ · NEJM · bioRxiv/medRxiv · Wiley · T&F · SAGE · Springer 🟡 |
| **自定义** | 后台可添加任意 RSS/Atom 地址，带 SSRF 校验与 feed 自动发现 |

默认只开启免注册源。新增源**不需要改代码** —— 一段 YAML + 选一个已有适配器即可。

> ⚠️ arXiv 官方分类 RSS 页已下线（`info.arxiv.org/help/rss` → 404），请用 API 查询串当 feed，遵守 ≤1 请求 / 3 秒。
> ⚠️ JournalTOCs 已于 2026-09-06 停止解析，Zetoc 于 2022 退役。本项目不依赖任何第三方 TOC 聚合器。
> 中文源：ChinaXiv 接口不稳定（默认关闭）；知网 / 万方 / 维普无开放 API，明确不做。请改用 OpenAlex / Crossref / DOAJ 检索其英文元数据。

**致谢**：*Thank you to arXiv for use of its open access interoperability.*

## 配置

`config/default.yaml` → `config/config.yaml` → 环境变量（`PAPERPULSE_*`）→ DB `system_settings`

```bash
PAPERPULSE_SECRET_KEY=...           # 生产必填
PAPERPULSE_ENCRYPTION_KEY=...       # 生产必填；轮换后已存凭据需重新配置
PAPERPULSE_SOURCES__CONTACT_EMAIL=you@example.com   # 进入 OpenAlex / Crossref 的 polite pool
```

凭据以 Fernet 加密存储。LLM 内置厂商预设（DeepSeek、通义千问、Moonshot、智谱、硅基流动、OpenAI、Anthropic、Gemini、Ollama），任何 OpenAI 兼容端点也能接入。用户可自带 Key；未自带时自动使用部署者的全局凭据。

## CLI

```bash
python -m app.cli setup                                    # 终端图形化配置向导（6 步）
python -m app.cli setup --check                            # 只跑系统自检
python -m app.cli setup --step 3                           # 从第 3 步开始
python -m app.cli init-db                                  # 建表 + 同步内置源
python -m app.cli create-admin --email you@example.com     # 创建或提升管理员
python -m app.cli verify-sources                            # 逐源连通性检查
python -m app.cli test-email --to you@example.com           # 发送测试邮件
python -m app.cli run-once fetch|dispatch|digest|purge|revise [id]
```

## 运维

后台「概览」显示用户数、活跃订阅、论文池条数、待发/失败投递与最近任务；「系统自检」一次检查出站网络、SMTP 出口、LLM、邮件通道、数据源、磁盘、内存、时钟漂移与密钥。此外每 30 分钟执行一次水位自保：

| 触发条件 | 动作 |
|---|---|
| 磁盘 ≥ 80% | 立即清理，保留天数临时收紧到 7 天 |
| 磁盘 ≥ 90% | 停止采集，只保留推送与清理并告警 |
| RSS ≥ 450 MB | 主动 GC 并记日志 |
| RSS ≥ 650 MB | 暂停采集，只保推送 |

自愈能力内置：任务超过 600 秒软超时会被重置（重试 3 次后标失败）；进程重启时 `running` 任务自动改回 `pending`；systemd 崩溃即拉起，配 60 秒看门狗。

## 文档

| | |
|---|---|
| [架构](docs/architecture.md) | 运行形态、内存预算、任务状态机 |
| [部署](docs/deployment.md) | 裸机前置检查、两种引导、升级与回滚 |
| [运维](docs/operations.md) | 巡检清单、故障对照表 |
| [数据源](docs/data-sources.md) | URL 模板、去重键、补全逻辑、RSS 要点 |
| [LLM 接入](docs/llm-providers.md) | 厂商矩阵、三级降级、BYOK |
| [邮件投递](docs/email-delivery.md) | 免费额度对比、配额治理、反垃圾清单
| **配置教程** | [获取 LLM API Key](docs/guides/llm-api-key.md) · [配置邮件通道与 DNS](docs/guides/email-delivery-setup.md) | |

## 站内「今日推荐」

邮件要受通道的内容政策与体积限制，站内列表不受。登录后访问 `/feed`
（或直接点首页）即可看到**全部**推荐，按相关度从高到低排序，含标题、DOI、
简述与推荐理由，可直接评分回流到画像。

若邮件通道按内容审核拒收（常见于国内通道），可在
`config/config.yaml` 配置 `email.content_filter_patterns`：

```yaml
email:
  content_filter_patterns:
    - "transgender|gender dysphoria"
    - "性少数|跨性别|性别认同"
```

命中的论文不进邮件正文、只在站内呈现，邮件里会告知
「另有 N 篇未通过本邮件通道送达，登录查看完整推荐」并给出入口。
详见 [邮件通道配置教程](docs/guides/email-delivery-setup.md#用内置过滤规则把高风险内容留在站内)。

### 通道自适应词库

投递被邮件通道按内容审核拒收时，系统会自动用 LLM 猜候选词，
再用**只含单个候选词**的最小探测邮件逐个试投，把稳定被拒的词沉淀进
[通道词库](/admin/terms)。命中词库的论文不进邮件正文，但仍完整保留在站内推荐。

连续 2 次被拒才入库（规避过滤器抖动误封），探测邮件发到部署者指定地址，
每日有封顶预算。详见 [邮件通道配置教程](docs/guides/email-delivery-setup.md)。

## 已知限制

- **未配 LLM 时的中文召回**：本地分词产出中文关键词，而论文池是英文元数据，FTS5 默认 `unicode61` 分词器无法跨语言匹配。配置 LLM 后会产出中英双语关键词，该限制即消失。
- **Brevo 免费版带 "Sent with Brevo" 角标**。若在意品牌干净，改用 Resend（100 封/天，无角标）。
- **SES 走 SMTP 端点**以避免引入 boto3，发送额度需在 AWS 控制台自管。
- **退信 webhook 未实现**。硬退信自动暂停需 provider 侧 webhook 或 IMAP 轮询。

## 参与贡献

欢迎提 issue 与 PR，详见 [CONTRIBUTING.md](CONTRIBUTING.md)。新增数据源无需改代码，清单见该文档。安全漏洞请见 [SECURITY.md](SECURITY.md)。

## 许可

Apache-2.0 © [yuyanfeixue123](https://github.com/yuyanfeixue123) 及贡献者，详见 [LICENSE](LICENSE)。
