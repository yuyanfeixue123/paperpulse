"""订阅配置页的预置关键词。

用途只是**快捷入口**：点击即加入关键词列表，降低「面对空白输入框想不出
该填什么」的门槛。输入框始终保留可自由输入 —— 论文关键词千奇百怪，
任何预置词表都覆盖不全。

选取原则：跨学科高频、检索语义明确、拼写无歧义。刻意不放人名、
机构名与带年份的词组，避免预置词把推荐范围带偏。
"""

from __future__ import annotations

KEYWORD_PRESETS: tuple[str, ...] = (
    # 城市与空间
    "urban morphology",
    "land use",
    "urban planning",
    "public transit",
    "walkability",
    "housing price",
    "spatial inequality",
    "smart city",
    "urban resilience",
    "community participation",
    "15-minute city",
    "informal settlement",
    # 环境与气候
    "climate adaptation",
    "urban heat island",
    "carbon neutrality",
    "air quality",
    "remote sensing",
    "GIS",
    "environmental justice",
    "biodiversity",
    "water resources",
    "renewable energy",
    # 公共政策与治理
    "public policy",
    "governance",
    "policy evaluation",
    "public participation",
    "urban governance",
    "planning law",
    "public service",
    "social equity",
    "stakeholder",
    "multi-level governance",
    "evidence-based policy",
    # 社会与经济
    "social science",
    "urban economics",
    "housing market",
    "gentrification",
    "demographics",
    "migration",
    "labour market",
    "inequality",
    "well-being",
    "social cohesion",
    "education",
    "public health",
    # 技术与数据
    "machine learning",
    "geospatial",
    "open data",
    "digital twin",
    "cellular automata",
    "agent-based model",
    "natural language processing",
    "computer vision",
    "spatial statistics",
    # 健康与福祉
    "active travel",
    "physical activity",
    "mental health",
    "ageing",
    "healthcare access",
)
