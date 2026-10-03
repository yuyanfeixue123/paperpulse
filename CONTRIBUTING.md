# 贡献指南

## 开发循环

每个 W（工作单元）做完立即 `make lint && make test`，跑通再进下一个。

```bash
make sync     # uv sync（或 pip install -e ".[dev]"）
make dev      # uvicorn --reload，访问 http://127.0.0.1:8000
make lint     # ruff + mypy
make test     # pytest
make migrate  # alembic upgrade head
```

首次进入时先 `python -m app.cli init-db` 建库并同步内置源。

## 提交规范

```
feat(W3): 数据模型与迁移
fix(W10): 修正配额释放时机
docs(W15): 补充数据源验证日期
```

一个 W 一次提交。

## 新增数据源

不需要改代码。在 `config/sources.yaml` 加一段，选用已有适配器（`arxiv` / `openalex` / `biorxiv` / `doaj` / `europepmc` / `osf` / `rss` / `zenodo` / `hal` / `chemrxiv`），然后：

```bash
python -m app.cli init-db          # 同步进 sources 表
python scripts/verify_sources.py <key>
```

约束：

- 新源若 `requires_key: true`，必须 `enabled: false`
- 在 README 的源清单表中补一行，注明验证状态与核实日期
- 如涉及新出版社 RSS，在 `docs/data-sources.md` 的模板表中补模板

## 新增 LLM Provider

在 `app/llm/providers.py` 实现 `complete_json(system, user, schema, model) -> (dict, Usage)`，注册进 `PROVIDERS`，并在 `app/web/routes/admin_llm.py` 的 `VENDOR_PRESETS` 加预设。补一个 respx mock 测试。

## 测试约定

- 外部 HTTP 一律用 `respx` mock，不发真实请求
- 数据库指向 `data/test_paperpulse.db`（conftest 自动处理）
- 不写针对超时 / 断网 / 限流异常的专门处理测试——失败就记日志并让任务失败，由下一次调度自然重试

## 代码风格

- `ruff`（line-length 100，规则 E/F/I/UP/B）
- `mypy`（`ignore_missing_imports = true`）
- 配置参数一律查 `config/default.yaml`；没有的选最简单直接的写法，不自行发明分支
