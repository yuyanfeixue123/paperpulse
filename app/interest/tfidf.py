"""轻量 TF-IDF：口味相似度与反馈显著词抽取（纯本地计算，零 API 成本）。"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

_TOKEN = re.compile(r"[A-Za-z][A-Za-z\-]{1,}|[\u4e00-\u9fff]{2,}")

STOPWORDS = {
    # 英文虚词与常见功能词
    "a", "an", "the", "and", "or", "but", "if", "of", "in", "on", "at", "to", "by",
    "for", "with", "from", "as", "is", "are", "was", "were", "be", "been", "being",
    "am", "do", "does", "did", "doing", "have", "has", "had", "having", "it", "its",
    "this", "that", "these", "those", "there", "here", "we", "our", "us", "you",
    "your", "they", "their", "them", "he", "she", "his", "her", "i", "my", "me",
    "not", "no", "nor", "so", "than", "then", "too", "very", "can", "could", "may",
    "might", "must", "shall", "should", "will", "would", "about", "above", "after",
    "again", "against", "all", "also", "any", "because", "before", "below",
    "between", "both", "each", "few", "further", "into", "more", "most", "only",
    "other", "out", "over", "same", "some", "such", "under", "until", "up", "via",
    "while", "within", "without", "however", "therefore", "thus", "although",
    # 学术写作高频但无区分度的词
    "use", "used", "uses", "using", "based", "study", "studies", "paper", "papers",
    "result", "results", "show", "shows", "shown", "find", "finds", "found",
    "propose", "proposed", "approach", "method", "methods", "model", "models",
    "data", "analysis", "research", "new", "novel", "first", "two", "one", "three",
    "high", "low", "large", "small", "different", "various", "respectively",
    "moreover", "furthermore", "et", "al",
    # 中文虚词
    "的", "了", "和", "与", "在", "是", "我", "也", "不", "有", "对", "中", "为",
    "这", "那", "其", "之", "并", "等", "上", "下", "个", "一", "二", "三", "以",
    "及", "或", "而", "且", "被", "把", "从", "到", "会", "能", "可", "就", "都",
    "很", "更", "最", "研究", "方法", "结果", "本文", "我们", "他们",
}


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text or "") if t.lower() not in STOPWORDS]


def build_centroid(docs: list[str]) -> dict[str, float]:
    """多篇文档合并后的 TF-IDF 质心（L2 归一化，单文档时退化为 TF 归一化）。"""
    if not docs:
        return {}
    tokenized = [tokenize(d) for d in docs]
    n = len(tokenized)
    df: Counter[str] = Counter()
    for toks in tokenized:
        df.update(set(toks))
    centroid: dict[str, float] = defaultdict(float)
    for toks in tokenized:
        tf = Counter(toks)
        if not tf:
            continue
        max_tf = max(tf.values()) or 1
        for term, count in tf.items():
            idf = math.log((n + 1) / (df[term] + 1)) + 1.0
            centroid[term] += (0.5 + 0.5 * count / max_tf) * idf
    return normalize(centroid)


def normalize(vec: dict[str, float]) -> dict[str, float]:
    norm = math.sqrt(sum(v * v for v in vec.values()))
    if norm == 0:
        return {}
    return {k: v / norm for k, v in vec.items()}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    return float(sum(a.get(t, 0.0) * v for t, v in b.items()))


def vectorize(text: str) -> dict[str, float]:
    tf = Counter(tokenize(text))
    if not tf:
        return {}
    return normalize({t: float(c) for t, c in tf.items()})


def salient_terms(text: str, profile_terms: set[str], top_n: int = 3) -> list[str]:
    """相对画像的增量显著词：去掉画像已覆盖的词，取 TF 最高的前 N 个。"""
    tf = Counter(tokenize(text))
    for t in profile_terms:
        tf.pop(t.lower(), None)
    return [t for t, _ in tf.most_common(top_n)]
