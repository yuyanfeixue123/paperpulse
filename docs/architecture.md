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
