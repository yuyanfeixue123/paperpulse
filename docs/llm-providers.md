# LLM 接入

## Provider 抽象

```python
class LLMProvider:
    def complete_json(self, system: str, user: str, schema: dict,
                      model: str) -> tuple[dict, Usage]: ...
```

| 适配器 | 端点 | 结构化输出手段 |
|---|---|---|
| `openai_compatible` | `POST {base_url}/chat/completions` | `response_format: {type: json_schema}` |
| `anthropic` | `POST {base_url}/v1/messages` | schema 定义成 tool + `tool_choice` 强制调用 |
| `gemini` | `POST {base_url}/v1beta/models/{model}:generateContent` | `generationConfig.responseMimeType=application/json` + `responseSchema` |
| `ollama` | `POST {base_url}/api/chat` | `format: json` |

统一三元组 **`base_url` + `api_key` + `model`**，三者齐全即可接入任何兼容端点。全部走 httpx 直连，不依赖厂商 SDK。

## 已内置厂商预设（后台一键填入）

| 厂商 | base_url | 可选模型 |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | deepseek-chat, deepseek-reasoner |
| OpenAI | `https://api.openai.com/v1` | gpt-4o-mini, gpt-4o |
| Moonshot | `https://api.moonshot.cn/v1` | moonshot-v1-8k |
| 通义千问（兼容模式） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | qwen-plus, qwen-turbo |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | glm-4-flash |
| 硅基流动 | `https://api.siliconflow.cn/v1` | Qwen/Qwen2.5-7B-Instruct |
| Anthropic | `https://api.anthropic.com` | claude-3-5-haiku-latest |
| Gemini | `https://generativelanguage.googleapis.com` | gemini-2.0-flash |
| Ollama（本地） | `http://127.0.0.1:11434` | qwen2.5:7b |

## 结构化输出三级降级

1. `response_format: json_schema`
2. 不支持 → tool-use 强制调用
3. 仍不支持 → 纯文本 + 服务端容错解析：提取首个 `[...]` / `{...}`、修复尾逗号、去掉 ```json 围栏；解析失败抛 `LLMError`，由调用方降级为 BM25 排序并注明"本次未启用 AI 精排"

## 模型角色分工

| 用途 | 推荐档位 | 频率 |
|---|---|---|
| 兴趣点解析 | 中档（GPT-4o-mini / DeepSeek-V3 / Qwen-Plus / Haiku） | 每次建订阅 1 次 |
| 论文精排 | 便宜档（DeepSeek-V3 / Qwen-Flash / GPT-4o-mini / Gemini Flash / 本地 7B） | 每用户每日约 6 次批调用 |
| 画像修订 | 中档 | 每用户每周级 |

## 调用治理

- 超时：连接 10s、读取 60s
- 重试 3 次（2^n + jitter），仅对 429 / 5xx / 超时重试
- 并发：Lite 模式 ≤ 2（进程内信号量）
- 每次调用写 `llm_usage(kind, model, prompt_tokens, completion_tokens)`，后台按用途统计
- Key 加密存储（Fernet，`enc:` 前缀），加载时解密

## 关键词降级模式

部署者可在引导 Step 3 显式选择「纯关键词模式」：不调 LLM，用 jieba（中文，按句切分后分词）+ 空格分词（英文）+ 停用词表生成最简画像。前台与邮件持续标注"当前为关键词模式，未启用 AI 精排"，后台常驻提示条引导补配，补配后立刻生效。

**已知限制**：关键词模式下产出的是中文关键词，而论文池以英文元数据为主，FTS5（unicode61 分词）无法跨语言匹配，召回会受限。配置 LLM 后生成中英双语关键词，该限制消失。

## BYOK（可选）

用户在「账户 → 高级」填写自己的 base_url / key / model，该账号调用走其自有凭据与预算；留空自动回落全局 Key。

## 用量查看

后台「LLM」页显示近 30 天按用途（parse / score / revise）的调用次数与 token 数；每用户用量见「用户」页。
