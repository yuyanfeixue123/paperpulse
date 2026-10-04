"""通道自适应词库。

邮件服务商的内容审核是一个黑盒：只有真正把信投出去，才知道哪个词会被拒。
本模块在投递被内容策略拒绝时，用管理员配置的 LLM 猜候选词，再用**最小探测邮件**
逐个试投，把「一测就中」的词沉淀进词库，供后续裁剪使用。

设计上的三条自律：
1. **一次探测只测一个词** —— 混着测无法归因，词库就会污染。
2. **必须复现才入库** —— 默认连续 2 次都被拒才激活，规避过滤器抖动导致的误封。
3. **有预算上限** —— 探测邮件是真发出去的信，会消耗额度，因此按天限额。

探测只用于**投递排障**：被判定为通道不便展示的论文仍完整保留在站内，
用户可登录查看，不会因为探测而被删除或改写。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)

# 服务商返回「这是内容问题」的典型特征
_CONTENT_HINTS = (
    "content spam",
    "antispam",
    "spam content",
    "message content",
    "blocked",
    "rejected by content",
    "550 ",
    "554 ",
)
_NETWORK_HINTS = (
    "timeout",
    "connection",
    "ssl",
    "dns",
    "temporarily",
    "421",
    "450",
)


def is_content_rejection(error: str) -> bool:
    """判断投递错误是否为内容策略拒绝（而非网络/鉴权/配额问题）。"""
    text = (error or "").lower()
    if any(h in text for h in _NETWORK_HINTS) and not any(
        h in text for h in _CONTENT_HINTS
    ):
        return False
    return any(h in text for h in _CONTENT_HINTS)


@dataclass
class ProbeOutcome:
    term: str
    blocked: bool
    error: str = ""
    message_id: str = ""


@dataclass
class ProbeReport:
    probed: int = 0
    newly_blocked: list[str] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)
    skipped_reason: str = ""
    candidates: list[str] = field(default_factory=list)
    all_passed: bool = False

    @property
    def ok(self) -> bool:
        return not self.skipped_reason

    @property
    def note(self) -> str:
        """给管理员的解读 —— 探测结果不能只报「成功/失败」。"""
        if self.newly_blocked:
            return f"已确认 {len(self.newly_blocked)} 个词会被该通道拒收，已加入词库"
        if self.all_passed and self.probed:
            return (
                f"{self.probed} 个候选词单独投递均被放行 —— 说明该通道不是按单词拦截，"
                "而是判定整封内容的组合语义。此时逐词探测无法复现拒收，"
                "建议在 /admin/terms 手工添加词条，或改用审核更宽松的通道"
            )
        return ""


# ---------------------------------------------------------------- 候选词生成

_TERMS_SCHEMA: dict = {
    "type": "object",
    "properties": {"terms": {"type": "array", "items": {"type": "string"}}},
}

_PROMPT = """下面是某封被邮件服务商内容审核拦截的论文推荐摘要节选。
请找出其中**最可能触发服务商内容审核的词或短语**，用于后续从邮件中排除。

要求：
- 只输出可能触发审核的词，不要输出中性学术词（如 urban morphology、deep learning）
- 每个词 2–4 个词，英文或中文均可，保持原文形态
- 从少到多排列，最可疑的放最前面
- 最多 12 个
- 严格输出 JSON 数组，不要任何解释文字

节选：
%s

JSON 数组："""


def propose_terms(snippets: list[str], max_terms: int = 12) -> list[str]:
    """用管理员配置的 LLM 猜候选触发词。失败时返回空列表（不影响主流程）。"""
    from app.llm.client import complete_json

    blob = "\n".join(snippets)[:3000]
    try:
        # 管理员配置的多为推理模型（deepseek-flash 等），reasoning 会吃掉大半
        # token 预算；这里显式放宽，避免 finish_reason=length 导致猜词失败。
        from app.core.config import get_settings

        data = complete_json(
            "revise",
            "你是一位熟悉邮件内容审核规则的助手。只输出 JSON 数组。",
            _PROMPT % blob,
            _TERMS_SCHEMA,
            max_tokens=max(8192, get_settings().llm.max_tokens * 2),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("probe.llm_failed", error=str(exc)[:180])
        return []
    # prompt 要求输出 JSON 数组，但模型常包一层 {"terms": [...]}，两种都要接
    raw: Any = data
    if isinstance(data, dict):
        raw = data.get("terms", data.get("result", data.get("results", [])))
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        log.info("probe.unexpected_shape", kind=type(data).__name__)
        return []

    terms: list[str] = []
    seen: set[str] = set()
    for item in raw:
        term = str(item).strip()
        if not term or len(term) > 40:
            continue
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)
        if len(terms) >= max_terms:
            break
    return terms


# ---------------------------------------------------------------- 探测执行


def _probe_body(term: str, site_name: str) -> tuple[str, str, str]:
    """探测邮件：极简、只含单个候选词，便于归因。"""
    subject = f"[{site_name}] 通道自检 {term}"
    html = (
        "<p>这是一封通道自检邮件，用于确认发信通道是否受理该内容。</p>"
        f"<p>关键词：<b>{term}</b></p>"
    )
    text = f"这是一封通道自检邮件。\n关键词：{term}\n"
    return subject, html, text


def run_probe(
    *,
    provider_row: Any,
    provider_factory: Any,
    to_address: str,
    site_name: str,
    candidates: list[str],
    budget: int,
    confirmations_required: int = 2,
) -> ProbeReport:
    """逐个试投候选词，把稳定被拒的词记入词库。

    探测邮件发到 ``to_address``（部署者自己的地址），内容极简、只含单个候选词。
    """
    from app.core.security import decrypt_value
    from app.email.providers import OutgoingMessage

    report = ProbeReport(candidates=list(candidates))
    if not to_address:
        report.skipped_reason = "未配置探测收件地址"
        return report
    if not candidates:
        report.skipped_reason = "LLM 未给出候选词"
        return report
    if budget <= 0:
        report.skipped_reason = "今日探测预算已用尽"
        return report

    cfg = dict(provider_row.config_json and json.loads(provider_row.config_json) or {})
    for key in ("password", "api_key"):
        if str(cfg.get(key, "")).startswith("enc:"):
            cfg[key] = decrypt_value(cfg[key][4:])

    try:
        provider = provider_factory(provider_row.kind, cfg)
    except Exception as exc:  # noqa: BLE001
        report.skipped_reason = f"通道不可用：{exc}"
        return report

    for term in candidates:
        if report.probed >= budget:
            report.skipped_reason = "达到本次探测预算上限"
            break
        subject, html, text = _probe_body(term, site_name)
        msg = OutgoingMessage(
            to_email=to_address,
            subject=subject,
            html=html,
            text=text,
            from_email=cfg.get("from_email", ""),
            from_name=site_name,
            headers={"Auto-Submitted": "auto-generated", "X-PaperPulse-Probe": "1"},
        )
        try:
            provider.send(msg)
            outcome = ProbeOutcome(term=term, blocked=False, message_id="delivered")
        except Exception as exc:  # noqa: BLE001
            blocked = is_content_rejection(str(exc))
            outcome = ProbeOutcome(term=term, blocked=blocked, error=str(exc)[:200])
        report.probed += 1
        _record(outcome, provider_row.key, confirmations_required, report)

    # 逐词探测全部放行 => 该通道不是按单词拦截，逐词探测无法复现整封拒收
    report.all_passed = report.probed > 0 and not report.newly_blocked
    if report.all_passed:
        log.info("probe.all_passed", probed=report.probed)
    return report


def _record(
    outcome: ProbeOutcome, provider_key: str, confirmations_required: int, report: ProbeReport
) -> None:
    """把探测结果写进词库；连续 N 次被拒才激活。"""
    from app.core.db import SessionLocal
    from app.core.utils import utc_iso
    from app.models.channel import ChannelTerm

    with SessionLocal() as s:
        row = (
            s.query(ChannelTerm)
            .filter(
                ChannelTerm.provider_key == provider_key,
                ChannelTerm.term == outcome.term,
            )
            .first()
        )
        if row is None:
            row = ChannelTerm(
                provider_key=provider_key,
                term=outcome.term,
                source="auto-probe",
                active=False,
                blocked_hits=0,
                miss_hits=0,
                last_error="",
                created_at=utc_iso(),
            )
        # 列默认值只在 INSERT 时生效，ORM 侧仍可能是 None
        row.blocked_hits = (int(row.blocked_hits or 0) + 1) if outcome.blocked else 0
        row.miss_hits = 0 if outcome.blocked else int(row.miss_hits or 0) + 1
        row.last_error = outcome.error[:300]
        row.last_tested_at = utc_iso()
        if row.blocked_hits >= confirmations_required:
            if not row.active:
                report.newly_blocked.append(row.term)
                log.info("probe.term_activated", term=row.term, provider=provider_key)
            row.active = 1
        elif not outcome.blocked and row.active:
            row.active = 0
            report.cleared.append(row.term)
            log.info("probe.term_deactivated", term=row.term, provider=provider_key)
        s.add(row)
        s.commit()


def escape_for_regex(term: str) -> str:
    """词库项按字面量匹配，避免用户输入的正则元字符引发意外。"""
    return re.escape(term.strip())
