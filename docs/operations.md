# 运维手册

## 巡检清单

| 周期 | 项目 | 方法 | 异常判定 |
|---|---|---|---|
| 每日 | 进程存活 | `curl https://<domain>/healthz` | 非 `{"status":"ok"}` |
| 每日 | 备份文件 | `ls -lt /opt/paperpulse/data/backup/` | 无当日文件 |
| 每日 | 磁盘 | `df -h` | > 80% |
| 每周 | 源连通性 | `python -m app.cli verify-sources` | 默认开启的源非全绿 |
| 每周 | 任务健康 | 后台「概览」最近任务 | 某类任务连续 2 个周期未成功 |
| 每周 | 投递成功率 | 后台「邮件」页 | 失败数 > 0 且未恢复 |
| 每月 | LLM 用量 | 后台「LLM」页 | 异常峰值（可能死循环或 Key 泄露） |

## 常见故障与处置

| 现象 | 可能原因 | 处置 |
|---|---|---|
| 采集条数为 0 | 时间窗内本就无更新 / 源改版 / 被限流 | `verify-sources` 定位；arXiv 检查是否违反 3 秒间隔 |
| 摘要 0 篇 | 关键词过窄 / lookback_days 太短 / LLM 未配 | 放宽关键词；检查 `interests.lookback_days`；后台配 LLM |
| 邮件一直 pending | 未配通道 / 额度耗尽 / SMTP 被拒 | 后台「邮件」测试发信；检查 `send_quota` |
| 邮件大量 deferred | 日额度打满 | 提高 `daily_budget` 或加备用通道 |
| 任务 stuck | 单任务超 10 分钟 | 自动重置（attempts ≤ 3）；> 3 次标 failed 需人工看 `last_error` |
| SQLite locked | 写竞争 | `busy_timeout=5000` 已覆盖；持续出现需降 `executor_threads` |
| 内存持续上涨 | 论文池过大 / 模板缓存 | 收紧保留天数；检查是否开启过多源 |

## 存储

- 保留天数默认 30 天；日采集 > 5000 篇时建议降到 14 天
- 估算：`单条 ≈ 2.5 KB`；默认源 30 天 ≈ 60 MB
- 清理分批 1000 条，批间 sleep 50ms，随后 VACUUM（磁盘余量 < DB × 1.2 时跳过）
- 「清空全部」需输入「清空」二次确认

## 升级

```bash
git pull && uv sync --frozen && alembic upgrade head && systemctl restart paperpulse
```

失败回滚：`git checkout <上一个 tag> && uv sync --frozen && systemctl restart paperpulse`
