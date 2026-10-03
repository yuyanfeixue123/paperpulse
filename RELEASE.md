# 发布清单

## 仓库

- **地址**：https://github.com/yuyanfeixue123/paperpulse
- **可见性**：Public
- **默认分支**：`main`
- **首个 tag**：`v0.1.0`

## 建议填写的仓库元信息

**Description**（一句话，GitHub 列表页与搜索结果会显示）：

```
Self-hosted paper digest: LLM filters new papers by your interest profile and emails them daily. Runs on 1 vCPU / 1 GB.
```

**Topics**（10 个上限，建议全用满，直接决定搜索可见度）：

```
paperpulse
literature-review
arxiv
openalex
research-tool
self-hosted
email-digest
llm
sqlite
python
```

## 首页内容已就绪

| 文件 | 作用 |
|---|---|
| `README.md` | GitHub 默认落地页（英文）：徽章、一句话定位、流程图、设计约束表、四种安装方式、源清单表、运维表、已知限制 |
| `README.zh-CN.md` | 中文版，顶部互链 |

两份 README 已互相引用，各自 188 行，22 个内部链接全部有效。

## 徽章说明

README 顶部的 CI 徽章指向 `actions/workflows/ci.yml`，推送后首次跑通即变绿。若仓库名与 `yuyanfeixue123/paperpulse` 不同，需同步替换两处：

- `README.md` / `README.zh-CN.md` 顶部的 `CI` 徽章
- README 中「一键安装」与「手动五步」的 clone 地址
- `scripts/install.sh` 第 4、6、29 行的注释与交互提示
- `.github/ISSUE_TEMPLATE/config.yml` 的 Discussions 链接

## 安全审计结论（发布前）

| 检查项 | 结果 |
|---|---|
| 用户提供的 LLM API Key | 全项目 0 处（含 git 历史） |
| 硬编码密钥（`sk-*` / `ghp_*` / `AKIA*` / 私钥） | 0 处 |
| `.env`（含真实 SECRET_KEY / ENCRYPTION_KEY） | 已删除；`install.sh` 会自动重新生成 |
| `config/config.yaml`（含加密凭据） | 已删除；`install.sh` 从 example 复制 |
| `data/`（69MB 开发数据库、真实邮件 `.eml`、旧测试库） | 已清理且被 gitignore |
| `data/_smtp_sink/*.eml` | 3 封真实邮件已删除（含 `yuyanfeixue@foxmail.com` 收件记录） |
| 测试脚手架 `scripts/_e2e_part1.py`、`_test_smtp_sink.py` | 已删除（非项目构成文件） |
| 工具元数据 `.workbuddy/`、`.zcodeignore` | 已加入 gitignore |
| 重复文档 `docs/design-v2.2.md` | 已删除（与根目录设计方案逐字节相同） |
| 暂存区敏感路径 | 0 项（`.venv/` `data/` `.env` `config/config.yaml` 各类缓存均未纳入） |

git 历史中唯一出现的邮箱是 `646776007@qq.com`，作为提交作者署名，属正常公开信息。

## 仓库内容

- 154 个文件，15132 行，`.git` 体积 758 KB
- `app/` 107 个文件（Python 后端 + 28 个 Jinja2 模板）
- `tests/` 7 个文件，57 项测试
- `docs/` 6 篇（架构、部署、运维、数据源、LLM、邮件）
- `.github/` 6 个（CI、Dependabot、Issue 模板 ×3、PR 模板）

## CI

`.github/workflows/ci.yml` 在 push 与 PR 时跑：

1. `ruff check app tests`
2. `mypy app`
3. `pytest tests -q`

## 依赖与版本

- Python 3.11+
- 核心依赖：FastAPI、SQLAlchemy 2.x、APScheduler、httpx、Jinja2、structlog、cryptography、argon2-cffi、jieba
- 开发依赖：pytest、respx、ruff、mypy、aiosmtpd
- 刻意不引入：boto3、pandas、numpy、torch、curses/prompt_toolkit（守 1C1G 的 95MB 依赖预算）
