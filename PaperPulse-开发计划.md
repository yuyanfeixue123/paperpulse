# PaperPulse 开发指南（Agent 连续施工用）

> 本文是 `PaperPulse-设计方案.md` v2.2 的施工版。设计文档讲"为什么"，本文讲"具体做成什么样"。
> **工作方式**：从 W1 顺序做到 W15。每个 W 做完立刻 `make lint && make test`，跑通再进下一个。文中所有参数已定死，直接取用，不要自行发明或增加分支。
> **不做**兜底重试链、降级链、边界防御。失败就记日志并让任务失败，由下一次调度自然重试。

---

## 0. 工作方式

1. 一次只做一个 W，做完在 §16 清单里勾掉。
2. 每个 W 的产出文件按文中给出的路径与职责创建，不额外拆分。
3. 需要决策的参数一律查 §1 硬参数表；表里没有的，选最简单直接的写法。
4. 数据库用同步 SQLAlchemy（Lite 模式线程池即可，不上 asyncio ORM）。
5. 提交：`feat(W3): 数据模型与迁移` 这类格式，一个 W 一次提交。
6. 外部 HTTP 在测试里用 `respx` mock；不写针对超时/断网/限流异常的专门处理。

---

## 1. 硬参数表（直接取用）

```yaml
# config/default.yaml —— 完整默认配置
run_mode: lite
web:
  host: 127.0.0.1
  port: 8000
  workers: 1
db:
  url: sqlite:///data/paperpulse.db
  wal: true
  busy_timeout_ms: 5000
  mmap_size_mb: 64
scheduler:
  executor_threads: 4
  fetch_interval_minutes: 30
  dispatch_interval_minutes: 10
  purge_hour: 4
  revise_hour: 5
  task_soft_timeout_seconds: 600
pipeline:
  fetch_window_days: 3          # 增量窗口，重叠设计，靠 upsert 去重
  fetch_page_size: 100
  candidate_top_n: 120          # 进入 LLM 的候选上限
  llm_batch_size: 20
  llm_concurrency: 2
  abstract_max_chars: 1200
  lookback_days: 7              # 只推最近 7 天的论文
  default_min_score: 4          # score>=4 才推送
  default_papers_per_day: 10
  freshness_half_life_days: 14
llm:
  provider: openai_compatible   # openai_compatible | anthropic | gemini | ollama
  base_url: ""                  # 部署者在引导中填
  model_score: ""               # 精排模型
  model_parse: ""               # 兴趣解析模型
  timeout_connect: 10
  timeout_read: 60
  max_retries: 3
email:
  daily_budget: 250             # Brevo 免费上限 300，留 50 缓冲
  max_per_minute: 20
  from_name: PaperPulse
retention:
  days: 30
  batch_size: 1000
  vacuum: true
sources:
  arxiv_request_interval_seconds: 3
  contact_email: ""
site:
  default_timezone: Asia/Shanghai
```

| 类别 | 值 |
|---|---|
| 去重键 | `doi:<小写>` → 无 DOI 用 `arxiv:<id>` → 再退化 `t:<标题归一化 sha1 前 16 位>` |
| 时间 | 库里一律存 UTC 的 ISO8601 字符串；展示与调度按用户时区换算 |
| 评分公式 | `final = 0.7*llm_score + 0.2*taste_sim + 0.1*freshness - 0.5*已推过` |
| 新鲜度 | `freshness = exp(-age_days / 14)` |
| 画像修订触发 | 距上次修订 ≥ 7 天 **且** 新增反馈 ≥ 10 条 |
| patch 限量 | `add_include ≤ 5`、`remove_include ≤ 3`、`add_exclude ≤ 5`、`min_score_delta ∈ [-1, 1]` |
| 反馈生效 | 同一候选词累计 ≤ −2 并入 exclude；≥ +2 并入 include |
| 默认推送时间 | `08:30` 用户本地时区 |

---

## 2. 目录结构（W1 一次性建好）

```
app/
  main.py                  FastAPI 实例、路由挂载、 lifespan
  cli.py                   init-db / create-admin / verify-sources / test-email / run-once
  core/
    config.py              分层加载 default.yaml → config.yaml → env → DB
    db.py                  engine / SessionLocal / PRAGMA / get_db
    logging.py             structlog JSON
    security.py            argon2id 哈希、Fernet 加解密、HMAC token
    http.py                httpx 客户端 + 每源令牌桶限流
  models/                  SQLAlchemy 模型（user/system/paper/interest/score/digest/delivery/task）
  sources/
    base.py                SourceBase 抽象
    arxiv.py openalex.py crossref.py doaj.py europepmc.py biorxiv.py osf.py rss.py nber.py
    enrich.py              DOI/摘要补全
    registry.py            读 sources.yaml，按 key 实例化
  scheduler/
    runner.py              APScheduler + ThreadPoolExecutor + task_runs
    jobs.py                五个周期任务的入口函数
  llm/
    base.py openai_compat.py anthropic.py gemini.py ollama.py client.py
  pipeline/
    fetch.py prefilter.py score.py rank.py render.py deliver.py
  interest/
    schema.py parse.py tfidf.py revise.py
  setup/
    wizard.py checks.py
  web/
    routes/                auth / subscribe / account / feedback / admin_* / setup
    templates/             layouts、setup、subscribe、email、admin
    static/
config/
  default.yaml             （见 §1）
  sources.yaml             源清单
migrations/                Alembic
deploy/
  paperpulse.service  caddy/Caddyfile  backup.sh
scripts/
  verify_sources.py
tests/
```

依赖（一次性写入 `pyproject.toml`）：
`fastapi uvicorn jinja2 sqlalchemy alembic apscheduler httpx feedparser pydantic pydantic-settings pyyaml structlog argon2-c cryptography openai anthropic google-generativeai tenacity python-dateutil email-validator`（开发：`pytest respx ruff mypy aiosmtpd`）

---

## 3. 数据模型（W3 直接执行的 DDL）

```sql
CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  display_name TEXT NOT NULL DEFAULT '',
  timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
  is_active INTEGER NOT NULL DEFAULT 1,
  is_admin INTEGER NOT NULL DEFAULT 0,
  email_verified INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE system_settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  setup_completed_at TEXT,
  setup_step INTEGER NOT NULL DEFAULT 1,
  site_name TEXT NOT NULL DEFAULT 'PaperPulse',
  site_url TEXT NOT NULL DEFAULT '',
  default_timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
  llm_mode TEXT NOT NULL DEFAULT 'keyword',       -- llm | keyword
  retention_days INTEGER NOT NULL DEFAULT 30,
  purge_scope TEXT NOT NULL DEFAULT 'all',
  max_pool_rows INTEGER NOT NULL DEFAULT 200000,
  updated_at TEXT NOT NULL
);

CREATE TABLE sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  type TEXT NOT NULL,
  field TEXT NOT NULL,
  url_template TEXT NOT NULL,
  params_json TEXT NOT NULL DEFAULT '{}',
  requires_key INTEGER NOT NULL DEFAULT 0,
  rate_limit_json TEXT NOT NULL DEFAULT '{}',
  enabled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE source_credentials (
  source_key TEXT PRIMARY KEY,
  encrypted_value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE papers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dedup_key TEXT NOT NULL UNIQUE,
  source_key TEXT NOT NULL,
  source_id TEXT NOT NULL DEFAULT '',
  doi TEXT,
  arxiv_id TEXT,
  title TEXT NOT NULL,
  abstract TEXT NOT NULL DEFAULT '',
  abstract_quality TEXT NOT NULL DEFAULT 'full',  -- full | short
  authors_json TEXT NOT NULL DEFAULT '[]',
  venue TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  published_at TEXT NOT NULL,
  first_seen_at TEXT NOT NULL
);
CREATE INDEX idx_papers_published ON papers(published_at);

CREATE TABLE interests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  include_keywords_json TEXT NOT NULL DEFAULT '[]',
  exclude_keywords_json TEXT NOT NULL DEFAULT '[]',
  source_keys_json TEXT NOT NULL DEFAULT '[]',
  arxiv_categories_json TEXT NOT NULL DEFAULT '[]',
  queries_json TEXT NOT NULL DEFAULT '{}',
  min_score INTEGER NOT NULL DEFAULT 4,
  max_papers_per_day INTEGER NOT NULL DEFAULT 10,
  lookback_days INTEGER NOT NULL DEFAULT 7,
  send_at TEXT NOT NULL DEFAULT '08:30',
  timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
  auto_optimize INTEGER NOT NULL DEFAULT 1,
  version INTEGER NOT NULL DEFAULT 1,
  is_active INTEGER NOT NULL DEFAULT 1,
  next_due_at TEXT,
  last_revised_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE interest_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  interest_id INTEGER NOT NULL REFERENCES interests(id),
  from_version INTEGER NOT NULL,
  to_version INTEGER NOT NULL,
  patch_json TEXT NOT NULL,
  rationale TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  rolled_back_at TEXT
);

CREATE TABLE interest_keyword_candidates (
  interest_id INTEGER NOT NULL,
  term TEXT NOT NULL,
  weight INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (interest_id, term)
);

CREATE TABLE llm_scores (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  paper_id INTEGER NOT NULL REFERENCES papers(id),
  interest_id INTEGER NOT NULL REFERENCES interests(id),
  interest_version INTEGER NOT NULL,
  score INTEGER NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE (paper_id, interest_id, interest_version)
);

CREATE TABLE llm_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  interest_id INTEGER,
  kind TEXT NOT NULL,                -- parse | score | revise
  model TEXT NOT NULL,
  prompt_tokens INTEGER NOT NULL,
  completion_tokens INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE digests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  interest_id INTEGER NOT NULL REFERENCES interests(id),
  digest_date TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  item_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  sent_at TEXT,
  UNIQUE (interest_id, digest_date)
);

CREATE TABLE digest_items (
  digest_id INTEGER NOT NULL REFERENCES digests(id),
  paper_id INTEGER NOT NULL REFERENCES papers(id),
  final_score REAL NOT NULL,
  llm_score INTEGER NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  position INTEGER NOT NULL,
  PRIMARY KEY (digest_id, paper_id)
);

CREATE TABLE user_papers (
  user_id INTEGER NOT NULL,
  paper_id INTEGER NOT NULL,
  status TEXT NOT NULL,              -- sent | hidden
  sent_at TEXT NOT NULL,
  PRIMARY KEY (user_id, paper_id)
);

CREATE TABLE feedbacks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  paper_id INTEGER NOT NULL,
  interest_id INTEGER NOT NULL,
  rating INTEGER NOT NULL,           -- 1..5
  action TEXT NOT NULL DEFAULT '',   -- useful | boring | block
  created_at TEXT NOT NULL
);

CREATE TABLE email_providers (
  key TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                -- brevo | resend | ses | smtp | postfix
  role TEXT NOT NULL,                -- primary | backup
  config_json TEXT NOT NULL,
  daily_budget INTEGER NOT NULL DEFAULT 250,
  priority INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE send_quota (
  provider_key TEXT NOT NULL,
  quota_date TEXT NOT NULL,
  sent_count INTEGER NOT NULL DEFAULT 0,
  deferred_count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (provider_key, quota_date)
);

CREATE TABLE deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  digest_id INTEGER NOT NULL REFERENCES digests(id),
  to_email TEXT NOT NULL,
  provider_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | sent | failed | deferred
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  message_id TEXT NOT NULL DEFAULT '',
  deferred_to_date TEXT,
  sent_at TEXT,
  UNIQUE (digest_id, to_email)
);

CREATE TABLE fetch_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_key TEXT NOT NULL,
  params_json TEXT NOT NULL,
  last_run_at TEXT,
  last_status TEXT NOT NULL DEFAULT '',
  UNIQUE (source_key, params_json)
);

CREATE TABLE task_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | running | done | failed
  scheduled_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_task_runs ON task_runs(status, scheduled_at);
```

全文检索（W9 用）：

```sql
CREATE VIRTUAL TABLE papers_fts USING fts5(title, abstract, content='papers', content_rowid='id');
-- papers 插入/更新/删除时同步维护（三个触发器）
```

---

## 4. 工作序列

### W1 项目初始化

产出：`pyproject.toml`、`Makefile`、`.gitignore`、目录骨架、`app/main.py` 最小可启动版本。

要点：`main.py` 挂 `/healthz` 返回 `{"status":"ok"}`；Makefile 提供 `dev / lint / test / migrate / run`。

跑通：`uv sync && make dev` 后 `curl localhost:8000/healthz` 返回 ok。

---

### W2 配置与日志

产出：`app/core/config.py`、`config/default.yaml`（§1 全文）、`app/core/logging.py`、`app/core/http.py`。

要点：
- config 四级加载：`default.yaml` → `config/config.yaml` → 环境变量（`PAPERPULSE_` 前缀）→ DB `system_settings`（W14 接入，此处留接口）。
- `http.py` 提供 `limited_get(url, source_key, **params)`：内部按 `source_key` 查 `rate_limit_json`，用令牌桶控制；统一 `User-Agent: PaperPulse/1.0 (+mailto:{contact_email})`；超时 15s。
- arXiv 必须串行：`rate_limit_json` 配 `{"interval_seconds": 3, "concurrency": 1}`。

跑通：单元测试验证配置合并顺序与限流间隔。

---

### W3 数据库与模型

产出：`app/core/db.py`、`app/models/*.py`、`migrations/`、`app/cli.py::init-db`。

要点：`db.py` 建 engine 时设 PRAGMA `journal_mode=WAL`、`busy_timeout=5000`、`mmap_size=67108864`、`synchronous=NORMAL`；`SessionLocal` 用 `scoped_session`。模型与 §3 DDL 一一对应。

跑通：`python -m app.cli init-db && python -m app.cli create-admin --email a@b.com --password x` 成功写库。

---

### W4 数据源框架与适配器

产出：`config/sources.yaml`、`app/sources/{base,registry,arxiv,openalex,crossref,doaj,europepmc,biorxiv,osf,rss,nber}.py`、`scripts/verify_sources.py`。

`SourceBase`：

```python
class SourceBase:
    key: str
    def fetch(self, start_date: str, end_date: str, params: dict) -> Iterator[Paper]: ...
    def healthcheck(self) -> tuple[bool, str]: ...
```

各源 URL 构造（照抄）：

```python
# arXiv
https://export.arxiv.org/api/query
  ?search_query=(cat:cs.AI+OR+cat:cs.CL)+AND+submittedDate:[202610010000+TO+202610032359]
  &start={offset}&max_results=100&sortBy=submittedDate&sortOrder=descending

# OpenAlex   —— 摘要需还原
https://api.openalex.org/works
  ?filter=from_publication_date:{d1},to_publication_date:{d2},default.search:{q},has_abstract:true
  &per-page=200&cursor={cursor}&mailto={contact}

# bioRxiv / medRxiv   —— 每页固定 30 条，cursor 递增
https://api.biorxiv.org/details/{server}/{d1}/{d2}/{cursor}/json

# DOAJ
https://doaj.org/api/search/articles/{urlquote(q)}?pageSize=100&sort=created_date:desc

# Europe PMC
https://www.ebi.ac.uk/europepmc/webservices/rest/search
  ?query=(FIRST_PDATE:[{d1} TO {d2}]) AND ({q})&format=json&pageSize=100&resultType=core

# OSF（provider: osf / psyarxiv / socarxiv / eartharxiv / engrxiv / medarxiv ...）
https://api.osf.io/v2/preprints/?filter[q]={q}&filter[provider]={p}&page[size]=100&sort=-date_created

# NBER（RSS）
https://www.nber.org/rss/new.xml

# Crossref（只做补全）
https://api.crossref.org/works?query.bibliographic={title}&rows=1&select=DOI,title,abstract&mailto={contact}
```

OpenAlex 摘要还原：

```python
inv = work.get("abstract_inverted_index") or {}
pos = {}
for word, places in inv.items():
    for p in places:
        pos[p] = word
abstract = " ".join(pos[i] for i in sorted(pos))
```

RSS 适配器：用 `feedparser` 解析；`source_id` 依次取 `guid` → `link` → 标题哈希；请求带 `If-Modified-Since`/`ETag`；出版社模板已在设计文档 §3.4(a)，`sources.yaml` 里每条直接写完整 URL。

`sources.yaml` 每条字段：`key / name / type / field / url_template / params / requires_key / rate_limit / enabled`。默认 `enabled: true` 的只许是 `requires_key: false` 的源。

跑通：`python scripts/verify_sources.py` 对每个免注册源打印 `✅ key  条数  最新日期`；需 Key 源打印 `— 需 Key（未配置）`。

---

### W5 采集流水线与调度

产出：`app/pipeline/fetch.py`、`app/sources/enrich.py`、`app/scheduler/{runner,jobs}.py`。

流程：
1. `build_fetch_jobs()`：扫所有 `is_active=1` 的 interests，把 `(source_key, categories, keywords)` 去重合并写入 `fetch_jobs`。
2. `run_fetch_job(job)`：窗口固定取**最近 3 天**（`fetch_window_days`），调对应适配器，逐条 `upsert_paper()`。
3. `upsert_paper(p)`：算 `dedup_key`，已存在则更新 `abstract/venue/url`，不存在则插入并写 `first_seen_at`，同时维护 FTS 触发器。
4. `enrich_paper(p)`：仅当 `doi` 为空或 `abstract` 短于 200 字符时执行——先按标题查 OpenAlex，再查 Crossref；补不到就标 `abstract_quality='short'`。
5. `scheduler/runner.py`：APScheduler `BackgroundScheduler` + `ThreadPoolExecutor(4)`；所有异步工作都先写 `task_runs(kind, payload, status='pending')` 再执行，状态流转 `pending → running → done/failed`；进程启动时把 `running` 全部改回 `pending`。
6. `scheduler/jobs.py` 注册周期：采集 30 分钟、派发 10 分钟、清理每日 04:00、画像修订每日 05:00、投递补发 2 分钟。

跑通：`python -m app.cli run-once fetch` 后 `papers` 有数据；再跑一次条数不翻倍。

---

### W6 LLM 接入层

产出：`app/llm/{base,openai_compat,anthropic,gemini,ollama,client}.py`。

```python
class LLMProvider:
    def complete_json(self, system: str, user: str, schema: dict,
                      model: str) -> tuple[dict, Usage]: ...
```

- `openai_compat`：POST `{base_url}/chat/completions`，带 `response_format={"type":"json_schema","json_schema":{...}}`。
- `anthropic`：POST `{base_url}/v1/messages`，把 schema 定义成 tool 并 `tool_choice={"type":"tool","name":"emit"}`。
- `gemini`：`POST {base_url}/v1beta/models/{model}:generateContent`，`generationConfig.responseMimeType="application/json"` + `responseSchema`。
- `ollama`：`POST {base_url}/api/chat`，`format="json"`。
- 返回值统一 `json.loads(content)`；解析失败抛 `LLMError` 并由调用方记日志。
- `client.py`：`complete_json(kind, ...)` 包装，超时（连 10s/读 60s）、429 与 5xx 退避重试 3 次、并发 ≤ 2、每次调用写 `llm_usage`。

跑通：四个 provider 各一个 respx mock 测试；`llm_usage` 有记录。

---

### W7 用户认证与前台骨架

产出：`app/web/routes/auth.py`、`templates/layout.html`、`templates/home.html`、`app/core/security.py`。

要点：argon2id 哈希；邮箱验证（发令牌链接）；服务端 session（HttpOnly + SameSite=Lax cookie，session 存 DB 或签名 cookie）；CSRF token；登录与注册速率限制用进程内令牌桶。

跑通：注册 → 收验证邮件 → 激活 → 登录成功。

---

### W8 兴趣解析与订阅向导

产出：`app/interest/{schema,parse}.py`、`app/web/routes/subscribe.py`、`templates/subscribe/*`。

`InterestProfile` schema：

```json
{
  "name": "string",
  "description": "string",
  "include_keywords": ["中文词", "english term"],
  "exclude_keywords": ["..."],
  "suggested_sources": ["arxiv", "openalex"],
  "arxiv_categories": ["cs.CV"],
  "queries": {"arxiv": "...", "openalex": "...", "europepmc": "..."},
  "min_score": 4,
  "max_papers_per_day": 8
}
```

解析 Prompt（system 部分）：

```
你是学术文献订阅配置助手。根据用户对自己研究兴趣的自然语言描述，生成结构化订阅配置。
只输出 JSON，不要任何解释文字。关键词必须同时给出中文与英文两种形式。
排除项要保守：只有用户明确表示不想要的方向才写入 exclude_keywords。
```

user 部分：用户原话 + 可用源清单（key / 覆盖领域）+ arXiv 分类词表。

前台三步：① 文本框输入 → ② 点"生成订阅配置"调 `parse.py` → ③ 编辑确认页（名称、描述、关键词 chip 增删、源勾选、分类多选、查询式、阈值滑块、推送时间、时区）→ 保存，`version=1`。

`llm_mode == 'keyword'` 时不调 LLM，直接用 jieba/空格分词 + 停用词生成最简关键词列表。

跑通：一段自然语言 → 生成草稿 → 编辑保存 → `interests` 表有记录。

---

### W9 候选筛选与 LLM 精排

产出：`app/pipeline/{prefilter,score,rank}.py`。

1. **硬过滤**：`published_at >= now - lookback_days`；标题或摘要命中任一 `exclude_keywords` 则丢弃；来源与分类限定。
2. **召回**：FTS5 BM25，`SELECT id FROM papers_fts WHERE papers_fts MATCH ? ORDER BY bm25(papers_fts, 10.0, 1.0) LIMIT 120`（`bm25` 值越小越相关）。
3. **已推排除**：`user_papers` 中已有的 `paper_id` 跳过。
4. **精排**：按 20 篇一批调用 `client.complete_json`，命中 `llm_scores` 缓存（同 `interest_version`）则跳过。

精排 Prompt：

```
你是学术文献筛选助手。根据用户的兴趣画像，判断每篇论文的相关程度。只输出 JSON 数组。

兴趣画像：{description}
必须包含的主题词：{include_keywords}
明确排除的主题词：{exclude_keywords}

论文列表（JSON）：[{id, title, abstract, venue, date}, ...]

对每篇输出：{"id": "...", "score": 0-5 整数, "reason": "不超过 40 字的中文理由"}
评分：5=直接命中核心问题；4=高度相关；3=方法或领域相关；2=弱相关；1=勉强沾边；0=无关。
只有 score>=4 值得推送。摘要信息不足时给 2 分。
```

5. **排序**：`final = 0.7*llm_score + 0.2*taste_sim + 0.1*freshness - 0.5*已推过`；`taste_sim` 用该用户历史 `rating>=4` 论文的 TF-IDF 质心余弦；`freshness = exp(-age_days/14)`。取 Top `max_papers_per_day`。

跑通：给定一份 interest，`python -m app.cli run-once digest <interest_id>` 打印出 10 篇有分数的论文。

---

### W10 邮件渲染与投递

产出：`app/pipeline/{render,deliver}.py`、`app/email/`、`templates/email/*`。

渲染：`multipart/alternative`，HTML 与纯文本两份。每条含：标题（链到 DOI）、作者前 3 位 + et al.、来源/期刊、发表日期、LLM 一句理由、相关度星级、DOI 链接、反馈链接（`★1..5` / `不感兴趣`）。头部：

```
List-Unsubscribe: <mailto:unsub@...>, <https://{site_url}/u/{token}>
List-Unsubscribe-Post: List-Unsubscribe=One-Click
X-PaperPulse-Digest: {digest_id}
```

投递：`EmailProvider` 接口 `send(msg) -> message_id`；实现 `brevo`（SMTP `smtp-relay.brevo.com:587` STARTTLS，或 API `https://api.brevo.com/v3/smtp/email`）、`resend`、`ses`、`smtp`、`postfix`。

流程：写 `deliveries(pending)` → 取 `email_providers` 中 `role=primary` 且 `enabled=1` 的 → **配额预占**：

```sql
UPDATE send_quota SET sent_count = sent_count + 1
WHERE provider_key = ? AND quota_date = ? AND sent_count < ?
-- rowcount == 0 → 该封标 deferred，deferred_to_date = 次日
```

→ 发送 → 成功写 `sent` 与 `message_id`，失败 `attempts+1` 并记 `last_error`。补发任务每 2 分钟扫 `pending` 与 `deferred`（到期的）重投。

跑通：`python -m app.cli test-email --to you@example.com` 收到一封真实邮件；配额设为 1 时第二封变 `deferred` 且次日补发。

---

### W11 定时推送调度

产出：`app/scheduler/jobs.py::dispatch_due_digests`、`app/pipeline/digest.py`。

```python
def compute_next_due(send_at: str, tz: str, now_utc) -> str:
    z = ZoneInfo(tz); local = now_utc.astimezone(z)
    h, m = map(int, send_at.split(":"))
    nxt = local.replace(hour=h, minute=m, second=0, microsecond=0)
    if nxt <= local: nxt += timedelta(days=1)
    return nxt.astimezone(timezone.utc).isoformat()
```

派发（每 10 分钟）：查 `interests WHERE is_active=1 AND (next_due_at IS NULL OR next_due_at <= ?)`，逐个入队 `build_digest`。

`build_digest(interest_id, date)`：
1. 用 `UNIQUE(interest_id, digest_date)` 插入 `digests`，冲突即返回（天然幂等）。
2. 走 W9 取 Top K → 写 `digest_items`。
3. W10 渲染并投递。
4. 写 `user_papers(status='sent')`。
5. 更新 `next_due_at = compute_next_due(...)`。

跑通：把推送时间设为 2 分钟后，等待后收到邮件；手动再跑一次不重复发送。

---

### W12 反馈与画像进化

产出：`app/web/routes/feedback.py`、`app/interest/{tfidf,revise}.py`。

- 反馈链接用 HMAC token（`security.make_token(user_id, paper_id, interest_id, action)`），免登录点击落 `feedbacks`。
- 实时轻量：rating ≤ 2 → 从标题+摘要提 TF-IDF 增量显著词 Top 3，`weight -= 1`；rating ≥ 4 → `weight += 1`；累计 ≤ −2 并入 `exclude_keywords`，≥ +2 并入 `include_keywords`（并入后 `version += 1`）。
- 周期修订（每日 05:00，按 interest 错峰）：条件 `距 last_revised_at ≥ 7 天 且 新反馈 ≥ 10 条`。取最近 30 条正样本 + 30 条负样本（标题 + 摘要前 200 字）+ 当前画像，让 LLM 输出：

```json
{
  "add_include": ["..."], "remove_include": ["..."],
  "add_exclude": ["..."], "remove_exclude": ["..."],
  "description_patch": "...",
  "min_score_delta": 0,
  "rationale": "..."
}
```

应用时按 §1 限量截断；写 `interest_revisions`；`version += 1`；更新 `last_revised_at`。

跑通：灌 15 条负反馈 → 跑 `run-once revise` → `interest_revisions` 有新记录，画像关键词被修改，前台可见 diff。

---

### W13 后台管理

产出：`app/web/routes/admin_*.py` + `templates/admin/*`。

页面：
1. **用户**：列表、启停、手动触发推送、查看该用户 LLM 用量与最近失败原因。
2. **数据源**：按领域分组列表；开关；`requires_key=1` 的行置灰并提供填 Key 入口；「连通性自检」按钮调 `healthcheck()`；可添加自定义 RSS URL。
3. **存储**：保留天数、清理时刻、清理范围（全部 / 仅未推送过）、预估清理量、立即清理、清空全部（二次确认）。
4. **邮件**：主/备 provider 配置、日额度、今日已发/剩余/顺延队列、测试发信。
5. **LLM**：厂商预设 / 自定义 base_url / 模型 / 测试连接 / 用量统计。
6. **系统自检**：出站网络、LLM、邮件、源、磁盘、内存、NTP。

跑通：后台能开关一个源并立即生效；能在存储页把保留天数改成 7 并触发清理。

---

### W14 存储清理与部署文件

产出：`app/core/retention.py`（合并进 `scheduler/jobs.py`）、`deploy/paperpulse.service`、`deploy/caddy/Caddyfile`、`deploy/backup.sh`。

清理（每日 04:00，参数取 `system_settings`）：

```sql
DELETE FROM papers WHERE id IN (
  SELECT id FROM papers WHERE first_seen_at < :cutoff LIMIT :batch
);
```
循环至无剩余，批间 `sleep(0.05)`；随后删 `user_papers`、`llm_scores` 中已无引用的行；`purge_vacuum` 为真时执行 `VACUUM`。同时删 `llm_scores` 里 `created_at < cutoff` 的记录。

`paperpulse.service`：

```ini
[Service]
Type=notify
User=paperpulse
WorkingDirectory=/opt/paperpulse
ExecStart=/opt/paperpulse/.venv/bin/python -m app.main
Restart=always
RestartSec=10
WatchdogSec=60
MemoryMax=700M
CPUQuota=80%
TimeoutStopSec=150
```

应用每 30 秒 `sd_notify("WATCHDOG=1")`（`systemd` 包或写 `/run/systemd/notify` socket）。

Caddy：

```
your.domain {
  reverse_proxy 127.0.0.1:8000
}
```

备份（每日 03:30 cron）：`sqlite3 data/paperpulse.db "VACUUM INTO '/opt/paperpulse/data/backup/paperpulse-$(date +%F).db'"`，保留 7 份。

---

### W15 文档与发布

产出：`README.md`、`docs/{architecture,deployment,data-sources,llm-providers,email-delivery}.md`、`LICENSE`（Apache-2.0）、`SECURITY.md`、`CONTRIBUTING.md`、`.github/workflows/ci.yml`、`dependabot.yml`、issue/PR 模板。

README 必含：一句话简介、内置源清单表（含验证日期）、裸机部署 5 步、环境变量表、arXiv 致谢语 *"Thank you to arXiv for use of its open access interoperability."*。

CI：`ruff` + `mypy` + `pytest`。

---

## 16. 进度清单

```
[x] W1  项目初始化
[x] W2  配置与日志
[x] W3  数据库与模型
[x] W4  数据源框架与适配器
[x] W5  采集流水线与调度
[x] W6  LLM 接入层
[x] W7  用户认证与前台骨架
[x] W8  兴趣解析与订阅向导
[x] W9  候选筛选与 LLM 精排
[x] W10 邮件渲染与投递
[x] W11 定时推送调度
[x] W12 反馈与画像进化
[x] W13 后台管理
[x] W14 存储清理与部署文件
[x] W15 文档与发布
```

## 17. 施工记录（v2.0 完成后补）

### 已验证

| 项 | 结果 |
|---|---|
| `make lint`（ruff + mypy） | 通过：ruff 0 问题，mypy 75 文件 0 问题 |
| `make test`（pytest） | 35 项全部通过 |
| `alembic upgrade head` 空库 | 25 张表 + FTS5 虚表 + 3 个同步触发器 |
| 采集（arXiv / NBER） | arXiv 1644 篇、NBER 43 篇入库；二次运行不翻倍（dedup_key 幂等） |
| 端到端摘要 | 召回 120 → 排序 → 取 5 篇 → 写 user_papers → 入队 delivery |
| 邮件投递（本地 SMTP 实测） | multipart/alternative，含 `List-Unsubscribe` / `List-Unsubscribe-Post` / `X-PaperPulse-Digest` |
| 日配额治理 | 额度设为 1 时：第 1 封 sent，第 2 封 deferred 且 `deferred_to_date` = 次日 |
| 反馈闭环 | 6 条反馈 → 3 次画像轻量调整（v1→v4）→ 回滚到 v1 成功 |
| 引导门禁 | 未完成 setup 时 `/` 与 `/admin` 均 303 到 `/admin/setup` |
| 页面渲染 | 前台 4 页 + 后台 6 页全部 200 |

### 实现偏离说明（3 处，均为有意为之）

1. **LLM 与邮件 SDK 未使用官方包**：四个 LLM provider 与 Brevo / Resend 均用 httpx 直连 REST 端点，避免引入 `openai` / `anthropic` / `google-generativeai` / `boto3` 等重型依赖——这与 1C1G 的 95MB 依赖预算直接冲突。
2. **SES 走 SMTP 端点**（`email-smtp.{region}.amazonaws.com:587）而非 API，同样是避免 boto3。发送额度需在 AWS 控制台自管。
3. **Alembic 初始迁移用 `Base.metadata.create_all` + `fts_ddl`** 而非逐条 `op.create_table`：FTS5 是虚表，autogenerate 无法识别且会误判为"待删除表"。此写法保证迁移与模型始终一致。

### 已知限制

- **关键词模式下的中文召回**：未配 LLM 时用 jieba 分词，产出的是中文关键词，而论文池以英文元数据为主，FTS5（unicode61 分词）无法跨语言匹配，召回会显著受限。配置 LLM 后生成中英双语关键词，该限制消失。已在 README 标注。
- 退信 webhook（`/webhooks/bounce`）未实现端点，需在 provider 侧配置后另行接入。
- 进程内限流在多实例部署下失效（Lite 模式为单进程，不受影响）。

---

*开发指南 v2.0 · 对应设计方案 v2.2 · 2026-10-03*
