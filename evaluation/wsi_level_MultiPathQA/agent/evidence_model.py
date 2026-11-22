#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
证据原子化模型（Evidence-Carrying Memory）

将分析发现结构化为独立的"证据单元"，支持聚合和审计。
"""

from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Any
from datetime import datetime
import json
import re
import logging

logger = logging.getLogger(__name__)


@dataclass
class EvidenceUnit:
    """证据单元：分析发现的原子化表示"""
    id: str                                    # 唯一ID
    roi_path: str                              # 路径：R1-B2-G3
    level: str                                 # red/blue/green
    finding_type: str                          # tumor/normal/lvsi/necrosis/artifact/inconclusive
    description: str                           # 详细描述
    confidence: float                          # 0-1 置信度
    coordinates: Dict[str, int] = field(default_factory=dict)  # {'x', 'y', 'w', 'h'}
    supporting_features: List[str] = field(default_factory=list)  # 支持特征列表
    diagnostic_relevance: Dict[str, str] = field(default_factory=dict)  # Q1-Q9相关性
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return asdict(self)
    
    def to_json(self) -> str:
        """转换为JSON字符串"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
    
    @classmethod
    def from_dict(cls, data: dict) -> 'EvidenceUnit':
        """从字典创建"""
        return cls(**data)


# ==================== 证据类型定义 ====================

FINDING_TYPES = {
    'tumor': '恶性肿瘤',
    'normal': '正常组织',
    'lvsi': '脉管内癌栓',
    'necrosis': '坏死',
    'artifact': '伪影/切片问题',
    'inflammation': '炎症',
    'atypical': '异型性',
    'mitosis': '核分裂象',
    'invasion': '浸润',
    'inconclusive': '无法确定'
}

# 回溯触发类型（Plan 阶段）
BACKTRACK_TRIGGERS = {
    'necrosis': ['坏死', '凝固性坏死', '组织坏死', 'necrosis'],
    'artifact': ['伪影', '切片问题', '染色不良', '折叠', '气泡', 'artifact'],
    'blank': ['空白', '无组织', '背景', '空白区域'],
    'low_value': ['无诊断价值', '无异常', '正常组织', '无需进一步观察'],
    'off_target': ['偏离目标', '非目标区域', '与诊断无关']
}


# ==================== 证据解析函数 ====================

def parse_evidence_from_analysis(
    analysis_result: dict, 
    roi_info: dict,
    level: str = 'unknown'
) -> List[EvidenceUnit]:
    """
    从AI分析结果中解析出结构化证据单元
    
    Args:
        analysis_result: AI分析结果字典，包含 findings, confidence 等
        roi_info: ROI信息，包含 id, rect 等
        level: 层级 (red/blue/green)
    
    Returns:
        List[EvidenceUnit]: 解析出的证据单元列表
    """
    evidence_list = []
    
    findings_text = analysis_result.get('findings', '')
    if not findings_text:
        return evidence_list
    
    confidence = analysis_result.get('confidence', 0.5)
    if isinstance(confidence, (int, float)):
        confidence = confidence / 100 if confidence > 1 else confidence
    
    roi_id = roi_info.get('id', 'unknown')
    rect = roi_info.get('rect', {})
    
    # 生成唯一ID
    timestamp = datetime.now().strftime('%H%M%S%f')[:10]
    evidence_id = f"EV_{roi_id}_{timestamp}"
    
    # 分析文本，提取特征
    finding_type = classify_finding_type(findings_text)
    features = extract_supporting_features(findings_text)
    
    # 创建证据单元
    evidence = EvidenceUnit(
        id=evidence_id,
        roi_path=roi_id,
        level=level,
        finding_type=finding_type,
        description=findings_text[:500] if len(findings_text) > 500 else findings_text,
        confidence=confidence,
        coordinates=rect,
        supporting_features=features
    )
    
    evidence_list.append(evidence)
    
    return evidence_list


def classify_finding_type(text: str) -> str:
    """根据文本内容分类发现类型"""
    text_lower = text.lower()
    
    # 恶性肿瘤关键词
    tumor_keywords = ['癌', '肿瘤', '恶性', 'carcinoma', 'tumor', 'malignant', 
                      '异型性明显', '核分裂象', '浸润']
    if any(kw in text_lower for kw in tumor_keywords):
        return 'tumor'
    
    # LVSI关键词
    lvsi_keywords = ['脉管', '癌栓', 'lvsi', '淋巴管', '血管内', '内皮细胞']
    if any(kw in text_lower for kw in lvsi_keywords):
        return 'lvsi'
    
    # 坏死关键词
    necrosis_keywords = ['坏死', 'necrosis', '凝固性', '液化性']
    if any(kw in text_lower for kw in necrosis_keywords):
        return 'necrosis'
    
    # 伪影关键词
    artifact_keywords = ['伪影', '染色', '折叠', '气泡', 'artifact']
    if any(kw in text_lower for kw in artifact_keywords):
        return 'artifact'
    
    # 正常组织
    normal_keywords = ['正常', '未见异常', '良性', 'normal', 'benign']
    if any(kw in text_lower for kw in normal_keywords):
        return 'normal'
    
    # 炎症
    inflammation_keywords = ['炎症', '炎性', 'inflammation', '淋巴细胞浸润']
    if any(kw in text_lower for kw in inflammation_keywords):
        return 'inflammation'
    
    return 'inconclusive'


def extract_supporting_features(text: str) -> List[str]:
    """从文本中提取支持特征"""
    features = []
    
    # 形态学特征关键词
    morphology_patterns = [
        (r'核分裂象[^\s,，。]*', '核分裂象'),
        (r'核浆比[^\s,，。]*', '核浆比异常'),
        (r'异型性[^\s,，。]*', '细胞异型性'),
        (r'背靠背[^\s,，。]*', '背靠背腺体'),
        (r'筛状[^\s,，。]*', '筛状结构'),
        (r'乳头状[^\s,，。]*', '乳头状结构'),
        (r'实性[^\s,，。]*', '实性生长'),
        (r'浸润[^\s,，。]*', '浸润性生长'),
        (r'内皮细胞[^\s,，。]*', '内皮细胞衬里'),
    ]
    
    for pattern, feature_name in morphology_patterns:
        if re.search(pattern, text):
            features.append(feature_name)
    
    return features[:10]  # 最多保留10个特征


# ==================== 证据聚合函数 ====================

def aggregate_evidence(evidence_pool: List[EvidenceUnit]) -> Dict[str, Any]:
    """
    聚合所有证据，计算全局置信度和诊断结论
    
    Args:
        evidence_pool: 证据单元列表
    
    Returns:
        dict: {
            'total_evidence': int,
            'by_type': dict,
            'global_confidence': float,
            'diagnostic_summary': dict
        }
    """
    if not evidence_pool:
        return {
            'total_evidence': 0,
            'by_type': {},
            'global_confidence': 0.0,
            'diagnostic_summary': {}
        }
    
    # 按类型统计
    by_type = {}
    for ev in evidence_pool:
        ft = ev.finding_type
        if ft not in by_type:
            by_type[ft] = {'count': 0, 'confidence_sum': 0}
        by_type[ft]['count'] += 1
        by_type[ft]['confidence_sum'] += ev.confidence
    
    # 计算各类型平均置信度
    for ft in by_type:
        by_type[ft]['avg_confidence'] = by_type[ft]['confidence_sum'] / by_type[ft]['count']
    
    # 计算全局置信度（加权平均，恶性证据权重更高）
    weights = {'tumor': 2.0, 'lvsi': 1.5, 'invasion': 1.5, 'normal': 0.5, 'artifact': 0.1}
    weighted_sum = 0
    weight_total = 0
    for ev in evidence_pool:
        w = weights.get(ev.finding_type, 1.0)
        weighted_sum += ev.confidence * w
        weight_total += w
    
    global_confidence = weighted_sum / weight_total if weight_total > 0 else 0.5
    
    # 诊断摘要（聚合Q1-Q9相关性）
    diagnostic_summary = {}
    for q in ['Q1', 'Q2', 'Q3', 'Q4', 'Q5', 'Q6', 'Q7', 'Q8', 'Q9']:
        q_findings = []
        for ev in evidence_pool:
            if q in ev.diagnostic_relevance:
                q_findings.append(ev.diagnostic_relevance[q])
        if q_findings:
            # 取最常见的结论
            from collections import Counter
            most_common = Counter(q_findings).most_common(1)
            diagnostic_summary[q] = most_common[0][0] if most_common else '待定'
    
    return {
        'total_evidence': len(evidence_pool),
        'by_type': by_type,
        'global_confidence': round(global_confidence, 3),
        'diagnostic_summary': diagnostic_summary
    }


def generate_evidence_chain(evidence_pool: List[EvidenceUnit]) -> str:
    """
    生成可审计的证据链文本
    
    Args:
        evidence_pool: 证据单元列表
    
    Returns:
        str: 格式化的证据链文本
    """
    if not evidence_pool:
        return "【证据链】\n无可用证据"
    
    # 按层级和路径排序
    sorted_evidence = sorted(evidence_pool, key=lambda e: (
        {'red': 0, 'blue': 1, 'green': 2}.get(e.level, 3),
        e.roi_path
    ))
    
    lines = ["【证据链 - Evidence Chain】", "=" * 60]
    
    current_level = None
    for ev in sorted_evidence:
        # 层级标题
        if ev.level != current_level:
            current_level = ev.level
            level_names = {'red': '10× 低倍镜', 'blue': '40× 高倍镜', 'green': '160× 高倍镜'}
            lines.append(f"\n## {level_names.get(current_level, current_level)}")
            lines.append("-" * 40)
        
        # 证据单元
        type_name = FINDING_TYPES.get(ev.finding_type, ev.finding_type)
        lines.append(f"\n📌 [{ev.roi_path}] {type_name}")
        lines.append(f"   置信度: {ev.confidence:.0%}")
        lines.append(f"   描述: {ev.description[:100]}{'...' if len(ev.description) > 100 else ''}")
        
        if ev.supporting_features:
            lines.append(f"   特征: {', '.join(ev.supporting_features[:5])}")
        
        if ev.diagnostic_relevance:
            relevance_str = ', '.join(f"{k}:{v}" for k, v in ev.diagnostic_relevance.items())
            lines.append(f"   诊断相关: {relevance_str}")
    
    # 聚合摘要
    aggregation = aggregate_evidence(evidence_pool)
    lines.append("\n" + "=" * 60)
    lines.append("【聚合摘要】")
    lines.append(f"总证据数: {aggregation['total_evidence']}")
    lines.append(f"全局置信度: {aggregation['global_confidence']:.0%}")
    
    if aggregation['diagnostic_summary']:
        lines.append("\n诊断结论:")
        for q, conclusion in aggregation['diagnostic_summary'].items():
            lines.append(f"  {q}: {conclusion}")
    
    return "\n".join(lines)


# ==================== 证据池管理 ====================

class EvidencePool:
    """证据池管理类"""
    
    def __init__(self):
        self._evidence: List[EvidenceUnit] = []
    
    def add(self, evidence: EvidenceUnit):
        """添加证据"""
        self._evidence.append(evidence)
        logger.debug(f"📌 [证据池] 添加证据: {evidence.id} ({evidence.finding_type})")
    
    def add_batch(self, evidence_list: List[EvidenceUnit]):
        """批量添加证据"""
        self._evidence.extend(evidence_list)
        logger.debug(f"📌 [证据池] 批量添加 {len(evidence_list)} 条证据")
    
    def get_all(self) -> List[EvidenceUnit]:
        """获取所有证据"""
        return self._evidence.copy()
    
    def get_by_type(self, finding_type: str) -> List[EvidenceUnit]:
        """按类型筛选"""
        return [ev for ev in self._evidence if ev.finding_type == finding_type]
    
    def get_by_level(self, level: str) -> List[EvidenceUnit]:
        """按层级筛选"""
        return [ev for ev in self._evidence if ev.level == level]
    
    def get_by_roi(self, roi_path: str) -> List[EvidenceUnit]:
        """按ROI路径筛选"""
        return [ev for ev in self._evidence if ev.roi_path.startswith(roi_path)]
    
    def aggregate(self) -> Dict[str, Any]:
        """聚合分析"""
        return aggregate_evidence(self._evidence)
    
    def generate_chain(self) -> str:
        """生成证据链"""
        return generate_evidence_chain(self._evidence)
    
    def clear(self):
        """清空证据池"""
        self._evidence.clear()
    
    def __len__(self):
        return len(self._evidence)
    
    def to_json(self) -> str:
        """导出为JSON"""
        return json.dumps([ev.to_dict() for ev in self._evidence], ensure_ascii=False, indent=2)
