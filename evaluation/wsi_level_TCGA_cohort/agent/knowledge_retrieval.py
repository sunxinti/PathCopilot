#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
知识检索模块（Knowledge Retrieval）

通过PubMed API检索相关医学文献，为病理诊断提供循证支持。
这是MCP Loop Plan阶段的知识检索功能实现。
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
# 不是 D 动作的主检索词表。正常路径由 Agent Plan 的「检索问题」原样交给
# PubMed / web-search MCP，不读本表，也不在此补子宫默认词。
# 仅 UCEC 在两条 MCP 都失败后回退到本地 WHO/FIGO 标准表（search_grading_criteria，不联网）。
# LUAD/COAD 不走这条兜底。

# 病理学术语中英对照（仅上述 UCEC 兜底抽词使用）
PATHOLOGY_TERM_MAPPING = {
    # 肿瘤类型
    '子宫内膜样腺癌': 'endometrioid adenocarcinoma',
    '浆液性癌': 'serous carcinoma',
    '透明细胞癌': 'clear cell carcinoma',
    '黏液性癌': 'mucinous carcinoma',
    '未分化癌': 'undifferentiated carcinoma',
    '癌肉瘤': 'carcinosarcoma',
    
    # 分级
    'G1': 'grade 1 well differentiated',
    'G2': 'grade 2 moderately differentiated',
    'G3': 'grade 3 poorly differentiated',
    '高分化': 'well differentiated',
    '中分化': 'moderately differentiated',
    '低分化': 'poorly differentiated',
    
    # 形态学特征
    '脉管内癌栓': 'lymphovascular space invasion LVSI',
    '核分裂象': 'mitotic figures',
    '背靠背腺体': 'back-to-back glands',
    '筛状结构': 'cribriform pattern',
    '乳头状结构': 'papillary pattern',
    '实性生长': 'solid growth pattern',
    '浸润性生长': 'invasive growth',
    '肌层浸润': 'myometrial invasion',
    
    # 其他
    '坏死': 'necrosis',
    '核异型性': 'nuclear atypia',
    '促纤维结缔组织增生': 'desmoplastic reaction',
}

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
        return []
    
    keywords = []
    # 检测中文关键词并转换为英文
    for chinese_term, english_term in PATHOLOGY_TERM_MAPPING.items():
        if chinese_term in analysis_text:
            keywords.append(english_term)
    
    # 检测英文关键词
    english_patterns = [
        r'endometrioid',
        r'serous',
        r'clear cell',
        r'LVSI|lymphovascular',
        r'myometrial invasion',
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



@lru_cache(maxsize=50)
def get_diagnostic_criteria(tumor_type: str) -> str:
    """
    获取特定肿瘤类型的诊断标准（带缓存）
    
    Args:
        tumor_type: 肿瘤类型（如 'endometrioid adenocarcinoma'）
    
    Returns:
        str: 诊断标准文本
    """
    # 诊断标准知识库（基于《中国妇科肿瘤临床实践指南 2025》引用 WHO 2020 / FIGO 2023）
    criteria_db = {
        'endometrioid adenocarcinoma': """
【子宫内膜样腺癌（Endometrioid adenocarcinoma）要点（指南2025引用WHO/FIGO）】
1. HE形态学诊断要点：
   - 以腺体性肿瘤为主；评估腺体结构与实性（非鳞化实性区）成分比例（用于分级）
   - 可出现"微囊性、伸长及碎片状浸润"（microcystic, elongated, fragmented；常作为MELF样描述）——指南在dMMR特征中提及
2. 鉴别诊断要点（与浆液性癌等高等级肿瘤）：
   - 低级别肿瘤若出现"不寻常的核异型性"，提示需评估p53状态以除外浆液性癌（指南明确提醒）
   - 高级别内膜样癌与浆液性癌在组织学/免疫表型可重叠，建议结合分子分型提高一致性（指南强调）
3. 分级（FIGO结构分级，适用于内膜样癌）：
   - G1：实性生长区 ≤5%
   - G2：实性生长区 6%–50%
   - G3：实性生长区 >50%
   - 结构为G1/G2但"核3级"显著时可上调1级；WHO 2014要求核3级区域>50%才升级
4. 核分级（1/2/3）：
   - 指南未明确提及核分级1/2/3的具体形态学判定标准（仅提到"核3级"用于分级上调的条件）
5. 分子分型对风险/分期/分级的影响（指南要点）：
   - 分子分型四类：POLEmut / dMMR(MSI-H) / NSMP / p53abn
   - FIGO 2023把分子分型用于修正Ⅰ–Ⅱ期分期：POLEmut→ⅠA_mPOLEmut；p53abn且伴肌层浸润→ⅡC_mp53abn
   - 风险分层：Ⅰ–Ⅱ期POLEmut按低危处理；p53abn若伴肌层浸润按高危处理
6. 关键IHC/分子检测（指南列出的"可选/推荐panel"）：
   - ER, PR, p53, MMR四项(MLH1/PMS2/MSH2/MSH6), p16, PTEN, PAX2, vimentin
   - 其他：Ki-67, PAX8, CK/CK7（必要时用于浸润/微转移/特殊浸润方式提示）
""",
        'serous carcinoma': """
【子宫内膜浆液性癌（Serous carcinoma）要点（指南2025引用WHO 2020）】
1. HE形态学诊断要点：
   - 常见于萎缩性内膜背景或内膜息肉中，呈特征性乳头状或腺样生长
   - 细胞核异型性弥漫且显著（高等级形态学特征）
2. 分级：
   - 浆液性癌属于高级别肿瘤；不按内膜样癌的FIGO G1/G2/G3结构分级体系分级
3. 鉴别诊断（与FIGO G3内膜样癌）：
   - 浆液性癌与G3内膜样癌可存在形态与免疫表型重叠，建议结合分子分型/关键IHC提高可重复性
   - 低级别肿瘤若出现"不寻常核异型性"应评估p53以除外浆液性癌
4. 关键IHC/分子检测（指南明确提及者）：
   - p53：浆液性癌"绝大多数存在p53突变"，IHC可见"p53突变型表达"
   - HER2：ERBB2(HER2)扩增约30%且可不均匀分布；晚期/复发建议按需做HER2检测
   - p16、WT1、ER/PR表达谱：指南未逐项明确（仅在"可选IHC列表"中出现p16/ER/PR）
""",
        'clear cell carcinoma': """
【子宫内膜透明细胞癌（Clear cell carcinoma）要点（指南2025引用WHO 2020）】
1. HE形态学诊断要点：
   - 细胞多形性：可为多角形/立方形/扁平或"鞋钉样（hobnail-like）"
   - 胞质透明或嗜酸性；结构多样：乳头状、管腺状和/或实性
2. 分级：
   - 透明细胞癌属于高级别肿瘤；不使用内膜样癌FIGO G1/G2/G3结构分级
3. 鉴别诊断：
   - 与分泌型内膜样癌、Arias–Stella反应的形态学鉴别：指南未展开具体要点
4. 关键IHC（指南提及的"可选检测panel"层面）：
   - Napsin A、HNF-1β（指南列入可选IHC，但未给出"阳性/阴性模式"）
   - ER/PR、p53的典型表达谱：指南未明确提及
""",
        'undifferentiated_dedifferentiated carcinoma': """
【子宫内膜未分化/去分化癌（Undifferentiated/Dedifferentiated carcinoma）要点（指南2025引用WHO 2020）】
1. HE形态学诊断要点：
   - 未分化癌：未分化上皮性恶性肿瘤；细胞大小一致，成片排列；无巢状或腺样结构；核分裂象多见
   - 去分化癌：由"分化较好的内膜样癌成分"与其下方/伴随的"未分化癌成分"共同构成
2. 分级：
   - 未分化/去分化癌属于高级别肿瘤；不按内膜样癌FIGO结构分级体系分级
3. 鉴别诊断：
   - 与高级别内膜样癌、癌肉瘤的具体形态学鉴别：指南未系统展开
4. 分子/生物学线索（指南明确提及）：
   - 约1/2–2/3去分化癌与约1/2未分化癌可见dMMR/MSI相关改变
   - 约2/3去分化癌与约1/2未分化癌与SWI–SNF蛋白复合体相关的突变失活有关
5. 关键IHC：
   - CK-/EMA-/PAX8/INI1/BRG1等具体"丢失/保留"判读：指南未明确提及（仅提示SWI–SNF相关改变与IHC/分子检测重要性）
""",
        'carcinosarcoma': """
【子宫癌肉瘤（Carcinosarcoma）要点（指南2025引用WHO 2020）】
1. 定义：
   - 双相性肿瘤：由"高级别癌成分 + 肉瘤成分"组成
2. 组织学要点：
   - 癌与肉瘤成分可有相似遗传学改变；目前公认肉瘤成分来源于上皮性癌的转化（EMT概念）
   - 癌肉瘤"被视为高级别上皮性癌，并据此进行治疗"
3. 分级：
   - 作为侵袭性/高级别肿瘤类型，不按内膜样癌FIGO结构分级体系分级
4. 鉴别诊断：
   - 与去分化癌的具体形态学鉴别：指南未系统展开
5. 关键IHC（上皮/间叶双相标记）：
   - CK/EMA/vimentin/desmin等"典型组合判读"：指南未明确提及（指南更多强调分类归属与分子背景）
""",
        'endometrial stromal sarcoma': """
【子宫内膜间质肉瘤（ESS：LGESS/HGESS）要点（指南2025·子宫肉瘤卷）】
1. LGESS（低级别ESS）— HE形态学要点：
   - 温和梭形细胞，类似增殖期子宫内膜间质
   - 特征性"舌状（finger-like/tongue-like）"浸润肌层；可伴或不伴LVSI
2. LGESS— 分子与IHC：
   - 融合：JAZF1–SUZ12最常见（>50%）；亦可见JAZF1–PHF1、EPC1–PHF1、MEAF6–PHF1等
   - IHC：CD10、ER、PR阳性；可用FISH/靶向RNA测序证实融合（缺乏融合也不能排除）
3. HGESS（高级别ESS）— HE形态学要点（两大谱系）：
   - YWHAE–NUTM2型：高级别圆形细胞成分 + 纤细分支状血管；核分裂象通常≥10/10HPF
   - BCOR相关（ZC3H7B–BCOR融合或BCOR-ITD）：高级别梭形细胞浸润于黏液样基质；舌状浸润与LVSI常见
4. HGESS— 关键IHC/分子：
   - 建议panel：CD10、ER、PR、cyclin D1（弥漫强阳性）及BCOR
   - 鉴别：BCOR型HGESS与黏液型LMS形态可相似；cyclin D1/BCOR/CD10 IHC + BCOR异常分子检测有助鉴别
5. 预后提示：
   - HGESS较LGESS更具侵袭性，淋巴结转移率略高，预后较差；分期是重要预后因素
""",
        'cervical adenocarcinoma': """
【宫颈腺癌（Cervical adenocarcinoma）要点（指南2025·宫颈癌卷 + 内膜癌卷鉴别段落）】
1. WHO 2020分类框架：
   - 腺癌分为：HPV相关（HPV-associated）与HPV非依赖（HPV-independent）
   - HPV非依赖亚型：胃型（gastric type）、透明细胞型、中肾管型、NOS等
2. HE形态学分类提示：
   - 可按形态学特征（腺腔缘核分裂象与凋亡小体）区分HPV相关与非依赖两大类
3. Silva浸润模式（HPV相关腺癌）：
   - 存在A/B/C三种具有临床意义的浸润模式；A型"无淋巴结转移或复发、预后好"
   - A/B/C各型的具体形态学判定细则：指南未明确提及
4. 与子宫内膜癌下段累及的鉴别（最重要，指南明确给出IHC方向）：
   - 子宫下段内膜样癌：p16"非块状阳性"、ER强阳性、HPV阴性
   - 宫颈原发HPV相关腺癌：p16"块状阳性"、ER阴性、HPV阳性
5. 关键检测：
   - 建议所有宫颈腺癌患者做HPV检测（HPV原位杂交或分子检测优先）；亦可用p16作为替代提示
""",
        'figo_staging_2023': """
【FIGO 2023 子宫内膜癌分期（指南2025原表 + 关键补充）】
1. I期：局限于子宫体和卵巢
   - IA：非侵袭性组织学类型/低级别内膜样癌：局限内膜或息肉（IA1）；浸润<50%且无/局灶LVSI（IA2）；低级别内膜样癌局限子宫+卵巢（IA3）
   - IB：非侵袭性组织学类型浸润≥50%且无/局灶LVSI
   - IC：侵袭性组织学类型局限于内膜息肉或内膜
2. II期：累及宫颈间质无宫外转移；广泛LVSI；侵袭性组织学类型伴肌层浸润
   - IIA：非侵袭性组织学类型累及宫颈间质
   - IIB：非侵袭性组织学类型伴广泛LVSI
   - IIC：侵袭性组织学类型伴肌层浸润
3. III期：任意组织学类型伴局部/区域病变
   - IIIA：累及浆膜/附件（IIIA1卵巢或输卵管，除外IA3；IIIA2浆膜下或浆膜）
   - IIIB：累及阴道和/或子宫旁（IIIB1）或盆腔腹膜（IIIB2）
   - IIIC：盆腔和/或腹主动脉旁淋巴结转移（IIIC1盆腔；IIIC2腹主动脉旁；各自分微转移i/宏转移ii）
4. IV期：累及膀胱/肠道黏膜或远处转移
   - IVA：膀胱和/或肠道黏膜；IVB：超出盆腔的腹腔腹膜转移；IVC：远处转移（含肾血管水平以上淋巴结、肺、肝、骨等）
5. "侵袭性 vs 非侵袭性组织学类型"定义：
   - 非侵袭性：低级别（G1/G2）内膜样癌
   - 侵袭性：G3内膜样癌、浆液性癌、透明细胞癌、未分化癌、混合癌、中肾管样腺癌、胃肠型黏液腺癌、癌肉瘤等
   - 分子分型未知的G3内膜样癌按侵袭性处理
6. 分子分型修正分期：
   - Ⅰ–Ⅱ期：若POLEmut→调整为ⅠA_mPOLEmut；若p53abn且伴肌层浸润→ⅡC_mp53abn
   - dMMR/NSMP不改变解剖学分期，但应记录（如ⅠmdMMR/ⅡmNSMP等）
7. 与FIGO 2009差异：
   - FIGO 2023在解剖学分期基础上融入病理与分子特征，引入"分子分期/下标m"
""",
        'molecular_classification': """
【子宫内膜癌分子分型（TCGA/WHO四分型与IHC替代）（指南2025）】
1. 四分型名称：
   - POLEmut（POLE ultra-mutated）
   - dMMR/MSI-H（错配修复缺陷/微卫星不稳定高）
   - p53abn（高拷贝数/copy number high）
   - NSMP（无特异性分子改变/低拷贝数 copy number low）
2. 判定流程（WHO分型示意）：
   - 先看POLE状态：致病突变→POLEmut
   - POLE野生/非致病→看MMR状态：缺陷→dMMR；正常→再看p53状态：突变/异常→p53abn；野生→NSMP
3. IHC/分子替代检测：
   - dMMR：用4种MMR蛋白IHC（MLH1/MSH2/MSH6/PMS2）；任一缺失提示MSI-H；MLH1/PMS2缺失需评估MLH1启动子甲基化
   - p53abn：用p53 IHC代替拷贝数状态识别
   - POLE：目前仍需测序（难以被简化方式取代）
4. ProMisE替代算法：
   - 先做MMR IHC确定dMMR组 → 再测序POLE → 再用p53 IHC分p53abn与NSMP
   - NCCN推荐可先POLE测序再做MMR IHC（检测次序不同）
5. 预后排序：
   - 预后良好：POLEmut
   - 预后中等：dMMR/MSI 与 NSMP
   - 预后差：p53abn
6. 对治疗决策影响：
   - 分子分型用于风险预后分层并指导辅助治疗强度选择（尤其POLEmut与p53abn影响更明显）
""",
        'who_2020_classification': """
【2020 WHO 子宫内膜癌病理学分类（基于《中国妇科肿瘤临床实践指南2025版》）】

1. 子宫内膜样癌
子宫内膜样癌完全由腺体细胞组成，通常表现为腺样或绒毛腺管状结构，伴有拥挤复杂的分支结构。子宫内膜样癌组织学分级沿用FIGO组织学分级，分为G1、G2和G3三级。依据FIGO分级标准，当腺上皮细胞呈现明显异型时，其分级可提高1级。子宫内膜样癌可伴有鳞状细胞分化或分泌性变化。WHO 2020分类中，将1级和2级统称为低级别癌，3级为高级别癌。

2. 子宫浆液性癌
表现为复杂的乳头和/或腺样结构，伴有弥漫而明显的核多形性。子宫浆液性癌多有p53突变，Ki-67指数较高。子宫浆液性癌可与子宫内膜样癌混合存在，浆液性成分超过10%时表现为浆液性癌生物学行为。子宫浆液性上皮内癌（serous endometrial intraepithelial carcinoma，SEIC）并非子宫浆液性癌的癌前病变，即使局限于子宫内膜亦可发生远处转移。目前倾向认为EmGD是浆液性癌的癌前病变。EmGD、SEIC与子宫浆液性癌有相似的分子遗传学改变，即常发生p53突变，推测三者为子宫浆液性癌逐渐进展的过程。子宫浆液性癌分化程度不再区分，其生物学和临床行为与卵巢高级别浆液性癌类似，常沿输卵管转移至腹膜。

3. 子宫透明细胞癌
较少见，多呈实性片状、腺管样、微囊状或乳头状排列，乳头通常短而圆，间质透明变。癌细胞呈柱状、多角形、鞋钉状或扁平状，细胞质透明或嗜酸性，细胞核多形性，呈现中度至重度异型性。恶性程度很高，多见于老年女性，多为晚期病变，预后很差。

4. 子宫内膜未分化癌和去分化癌
子宫内膜未分化癌约占2%，是一种未分化的上皮性恶性肿瘤，细胞大小一致，成片排列，无巢状或腺样结构，核分裂象多见。子宫内膜去分化癌由处于内膜表层分化较好的子宫内膜样癌和其下方的未分化癌组成，此类癌预后不良。

5. 癌肉瘤
表现为多形性上皮细胞与间叶分化区域混杂。癌性成分通常为浆液性癌或内膜样癌，也可为透明细胞癌和未分化癌，间质成分最常由无特殊分化的高级别肉瘤组成，但也可见具有异源性分化的肉瘤成分，如横纹肌肉瘤、软骨肉瘤和罕见的骨肉瘤。这种混合性肿瘤实际上是由上皮来源单细胞克隆去分化而来的，属于上皮性癌。当肉瘤成分多于50%时，称为以肉瘤为主的癌肉瘤。

6. 混合性腺癌
通常由2种或以上不同组织类型子宫内膜癌组成，其中至少有1种成分是透明细胞癌或浆液性癌，每种成分超过10%。最常见的是子宫内膜样癌和浆液性癌的混合。

7. 中肾管癌和中肾管样癌
中肾管癌罕见，中肾管样癌占子宫内膜癌的比例＜1%。中肾管样腺癌与中肾管腺癌为2种不同类型，两者起源不同，目前认为前者起源于Muller上皮，向中肾管方向分化；后者起源于中肾管残迹，一般发生于子宫颈，子宫内膜罕见。中肾管样癌组织学形态多样，典型者在腺腔内可见嗜酸性胶体样物质的小腺体和小管。免疫组化常表现为ER和PR阴性，P53表达呈野生型，GATA3弥漫表达，TTF-1核阳性，CD10呈腔面特征性阳性染色。

8. 胃肠型黏液性癌
罕见，主要由黏液分泌上皮形成的胃型或肠型腺体组成，可含有杯状细胞和丰富的嗜伊红或透明胞质，细胞核异型性可高可低，预后不良。

9. 鳞状细胞癌
占比＜0.5%，可能与慢性炎症、长期子宫积脓、既往放疗相关，但与HPV相关性不明（大多数学者认为不相关）；可能表现为尖锐湿疣样外观，因角化呈现白色切面；侵袭性强，预后不良。

除以上之外，子宫神经内分泌肿瘤是一组具有神经内分泌形态的肿瘤，分为高、低两种级别。低级别神经内分泌肿瘤类似于胃肠道的类癌；高级别神经内分泌肿瘤又分为小细胞和大细胞两种类型，均预后不良。
""",
        'lvsi_criteria': """
【LVSI评估与分层（指南2025引用WHO 2020；用于风险分层/分期记录）】
1. 记录原则：
   - 任何分期均应记录LVSI；风险分层与辅助治疗决策需结合LVSI
2. 灶性/局灶 vs 广泛LVSI：
   - 广泛LVSI（extensive LVSI）：WHO 2020定义 ≥5个脉管癌栓
   - 操作性表述：在至少1张HE切片上累及的脉管数目 ≥5个
   - 灶性/局灶LVSI的定量阈值：指南未明确提及（仅作为与"广泛LVSI"相对的分类项出现）
3. 预后/分层意义：
   - "广泛LVSI（≥5）"作为高中危/高危风险分层的重要条件之一（用于辅助治疗决策）
4. 真性 vs 假性LVSI（形态学判读细则）：
   - 指南未明确提及"真性/假性LVSI"的完整形态学鉴别标准（亦未给出收缩伪影/人工裂隙等细则）
5. MELF相关注意事项：
   - dMMR/MSI-H型常伴"微囊性、伸长及碎片状浸润"并可伴广泛LVSI；但未给出MELF背景下LVSI判读的专门细则
""",
    }

    return criteria_db.get(tumor_type.lower(), "")


def search_grading_criteria(findings: str) -> str:
    """
    根据发现检索相关的分级/诊断标准
    
    按优先级匹配：分期/分子分型 > LVSI > 具体肿瘤类型。
    每次调用只返回一个最匹配的条目。
    
    Args:
        findings: 病理发现文本
    
    Returns:
        str: 分级标准参考文本
    """
    if not findings:
        return ""
    findings_lower = findings.lower()

    # --- 直接匹配 criteria_db key（LLM 通过 MCP 工具调用传入精确 key）---
    if findings_lower in ('who_2020_classification',):
        return get_diagnostic_criteria('who_2020_classification')

    # --- 优先匹配：分期 / 分子分型 / LVSI（跨肿瘤类型的通用知识）---
    if 'figo 2023' in findings_lower or 'figo2023' in findings_lower \
       or '2023分期' in findings or '分子分期' in findings:
        return get_diagnostic_criteria('figo_staging_2023')

    if '分子分型' in findings or 'tcga' in findings_lower \
       or 'promise' in findings_lower or 'pole' in findings_lower \
       or 'dmmr' in findings_lower or 'nsmp' in findings_lower \
       or 'p53abn' in findings_lower:
        return get_diagnostic_criteria('molecular_classification')

    if 'lvsi' in findings_lower or '脉管' in findings or '淋巴血管' in findings \
       or '脉管瘤栓' in findings:
        return get_diagnostic_criteria('lvsi_criteria')

    # --- 具体肿瘤类型（按匹配优先级）---
    if '内膜样' in findings or 'endometrioid' in findings_lower \
       or '筛状' in findings:
        return get_diagnostic_criteria('endometrioid adenocarcinoma')

    if '浆液性' in findings or 'serous' in findings_lower \
       or 'seic' in findings_lower:
        return get_diagnostic_criteria('serous carcinoma')

    if '透明细胞' in findings or 'clear cell' in findings_lower \
       or '鞋钉' in findings or 'hobnail' in findings_lower:
        return get_diagnostic_criteria('clear cell carcinoma')

    if '去分化' in findings or '未分化' in findings \
       or 'dedifferentiated' in findings_lower or 'undifferentiated' in findings_lower:
        return get_diagnostic_criteria('undifferentiated_dedifferentiated carcinoma')

    if '癌肉瘤' in findings or 'carcinosarcoma' in findings_lower \
       or 'mmmt' in findings_lower or '双相' in findings:
        return get_diagnostic_criteria('carcinosarcoma')

    if '内膜间质肉瘤' in findings or '间质肉瘤' in findings \
       or 'lgess' in findings_lower or 'hgess' in findings_lower \
       or 'stromal sarcoma' in findings_lower:
        return get_diagnostic_criteria('endometrial stromal sarcoma')

    if '宫颈腺癌' in findings or 'cervical adenocarcinoma' in findings_lower \
       or 'hpv相关' in findings or '胃型' in findings \
       or 'silva' in findings_lower:
        return get_diagnostic_criteria('cervical adenocarcinoma')

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
