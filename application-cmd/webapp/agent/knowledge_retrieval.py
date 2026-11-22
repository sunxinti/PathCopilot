#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
知识检索模块（Knowledge Retrieval）

通过PubMed API检索相关医学文献，为病理诊断提供循证支持。
这是 PathCopilot Agent Loop 的 Plan 阶段知识检索功能实现。
"""

import requests
import re
import time
import logging
from typing import List, Dict, Optional
from functools import lru_cache

logger = logging.getLogger(__name__)

# ==================== PubMed API 配置 ====================

PUBMED_BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
PUBMED_ESEARCH = PUBMED_BASE_URL + "esearch.fcgi"
PUBMED_EFETCH = PUBMED_BASE_URL + "efetch.fcgi"
PUBMED_ESUMMARY = PUBMED_BASE_URL + "esummary.fcgi"

# 免费使用限制：3请求/秒（无API Key时）
# 如有API Key可提高到10请求/秒
PUBMED_API_KEY = None  # 可选：申请NCBI API Key提高请求频率

# 请求间隔（秒）
REQUEST_INTERVAL = 0.4 if PUBMED_API_KEY else 0.35


# ==================== 诊断关键词库 ====================

# 病理学术语中英对照（用于关键词提取和检索）
PATHOLOGY_TERM_MAPPING = {
    # 通用诊断类别
    '腺癌': 'adenocarcinoma',
    '鳞状细胞癌': 'squamous cell carcinoma',
    '浆液性癌': 'serous carcinoma',
    '透明细胞癌': 'clear cell carcinoma',
    '黏液性癌': 'mucinous carcinoma',
    '未分化癌': 'undifferentiated carcinoma',
    '淋巴瘤': 'lymphoma',
    '肉瘤': 'sarcoma',
    
    # 分级
    'G1': 'grade 1 well differentiated',
    'G2': 'grade 2 moderately differentiated',
    'G3': 'grade 3 poorly differentiated',
    '高分化': 'well differentiated',
    '中分化': 'moderately differentiated',
    '低分化': 'poorly differentiated',
    
    # 形态学特征
    '核分裂象': 'mitotic figures',
    '背靠背腺体': 'back-to-back glands',
    '筛状结构': 'cribriform pattern',
    '乳头状结构': 'papillary pattern',
    '实性生长': 'solid growth pattern',
    '浸润性生长': 'invasive growth',
    
    # 其他
    '坏死': 'necrosis',
    '核异型性': 'nuclear atypia',
    '促纤维结缔组织增生': 'desmoplastic reaction',
}

# 器官未知时只使用通用领域词；具体器官和病种必须来自用户问题或形态学文本。
DEFAULT_SEARCH_FIELDS = ["diagnostic pathology", "histopathology differential diagnosis"]


# ==================== 关键词提取函数 ====================

def extract_diagnostic_keywords(analysis_text: str, max_keywords: int = 5) -> List[str]:
    """
    从分析文本中提取诊断关键词
    
    Args:
        analysis_text: 病理分析文本
        max_keywords: 最大关键词数量
    
    Returns:
        List[str]: 英文关键词列表（用于PubMed检索）
    """
    if not analysis_text:
        return DEFAULT_SEARCH_FIELDS[:2]
    
    keywords = []
    text_lower = analysis_text.lower()
    
    # 检测中文关键词并转换为英文
    for chinese_term, english_term in PATHOLOGY_TERM_MAPPING.items():
        if chinese_term in analysis_text:
            keywords.append(english_term)
    
    # 检测英文关键词
    english_patterns = [
        r'adenocarcinoma|squamous cell carcinoma|lymphoma|sarcoma',
        r'serous',
        r'clear cell',
        r'grade [123]|G[123]',
        r'poorly differentiated|well differentiated',
        r'mitotic',
        r'cribriform|papillary|solid',
    ]
    
    for pattern in english_patterns:
        if re.search(pattern, analysis_text, re.IGNORECASE):
            match = re.search(pattern, analysis_text, re.IGNORECASE)
            if match:
                keywords.append(match.group(0).lower())
    
    # 去重并限制数量
    unique_keywords = list(dict.fromkeys(keywords))
    
    # 如果关键词不足，添加默认领域
    if len(unique_keywords) < 2:
        unique_keywords.extend(DEFAULT_SEARCH_FIELDS[:2])
    
    return unique_keywords[:max_keywords]


# ==================== PubMed API 调用函数 ====================

def search_pubmed(keywords: List[str], max_results: int = 3) -> List[Dict]:
    """
    调用PubMed API检索相关文献
    
    Args:
        keywords: 检索关键词列表
        max_results: 最大返回结果数
    
    Returns:
        List[Dict]: 文献列表 [{'title': ..., 'abstract': ..., 'pmid': ..., 'authors': ...}, ...]
    """
    if not keywords:
        return []
    
    try:
        # 构建检索词（AND连接）
        query = " AND ".join([f'"{kw}"[Title/Abstract]' for kw in keywords[:3]])
        
        # 如果检索词太具体可能返回空结果，也尝试OR连接
        query_or = " OR ".join([f'"{kw}"[Title/Abstract]' for kw in keywords[:3]])
        
        logger.info(f"🔍 [PubMed] 检索关键词: {keywords[:3]}")
        
        # Step 1: esearch 获取PMID列表
        esearch_params = {
            'db': 'pubmed',
            'term': query,
            'retmax': max_results,
            'retmode': 'json',
            'sort': 'relevance'
        }
        if PUBMED_API_KEY:
            esearch_params['api_key'] = PUBMED_API_KEY
        
        response = requests.get(PUBMED_ESEARCH, params=esearch_params, timeout=10)
        response.raise_for_status()
        
        search_results = response.json()
        pmid_list = search_results.get('esearchresult', {}).get('idlist', [])
        
        # 如果AND检索无结果，尝试OR检索
        if not pmid_list:
            esearch_params['term'] = query_or
            response = requests.get(PUBMED_ESEARCH, params=esearch_params, timeout=10)
            response.raise_for_status()
            search_results = response.json()
            pmid_list = search_results.get('esearchresult', {}).get('idlist', [])
        
        if not pmid_list:
            logger.info(f"🔍 [PubMed] 未找到相关文献")
            return []
        
        logger.info(f"🔍 [PubMed] 找到 {len(pmid_list)} 篇相关文献")
        
        time.sleep(REQUEST_INTERVAL)
        
        # Step 2: efetch 获取文献详情
        efetch_params = {
            'db': 'pubmed',
            'id': ','.join(pmid_list[:max_results]),
            'rettype': 'abstract',
            'retmode': 'xml'
        }
        if PUBMED_API_KEY:
            efetch_params['api_key'] = PUBMED_API_KEY
        
        response = requests.get(PUBMED_EFETCH, params=efetch_params, timeout=15)
        response.raise_for_status()
        
        # 解析XML响应
        articles = parse_pubmed_xml(response.text)
        
        return articles
        
    except requests.RequestException as e:
        logger.warning(f"⚠️ [PubMed] 请求失败: {e}")
        return []
    except Exception as e:
        logger.warning(f"⚠️ [PubMed] 解析失败: {e}")
        return []


def parse_pubmed_xml(xml_text: str) -> List[Dict]:
    """
    解析PubMed efetch XML响应
    
    Args:
        xml_text: XML格式的响应文本
    
    Returns:
        List[Dict]: 解析后的文献列表
    """
    articles = []
    
    try:
        # 简单的正则解析（避免引入xml库依赖）
        # 提取每篇文章
        article_pattern = r'<PubmedArticle>(.*?)</PubmedArticle>'
        article_matches = re.findall(article_pattern, xml_text, re.DOTALL)
        
        for article_xml in article_matches:
            article = {}
            
            # 提取PMID
            pmid_match = re.search(r'<PMID[^>]*>(\d+)</PMID>', article_xml)
            if pmid_match:
                article['pmid'] = pmid_match.group(1)
            
            # 提取标题
            title_match = re.search(r'<ArticleTitle>(.+?)</ArticleTitle>', article_xml, re.DOTALL)
            if title_match:
                article['title'] = clean_xml_text(title_match.group(1))
            
            # 提取摘要
            abstract_match = re.search(r'<AbstractText[^>]*>(.+?)</AbstractText>', article_xml, re.DOTALL)
            if abstract_match:
                article['abstract'] = clean_xml_text(abstract_match.group(1))[:500]  # 限制长度
            else:
                article['abstract'] = ''
            
            # 提取作者（第一作者）
            author_match = re.search(r'<Author[^>]*>.*?<LastName>(.+?)</LastName>', article_xml, re.DOTALL)
            if author_match:
                article['first_author'] = clean_xml_text(author_match.group(1))
            
            # 提取年份
            year_match = re.search(r'<PubDate>.*?<Year>(\d{4})</Year>', article_xml, re.DOTALL)
            if year_match:
                article['year'] = year_match.group(1)
            
            # 提取期刊
            journal_match = re.search(r'<Title>(.+?)</Title>', article_xml)
            if journal_match:
                article['journal'] = clean_xml_text(journal_match.group(1))[:50]
            
            if article.get('title'):
                articles.append(article)
        
    except Exception as e:
        logger.warning(f"XML解析异常: {e}")
    
    return articles


def clean_xml_text(text: str) -> str:
    """清理XML文本中的标签和特殊字符"""
    # 移除HTML/XML标签
    text = re.sub(r'<[^>]+>', '', text)
    # 移除多余空白
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# ==================== 知识上下文格式化 ====================

def format_knowledge_context(pubmed_results: List[Dict], purpose: str = 'diagnosis') -> str:
    """
    格式化检索结果为上下文文本
    
    Args:
        pubmed_results: PubMed检索结果列表
        purpose: 使用目的 ('diagnosis' | 'grading' | 'staging')
    
    Returns:
        str: 格式化的知识上下文文本
    """
    if not pubmed_results:
        return ""
    
    context_lines = ["【循证医学参考】"]
    context_lines.append(f"以下是基于PubMed检索的相关文献摘要，供诊断参考：")
    context_lines.append("")
    
    for idx, article in enumerate(pubmed_results, 1):
        title = article.get('title', '未知标题')
        abstract = article.get('abstract', '')
        pmid = article.get('pmid', '')
        year = article.get('year', '')
        first_author = article.get('first_author', '')
        
        context_lines.append(f"📄 文献{idx}: {title}")
        if first_author and year:
            context_lines.append(f"   作者: {first_author} et al. ({year})")
        if pmid:
            context_lines.append(f"   PMID: {pmid}")
        if abstract:
            context_lines.append(f"   摘要: {abstract[:300]}{'...' if len(abstract) > 300 else ''}")
        context_lines.append("")
    
    return "\n".join(context_lines)


# ==================== 高级检索功能 ====================

@lru_cache(maxsize=50)
def get_diagnostic_criteria(topic: str) -> str:
    """Do not inject bundled organ-specific criteria into a general task.

    Callers should use the user's explicit organ and question to perform a
    targeted literature search. Returning an empty string prevents a broad
    morphology term from silently selecting the wrong organ's guideline.
    """
    logger.info(
        "No bundled organ-specific diagnostic criteria for open-source query: %s",
        topic,
    )
    return ""


def search_grading_criteria(findings: str) -> str:
    """Compatibility hook; grading criteria require an explicit user task."""
    return ""


def get_knowledge_for_report(all_findings: str, max_articles: int = 2) -> str:
    """
    为最终报告获取知识上下文
    
    整合诊断标准和PubMed文献，为报告生成提供循证支持。
    
    Args:
        all_findings: 所有分析发现的汇总文本
        max_articles: 最大文献数量
    
    Returns:
        str: 完整的知识上下文文本
    """
    context_parts = []
    
    # 1. 获取诊断标准
    grading_criteria = search_grading_criteria(all_findings)
    if grading_criteria:
        context_parts.append(grading_criteria)
    
    # 2. 检索PubMed文献
    keywords = extract_diagnostic_keywords(all_findings)
    if keywords:
        pubmed_results = search_pubmed(keywords, max_results=max_articles)
        if pubmed_results:
            formatted = format_knowledge_context(pubmed_results)
            context_parts.append(formatted)
    
    if not context_parts:
        return ""
    
    return "\n\n".join(context_parts)
