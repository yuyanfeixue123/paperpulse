# 获取 LLM API Key（管理员必读）

PaperPulse 需要一个 LLM 凭据来做两件事：**解析自然语言兴趣** 与 **给论文打分**。

- 部署者配置一次 → **本站所有用户直接可用，用户无需注册任何服务**
- 用户也可在「账户 → API Key」自带 Key（BYOK），未自带时回落你的全局凭据

---

## 一、选哪家

| 厂商 | 入口 | 免费额度 | 国内直连 | 备注 |
|---|---|---|---|---|
| **DeepSeek** | [platform.deepseek.com](https://platform.deepseek.com) | 有赠额，价格极低 | ✅ | **推荐首选**，性价比最高 |
| 通义千问 | [bailian.console.aliyun.com](https://bailian.console.aliyun.com) | 送 token | ✅ | 阿里云用户方便 |
| 智谱 GLM | [open.bigmodel.cn](https://open.bigmodel.cn) | 送 token | ✅ | |
| Moonshot | [platform.moonshot.cn](https://platform.moonshot.cn) | 送 token | ✅ | |
| 硅基流动 | [cloud.siliconflow.cn](https://cloud.siliconflow.cn) | 送额度 | ✅ | 模型多 |
| OpenAI | [platform.openai.com](https://platform.openai.com) | 需绑卡 | ❌ | 海外服务器才考虑 |
| Anthropic | [console.anthropic.com](https://console.anthropic.com) | 需绑卡 | ❌ | |
| Gemini | [aistudio.google.com](https://aistudio.google.com) | 有免费层 | ❌ | |
| Ollama | [ollama.com](https://ollama.com) | 完全免费 | 本地 | 无 Key，本地模型 |

> 国内服务器用海外直连的厂商（OpenAI / Anthropic / Gemini）通常会被卡在网络上，优先选前五个。

---

## 二、拿 Key 的通用四步

以 DeepSeek 为例，其他家流程一致：

1. **注册登录**对应平台（一般支持手机号 / 邮箱 / 微信）
2. **实名认证**（国内厂商基本强制，不认证无法调用）
3. **充值或领取赠额** — 在「充值」页领优惠券即可，多数厂商新用户有几十元赠额
4. **创建 API Key** — 在「API Keys」页点创建，**Key 只显示一次**，立刻复制保存

创建后你会得到一串 `sk-...` 开头的字符串，那就是 API Key。

---

## 三、填进 PaperPulse

### 方式 A：首次部署向导（推荐）

```bash
sudo -u paperpulse -H /opt/paperpulse/.venv/bin/python -m app.cli setup
```

走到「3 LLM 接入」，选厂商编号 → 粘贴 Key → 填模型名。向导会**当场发一个极小请求验证鉴权**，通过才让你继续。

也可以在浏览器引导的 `/admin/setup` 第 3 步完成，效果相同。

### 方式 B：后台随时改

已部署好的站点，进「后台 → LLM」，填厂商、Base URL、API Key、模型，点「测试连接」。

**Key 的存储形式**：写入 `config/config.yaml` 时会加 `enc:` 前缀并用 Fernet 加密，明文不落盘。

```yaml
llm:
  provider: openai_compatible
  base_url: https://api.deepseek.com/v1
  api_key: enc:gAAAAAB...        # 加密后的密文
  model_score: deepseek-flash
  model_parse: deepseek-flash
  max_tokens: 4096
```

> 换 `PAPERPULSE_ENCRYPTION_KEY` 会导致已加密的 Key 不可解密，需重新填写。

---

## 四、Base URL 怎么填

| 厂商 | Base URL |
|---|---|
| DeepSeek | `https://api.deepseek.com/v1` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` |
| Moonshot | `https://api.moonshot.cn/v1` |
| 硅基流动 | `https://api.siliconflow.cn/v1` |
| OpenAI | `https://api.openai.com/v1` |
| Anthropic（原生） | `https://api.anthropic.com`，provider 选 `anthropic` |
| Gemini（原生） | `https://generativelanguage.googleapis.com`，provider 选 `gemini` |
| Ollama | `http://127.0.0.1:11434`，provider 选 `ollama` |

**规则**：填到 `/v1` 为止，**不要**填 `/chat/completions`（程序会自己拼）。任何 OpenAI 兼容的自建端点（vLLM、LM Studio、OneAPI、New API 等）同样适用。

---

## 五、模型怎么选

PaperPulse 用模型做两件事，可以分别指定：

| 用途 | 频次 | 建议档位 |
|---|---|---|
| `model_parse` 兴趣解析 | 每次建订阅 1 次 | 中档（`deepseek-chat`、`qwen-plus`、`claude-3-5-haiku`） |
| `model_score` 论文打分 | 每用户每日约 6 次批调用 | **便宜档**（`deepseek-flash`、`qwen-turbo`、`gemini-2.0-flash`） |

打分是成本大头——每批 20 篇，一天几十个用户就是几百次调用。

> **注意推理模型**：带思维链的模型（如 `deepseek-reasoner`、`o1` 系列）会把 token 预算消耗在 reasoning 上，
> `max_tokens` 给小了会返回空正文。PaperPulse 已默认 `max_tokens: 4096` 并对空正文给出明确报错，
> 但**不建议**把推理模型用作 `model_score`。

---

## 六、常见问题

**Q：报 `401 Unauthorized`？**
Key 错了、过期了，或 Base URL 与厂商不匹配。也可能复制时带了首尾空格。

**Q：报 `429 Too Many Requests`？**
触发了厂商限流。检查是否开了多个 `paperpulse` 实例；DeepSeek 的并发上限不高，Lite 模式已默认限并发 2。

**Q：报 `Resource not accessible` / 400？**
模型名不存在。先在厂商控制台确认模型 ID，或换用该厂商的默认模型。

**Q：能不能不配 LLM？**
可以，引导第 3 步选「纯关键词模式」。但会有明显限制：

- 关键词由本地分词器产生，**只匹配论文标题/摘要的字面词**
- 论文池以英文为主而分词结果偏中文时，**召回会严重不足**
- 不会有「一句话生成中英双语关键词」的效果

建议至少配一个便宜档模型。

**Q：用户能不能自己填 Key？**
能。用户在「账户 → API Key」填自己的，系统优先用用户的、没填才用你的全局凭据。
这样你可以只给站点兜底，让在意成本的���户自带。

---

## 相关文档

- [LLM 接入总览](../llm-providers.md) — Provider 矩阵与三级降级机制
- [邮件通道配置教程](email-delivery-setup.md) — 另一项必配的部署者凭据
- [部署手册](../deployment.md) — 完整裸机部署流程
