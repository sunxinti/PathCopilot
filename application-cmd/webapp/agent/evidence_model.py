#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Organ-agnostic evidence records used by the open-source Agent Loop.

This module stores traceable observations for audit and synthesis. It does not
infer a diagnosis, grade, stage, invasion target, or hidden question from
keywords in model prose.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List
import json
import logging
import re


logger = logging.getLogger(__name__)


@dataclass
class EvidenceUnit:
    """One traceable observation derived from a single ROI analysis."""

    id: str
    roi_path: str
    level: str
    finding_type: str
    description: str
    confidence: float
    coordinates: Dict[str, int] = field(default_factory=dict)
    supporting_features: List[str] = field(default_factory=list)
    diagnostic_relevance: Dict[str, str] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> "EvidenceUnit":
        return cls(**data)


FINDING_TYPES = {
    "lesion": "病变组织",
    "normal": "未见明确异常",
    "necrosis": "坏死",
    "artifact": "伪影/切片质量问题",
    "inflammation": "炎症",
    "atypia": "细胞异型",
    "mitosis": "核分裂象",
    "invasion": "浸润性生长",
    "inconclusive": "无法确定",
}


BACKTRACK_TRIGGERS = {
    "necrosis": ["坏死", "凝固性坏死", "组织坏死", "necrosis"],
    "artifact": ["伪影", "切片问题", "染色不良", "折叠", "气泡", "artifact"],
    "blank": ["空白", "无组织", "背景", "空白区域"],
    "low_value": ["无诊断价值", "无异常", "正常组织", "无需进一步观察"],
    "off_target": ["偏离目标", "非目标区域", "与诊断无关"],
}


def _normalise_confidence(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.5
    if numeric > 1:
        numeric /= 100
    return min(1.0, max(0.0, numeric))


def classify_finding_type(text: str) -> str:
    """Assign a broad audit category without making a diagnosis."""

    lowered = (text or "").lower()
    categories = (
        ("artifact", ("伪影", "折叠", "污染", "挤压", "artifact", "fold")),
        ("necrosis", ("坏死", "necrosis")),
        ("inflammation", ("炎症", "炎细胞", "inflammation", "lymphocyte")),
        ("mitosis", ("核分裂", "mitosis", "mitotic")),
        ("atypia", ("异型", "atypia", "pleomorphism")),
        ("invasion", ("浸润性生长", "破坏性浸润", "invasive growth")),
        ("normal", ("未见明确异常", "未见特殊", "正常组织", "no significant")),
        ("lesion", ("病变", "肿物", "lesion", "neoplasm")),
    )
    for category, keywords in categories:
        if any(keyword in lowered for keyword in keywords):
            return category
    return "inconclusive"


def extract_supporting_features(text: str) -> List[str]:
    """Extract neutral morphologic feature labels for search and display."""

    patterns = (
        (r"核分裂象[^\s,，。]*", "核分裂象"),
        (r"核浆比[^\s,，。]*", "核浆比异常"),
        (r"异型性[^\s,，。]*", "细胞异型性"),
        (r"筛状[^\s,，。]*", "筛状结构"),
        (r"乳头状[^\s,，。]*", "乳头状结构"),
        (r"实性[^\s,，。]*", "实性结构"),
        (r"坏死[^\s,，。]*", "坏死"),
        (r"炎症[^\s,，。]*", "炎症背景"),
        (r"伪影[^\s,，。]*", "伪影"),
    )
    return [label for pattern, label in patterns if re.search(pattern, text or "")][:10]


def extract_diagnostic_relevance(text: str) -> Dict[str, str]:
    """Compatibility hook: relevance is supplied by the active user task."""

    return {}


def detect_additional_findings(
    text: str,
    base_roi_id: str,
    rect: dict,
    level: str,
) -> List[EvidenceUnit]:
    """Do not synthesize additional findings from keywords."""

    return []


def parse_evidence_from_analysis(
    analysis_result: dict,
    roi_info: dict,
    level: str = "unknown",
) -> List[EvidenceUnit]:
    """Create one audit record from an ROI result without diagnostic inference."""

    findings_text = str(analysis_result.get("findings") or "").strip()
    if not findings_text:
        return []

    roi_id = str(roi_info.get("id", "unknown"))
    timestamp = datetime.now().strftime("%H%M%S%f")[:10]
    unit = EvidenceUnit(
        id=f"EV_{roi_id}_{timestamp}",
        roi_path=roi_id,
        level=level,
        finding_type=classify_finding_type(findings_text),
        description=findings_text[:2000],
        confidence=_normalise_confidence(analysis_result.get("confidence", 0.5)),
        coordinates=roi_info.get("rect", {}) or {},
        supporting_features=extract_supporting_features(findings_text),
        diagnostic_relevance={},
    )
    return [unit]


def aggregate_evidence(evidence_pool: List[EvidenceUnit]) -> Dict[str, Any]:
    """Return descriptive audit statistics, never a diagnostic conclusion."""

    if not evidence_pool:
        return {
            "total_evidence": 0,
            "by_type": {},
            "global_confidence": 0.0,
            "diagnostic_summary": {},
        }

    by_type: Dict[str, Dict[str, float]] = {}
    for unit in evidence_pool:
        bucket = by_type.setdefault(
            unit.finding_type,
            {"count": 0, "confidence_sum": 0.0},
        )
        bucket["count"] += 1
        bucket["confidence_sum"] += unit.confidence
    for bucket in by_type.values():
        bucket["avg_confidence"] = bucket["confidence_sum"] / bucket["count"]

    return {
        "total_evidence": len(evidence_pool),
        "by_type": by_type,
        "global_confidence": round(
            sum(unit.confidence for unit in evidence_pool) / len(evidence_pool),
            3,
        ),
        "diagnostic_summary": {},
    }


def generate_evidence_chain(evidence_pool: List[EvidenceUnit]) -> str:
    """Render the traceable observations without adding inferred labels."""

    if not evidence_pool:
        return "【证据链】\n无可用证据"

    level_order = {"red": 0, "blue": 1, "green": 2}
    sorted_units = sorted(
        evidence_pool,
        key=lambda unit: (level_order.get(unit.level, 3), unit.roi_path),
    )
    lines = ["【证据链 - Evidence Chain】", "=" * 60]
    current_level = None
    level_names = {
        "red": "10× 低倍",
        "blue": "40× 中倍",
        "green": "160× 高倍",
    }
    for unit in sorted_units:
        if unit.level != current_level:
            current_level = unit.level
            lines.extend([f"\n## {level_names.get(current_level, current_level)}", "-" * 40])
        lines.extend(
            [
                f"\n[{unit.roi_path}] {FINDING_TYPES.get(unit.finding_type, unit.finding_type)}",
                f"证据支持度: {unit.confidence:.0%}",
                f"描述: {unit.description}",
            ]
        )
        if unit.supporting_features:
            lines.append(f"形态特征: {', '.join(unit.supporting_features)}")

    aggregation = aggregate_evidence(sorted_units)
    lines.extend(
        [
            "\n" + "=" * 60,
            "【审计摘要】",
            f"证据记录数: {aggregation['total_evidence']}",
            f"平均证据支持度: {aggregation['global_confidence']:.0%}",
        ]
    )
    return "\n".join(lines)


class EvidencePool:
    """In-memory collection of traceable evidence records."""

    def __init__(self):
        self._evidence: List[EvidenceUnit] = []

    def add(self, evidence: EvidenceUnit):
        self._evidence.append(evidence)
        logger.debug("[EvidencePool] added %s", evidence.id)

    def add_batch(self, evidence_list: List[EvidenceUnit]):
        self._evidence.extend(evidence_list)
        logger.debug("[EvidencePool] added %s records", len(evidence_list))

    def get_all(self) -> List[EvidenceUnit]:
        return self._evidence.copy()

    def get_by_type(self, finding_type: str) -> List[EvidenceUnit]:
        return [unit for unit in self._evidence if unit.finding_type == finding_type]

    def get_by_level(self, level: str) -> List[EvidenceUnit]:
        return [unit for unit in self._evidence if unit.level == level]

    def get_by_roi(self, roi_path: str) -> List[EvidenceUnit]:
        return [unit for unit in self._evidence if unit.roi_path.startswith(roi_path)]

    def aggregate(self) -> Dict[str, Any]:
        return aggregate_evidence(self._evidence)

    def generate_chain(self) -> str:
        return generate_evidence_chain(self._evidence)

    def clear(self):
        self._evidence.clear()

    def __len__(self):
        return len(self._evidence)

    def to_json(self) -> str:
        return json.dumps(
            [unit.to_dict() for unit in self._evidence],
            ensure_ascii=False,
            indent=2,
        )
