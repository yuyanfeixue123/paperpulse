# 数据源

## 设计约定

- 所有源定义集中在 `config/sources.yaml`，字段：`key / name / type / field / url_template / params / requires_key / rate_limit / enabled`。
- **默认开启的源必须全部免注册、免 Key**。需 Key 的源在未配置凭据时开关置灰不可启用。
- 统一接口：`fetch(start_date, end_date, params) -> Iterator[PaperItem]`、`healthcheck() -> (bool, str)`。
- 新增源不需要改代码：在 `sources.yaml` 加一段，并选用已有适配器。

## 适配器类型

| type | 适配对象 | 分页 |
|---|---|---|
| `arxiv` | arXiv Atom API | `start` 偏移，单连接、3 秒间隔 |
| `openalex` | OpenAlex works | cursor 游标 |
| `biorxiv` | bioRxiv / medRxiv details | cursor 递增，每页固定 30 条 |
| `doaj` | DOAJ 检索 | page |
| `europepmc` | Europe PMC search | cursorMark |
| `osf` | OSF Preprints（按 provider 区分） | links.next |
| `rss` | 任意 RSS / Atom | 条件请求（ETag / If-Modified-Since） |
| `zenodo` / `hal` / `chemrxiv` | 简单 JSON 检索源 | — |
| `crossref` | **仅补全**，不参与常规召回 | — |

`UnsupportedSource` 兜住 `sources.yaml` 中已登记但暂无适配器的类型（默认关闭）。

## URL 构造（照抄）

```python
# arXiv
https://export.arxiv.org/api/query
  ?search_query=(cat:cs.AI+OR+cat:cs.CL)+AND+submittedDate:[202610010000+TO+202610032359]
  &start={offset}&max_results=100&sortBy=submittedDate&sortOrder=descending

# OpenAlex
https://api.openalex.org/works
  ?filter=from_publication_date:{d1},to_publication_date:{d2},default.search:{q},has_abstract:true
  &per-page=200&cursor={cursor}&mailto={contact}

# bioRxiv / medRxiv
https://api.biorxiv.org/details/{server}/{d1}/{d2}/{cursor}/json

# DOAJ
https://doaj.org/api/search/articles/{urlquote(q)}?pageSize=100&sort=created_date:desc

# Europe PMC
https://www.ebi.ac.uk/europepmc/webservices/rest/search
  ?query=(FIRST_PDATE:[{d1} TO {d2}]) AND ({q})&format=json&pageSize=100&resultType=core

# OSF
https://api.osf.io/v2/preprints/?filter[q]={q}&filter[provider]={p}&page[size]=100&sort=-date_created

# Crossref（补全）
https://api.crossref.org/works?query.bibliographic={title}&rows=1&select=DOI,title,abstract&mailto={contact}
```

## OpenAlex 摘要还原

```python
inv = work.get("abstract_inverted_index") or {}
pos = {}
for word, places in inv.items():
    for p in places:
        pos[p] = word
abstract = " ".join(pos[i] for i in sorted(pos))
```

## 去重键

```
doi:<小写>  →  arxiv:<id>  →  t:<标题归一化 sha1 前 16 位>
```

## 补全（enrich）

仅当 `doi` 为空或摘要短于 200 字符时执行：

1. 从 URL 中用正则提取 DOI
2. 按标题查 OpenAlex
3. 仍不足则按标题查 Crossref
4. 补不到 → `abstract_quality = 'short'`，打分时提示降权

## RSS 要点

1. `source_id` 依次取 `guid` → `link` → 标题哈希
2. **订阅制 vs 检索制**：RSS 源不过滤关键词（feed 给什么收什么），靠后续关键词 + LLM 筛；API 源才带关键词召回
3. 条件请求：`If-Modified-Since` / `ETag`，304 直接跳过
4. 自定义 feed 入库前做 SSRF 校验（仅 http/https，拒绝内网 / 回环 / 链路本地）
5. 支持「粘贴期刊主页自动发现」：解析 `<link rel="alternate" type="application/rss+xml">`

## 已知现状（诚实说明）

- **arXiv 官方分类 RSS 页已下线**（`info.arxiv.org/help/rss` 返回 404）。arXiv API 返回的就是 Atom，直接用查询串当 feed 订阅。
- **JournalTOCs 已于 2026-09-06 停止解析**，Zetoc 于 2022 退役。不依赖任何第三方 TOC 聚合器，只用出版社自己的 feed。
- **ChinaXiv** 接口不稳定，默认关闭；知网 / 万方 / 维普无开放 API，明确不做。
- 现实替代：通过 OpenAlex / Crossref / DOAJ 检索中文期刊的英文元数据条目（多数中文期刊已被 Crossref 收录 DOI）。

## 连通性自检

```bash
python scripts/verify_sources.py            # 全部
python scripts/verify_sources.py arxiv nber_new   # 指定
```

免 Key 源打印 `✅ key 条数 最新日期`；需 Key 未配置打印 `— 需 Key（未配置）`。

## 致谢

*Thank you to arXiv for use of its open access interoperability.*
