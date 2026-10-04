# 架构说明

## 运行形态

Lite 模式（默认）为**单进程**：

```
uvicorn (1 worker)
  ├── FastAPI 路由（前台 / 后台 / 引导 / 反馈链接）
  ├── APScheduler 后台线程（周期任务只入队）
  └── ThreadPoolExecutor(4)（实际执行 task_runs）
```

- **任务队列**落 SQLite `task_runs` 表而非 Redis：进程崩溃后未完成任务仍在表里，重启自动续跑。
- **数据库** SQLite WAL；PRAGMA：`journal_mode=WAL`、`busy_timeout=5000`、`mmap_size=64MB`、`synchronous=NORMAL`、`foreign_keys=ON`。
- **限流**为进程内令牌桶，按 `source_key` 独立；arXiv 强制 `interval_seconds=3, concurrency=1`。

## 内存预算（1C1G）

| 项目 | 预算 |
|---|---|
| OS 基础（minimal + sshd + journald + chrony） | 110 MB |
| Python 3.12 + 精简依赖 | 95 MB |
| 应用常驻（路由、模板、连接池） | 35 MB |
| 任务执行峰值 | 60 MB |
| **合计** | **≈ 300 MB**（告警 450，硬限 700） |

不装 pandas / numpy / torch。建议配 512MB zram swap。

## 数据流

```
sources.yaml ──registry──> SourceBase 适配器
                              │ fetch(window)
                              ▼
                     upsert_paper（dedup_key 去重）
                              │  enrich_paper（DOI / 摘要补全）
                              ▼
                        papers + papers_fts(FTS5)
                              │
    interests ──prefilter（硬过滤 + BM25 召回 + 已推排除）
                              │
                     score（LLM 20 篇一批，缓存按 interest_version）
                              │
                     rank（final = 0.7*llm + 0.2*taste_sim + 0.1*freshness - 0.5*已推）
                              │
                     digest_items ──render──> multipart/alternative
                              │
                     deliveries（配额预占 → 发送 / 顺延）
                              │
                     邮件内 HMAC 反馈链接 ──> feedbacks
                              │
                     实时轻量调整 + 周期 LLM 修订 → interests.version += 1
```

## 任务状态机

```
pending ──claim──> running ──> done / failed
   ▲                  │
   └──── 超过软超时（600s）且 attempts ≤ 3 ────┘（attempts > 3 → failed）
```

进程启动时把所有 `running` 改回 `pending`。

## 周期任务

| 任务 | 周期 | 说明 |
|---|---|---|
| `job_fetch` | 30 分钟 | 最近 3 天窗口增量采集（重叠设计，靠 upsert 去重） |
| `job_dispatch` | 10 分钟 | 到期的 interest 入队 build_digest |
| `job_flush` | 2 分钟 | 重投 pending 与到期的 deferred |
| `job_purge` | 每日 04:00 | 分批清理 + VACUUM |
| `job_revise` | 每日 05:00 | 画像修订（逐 interest 错峰） |
| `pump_tasks` | 30 秒 | 捞取到期 pending 投递到线程池 |
| `reset_stuck_tasks` | 5 分钟 | 卡死任务重置 |

## 关键设计取舍

1. **不用 Redis / Celery**：Redis 常驻 15–30MB + 每 worker 60–90MB，在 1GB 内存里是纯浪费。本应用瓶颈是外网 API 等待（IO 密集），线程池足够。
2. **任务表落 SQLite**：比 Redis 更可靠，崩溃后可续跑。
3. **采集窗口重叠 3 天**：宁可重复拉取，也不错失；靠 `dedup_key` 幂等。
4. **流式处理**：分页 / 生成器，禁止整表读进内存排序；打分按批 20 篇进出。

## 信号与节奏

### 排序信号

`final = 0.7·LLM + 0.2·口味 + 0.1·新鲜度 − 0.5·已推过`，
**引文数不并入上式**，只在 `final` 完全相同时作为 tie-breaker。

理由：裸引文数有强时间偏置 —— 三天前的新论文必然是 0，
而 `freshness` 的半衰期只有 14 天，压不住这个偏置。并入主公式会系统性
压制新作，正是最不该发生的事。引文数取自 OpenAlex `cited_by_count`，
由 `enrich_paper` 顺带写入（该接口本来就在调，不额外花请求）。

`papers.cited_by_count` 用 `-1` 表示「未获取」，与 `0`（查过、无人引）区分 ——
否则排序会把「没数据」当成「没人引」。

### 预印本归并

去重分三层，逐层放宽：

1. `dedup_key_of` —— DOI / arXiv ID / 归一化标题
2. **等价 DOI 集合** —— OpenAlex `locations` 里同一作品的预印本与期刊版互指
   （`10.1101/xxx` ↔ `10.1038/yyy`）。第 2 层是必要的：预印本与正式版
   DOI 不同、标题常有微调，三个原始键都判不出是同一篇，用户会收到两次
3. 落库时把 `alternate_dois` 一并记下，供后续批次匹配

匹配按「任一等价 DOI 命中」而非单个 `canonical_doi` 比较：预印本入库时
它的等价集合里只有自己，两边的 canonical 值不相等，永远匹配不上。

### 推送频率

`interests.cadence` 支持 `daily` / `weekdays` / `every_n_days`（间隔 1–30 天）。
`compute_next_due` 按用户本地时区算下一次；间隔模式从 `last_sent_date`
按 n 天步进，取第一个尚未到达的时刻。

「上周四推过、间隔 3 天、今天周一」这类场景容易算错成间隔 6 天 ——
实现里是从上次日期**步进**而不是「last+n，若已过再 +n」。

### 论文卡片增强

外链（alphaXiv 讨论、Connected Papers 图谱）都是**按标识拼 URL**，
服务端不做任何事，链接在前端构造。代码仓库来自 HF Daily Papers 的
`githubRepo` 字段（作者提交时自填），不靠 GitHub Search 按标题盲搜 ——
实测搜 "attention is all you need" 第一条是冒名仓库而非 karpathy 的。

卡片数据经 `b64paper` 过滤器以 base64 传入 `data-paper` 属性。**不能直接
用 `| tojson`**：Jinja 会把内部引号转义成 `\"`，而 HTML 属性解析器不认
那个反斜杠，属性会在第一个引号处被截断 —— 论文标题里带引号很常见。
