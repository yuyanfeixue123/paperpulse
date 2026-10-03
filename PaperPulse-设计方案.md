# PaperPulse 设计方案 v2.2

> 定时采集最新论文元数据 → LLM 按用户兴趣画像筛选 → 每日邮件推送 → 按反馈进化画像。
> 目标：Linux 自托管、多用户、无人值守、标准开源仓库发布。
> 本文是**设计与决策文档**，不含实现代码。源链接核实日期：2026-10-03。

---

## 0. v1.0 → v2.0 变更摘要

| # | 需求 | 设计响应 | 影响范围 |
|---|---|---|---|
| 1 | 最低 1C1G 稳定运行 | 新增 **Lite 运行模式**：单进程（uvicorn + APScheduler + 线程池）+ SQLite WAL，无 Redis/Postgres/Docker；给出逐项内存预算与降级参数 | 架构、部署 |
| 2 | 后台自定义论文池清空时间 | 新增**存储生命周期模块**：保留天数 + 清理时刻 + 分批清理 + 手动立即清理 + 清空全部 + 磁盘水位自保 | 数据模型、后台 |
| 3 / 8 | 写清各源可用链接（尤其 RSS/Atom）+ 默认带尽可能多源并可开关 | 新增**信息源总表**（30+ 源，含 URL 模板、验证状态、默认开关），统一收敛到 `config/sources.yaml`，后台开关与连通性自检 | 新增章节 3 |
| 4 | LLM 支持主流模型 | 新增 **LLM Provider 抽象层**：OpenAI 兼容 / Anthropic 原生 / Gemini 原生 / Ollama，含结构化输出三级降级 | 新增章节 4 |
| 5 | 前台自然语言输入兴趣，配置 API 后建订阅时由 LLM 解析 | 新增**订阅创建向导**与 `InterestProfile` 解析流程、Schema、Prompt、降级 | 新增章节 5 |
| 6 | 论文兴趣度评价 → 后台调整画像 | 新增**反馈闭环**：反馈采集 + 实时轻量调整 + 周期性 LLM 画像修订（版本化、可 diff、可回滚） | 新增章节 6 |
| 7 | 邮件免费、可靠、长期批量 | 调研结论：**Brevo 免费版 300 封/天永久免费可支撑约 300 用户**，第三方方案足够，**无需服务器直发**；新增日配额治理（超限顺延补发）、主备通道自动切换、Postfix 直发降级为兜底（阿里云/腾讯云上条款禁止） | 章节 7 重写 |
| 10 | 部署形态为裸机 | Docker 降级为可选；交付目标锁定 systemd + uv venv；新增裸机前置检查清单与升级回滚流程 | 章节 10 重写 |
| 11 | 需个人注册/申请 Key 才能启用的内容，一律改为首次部署时后台设置 | 新增 §0.1 **配置责任边界原则**与 §10.4 **首次部署引导（6 步）**：邮件必配、LLM 可显式降级为关键词模式；用户订阅流程删除 API 配置步骤，改为纯自然语言输入；数据源标注 `requires_key`，需 Key 源未配凭据时开关置灰 | 章节 0.1 / 5.1 / 10.4 重写 |
| 9 | 常态化无人值守 | 新增**守护与自愈**：systemd 看门狗、任务卡死重置、外部心跳、日志轮转、备份、优雅关机 | 新增章节 8 |

---

## 0.1 配置责任边界：注册类依赖一律收归首次部署后台设置（设计原则）

**原则**：终端用户**永远不需要**为了使用本应用去注册任何第三方服务或申请 API Key。所有"必须注册/申请才能生效"的外部依赖，一律由**部署者**在首次部署时于后台一次性配置完成；应用在完成该引导前处于**未启用**状态（访问任何页面均重定向到 `/admin/setup`）。

| 依赖 | 是否需个人注册 | 谁配置 | 何时配置 | 未配置的后果 |
|---|---|---|---|---|
| **LLM API Key** | 是 | **部署者（后台）** | 首次部署引导 Step 3 | 无法做兴趣解析与 AI 精排；部署者可显式选择"纯关键词模式"降级启用 |
| **邮件通道账号**（Brevo 等） | 是 | **部署者（后台）** | 首次部署引导 Step 4 | **无法启用**：不能发验证邮件与每日推送 |
| **默认数据源**（arXiv / OpenAlex / Crossref / DOAJ / Europe PMC / bioRxiv / medRxiv / OSF / 出版社 RSS） | **否，全部免注册免 Key** | 部署者（后台开关） | 首次部署引导 Step 5 | 无 |
| **可选增强源**（Semantic Scholar / PubMed / Zenodo / HAL / Unpaywall） | 是（免费申请） | 部署者（后台，可选） | 任何时候 | 默认关闭，不影响运行 |
| 用户自带 LLM Key（BYOK） | 可选 | 用户（高级设置） | 任何时候 | 无，自动回落到全局 Key |
| 用户自带 SMTP | 可选 | 用户（高级设置） | 任何时候 | 无，自动回落到全局通道 |

**设计不变量**：默认开启的数据源必须全部是**免注册、免 Key** 的。`config/sources.yaml` 中每个源显式标注 `requires_key: true|false`；后台对需 Key 的源显示"需 Key"徽章并提供填写入口，**未填 Key 的源不可启用**（开关置灰）。任何新增源若 `requires_key: true`，则默认关闭。

> 这条原则同时修正了 v2.0 中的订阅向导设计：原本要求"用户先配置自己的 LLM API 再建订阅"，现改为**用户只需用自然语言描述兴趣**，LLM 凭据由部署者在首次部署时统一配置。

---

## 1. 运行环境：1C1G 下的稳定运行（需求 1）

### 1.1 两套运行模式（同一套代码，`RUN_MODE` 切换）

| | **Lite 模式（默认，1C1G 目标）** | **Standard 模式（≥2C4G）** |
|---|---|---|
| 进程 | **单进程**：uvicorn 1 worker + APScheduler 后台线程 + `ThreadPoolExecutor(4)` | web / worker / beat 分离 |
| 任务队列 | **SQLite 任务表 `task_runs`**（状态机持久化） | Celery + Redis |
| 数据库 | **SQLite**（WAL 模式） | PostgreSQL 16 |
| 缓存 / 锁 | 进程内令牌桶 + SQLite 行锁 | Redis |
| 部署 | systemd + venv（uv），**不用 Docker** | Docker Compose（+ Caddy） |
| 适用规模 | ≤ 200 用户、日采集 ≤ 5000 篇 | 更大规模 |

**为什么 Lite 模式不用 Redis/Celery**：Redis 常驻约 15–30MB 且需额外进程守护；每个 Celery worker 约 60–90MB。在 1GB 内存里这是纯浪费——本应用的瓶颈是**外网 API 等待**（IO 密集），不是 CPU 计算，一个进程的线程池足够把带宽跑满。任务表落 SQLite 反而比 Redis 更可靠：进程崩溃后未完成的任务仍在表里，重启自动续跑。

### 1.2 内存预算（1C1G，单位 MB RSS）

| 项目 | 预算 | 说明 |
|---|---|---|
| OS 基础（Debian/Ubuntu minimal + sshd + journald + chrony） | 110 | 无桌面、无 snap |
| Python 3.12 + 依赖（FastAPI / uvicorn / httpx / SQLAlchemy / Jinja2 / APScheduler） | 95 | 精简依赖，**不装** pandas / numpy / torch |
| 应用常驻（路由、模板缓存、连接池） | 35 | |
| 任务执行峰值（并发打分 / 渲染 / XML 解析） | 60 | 峰值，非稳态 |
| **合计（目标）** | **≈ 300** | 告警线 450，硬限 700 |
| 安全垫 | 余 ~500MB 给 page cache 与突发 | |

**兜底**：配置 512MB swap（优先 zram；1C1G 机器上比磁盘 swap 更不容易拖垮 IO）。

### 1.3 1C1G 下的默认约束参数

```yaml
# config/default.yaml（Lite 模式默认值）
run_mode: lite
web:
  uvicorn_workers: 1
  limit_concurrency: 20          # 防突发请求撑爆内存
  timeout_keep_alive: 5
scheduler:
  executor_threads: 4            # 1 核够用，>4 只会增加上下文切换
  misfire_grace_time: 900        # 任务迟到 15 分钟内仍执行
  task_soft_timeout: 600         # 单任务软超时 10 分钟
pipeline:
  fetch_concurrency: 1           # 采集串行（arXiv 要求单连接，且省内存）
  llm_concurrency: 2             # LLM 并发（网络 IO，非 CPU）
  llm_batch_size: 20
  abstract_max_chars: 1200       # 摘要截断，控 token 与内存
  candidate_top_n: 120           # 进入 LLM 的候选上限
  fetch_page_size: 100           # 分页处理，不全量载入内存
db:
  sqlite_wal: true
  sqlite_busy_timeout_ms: 5000
  sqlite_mmap_size_mb: 64        # 限制 mmap，防止 RSS 虚高
memory:
  rss_warn_mb: 450               # 超阈值：降并发 + 主动 GC + 记日志
  rss_hard_mb: 650               # 超硬限：暂停采集，只保推送
disk:
  usage_warn_pct: 80             # 触发一次紧急清理
  usage_hard_pct: 90             # 停止采集，仅保留推送与清理
```

**流式处理原则**：采集与筛选全程分页 / 生成器处理，**禁止**把论文整表读进内存排序；打分按批 20 篇进出，批完即释放。

### 1.4 验收标准（需求 1）

- 空载稳态 RSS ≤ 250MB；每日推送高峰 ≤ 380MB；连续 7 天无 OOM、swap 无持续增长。
- 单核平均负载 < 0.6，峰值 < 1.5。
- 20 用户规模下，每日完整流程（采集 + 筛选 + 推送）30 分钟内跑完。

---

## 2. 存储生命周期与论文池清理（需求 2）

### 2.1 后台可配置项

| 配置项 | 默认 | 说明 |
|---|---|---|
| `retention_days` | **30** | 论文池保留天数，超期清理 |
| `purge_cron` | `0 4 * * *` | 清理执行时刻（**站点本地时区**，后台直接选小时/分钟） |
| `purge_scope` | `all` / `unsent_only` | 全部超期清理，或只清理"从未推送过"的（保留用户历史可回溯） |
| `purge_batch_size` | 1000 | 分批删除，避免长事务锁表与内存尖峰 |
| `purge_vacuum` | `true` | 清理后 `VACUUM`（SQLite 删数据不自动还空间） |
| `purge_dry_run` | — | 后台"预估清理量"按钮，只算不删 |
| `max_pool_rows` | 200000 | 行数硬上限，达到即触发紧急清理（与保留天数取更严者） |
| 手动操作 | — | 「立即清理」「清空全部论文池」（后者二次输入确认 + 审计日志） |

后台面板实时显示：**论文池总条数 / 估算占用 / 最早论文日期 / 下次清理时间 / 上次清理释放量**。

### 2.2 存储量估算（后台按此公式预测）

```
单条论文元数据 ≈ 2.5 KB（含索引与页开销）
估算占用(MB) ≈ 每日新增条数 × 保留天数 × 2.5 / 1024
```

| 每日采集量 | 保留 14 天 | 保留 30 天 | 保留 90 天 |
|---|---|---|---|
| 800 篇（默认源） | 27 MB | 59 MB | 176 MB |
| 3000 篇（开 15 源） | 103 MB | 220 MB | 659 MB |
| 6000 篇（全开） | 205 MB | 440 MB | 1.3 GB |

> 1C1G 机器通常配 20–40GB 盘。**默认保留 30 天 + 默认源 ≈ 60MB**，完全可控。后台在日采集 > 5000 篇时提示把保留天数降到 14 天。

### 2.3 清理任务实现要点

1. 分批：`DELETE FROM papers WHERE id IN (SELECT id FROM papers WHERE first_seen_at < ? LIMIT ?)`，循环直到无剩余，批间 `sleep(50ms)` 让出 IO。
2. 关联清理：`user_papers`、`llm_scores` 中已无引用的行一并批删。
3. `unsent_only` 模式下，已被推送过的论文（在 `user_papers` 有记录）不动。
4. `VACUUM` 需额外约一倍临时空间：执行前检查磁盘余量 > 当前 DB 大小 × 1.2，否则跳过并告警。
5. 清理任务本身也进 `task_runs`，失败可重试；清理期间采集任务让行（同一把 SQLite 写锁）。

### 2.4 磁盘水位自保（无人值守关键）

- 每 30 分钟检查磁盘占用：> `usage_warn_pct` → 立即跑一次清理（保留天数临时收紧到 7 天）；> `usage_hard_pct` → 停止采集、只保留推送与清理，并告警管理员。
- SQLite 写入失败（`SQLITE_FULL`）→ 立即停机保护（保住任务状态）+ 告警。

---

## 3. 信息源总表与可用链接（需求 3、8）

### 3.1 设计约定

- 所有源定义集中在 **`config/sources.yaml`**（源 ID、类型、领域、URL 模板、参数、限流、默认开关、字段能力），每个源显式标注 **`requires_key: true|false`**。后台读取该文件渲染开关列表，开关状态写配置层（不直接改仓库文件，避免升级冲突）。
- **需 Key 的源在未配置凭据时开关置灰不可启用**，后台提供填写入口；填 Key 后即可启用。默认开启的源全部为免注册源。
- 统一接口：`fetch(window) -> Iterator[Paper]`、`healthcheck() -> bool`。
- **验证状态图例**：🟢 已核实（官方说明/实测）｜🟡 按官方模板推导，部署时须现场验证｜⚪ 待验证 / 可选。
- 后台提供「**源连通性自检**」按钮：对每个源发最小请求，返回延迟、条数、最近一条日期；失败给出原因（DNS / 403 / 超时 / 解析失败）。

> ⚠️ 两个必须知道的现状：
> - **arXiv 官方分类 RSS 页已下线**（`info.arxiv.org/help/rss` 返回 404）。arXiv 的 API 返回的就是 Atom，**直接用查询串当 feed 订阅**（见 3.4）。
> - **JournalTOCs 已于 2026-09-06 停止解析**，Zetoc 于 2022 退役。**不要依赖任何第三方 TOC 聚合器**，只用出版社自己的 feed。

### 3.2 全领域 / 综合源（API）

| ID | 名称 | 接口 URL 模板 | 认证 | 摘要 | DOI | 限流 | 默认 |
|---|---|---|---|---|---|---|---|
| `arxiv` | arXiv | `https://export.arxiv.org/api/query?search_query={q}&start={s}&max_results={n}&sortBy=submittedDate&sortOrder=descending` | 无 | ✅ | 部分（新论文有 `10.48550/*`） | **≤1 请求/3 秒，单连接**；单次 ≤2000 | 🟢 开 |
| `openalex` | OpenAlex | `https://api.openalex.org/works?filter=from_publication_date:{d1},to_publication_date:{d2},default.search:{q}&per-page=200&cursor=*&mailto={email}` | 无（`mailto` 进 polite pool） | ✅ `abstract_inverted_index` | ✅ | per-page ≤200，游标分页 | 🟢 开 |
| `crossref` | Crossref | `https://api.crossref.org/works?filter=from-pub-date:{d1},until-pub-date:{d2}&rows=200&cursor=*&select=DOI,title,abstract,published&mailto={email}` | 无 | ⚠️ 约 30–50%，JATS XML | ✅ 权威 | rows ≤1000 | 🟢 开（DOI 校验/补全） |
| `doaj` | DOAJ | `https://doaj.org/api/search/articles/{query}?pageSize=100&sort=created_date:desc`（v3：`/api/v3/search/articles/{query}`） | 无 | ✅ | ✅ | 建议 2–5 req/s | 🟢 开 |
| `europepmc` | Europe PMC | `https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=(FIRST_PDATE:[{d1} TO {d2}]) AND ({q})&format=json&pageSize=100&resultType=core` | 无 | ✅ `abstractText` | ✅ | 合理并发 | 🟢 开 |
| `osf` | OSF Preprints | `https://api.osf.io/v2/preprints/?filter[q]={q}&filter[provider]={p}&page[size]=100&sort=-date_created` | 无 | ✅ `description` | ✅ | page[size] ≤100 | 🟢 开 |
| `zenodo` | Zenodo | `https://zenodo.org/api/records?q={q}&sort=mostrecent&size=25&page={p}` | 读公开记录无需 token | ✅ `metadata.description` | ✅ | 匿名 size ≤25，认证 ≤100 | 🟡 关（数据集噪音大） |
| `hal` | HAL | `https://api.archives-ouvertes.fr/search/?q={q}&rows=100&sort=producedDate_tdate+desc&fl=title_s,abstract_s,doiId_s,uri_s` | 无 | ✅（多语言） | 部分 | 合理并发 | 🟡 关 |
| `s2` | Semantic Scholar | `https://api.semanticscholar.org/graph/v1/paper/search/bulk?query={q}&fields=title,abstract,externalIds,publicationDate` | **需免费 key** | ✅ | ✅ | key 决定 | 🟡 关 |
| `pubmed` | PubMed E-utilities | `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?...` + `efetch.fcgi` | 无（有 key 更快） | ✅ | 部分 | 无 key 3 req/s，有 key 10 | 🟡 关 |
| `repec` | RePEc / IDEAS | `https://ideas.repec.org/cgi-bin/htsearch?q={q}&cmd=Search&fmt=json&ps=50`（元数据 `https://api.repec.org/handle?handle={h}`） | 无 | 部分 | 部分 | 合理并发 | ⚪ 关 |
| `unpaywall` | Unpaywall | `https://api.unpaywall.org/v2/{doi}?email={email}` | 无 | ✗ | 输入 | 维护状态，仅作 OA 链接补全 | ⚪ 关 |

**OSF 一个接口覆盖多个专业预印本**（`filter[provider]`）：`osf`（综合）、`psyarxiv`（心理）、`socarxiv`（社会）、`eartharxiv`（地学）、`engrxiv`（工程）、`medarxiv`（医学）、`nutrixiv`（营养）、`biohackrxiv`、`metaarxiv`、`inarxiv`、`sportrxiv`、`thesiscommons`。后台按 provider **各自独立开关**。

### 3.3 专业领域源（API）

| ID | 领域 | 接口 | 默认 |
|---|---|---|---|
| `biorxiv` | 生命科学 | `https://api.biorxiv.org/details/biorxiv/{d1}/{d2}/{cursor}/json`（每页 **30** 条；`?category=cell_biology`；interval 也支持 `Nd`） | 🟢 开 |
| `medrxiv` | 医学 | `https://api.biorxiv.org/details/medrxiv/{d1}/{d2}/{cursor}/json` | 🟢 开 |
| `chemrxiv` | 化学 | `https://chemrxiv.org/engage/chemrxiv/public-api/v1/items?term={q}&limit={n}&sort=PUBLISHED_DATE_DESC&searchDateFrom={iso}`；按 DOI：`https://www.cambridge.org/engage/coe/public-api/v1/items/doi/{doi}` | 🟡 开 |
| `nber` | 经济 | API：`https://www.nber.org/api/v1/working_page_listing/contentType/working_paper/_/_/search?page=1&perPage=20&q={q}&newThisWeek=true`；RSS：`https://www.nber.org/rss/new.xml`（🟢 实测可用，约 42 条） | 🟢 开（RSS） |
| `essoar` | 地学 / 空间 | `https://www.essoar.org/`（Atypon Literatum 平台，feed 见 3.4 模板） | ⚪ 关 |
| `techrxiv` | 工程 / IEEE | `https://techrxiv.org/`（同上，feed 待现场验证） | ⚪ 关 |
| `scielo_preprints` | 综合（拉美） | `https://preprints.scielo.org/index.php/scielo` | ⚪ 关 |
| `preprints_org` | 综合（MDPI） | `https://www.preprints.org/` | ⚪ 关 |

### 3.4 RSS / Atom 源（重点）

#### (a) 出版社 URL 模板（🟢 已核实，2026 年实测有效）

| 出版社 / 平台 | 模板 | 参数示例 |
|---|---|---|
| **Nature Portfolio** | `https://www.nature.com/{code}.rss` | `nature`、`ncomms`、`srep`、`natmachintell`、`nmeth`、`ng` |
| **Science / AAAS（Atypon）** | `https://www.science.org/action/showFeed?type=etoc&feed=rss&jc={code}` | `science`、`sciadv`、`scisignal`、`scitranslmed` |
| **PNAS** | `https://www.pnas.org/action/showFeed?type=etoc&feed=rss&jc=PNAS` | |
| **Cell Press** | `https://www.cell.com/{code}/current.rss` | `cell`、`molecular-cell`、`developmental-cell` |
| **Elsevier（ScienceDirect）** | `https://rss.sciencedirect.com/publication/science/{ISSN}` | ISSN **去掉横杠**，如 `00928674` |
| **Wiley** | `https://onlinelibrary.wiley.com/feed/{eISSN}/most-recent` | 在线 ISSN |
| **Taylor & Francis** | `https://www.tandfonline.com/feed/rss/{code}` | 期刊地址中的 code |
| **Springer / Nature Link** | `https://link.springer.com/search.rss?facet-journal-id={id}&search-within=Journal&query=` | 期刊 ID |
| **SAGE** | `https://journals.sagepub.com/action/showFeed?type=etoc&feed=rss&jc={code}` | |
| **ACS** | `https://pubs.acs.org/action/showFeed?type=etoc&feed=rss&jc={code}` | |
| **NEJM（Atypon）** | `https://www.nejm.org/action/showFeed?jc=nejm&type=etoc&feed=rss` | |
| **The Lancet** | `https://www.thelancet.com/rssfeed/{journal}_current.xml` | `lancet` |
| **JAMA** | `https://jamanetwork.com/rss/site_3/67.xml` | |
| **BMJ** | `https://www.bmj.com/rss/recent.xml` | |
| **eLife** | `https://elifesciences.org/rss/recent.xml` | |
| **PLOS（Atom）** | `https://journals.plos.org/{journal}/feed/atom` | `plosone`、`plosbiology`、`plosmedicine`、`plosgenetics`、`ploscompbiol` |
| **bioRxiv（RSS）** | `https://connect.biorxiv.org/biorxiv_xml.php?subject={s}` | `all`；或 `bioinformatics`、`genomics`、`neuroscience`、`cancer_biology`、`immunology`、`microbiology`（下划线代空格，多个用 `+` 连接） |
| **medRxiv（RSS）** | `https://connect.medrxiv.org/medrxiv_xml.php?subject={s}` | 同上 |
| **NBER** | `https://www.nber.org/rss/new.xml` | 🟢 实测 200 / 约 42 条 |
| **PubMed（保存检索）** | `https://pubmed.ncbi.nlm.nih.gov/rss/search/{saved_search_id}?limit=100` | 需在 PubMed 网页创建检索后取该地址 |
| **Europe PMC** | `https://europepmc.org/RssFeeds` | 按期刊 / 检索生成 |
| **arXiv（Atom = RSS）** | `https://export.arxiv.org/api/query?search_query={q}&sortBy=submittedDate&sortOrder=descending&max_results=100` | `{q}` 如 `cat:cs.AI`、`all:"urban+planning"`、`au:hinton`；**遵守 3 秒/请求** |

#### (b) 默认内置 feed 清单（`config/sources.yaml` 节选）

```yaml
- id: nature_main      url: https://www.nature.com/nature.rss                                    field: multidisciplinary enabled: true
- id: nature_comms     url: https://www.nature.com/ncomms.rss                                    field: multidisciplinary enabled: true
- id: sci_reports      url: https://www.nature.com/srep.rss                                      field: multidisciplinary enabled: false
- id: science_main     url: https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=science field: multidisciplinary enabled: true
- id: science_advances url: https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=sciadv  field: multidisciplinary enabled: true
- id: pnas             url: https://www.pnas.org/action/showFeed?type=etoc&feed=rss&jc=PNAS      field: multidisciplinary enabled: true
- id: cell             url: https://www.cell.com/cell/current.rss                                field: life_sciences    enabled: false
- id: elife            url: https://elifesciences.org/rss/recent.xml                             field: life_sciences    enabled: true
- id: plos_one         url: https://journals.plos.org/plosone/feed/atom                          field: multidisciplinary enabled: true
- id: biorxiv_all      url: https://connect.biorxiv.org/biorxiv_xml.php?subject=all              field: life_sciences    enabled: true
- id: medrxiv_all      url: https://connect.medrxiv.org/medrxiv_xml.php?subject=all              field: medicine         enabled: true
- id: nber_new         url: https://www.nber.org/rss/new.xml                                     field: economics        enabled: true
- id: arxiv_cs_ai      url: https://export.arxiv.org/api/query?search_query=cat:cs.AI&sortBy=submittedDate&sortOrder=descending&max_results=100   field: cs          enabled: true
- id: arxiv_cs_cv      url: https://export.arxiv.org/api/query?search_query=cat:cs.CV&...        field: cs          enabled: false
- id: arxiv_cs_lg      url: https://export.arxiv.org/api/query?search_query=cat:cs.LG&...        field: cs          enabled: false
- id: arxiv_cs_cl      url: https://export.arxiv.org/api/query?search_query=cat:cs.CL&...        field: cs          enabled: false
- id: arxiv_econ_gn    url: https://export.arxiv.org/api/query?search_query=cat:econ.GN&...      field: economics   enabled: false
- id: arxiv_qfin_gn    url: https://export.arxiv.org/api/query?search_query=cat:q-fin.GN&...     field: finance     enabled: false
- id: arxiv_stat_me    url: https://export.arxiv.org/api/query?search_query=cat:stat.ME&...      field: statistics  enabled: false
- id: arxiv_eess_sp    url: https://export.arxiv.org/api/query?search_query=cat:eess.SP&...      field: engineering enabled: false
- id: arxiv_qbio       url: https://export.arxiv.org/api/query?search_query=cat:q-bio.*&...      field: biology     enabled: false
- id: arxiv_physics    url: https://export.arxiv.org/api/query?search_query=cat:physics.*&...    field: physics     enabled: false
- id: arxiv_math       url: https://export.arxiv.org/api/query?search_query=cat:math.*&...       field: mathematics enabled: false
```

#### (c) RSS 源的工程要点（必做）

1. **RSS 通常没有标准 DOI 与完整摘要** → 设计 `enrich_paper` 步骤：按 (i) link 中的 DOI；(ii) 标题精确匹配查 OpenAlex；(iii) 标题查 Crossref —— 补出 DOI 与摘要；补不到则用 feed 的 `description`，并标记 `abstract_quality=short`，LLM 打分时提示降权。
2. **RSS 是"订阅制"，API 是"检索制"**：RSS 源不过滤关键词（feed 给什么收什么），靠后续关键词 + LLM 筛；API 源才带关键词召回。二者在候选池合并去重。
3. **条件请求**：对 RSS 源做 `If-Modified-Since` / `ETag`，304 直接跳过，省带宽与 CPU。
4. **去重键**：`<guid>` → `link` → 标题哈希，作为 `source_id`。
5. **用户自定义 feed**：后台/前台可添加任意 RSS URL。入库前 SSRF 校验（仅 http/https，解析后拒绝内网/回环/链路本地 IP）；支持"粘贴期刊主页自动发现"（解析 `<link rel="alternate" type="application/rss+xml">`）。

### 3.5 中文源现状（诚实说明）

| 源 | 状态 |
|---|---|
| ChinaXiv `http://www.chinaxiv.org/` | ⚪ 待验证，接口不稳定，默认关闭 |
| 中国知网 / 万方 / 维普 | ❌ 无开放 API，明确不做 |
| 中文期刊（玛格泰克等平台） | ⚪ 仅 HTML 抓取，脆弱，默认关闭 |
| **现实替代** | 通过 **OpenAlex / Crossref / DOAJ** 检索中文期刊的英文元数据条目（多数中文期刊已被 Crossref 收录 DOI） |

### 3.6 源开关与默认启用策略（需求 8）

- 后台「数据源」页：按领域分组（综合 / CS / 生命科学 / 医学 / 化学 / 地学 / 经济 / 工程 / 人文社科 / 中文），每个源一行：开关、类型徽章、上次运行时间、成功条数、错误提示、连通性自检按钮。
- **默认开启**：arXiv（按领域推荐分类）、OpenAlex、Crossref（仅补全）、DOAJ、Europe PMC、bioRxiv、medRxiv、OSF（综合）、Nature / Science / PNAS / eLife / PLOS ONE / NBER 的 RSS —— 兼顾覆盖与噪音。
- **默认关闭但内置**：其余 20+ 源，一行开关启用。
- 全局开关之外，**每个用户兴趣画像可再选子集**（LLM 解析兴趣时推荐应开哪些源）。
- 新增源不需要改代码：在 `sources.yaml` 加一段 + 选用已有适配器（`arxiv` / `openalex` / `biorxiv` / `api_json` / `api_atom` / `rss` / `osf`），支持字段映射。

---

## 4. LLM 接入层（需求 4）

### 4.1 Provider 抽象

```python
class LLMProvider(Protocol):
    def complete_json(self, system: str, user: str, schema: dict,
                      model: str, timeout: int) -> tuple[dict, Usage]: ...
```

| 适配器 | 协议 | 覆盖厂商 | 结构化输出手段 |
|---|---|---|---|
| `openai_compatible` | OpenAI Chat Completions | OpenAI、DeepSeek、通义千问（兼容模式）、Moonshot、智谱 GLM、硅基流动、OpenRouter、Groq、Together、Fireworks、本地 vLLM / LM Studio / Ollama 兼容端点 | `response_format: {type: json_schema}` |
| `anthropic` | Anthropic Messages | Claude 全系（含兼容层暴露的端点） | tool-use 强制调用（schema 定义成 tool） |
| `gemini` | Google Generative Language | Gemini 全系 | `responseMimeType: application/json` + `responseSchema` |
| `ollama` | Ollama 本地 | Llama / Qwen / GLM 等本地模型 | `format: json`（或文本解析） |

统一配置三元组 **`base_url` + `api_key` + `model`**，三者齐全即可接入任何兼容端点。后台/前台提供「厂商预设」下拉（一键填入 base_url 与可选模型列表），也允许完全自定义。

### 4.2 结构化输出三级降级（保证任何模型都能跑）

1. `response_format: json_schema`（OpenAI 兼容 / Gemini 原生）
2. 不支持 → **tool-use 强制调用**（Anthropic、部分兼容层）
3. 仍不支持 → **纯文本 + 服务端容错解析**：prompt 给出 JSON 示例并要求"只输出 JSON"；服务端提取首个 `[...]` / `{...}`、修复尾逗号、去掉 ```json 围栏；解析失败 → 拆半批重试 1 次 → 再失败则整批降级为 BM25 排序，并在推送中注明"本次未启用 AI 精排"。

### 4.3 模型角色分工（成本与质量平衡）

| 用途 | 推荐档位 | 频率 |
|---|---|---|
| **兴趣点解析**（需求 5） | 中档（GPT-4o-mini / DeepSeek-V3 / Qwen-Plus / Claude Haiku） | 每次建订阅 1 次 |
| **论文精排** | 便宜档（DeepSeek-V3 / Qwen-Flash / GPT-4o-mini / Gemini Flash / 本地 7B） | 每用户每日约 6 次批调用 |
| **画像修订**（需求 6） | 中档 | 每用户每周级 |

### 4.4 调用治理

- 超时：连接 10s、读取 60s；重试 3 次（2^n + jitter），仅对 429 / 5xx / 超时重试。
- 并发：Lite 模式 ≤2；每用户每日 token 预算可配（超限自动降级为纯关键词筛选并通知）。
- 每次调用记录 `model / prompt_tokens / completion_tokens / latency / 成功与否`，后台按用户、按天统计成本。
- Key 加密存储（Fernet，`ENCRYPTION_KEY` 来自环境变量）。**全局 Key 由部署者在首次部署引导中必配**，是所有用户调用的默认凭据；**BYOK 为可选高级功能**，用户填了才走其自有 Key 与预算，未填自动回落全局 Key。

---

## 5. 订阅创建与兴趣点 LLM 解析（需求 5）

### 5.1 前台流程

> **前置条件**：LLM 凭据由部署者在首次部署时于后台统一配置（§10.4 Step 3），**用户无需申请任何 API**。用户侧只做一件事：用自然语言描述兴趣。

```
新建订阅（用户侧，3 步）
 ├─ Step 1  自然语言描述兴趣
 │    · 文本框 + 引导示例：
 │      "我在读城市规划硕士，关心用街景图像和深度学习做建成环境评估，
 │       也想看 15 分钟生活圈和公共交通可达性的研究。
 │       不想看纯算法理论推导，也不想看医学图像分割。"
 │    · 若系统为"关键词降级模式"，此处提示当前无 AI 解析，改用关键词抽取
 ├─ Step 2  【生成订阅配置】→ LLM 解析 → 返回草稿
 └─ Step 3  确认 / 编辑页（每项可改）
      · 订阅名称、兴趣描述（可编辑）
      · include / exclude 关键词（chip 式增删）
      · 建议启用源（勾选，仅显示免注册源 + 该用户可见的源）
      · arXiv 分类（多选）
      · 检索查询式（可编辑，预览召回条数）
      · 最低分阈值滑块、每日推送篇数、推送时间、时区
      └─ 保存 → 可选勾选【立即推送一份（冷启动）】→ 完成
```

**高级设置（可选，非必需）**：用户若希望用自己的 LLM Key（BYOK），可在「账户设置 → 高级」中填写；填写后该用户的调用走其自有 Key 与预算，未填写则自动使用部署者配置的全局 Key。此入口默认折叠，不出现于订阅主流程。

### 5.2 LLM 输出 Schema（`InterestProfile`）

```json
{
  "name": "建成环境评估与城市可达性",
  "description": "用街景图像、深度学习与时空数据评估建成环境，关注 15 分钟生活圈与公共交通可达性",
  "include_keywords": ["street view", "built environment", "15-minute city", "walkability",
                       "transit accessibility", "urban morphology", "deep learning", "urban planning"],
  "exclude_keywords": ["medical image segmentation", "theoretical proof", "convergence analysis"],
  "suggested_sources": ["arxiv", "openalex", "doaj", "nature_comms", "plos_one"],
  "arxiv_categories": ["cs.CV", "cs.LG"],
  "queries": {
    "arxiv": "abs:\"street view\" OR abs:\"built environment\" OR abs:\"15-minute\"",
    "openalex": "built environment street view deep learning",
    "europepmc": ""
  },
  "min_score": 4,
  "max_papers_per_day": 8,
  "language": "mixed"
}
```

### 5.3 解析 Prompt 要点

- 系统提示：输出严格 JSON、不外解释；关键词**中英双语都给**（中英文源都能召回）；区分"必须包含"与"明确排除"；排除项要保守（用户说"不想看纯理论"→ 排除 `theoretical proof`，不要排除 `deep learning`）。
- 用户提示：用户原话 + 系统内置**源能力清单**（各源覆盖领域，供 LLM 判断开哪些源）+ arXiv 分类词表。
- 校验失败 → 重试 1 次；再失败 → 降级为**本地关键词抽取**（中文 jieba + 停用词，英文空格 + 停用词），生成最简画像并提示用户手工补充。

### 5.4 后续编辑

- 画像可随时编辑；任何改动使 `version += 1` → 已有 `llm_scores` 缓存失效，下次推送重新打分。
- 提供"重新用自然语言生成"按钮，保留历史版本可回滚。

---

## 6. 反馈闭环与画像自进化（需求 6）

### 6.1 反馈采集

每封邮件每篇论文下方提供（HMAC 签名 token 链接，免登录点击）：

- ⭐ 五星评分（1–5）
- 👍 有用 / 👎 不感兴趣（快捷，等价 5 / 1）
- 🚫 拉黑主题（把该论文显著主题词加入排除）

落库 `feedbacks(user_id, paper_id, interest_id, rating, action, created_at)`。

### 6.2 两层调整机制

**第一层：实时轻量调整（不调 LLM，零成本）**

| 反馈 | 动作 |
|---|---|
| rating ≤ 2 | 从该论文标题 + 摘要提取相对用户画像的**增量显著词**（TF-IDF，去停用词，Top 3）写入 `interest_keyword_candidates(term, weight=-1)` |
| rating ≥ 4 | 同上但 `weight=+1` |
| 生效规则 | 同一 term 累计 ≤ −2 → 自动并入 `exclude_keywords`；≥ +2 → 并入 `include_keywords`；用户在画像页可见候选词并一键采纳/忽略 |

**第二层：周期性 LLM 画像修订（每天 05:00，按用户错峰）**

触发条件：距上次修订 ≥ 7 天 **且** 新增反馈 ≥ 10 条。

输入：当前画像（description + include/exclude + min_score）+ 最近最多 30 条正样本（rating ≥ 4 的标题 + 摘要首 200 字）+ 30 条负样本（rating ≤ 2）。

输出（patch，不是重写）：

```json
{
  "add_include": ["transit-oriented development", "POI data"],
  "remove_include": ["urban morphology"],
  "add_exclude": ["image segmentation benchmark"],
  "remove_exclude": [],
  "description_patch": "补充：更偏好有真实城市实证数据的研究，而非纯方法论文",
  "min_score_delta": 0,
  "rationale": "负样本集中在医学影像分割的迁移学习，与用户实际方向无关"
}
```

**保护机制（防画像漂移，必做）**：

- 单次 patch 限制：`add_include` ≤ 5、`remove_include` ≤ 3、`add_exclude` ≤ 5、`min_score_delta` ∈ [−1, +1]；超限截断并告警。
- 修订写入 `interest_revisions(from_version, to_version, patch, rationale)`，`interests.version += 1`。
- **可回滚**：后台与用户前台都能看到变更 diff（"新增关键词 X、移除 Y"），一键回滚到任意历史版本。
- **效果度量**：记录每次修订后连续 7 天的正反馈率（rating ≥ 4 占比）；若比修订前下降 10% 以上 → 自动回滚并冻结该画像自动修订 30 天。
- **用户开关**：用户可在订阅设置里关闭"自动优化画像"。

### 6.3 反馈也直接影响排序（不只是画像）

```
final_score = llm_score × 0.7
            + 相似度(该论文 vs 用户历史高分论文) × 0.2
            + 新鲜度 × 0.1
            − 已推送惩罚
```

相似度用轻量 TF-IDF 余弦（本地计算，零 API 成本），实现对"用户口味"的软记忆。

---

## 7. 邮件投递：免费、可靠、可长期批量（需求 7）

### 7.1 调研结论（各家政策实测于 2026-10-03）

| 服务商 | 免费额度 | 日上限 | 品牌角标 | 永久？ | 判定 |
|---|---|---|---|---|---|
| **Brevo** | **300 封/天**，无时间限制、无需信用卡 | 300 | 有（"Sent with Brevo"） | ✅ | **主通道** |
| Mailjet | 6,000 封/月（即 200/天） | 200 | 有（Mailjet logo） | ✅ | 备选 |
| Resend | 3,000 封/月 | 100 | 无 | ✅ | 备选（品牌干净） |
| Mailgun | 100 封/天 | 100 | — | ✅ | 备选（日志仅留 1 天） |
| ZeptoMail | 注册送 10,000 封（1–6 个月内有效） | 100 | — | ❌ | 排除 |
| SendGrid | **2025-05-27 起取消永久免费**，仅 60 天试用 | 100 | — | ❌ | 排除 |
| Amazon SES | **2026-07-21 起新账户不再享有 3,000 封/月免费额度**；按量 $0.10/1000 | — | 无 | ❌ | 付费升级路径 |

**关键推论**：

1. 本应用的日发信量 = 活跃用户数（每人每天 1 封个性化摘要）。Brevo 免费版 300 封/天 → **可支撑约 300 用户永久免费**，月发信 9,000 封。
2. 超过 300 用户后，AWS SES 按量计费约为 **3,000 封/月 ≈ $0.30**（$0.10/1000），成本仍接近零。
3. 因此**第三方免费方案完全能够满足"长期、批量、可靠"的要求，无需服务器直发**。直发仅保留为兜底路径（§7.5）。
4. 需接受的代价：Brevo / Mailjet 免费版强制在邮件中显示服务商角标；若在意品牌干净，改用 Resend（100/天）或付费档。

### 7.2 通道决策

| 角色 | 通道 | 触发条件 |
|---|---|---|
| **主** | Brevo（SMTP relay 或 REST API，300/天） | 默认 |
| **备 1** | Resend（100/天，无角标） | 主通道连续失败 3 次或日额度耗尽 |
| **备 2** | AWS SES（$0.10/1000） | 用户数 > 300 后作为扩容通道 |
| **兜底** | 本机 Postfix 直发 | 仅当满足 §7.5 全部前置条件时手工启用，默认关闭 |
| **BYO** | 用户自带 SMTP（凭据加密） | 可选 |

统一抽象 `EmailProvider.send(msg) -> message_id` + `handle_webhook()`。后台可配置主/备、一键切换、实时查看各通道健康度。

**部署前置**：Brevo 免费账户需通过平台发送审核后才可发信；须先在后台完成域名验证（SPF/DKIM）。

### 7.3 日配额治理（免费层的生命线，必做）

免费额度的硬约束是**日上限**，应用必须内建配额管理，否则超限即被暂停发信：

- 配置项 `provider_daily_budget`，默认 **250**（低于 Brevo 的 300，为重试、管理员告警、注册验证码预留 50 封缓冲）。
- 发送前对当日计数做原子自增（SQLite 计数表 / Redis）；超限的邮件标记 `deferred`，**次日优先补发**（论文在保留期内仍在池中，用户收到的是"昨日摘要"补发件，内容不失效）。
- 超限且配置了备用通道 → 自动切备；无备用 → 顺延并告警管理员。
- 后台仪表盘显示：今日已发 / 额度 / 剩余 / 顺延队列长度 / 各通道成功失败数。
- 超限保护是**硬闸门**：宁可顺延，不可突破免费额度。

> 补充：Brevo 免费版在达到日限后，最多还有 **1,000 封进入 retry queue**，可作为短时突发缓冲；但不可依赖。

### 7.4 可靠性设计

- **持久化队列**：待发邮件先写 `deliveries(status=pending)` 再发送；进程重启不丢。Lite 模式由 `flush_deliveries` 任务（每 2 分钟）扫描 pending / 超时 running 项重投。
- **幂等**：`UNIQUE(digest_id, to_email)`；重复入队直接跳过。
- **重试**：3 次，退避 5min → 30min → 2h；4xx（除 429）不重试，直接标 `failed` 并告警。
- **速率限制**：按 provider 配 `max_per_minute`（新域名建议 ≤ 20/分钟预热）。
- **退信处理**：API 通道配 webhook（`/webhooks/bounce`）；SMTP 通道可选 IMAP 轮询（默认关，省资源）。**连续 2 次硬退信**（invalid recipient）→ 自动暂停该用户推送，前台与邮件双通道提示更新邮箱。
- **超时**：连接 10s、发送 30s；超时按失败重试处理。

### 7.5 服务器直发（兜底路径，默认关闭）

启用前必须**全部满足**以下条件，否则不启用：

1. 裸机**不在**阿里云 / 腾讯云 / 华为云 / AWS / GCP 等默认封禁 TCP 25 出方向的平台。⚠️ 阿里云与腾讯云的 25 端口解封协议明确要求"仅可用于连接第三方 SMTP 服务器"，若被发现直接用该 IP 通过 SMTP 对外发信，**服务商有权永久封禁端口**。因此在这两家平台上**禁止**启用直发。
2. 拥有**静态公网 IPv4**，且服务商允许自助设置 PTR / rDNS（指向 `mail.<domain>`）。
3. 出方向 TCP 25 确实可达。
4. IP 不在 Spamhaus PBL / SORBS / Barracuda 等黑名单中。

```bash
# 部署前自检（三条都必须通过）
nc -zv gmail-smtp-in.l.google.com 25                 # 端口 25 出方向可达
dig +short -x <your-ip>                              # 应返回 mail.<domain>
dig +short <ip-反写>.zen.spamhaus.org                # 无返回结果 = 未被列入
```

技术形态：Postfix 仅作**出站 relay**（不收信、不开 25 入站），OpenDKIM 签名，SPF/DMARC 配在 DNS。常驻内存约 25–40MB，1C1G 可接受。

**明确风险**：新 IP 无历史声誉，Gmail / Outlook 投递率不可保证；需 IP 预热（首周日发 ≤ 20 封，逐周翻倍）、MTA-STS、持续解析 DMARC 报告。直发是**兜底而非推荐路径**，仅在第三方通道全部不可用时启用。

### 7.6 反垃圾清单（部署文档强制项）

1. 发信域名配置 **SPF**（`v=spf1 include:<provider> ~all`）
2. 配置 **DKIM**（provider 提供 CNAME / TXT）
3. 配置 **DMARC**（`v=DMARC1; p=quarantine; rua=mailto:dmarc@<domain>`）
4. `Return-Path` / `Envelope-From` 与 `From` 域名对齐
5. `List-Unsubscribe: <mailto:...>, <https://...>` + `List-Unsubscribe-Post: List-Unsubscribe=One-Click`（RFC 8058）
6. `multipart/alternative`：必须有纯文本版本
7. 正文链接指向**与发信域名同域**的跳转地址（不堆外链、不用短链）
8. 固定发信节奏（同一时刻批量发送，而非随机刷）
9. 新域名先小规模预热
10. 邮件头加 `X-PaperPulse-Digest: <digest_id>` 便于排障

### 7.7 1C1G 下

发信是纯 IO，单线程串行足够；并发发信限 1，批间间隔按速率限制动态计算；HTML 用 Jinja2 流式渲染，单封邮件内存开销 < 1MB。

---

## 8. 常态化无人值守（需求 9）

### 8.1 进程守护（systemd，Lite 模式推荐）

```ini
[Unit]
Description=PaperPulse
After=network-online.target
Wants=network-online.target

[Service]
Type=notify                 # 应用就绪后 sd_notify，配合看门狗
User=paperpulse
WorkingDirectory=/opt/paperpulse
ExecStart=/opt/paperpulse/.venv/bin/python -m app.main
Restart=always
RestartSec=10
StartLimitBurst=5
StartLimitIntervalSec=300
WatchdogSec=60              # 应用每 30s 心跳一次，60s 未发则 systemd 重启
OOMPolicy=stop
MemoryMax=700M              # cgroup 硬限，防止拖垮整机
CPUQuota=80%
StandardOutput=journal
StandardError=journal
TimeoutStopSec=150

[Install]
WantedBy=multi-user.target
```

Docker 部署对应：`restart: unless-stopped` + `mem_limit: 700m` + `healthcheck`。

### 8.2 应用内自愈

| 场景 | 自愈动作 |
|---|---|
| 任务卡死 | `task_runs` 中 `running` 超过 `soft_timeout` → 重置为 `pending`，重试计数 +1，超 3 次标 `failed` |
| 进程重启 | 启动时把 `running` 任务全部重置为 `pending`；`pending` 按 `scheduled_at` 补跑 |
| DB 锁死 | `busy_timeout=5s`；连续 3 次 `database is locked` → 重建连接并退避 |
| 网络中断 | HTTP 全带超时 + 重试；连续失败 5 次 → 该源本次跳过，下一周期再试 |
| 磁盘告警 | 见 §2.4 水位自保 |
| 内存告警 | RSS > `rss_warn_mb` → 并发降至 1、主动 `gc.collect()`、跳过本轮非关键任务；> 硬限 → 只保推送 |
| 时钟漂移 | 依赖 chrony；调度用 UTC 存储、本地时区计算，`misfire_grace_time` 容忍迟到 |

### 8.3 外部心跳与告警

- 后台可配 **heartbeat URL**（healthchecks.io / Uptime Kuma / 自建 cron 监控）：`watchdog` 任务每 5 分钟 ping 一次，停跳即告警。
- 告警渠道：管理员邮件（走备用 provider）、可选 Webhook（企业微信 / 钉钉 / Bark / Telegram）。
- 告警内容：源连续失败、投递连续失败、磁盘/内存越限、LLM 额度异常、任务积压超阈值。
- 内置 `/healthz`（进程存活）、`/readyz`（DB + 关键源可达）、`/stats`（内部观测，需 admin）。

### 8.4 日志、备份与更新

- 日志：structlog JSON → journald；`SystemMaxUse=200M`、`MaxRetentionSec=14day`。
- 备份：每日 03:30 执行 `VACUUM INTO '/backup/paperpulse-YYYYMMDD.db'`（SQLite 在线备份，不阻塞读写），保留 7 份；可选 rclone 同步到对象存储。备份失败要告警。
- 更新策略：**默认手动**（`git pull` → `uv sync` → `alembic upgrade head` → `systemctl restart`）。`AUTO_UPDATE` 可选 `off | patch | all`，即使开启也只在 03:00–05:00 低峰执行，失败自动回滚到上一版本（保留上一份 venv）。
- 优雅关机：SIGTERM → 停止接新任务 → 等当前任务 ≤120s → 关闭连接池 → 退出。

### 8.5 巡检清单（写进 README）

周期任务：采集 / 派发 / 清理 / 画像修订 / 投递 / 看门狗，各自有 `last_run_at`、`last_status`、`next_run_at`，后台一页可看全部；任何一项超过 2 个周期未成功即高亮。

---

## 9. 数据模型（v2 增量）

在 v1 基础上新增/调整：

```sql
-- 首次部署引导与全局配置（§0.1、§10.4）
system_settings(id, setup_completed_at, setup_step, site_name, site_url,
                default_timezone, llm_mode, degraded_keyword_mode, updated_at)
source_credentials(id, source_key, credential_kind, encrypted_value, enabled, updated_at)

-- 源与开关
sources(id, key, name, type, field, url_template, params JSON, rate_limit JSON,
        requires_key, enabled_global, verified_status, last_ok_at, last_error)
user_source_prefs(user_id, source_key, enabled)

-- 任务（Lite 模式的持久化队列）
task_runs(id, kind, payload JSON, status, scheduled_at, started_at, finished_at,
          attempts, last_error, lock_key)

-- 兴趣画像与版本
interests(id, user_id, name, description, include_keywords JSON, exclude_keywords JSON,
          sources JSON, arxiv_categories JSON, queries JSON, min_score,
          max_papers_per_day, send_at, timezone, auto_optimize, version, is_active)
interest_revisions(id, interest_id, from_version, to_version, patch JSON, rationale,
                   created_at, rolled_back_at)
interest_keyword_candidates(id, interest_id, term, weight, evidence_count, resolved_at)

-- 反馈
feedbacks(id, user_id, paper_id, interest_id, rating, action, created_at)

-- 邮件通道与配额（需求 7）
email_providers(id, key, kind, role, config JSON, daily_budget, enabled, priority,
                last_ok_at, last_error)   -- role: primary | backup | fallback
send_quota(id, provider_key, quota_date, sent_count, deferred_count, failed_count,
           UNIQUE(provider_key, quota_date))

-- 存储生命周期设置（后台可改，单条记录）
retention_settings(id, retention_days, purge_cron, purge_scope, purge_batch_size,
                   purge_vacuum, max_pool_rows, updated_at)

-- 既有表补充字段
papers(... , abstract_quality, source_key, enriched_at)
deliveries(... , provider_key, bounce_count, deferred_to_date)   -- 支持顺延补发
llm_usage(id, user_id, interest_id, kind, model, prompt_tokens, completion_tokens, created_at)
```

---

## 10. 部署方案（裸机，唯一目标形态）

**交付目标已确定为裸机部署**：systemd + uv 虚拟环境，不使用 Docker（省 50–80MB 常驻内存，且减少一层运维复杂度）。Docker Compose 仅作为开发者可选的本地便利，不作为生产部署形态。

```
/opt/paperpulse/          git clone
  .venv/                  uv sync --frozen
  data/                   paperpulse.db, backup/
  config/                 config.yaml, sources.yaml
  .env                    SECRET_KEY, ENCRYPTION_KEY, LLM/邮件通道配置
/etc/systemd/system/paperpulse.service
/etc/caddy/Caddyfile      反代 127.0.0.1:8000，自动 HTTPS
```

最低要求：**1 核 / 1GB 内存 / 20GB SSD / 公网 IP + 一个域名**（HTTPS 与邮件域名认证均需要）。

### 10.1 裸机部署前置检查清单

```bash
# 1 出网能力（采集全部依赖 HTTPS 出站）
curl -sS -o /dev/null -w '%{http_code}\n' https://export.arxiv.org/api/query?search_query=all:test
curl -sS -o /dev/null -w '%{http_code}\n' https://api.openalex.org/works?per-page=1

# 2 邮件：走第三方通道需 587/465 出站（不是 25）
nc -zv smtp-relay.brevo.com 587
nc -zv smtp.resend.com 465

# 3 仅在启用直发兜底时才检查 25（见 §7.5，多数云平台默认封禁且条款禁止）
# nc -zv gmail-smtp-in.l.google.com 25

# 4 时间同步（定时推送依赖准确时钟）
timedatectl status | grep -E 'NTP service|synchronized'

# 5 内存与 swap
free -m && swapon --show     # 目标：可用 ≥900MB，swap ≥512MB
```

### 10.2 一次性初始化

```bash
useradd -r -m -d /opt/paperpulse paperpulse
cd /opt/paperpulse && git clone <repo> . && uv sync --frozen
cp config/config.example.yaml config/config.yaml
python -m app.cli init-db
python -m app.cli create-admin --email you@example.com
python -m app.cli verify-sources          # 校验所有数据源连通性
python -m app.cli test-email --to you@example.com
systemctl enable --now paperpulse
```

### 10.3 升级与回滚

```bash
git pull && uv sync --frozen && alembic upgrade head && systemctl restart paperpulse
# 失败回滚：git checkout <上一个 tag> && uv sync --frozen && systemctl restart paperpulse
```

### 10.4 首次部署引导（Setup Wizard）——注册类依赖的唯一入口

首次启动后，应用处于 `setup_pending` 状态，所有页面重定向到 `/admin/setup`。引导共 6 步，**每步都可"测试连通性"后才允许进入下一步**，中途退出后再次访问会回到未完成的步骤。

| Step | 内容 | 是否可跳过 | 校验方式 |
|---|---|---|---|
| **1. 管理员账号** | 邮箱、密码、时区 | 否 | 密码强度校验 |
| **2. 站点信息** | 站点名称、对外 URL（用于生成退订与跳转链接）、默认时区 | 否 | URL 可达性自检 |
| **3. LLM 接入** | 厂商预设 / 自定义 `base_url` + `api_key` + `model`；【测试连接】 | **可显式跳过**（选"纯关键词模式"降级启用） | 发一个极小请求验证鉴权与模型可用 |
| **4. 邮件通道** | 选 Brevo（默认）/ Resend / SES / 自建 SMTP；填凭据、发信域名、发信地址；【发送测试邮件】 | **否**（阻断启用） | 实际投递一封测试邮件并等待回执 |
| **5. 数据源确认** | 按领域分组展示全部内置源；免注册源默认勾选可直接开关；需 Key 源置灰并提示"需 Key，可在设置中补配" | 是（后续可改） | 逐源连通性自检（可全选后一键跑） |
| **6. 完成** | 生成**系统自检报告**：出站网络、LLM、邮件、数据源、磁盘、内存、NTP | — | 全绿灯后方可进入正常模式 |

完成后写入 `system_settings.setup_completed_at`，应用转入正常运行；此后所有配置均可在 `/admin/settings/*` 随时修改，无需重跑引导。

**降级模式说明**：Step 3 若选择跳过，应用在界面与邮件中持续标注"当前为关键词模式，未启用 AI 精排"，并在后台顶部常驻提示条引导补配 LLM——补配后立刻生效，无需重跑引导。

---

## 11. GitHub 仓库结构（v2 增量）

```
config/
  default.yaml              # Lite 模式默认参数（内存/并发/清理）
  sources.yaml              # 30+ 内置源清单（URL 模板 + 领域 + 默认开关）
docs/
  architecture.md  deployment.md  data-sources.md    # 源清单与验证状态
  llm-providers.md  email-delivery.md  operations.md
app/
  core/{config.py, memory.py, retention.py, watchdog.py}
  scheduler/{lite_runner.py, standard_runner.py}     # 两种执行器，同一 Task 接口
  sources/{base.py, arxiv.py, openalex.py, biorxiv.py, chemrxiv.py, europepmc.py,
           doaj.py, osf.py, zenodo.py, nber.py, rss.py, registry.py, enrich.py}
  llm/{base.py, openai_compat.py, anthropic.py, gemini.py, ollama.py, json_repair.py}
  pipeline/{fetch.py, prefilter.py, score.py, rank.py, render.py, deliver.py}
  interest/{parse.py, revise.py, schema.py}          # 需求 5、6
  setup/{wizard.py, checks.py}                       # 首次部署引导与系统自检（§10.4）
  web/{routes/, templates/, static/}                 # 含 /admin/setup 引导页
tests/{unit/, integration/, fixtures/sources/}       # 真实 feed 快照做解析测试
scripts/verify_sources.py                            # 一键校验所有源连通性
```

其余（`.github/`、LICENSE Apache-2.0、README、CONTRIBUTING、SECURITY、CHANGELOG、Makefile）同 v1；README 中列出内置源表格与"源验证日期"。

---

## 12. 路线图与验收

| 里程碑 | 内容 | 验收标准（对应需求） |
|---|---|---|
| M0 | 骨架 + Lite 运行器 + **首次部署引导（6 步）** + arXiv/OpenAlex 采集 + 单用户端到端 | 1C1G 上 RSS ≤ 300MB；**未跑引导时应用不可启用**（需求 1、配置责任边界） |
| M1 | 多用户 + 订阅向导 + LLM 兴趣解析 + 定时推送 | 自然语言 → 结构化画像可用（需求 4、5） |
| M2 | 源注册表（30+ 源 + RSS 适配器 + 补全 + 后台开关 + 自检） | `scripts/verify_sources.py` 全绿（需求 3、8） |
| M3 | 反馈闭环 + 画像修订 + 版本回滚 | 负反馈后出现可解释的画像变更（需求 6） |
| M4 | 存储生命周期 + 磁盘自保 + 清理面板 | 保留天数可调、清理可预估（需求 2） |
| M5 | 投递通道（Brevo 主 + Resend 备）+ 日配额治理 + 反垃圾 + 退信处理 | 连续 30 天投递成功率 ≥ 99%，且**从未突破免费日额度**（需求 7） |
| M6 | 守护/自愈/心跳/备份 + 文档 + CI + GHCR 发布 | 连续 30 天无人值守无人工干预（需求 9） |

---

## 13. 已定与待定

**已定**：

- 部署形态 = 裸机（systemd + uv）。
- 邮件主通道 = Brevo 免费版（300/天），备用 Resend，扩容走 AWS SES（$0.10/1000）；服务器直发仅作兜底且默认关闭。
- **配置责任边界**：用户侧零注册依赖；LLM 与邮件凭据由部署者在首次部署引导中后台配置（邮件必配、LLM 可显式降级为关键词模式）。

**待你拍板（3 项）**：

1. **部署者使用的 LLM 厂商**：决定后台 `base_url` 预设与默认模型名（架构无关，仅影响默认值与文档示例）。
2. **首要领域**：决定默认开启哪些源。若以城乡规划 / 城市科学为主，我会把 `arxiv cs.CV / cs.LG` + OpenAlex 城市类 + Nature / Science / PLOS 的 RSS 设为默认，并预置规划类期刊 feed。
3. **Brevo 角标接受度**：免费版邮件会带 "Sent with Brevo" 角标。若不接受，主通道改为 Resend（无角标，容量降至 100 用户/天）。

---

*文档版本：v2.2 · 2026-10-03 · 源链接与邮件政策核实日期 2026-10-03*
