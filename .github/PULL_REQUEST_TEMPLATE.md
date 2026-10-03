## 变更内容

<!-- 一句话说明改了什么 -->

## 类型

- [ ] 新增数据源 / 新增 LLM Provider
- [ ] 功能
- [ ] 修复
- [ ] 文档
- [ ] 重构（无行为变化）

## 约束确认（必勾）

- [ ] 未新增常驻依赖，或已说明为何无法避免（目标：1C1G 可运行，依赖预算 95MB）
- [ ] 未要求终端用户注册任何第三方服务或申请 API Key
- [ ] 外部 HTTP 在测试中用 `respx` mock，未发起真实请求
- [ ] `make lint` 通过（ruff + mypy）
- [ ] `make test` 通过

## 数据源相关（若涉及）

- [ ] 在 `config/sources.yaml` 补充条目，并选用已有适配器
- [ ] 默认 `enabled: true` 的源确认 `requires_key: false`
- [ ] 在 README 的源清单表补一行，注明验证状态与核实日期
- [ ] `python scripts/verify_sources.py <key>` 现场验证过

## 数据库相关（若涉及）

- [ ] 提供了 Alembic 迁移，且在空库上验证过 `alembic upgrade head`
- [ ] 模型改动与迁移一致（`Base.metadata` 与 FTS 触发器）

## 验证方式

<!-- 怎么手动确认这个改动生效了 -->
