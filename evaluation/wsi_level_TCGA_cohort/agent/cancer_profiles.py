# -*- coding: utf-8 -*-
"""Cancer-specific, model-visible prompt contracts for the TCGA cohort runner.

The pathology engine is shared. Only this registry supplies organ identity,
clinical questions, knowledge-search fallback wording, and scale-specific
morphology focus. Profile selection must happen before agent_loop is imported
by setting PATHCOPILOT_CANCER_PROFILE.
"""

from dataclasses import dataclass
from typing import Dict, Mapping
import os


@dataclass(frozen=True)
class CancerPromptProfile:
    key: str
    label_zh: str
    label_en: str
    organ_zh: str
    organ_en: str
    specimen_intro_zh: str
    specimen_intro_en: str
    questions_zh: str
    questions_en: str
    question_count: int
    knowledge_query_zh: str
    zoom_focus_zh: Mapping[int, str]
    zoom_focus_en: Mapping[int, str]

    def clinical_prompt(self, lang: str = "zh") -> str:
        intro = self.specimen_intro_en if lang == "en" else self.specimen_intro_zh
        questions = self.questions_en if lang == "en" else self.questions_zh
        return f"{intro}\n\n{questions}".strip()

    def zoom_focus(self, zoom_level: int, lang: str = "zh") -> str:
        mapping = self.zoom_focus_en if lang == "en" else self.zoom_focus_zh
        return mapping.get(
            int(zoom_level),
            (
                f"Focus on morphology visible at {zoom_level}×."
                if lang == "en"
                else f"重点描述当前 {zoom_level}× 视野内可见的形态。"
            ),
        )


_UCEC_QUESTIONS_ZH = """Q1：本张切片是否存在明确恶性肿瘤？
解释 / 作答要求：

请基于当前切片可见的形态学证据作答，并输出：
1. 结论：有 / 无 / 无法确定
2. 证据所在的 ROI / 区域
3. 支持结论的主要形态学依据
4. 主要鉴别或证据局限（如适用）
5. 置信度评分（0–4）

Q2：肿瘤的组织学分型是什么？
解释 / 作答要求：
请按 WHO 命名框架进行开放式判断，并提供明确的形态学依据。
若证据不足以唯一确定，请按以下结构输出：
1. 首选分型（primary type）
2. 鉴别分型（differential types）
3. 支持与反对证据
4. 建议补充的 IHC / 分子检测（用于鉴别）
5. 置信度评分（0–4）

Q3：肿瘤的组织学分级是什么？
解释 / 作答要求：
请根据 Q2 已判断的组织学分型，使用对应的分级体系进行判定（如适用）。
若该分型不适用 Grade 1/2/3，请明确说明"不适用"并给出原因。
请输出：
1. 分级结果：Grade 1 / Grade 2 / Grade 3 / 不适用 / 无法判断
2. 分级依据
3. 影响分级判断的不确定因素
4. 置信度评分（0–4）

Q4：肿瘤浸润深度如何？
解释 / 作答要求：
请尽量基于形态学评估以下内容（若切片条件允许）：
1. 是否存在肌层浸润：有 / 无 / 可疑 / 无法判断
2. 浸润深度表达方式：
   - 无肌层浸润：未见肌层浸润证据，深度记为不适用（0%）。
   - <50%：最深浸润点未达到肌层总厚度的一半。
   - ≥50%：最深浸润点达到或超过肌层总厚度的一半。
   - 有肌层浸润，深度无法可靠评估：可确认存在肌层浸润，但无法可靠评估其相对于肌层总厚度的比例。
   - 无法判断：当前切片不足以可靠判断是否存在肌层浸润。
仅在能够可靠识别内膜-肌层交界、肌层外侧界限及肌层总厚度时评估 <50% 或 ≥50%；否则请说明证据局限。
3. 判定依据
4. 局限性说明
5. 置信度评分（0–4）

Q5：是否存在脉管内癌栓（LVSI）？
解释 / 作答要求：
请严格依据形态学标准判断，不得将收缩伪影、组织裂隙或拖带伪影误判为 LVSI。
若当前倍率下怀疑存在 LVSI，应优先选择对该可疑区域进行下钻放大，以收集更充分的高倍镜证据后再做判定。
请输出：
1. LVSI 结论：有 / 无 / 可疑 / 无法判断
2. 形态学依据：
   - 是否见明确脉管腔结构
   - 是否见内皮衬里（或支持脉管结构的证据）
   - 肿瘤团与周围组织的关系（是否符合脉管内分布）
3. 鉴别要点（与收缩伪影、裂隙、组织拖带的区分）
4. 若为"可疑"，建议补充（如更高倍复核；必要时 IHC 辅助等）
5. 置信度评分（0–4）

Q6：若存在 LVSI，请统计明确脉管内癌栓的数量
解释 / 作答要求：
请输出：
1. 统计范围：当前 ROI / 当前切片全视野（必须注明）
2. 计数对象：仅统计"明确 LVSI"，不包含"可疑 LVSI"
3. 计数结果：
   - 明确数量（n = ）
   - 或区间估计（如 n≥3；当存在重叠/边界不清/视野不全时）
4. 若无法可靠计数，请说明原因
5. 置信度评分（0–4）

置信度定义：0=证据不足，无法判断 | 1=不确定，需更多信息 | 2=可能，存在鉴别诊断 | 3=较确定，特征较明确 | 4=确定，特征典型"""
_UCEC_QUESTIONS_EN = """Q1: Is definite malignancy present in this slide?
Instructions:
Answer based on the morphological evidence visible in the current slide and provide:
1. Conclusion: Yes / No / Indeterminate
2. ROI / region containing the evidence
3. Main morphological evidence supporting the conclusion
4. Main differential diagnoses or evidence limitations (if applicable)
5. Confidence score (0–4)

Q2: What is the histological type?
Instructions:
Use the WHO classification framework for open-ended assessment with clear morphological evidence.
If uncertain, provide:
1. Primary type
2. Differential types
3. Supporting and opposing evidence
4. Recommended IHC / molecular tests
5. Confidence score (0–4)

Q3: What is the histological grade?
Instructions:
Use the grading system appropriate for Q2 subtype. If Grade 1/2/3 does not apply, state "Not applicable" and why.
Provide:
1. Grade: Grade 1 / Grade 2 / Grade 3 / Not applicable / Indeterminate
2. Grading rationale
3. Factors of uncertainty affecting the grade
4. Confidence score (0–4)

Q4: What is the depth of invasion?
Instructions:
Based on morphological assessment (if slide conditions allow):
1. Myometrial invasion: Yes / No / Suspicious / Indeterminate
2. Depth category:
   - No myometrial invasion: no myometrial invasion is identified; depth is not applicable (0%).
   - <50%: the deepest point of invasion does not reach half of the total myometrial thickness.
   - ≥50%: the deepest point of invasion reaches or exceeds half of the total myometrial thickness.
   - Myometrial invasion present, depth cannot be reliably assessed: invasion is identified, but its proportion relative to the total myometrial thickness cannot be assessed reliably.
   - Indeterminate: the current slide is insufficient to determine reliably whether myometrial invasion is present.
Assign <50% or ≥50% only when the endomyometrial junction, outer myometrial boundary, and total myometrial thickness can be identified reliably; otherwise state the evidence limitation.
3. Basis
4. Limitations
5. Confidence score (0–4)

Q5: Is LVSI present?
Instructions:
Strictly apply morphological criteria. Do NOT misidentify retraction artifact, tissue clefts, or drag artifact as LVSI.
If LVSI is suspected at the current magnification, prioritize drilling down into the suspicious area to collect higher-power evidence before making a final determination.
Provide:
1. LVSI conclusion: Yes / No / Suspicious / Indeterminate
2. Morphological evidence:
   - Clear vascular lumen structure?
   - Endothelial lining (or other evidence supporting a vascular structure)?
   - Relationship of the tumor cluster to surrounding tissue (consistent with an intravascular location)?
3. Differential points (vs. retraction artifact, clefts, tissue drag)
4. If "Suspicious", recommend additional workup (e.g., higher-power review; IHC if needed)
5. Confidence score (0–4)

Q6: If LVSI present, count definite foci
Instructions:
Provide:
1. Scope: current ROI / full slide (must specify)
2. Count target: definite LVSI only, excluding suspicious
3. Count result: definite count (n=) or range estimate (n≥3 when overlap/unclear)
4. If unreliable, explain why
5. Confidence score (0–4)

Confidence scale: 0=Not assessable | 1=Uncertain, need more info | 2=Possible, differentials exist | 3=Probable, features fairly clear | 4=Definite, classic features"""

_LUAD_QUESTIONS_ZH = """Q1：这张切片中是否存在肿瘤性病变？
解释 / 作答要求：
请基于当前切片可见的形态学证据作答，并输出：
1. 结论：有 / 无 / 可疑 / 无法判断
2. 证据所在的 ROI / 区域
3. 支持结论的主要形态学依据
4. 主要鉴别或证据局限（如适用）
5. 置信度评分（0–4）

Q2：肿瘤的主要生长模式是什么？各生长模式占肿瘤的比例是多少？
解释 / 作答要求：
请按 WHO 肺腺癌组织学亚型评估：贴壁(lepidic)/腺泡(acinar)/乳头(papillary)/微乳头(micropapillary)/实性(solid)，以及其他特殊形态（如复杂腺体/筛状、黏液型等）。
1. 主要（predominant）生长模式
2. 各生长模式占比（合计约 100%，如：腺泡 60% + 实性 30% + 贴壁 10%）
3. 形态学依据（标注来源路径 R?-B?-G?）
4. 影响判断的不确定因素（取材局限、模式交错、倍率不足等）
5. 置信度评分（0–4）

Q3：IASLC 2020 分级是多少？
解释 / 作答要求：
仅当 Q2 支持浸润性非黏液性肺腺癌时，依据 IASLC 2020 分级系统，结合主要生长模式与高级别成分的合计占比进行判断：
- Grade 1：贴壁型为主，且高级别成分合计 <20%
- Grade 2：腺泡型或乳头型为主，且高级别成分合计 <20%
- Grade 3：不论主要生长模式，高级别成分合计 ≥20%

高级别成分包括实性、微乳头及复杂腺体模式（筛状或融合腺体）。
若组织学类型不适用该体系，或当前切片不足以可靠估计各成分比例，请报告“不适用”或“无法判断”，并说明局限。

Q4：是否存在贴壁生长以外的浸润性成分？
解释 / 作答要求：
请评估贴壁生长区域之外是否存在浸润性成分（腺泡/乳头/微乳头/实性、间质浸润、肌纤维母细胞性间质反应等）。贴壁生长模式本身不能单独确定 AIS 或 MIA；AIS / MIA / 浸润性腺癌的最终区分还需结合完整病灶范围、浸润灶大小及其他排除条件。
1. 结论：有 / 无 / 可疑 / 无法判断
2. 浸润性成分的类型与位置（标注来源路径）
3. 浸润灶大小（如可估计，最大浸润径 mm）
4. 支持与反对证据
5. 置信度评分（0–4）

Q5：是否有气腔播散（STAS）？
解释 / 作答要求：
请评估肿瘤主体边缘以外的肺泡腔内是否存在游离的肿瘤细胞巢、微乳头簇或单个肿瘤细胞，须与切片漂浮/刀割拖带等伪影鉴别。
1. STAS 结论：有 / 无 / 可疑 / 无法判断
2. 形态学依据（与肿瘤主体边缘的距离、肺泡腔内形态；标注来源路径）
3. 与伪影（组织漂移、边缘机械拖带）的鉴别
4. 置信度评分（0–4）

Q6：是否有胸膜侵犯？如果有，请做 PL 分级。
解释 / 作答要求：
请评估脏层胸膜受累程度（弹力纤维染色有助判断）：
- PL0：未突破脏层胸膜弹力层
- PL1：突破脏层胸膜弹力层，但未达脏层胸膜表面
- PL2：累及脏层胸膜表面
- PL3：累及壁层胸膜
请输出：
1. 胸膜侵犯结论：有 / 无 / 可疑 / 无法判断 / 切片不含胸膜
2. PL 分级：PL0 / PL1 / PL2 / PL3 / 无法判断
3. 形态学依据（胸膜位置、与弹力层的关系；标注来源路径）
4. 局限性（无弹力纤维特殊染色、切面未含胸膜等）
5. 置信度评分（0–4）

Q7：是否有 LVSI（脉管侵犯）？如果有，请做 LVSI 计数。
解释 / 作答要求：
请严格依据形态学标准判断脉管（血管/淋巴管）内癌栓，不得将收缩伪影、组织裂隙或拖带伪影误判为 LVSI。
若当前倍率下怀疑存在 LVSI，应优先对该可疑区域下钻放大，收集更充分的高倍镜证据后再做判定。
请输出：
1. LVSI 结论：有 / 无 / 可疑 / 无法判断
2. 形态学依据：
   - 是否见明确脉管腔结构
   - 是否见内皮衬里（或支持脉管结构的证据）
   - 腔内肿瘤团与周围组织的关系（是否符合脉管内分布）
3. LVSI 计数：明确灶数量（n = ）或区间估计（如 n≥3），并注明统计范围（当前 ROI / 当前切片全视野）
4. 与伪影鉴别 / 建议（必要时 IHC 辅助，或弹力纤维染色等）
5. 置信度评分（0–4）

置信度定义：0=证据不足，无法判断 | 1=不确定，需更多信息 | 2=可能，存在鉴别诊断 | 3=较确定，特征较明确 | 4=确定，特征典型"""
_LUAD_QUESTIONS_EN = """Q1: Is there a neoplastic lesion in this slide?
Instructions:
Answer based on the morphological evidence visible in the current slide and provide:
1. Conclusion: Yes / No / Suspicious / Indeterminate
2. ROI / region containing the evidence
3. Main morphological evidence supporting the conclusion
4. Main differential diagnoses or evidence limitations (if applicable)
5. Confidence score (0–4)

Q2: What is the predominant growth pattern, and what is the percentage of each pattern?
Instructions:
Assess WHO lung adenocarcinoma subtypes: lepidic / acinar / papillary / micropapillary / solid, plus special patterns (e.g., complex glands/cribriform, mucinous).
1. Predominant growth pattern
2. Percentage of each pattern (summing to ~100%, e.g., acinar 60% + solid 30% + lepidic 10%)
3. Morphological basis (cite source path R?-B?-G?)
4. Factors of uncertainty (sampling limitation, intermixed patterns, insufficient magnification)
5. Confidence score (0–4)

Q3: What is the IASLC 2020 grade?
Instructions:
Apply the IASLC 2020 grading system only when Q2 supports invasive non-mucinous lung adenocarcinoma, combining the predominant growth pattern with the combined proportion of high-grade components:
- Grade 1: lepidic predominant, with high-grade components totaling <20%
- Grade 2: acinar or papillary predominant, with high-grade components totaling <20%
- Grade 3: regardless of the predominant pattern, high-grade components totaling ≥20%

High-grade components include solid, micropapillary, and complex glandular patterns (cribriform or fused glands).
If the histologic type is not applicable to this system, or the current slide is insufficient to estimate component proportions reliably, report "Not applicable" or "Indeterminate" and state the limitation.

Q4: Is there an invasive component beyond lepidic growth?
Instructions:
Assess whether an invasive component beyond lepidic growth is present (acinar/papillary/micropapillary/solid, stromal invasion, or myofibroblastic stromal reaction). Lepidic growth alone does not establish AIS or MIA; final distinction among AIS, MIA, and invasive adenocarcinoma also requires evaluation of the complete lesion, invasive size, and applicable exclusion criteria.
1. Conclusion: Yes / No / Suspicious / Indeterminate
2. Type and location of invasive component (cite source path)
3. Size of invasive focus (if estimable, largest invasive dimension in mm)
4. Supporting and opposing evidence
5. Confidence score (0–4)

Q5: Is there spread through air spaces (STAS)?
Instructions:
Assess whether tumor cell nests, micropapillary clusters, or single tumor cells are present within alveolar spaces beyond the main tumor edge; distinguish from floating/knife-drag artifact.
1. STAS conclusion: Yes / No / Suspicious / Indeterminate
2. Morphological evidence (distance from main tumor edge, intra-alveolar morphology; cite source path)
3. Differentiation from artifact (tissue floaters, mechanical edge drag)
4. Confidence score (0–4)

Q6: Is there pleural invasion? If so, provide PL grade.
Instructions:
Assess visceral pleural involvement (elastic stain helps):
- PL0: does not breach the visceral pleural elastic layer
- PL1: breaches the elastic layer but not the pleural surface
- PL2: reaches the visceral pleural surface
- PL3: invades the parietal pleura
Provide:
1. Pleural invasion conclusion: Yes / No / Suspicious / Indeterminate / Slide lacks pleura
2. PL grade: PL0 / PL1 / PL2 / PL3 / Indeterminate
3. Morphological basis (pleural location, relationship to elastic layer; cite source path)
4. Limitations (no elastic special stain, section lacks pleura)
5. Confidence score (0–4)

Q7: Is LVSI present? If so, provide LVSI count.
Instructions:
Strictly apply morphological criteria for intravascular (vascular/lymphatic) tumor emboli. Do NOT misidentify retraction artifact, tissue clefts, or drag artifact as LVSI.
If LVSI is suspected at the current magnification, prioritize drilling down into the suspicious area to collect higher-power evidence before making a final determination.
Provide:
1. LVSI conclusion: Yes / No / Suspicious / Indeterminate
2. Morphological evidence:
   - Clear vascular lumen structure?
   - Endothelial lining (or other evidence supporting a vascular structure)?
   - Relationship of the intraluminal tumor cluster to surrounding tissue (consistent with an intravascular location)?
3. LVSI count: definite foci (n=) or range estimate (n≥3), specifying scope (current ROI / full slide)
4. Differentiation from artifact / recommendation (IHC if needed, or elastic stain)
5. Confidence score (0–4)

Confidence scale: 0=Not assessable | 1=Uncertain, need more info | 2=Possible, differentials exist | 3=Probable, features fairly clear | 4=Definite, classic features"""

_COAD_QUESTIONS_ZH = """Q1：这张切片中是否存在肿瘤性病变？
解释 / 作答要求：
请基于当前切片可见的形态学证据作答，并输出：
1. 结论：有 / 无 / 可疑 / 无法判断
2. 证据所在的 ROI / 区域
3. 支持结论的主要形态学依据
4. 主要鉴别或证据局限（如适用）
5. 置信度评分（0–4）

Q2：肿瘤的组织学类型是什么？
解释 / 作答要求：
请依据 WHO 2019 消化系统肿瘤分类评估组织学类型。可报告腺癌，非特殊型（NOS）、黏液腺癌、印戒细胞癌、髓样癌、锯齿状腺癌、微乳头状腺癌、腺鳞癌或其他特殊类型。
黏液腺癌要求含肿瘤细胞的细胞外黏液池占肿瘤面积 >50%；印戒细胞癌要求印戒细胞占肿瘤细胞 >50%。未达到相应阈值时，可描述特殊组织学成分及其估计占比，但不应将其单独诊断为该组织学亚型。
请输出：
1. 主要组织学类型
2. 特殊组织学成分及其估计占比（如适用；无法可靠估计时说明局限）
3. 形态学依据及证据所在的 ROI / 路径
4. 主要鉴别或证据局限（如适用）
5. 置信度评分（0–4）

Q3：肿瘤的组织学分级是什么？
解释 / 作答要求：
请依据 WHO 2019 对当前组织学类型采用适用的分级体系。对于腺癌 NOS 和黏液腺癌，采用以腺体形成程度为基础的两级制：
- 低级别：腺体形成 ≥50%
- 高级别：腺体形成 <50%
对黏液腺癌，还应同时考虑肿瘤上皮成熟度，不得仅凭腺体形成比例确定分级。
若当前组织学类型不适用该分级体系，请报告“不适用”。
请输出：
1. 分级结果：低级别 / 高级别 / 不适用 / 无法判断
2. 分级依据及证据所在的 ROI / 路径
3. 主要证据局限
4. 置信度评分（0–4）

Q4：当前切片可见的最深浸润层次是什么？其对应的最低 pT 类别是什么？
解释 / 作答要求：
请依据当前切片可见的肠壁层次和肿瘤最深浸润位置进行判断：
- pTis：黏膜内癌，累及固有层或黏膜肌，但未穿过黏膜肌进入黏膜下层
- pT1：侵入黏膜下层，但未累及固有肌层
- pT2：侵入固有肌层
- pT3：穿透固有肌层并进入结直肠周组织
- pT4a：穿透脏层腹膜并到达腹膜表面
- pT4b：显微镜下确认直接侵犯邻近器官或结构
请输出：
1. 当前切片所见的最深浸润层次及对应的最低 pT 类别：pTis / pT1 / pT2 / pT3 / pT4a / pT4b / 无法判断
2. 形态学依据及证据所在的 ROI / 路径
3. 完整肠壁、腹膜表面或邻近器官是否包含在当前切片中
4. 证据局限；若无法代表完整切除标本，不得将当前切片结论表述为患者最终 pT
5. 置信度评分（0–4）

Q5：是否存在脉管侵犯？请分别评估淋巴管/小血管侵犯与壁外静脉侵犯（EMVI）。
解释 / 作答要求：
请基于当前切片可见的形态学证据分别作答，并结合脉管结构、管壁或内皮支持证据以及肿瘤团的位置，与组织裂隙、收缩或拖带伪影等进行鉴别。
A. 淋巴管/小血管侵犯：
1. 结论：有 / 无 / 可疑 / 无法判断
2. 形态学依据及证据所在的 ROI / 路径
3. 明确灶数量或区间估计，并注明统计范围
4. 主要鉴别或证据局限
B. 壁外静脉侵犯（EMVI）：
1. 结论：有 / 无 / 可疑 / 无法判断
2. 形态学依据及证据所在的 ROI / 路径
3. 明确灶数量或区间估计，并注明统计范围
4. 主要鉴别或证据局限
C. 综合置信度评分（0–4）

Q6：是否存在神经侵犯（PNI）？
解释 / 作答要求：
请评估肿瘤是否围绕神经周径 ≥33%，或侵入神经鞘任一层或神经内，并结合当前切片可见证据与神经周围炎症、挤压或切面伪影进行鉴别。
请输出：
1. 结论：有 / 无 / 可疑 / 无法判断
2. 形态学依据及证据所在的 ROI / 路径
3. 主要鉴别或证据局限
4. 置信度评分（0–4）

Q7：是否存在癌结节（Tumor deposits）？
解释 / 作答要求：
请评估肠周脂肪或系膜软组织内与原发肿瘤不连续的癌性结节，并判断其中是否存在残余淋巴结、脉管壁或神经结构。请与原发肿瘤连续浸润、淋巴结转移、脉管侵犯及神经侵犯鉴别。
请输出：
1. 结论：有 / 无 / 可疑 / 无法判断 / 切片不含肠周脂肪或系膜软组织
2. 数量、位置及证据所在的 ROI / 路径
3. 支持癌结节判断的形态学依据
4. 主要鉴别或证据局限
5. 置信度评分（0–4）
仅凭当前切片不得将癌结节结论表述为患者最终 pN1c。
置信度定义：0=证据不足，无法判断 | 1=不确定，需更多信息 | 2=可能，存在鉴别诊断 | 3=较确定，特征较明确 | 4=确定，特征典型"""
_COAD_QUESTIONS_EN = """Q1: Is there a neoplastic lesion in this slide?
Instructions:
Based on morphological evidence visible on the current slide, provide:
1. Conclusion: Yes / No / Suspicious / Indeterminate
2. ROI / region of the evidence
3. Main morphological basis supporting the conclusion
4. Main differentials or evidence limitations (if applicable)
5. Confidence score (0–4)

Q2: What is the histologic type of the tumor?
Instructions:
Assess histologic type per WHO 2019 Classification of Digestive System Tumours. Report adenocarcinoma, NOS; mucinous adenocarcinoma; signet-ring cell carcinoma; medullary carcinoma; serrated adenocarcinoma; micropapillary adenocarcinoma; adenosquamous carcinoma; or other special types.
Mucinous adenocarcinoma requires extracellular mucin pools containing tumor cells occupying >50% of the tumor area; signet-ring cell carcinoma requires signet-ring cells occupying >50% of tumor cells. If the corresponding threshold is not met, special histologic components and their estimated percentages may be described, but they should not be diagnosed as that histologic subtype on their own.
Provide:
1. Predominant histologic type
2. Special histologic components and their estimated percentages (if applicable; state limitations when they cannot be estimated reliably)
3. Morphological basis and the ROI / path of the evidence
4. Main differentials or evidence limitations (if applicable)
5. Confidence score (0–4)

Q3: What is the histologic grade of the tumor?
Instructions:
Apply the grading system appropriate to the current histologic type per WHO 2019. For adenocarcinoma NOS and mucinous adenocarcinoma, use a two-tier system based on gland formation:
- Low grade: gland formation ≥50%
- High grade: gland formation <50%
For mucinous adenocarcinoma, epithelial maturation must also be considered; grade should not be assigned from gland formation alone.
If the current histologic type is not eligible for this grading system, report "not applicable".
Provide:
1. Grade: low grade / high grade / not applicable / indeterminate
2. Grading rationale and the ROI / path of the evidence
3. Main evidence limitations
4. Confidence score (0–4)

Q4: What is the deepest invasion depth visible on the current slide, and what is the corresponding minimum pT category?
Instructions:
Judge based on the bowel-wall layers visible on the current slide and the deepest location of tumor invasion:
- pTis: intramucosal carcinoma involving lamina propria or muscularis mucosae, but not through the muscularis mucosae into the submucosa
- pT1: invades submucosa, but not muscularis propria
- pT2: invades muscularis propria
- pT3: invades through muscularis propria into pericolorectal tissues
- pT4a: penetrates visceral peritoneum and reaches the peritoneal surface
- pT4b: direct invasion of adjacent organs or structures confirmed microscopically
Provide:
1. The deepest invasion layer seen on the current slide and the corresponding minimum pT category: pTis / pT1 / pT2 / pT3 / pT4a / pT4b / Indeterminate
2. Morphological basis and the ROI / path of the evidence
3. Whether full bowel wall, peritoneal surface, or adjacent organs are present on the current slide
4. Evidence limitations; if the slide cannot represent the complete resection specimen, do not state the current-slide conclusion as the patient's final pT
5. Confidence score (0–4)

Q5: Is vascular invasion present? Assess lymphatic/small-vessel invasion and extramural venous invasion (EMVI) separately.
Instructions:
Answer each item from morphological evidence visible on the current slide. Use vascular structure, vessel-wall or endothelial supporting evidence, and the location of tumor clusters, and differentiate from tissue clefts, retraction, or drag artifact.
A. Lymphatic / small-vessel invasion:
1. Conclusion: Yes / No / Suspicious / Indeterminate
2. Morphological basis and the ROI / path of the evidence
3. Definite count or range estimate, specifying the counting scope
4. Main differentials or evidence limitations
B. Extramural venous invasion (EMVI):
1. Conclusion: Yes / No / Suspicious / Indeterminate
2. Morphological basis and the ROI / path of the evidence
3. Definite count or range estimate, specifying the counting scope
4. Main differentials or evidence limitations
C. Overall confidence score (0–4)

Q6: Is perineural invasion (PNI) present?
Instructions:
Assess whether tumor encircles ≥33% of the nerve circumference, or invades any layer of the nerve sheath or the nerve proper; differentiate from peri-neural inflammation, crush, or sectioning artifact using evidence visible on the current slide.
Provide:
1. Conclusion: Yes / No / Suspicious / Indeterminate
2. Morphological basis and the ROI / path of the evidence
3. Main differentials or evidence limitations
4. Confidence score (0–4)

Q7: Are tumor deposits present?
Instructions:
Assess discontinuous carcinomatous nodules in pericolic fat or mesenteric soft tissue, and determine whether residual lymph node, vessel wall, or nerve structure is present. Differentiate from continuous extension of the primary tumor, lymph-node metastasis, vascular invasion, and perineural invasion.
Provide:
1. Conclusion: Yes / No / Suspicious / Indeterminate / Slide lacks pericolic fat or mesenteric soft tissue
2. Number, location, and the ROI / path of the evidence
3. Morphological basis supporting a tumor-deposit interpretation
4. Main differentials or evidence limitations
5. Confidence score (0–4)
Do not state a tumor-deposit finding on the current slide as the patient's final pN1c.
Confidence scale: 0=Not assessable | 1=Uncertain, need more info | 2=Possible, differentials exist | 3=Probable, features fairly clear | 4=Definite, classic features"""


_PROFILES: Dict[str, CancerPromptProfile] = {
    "ucec": CancerPromptProfile(
        key="ucec",
        label_zh="子宫",
        label_en="Uterus",
        organ_zh="子宫",
        organ_en="Uterus",
        specimen_intro_zh="标本：子宫（切除标本，H&E）。请仅依据本张切片可见形态回答下列问题。",
        specimen_intro_en=(
            "Specimen: uterus (resection specimen, H&E). Answer the following "
            "questions using only morphology visible on this slide."
        ),
        questions_zh=_UCEC_QUESTIONS_ZH,
        questions_en=_UCEC_QUESTIONS_EN,
        question_count=6,
        knowledge_query_zh="子宫肿瘤病理诊断、分型、分级、肌层浸润与脉管侵犯鉴别",
        zoom_focus_zh={
            10: "评估子宫内膜与肌层整体结构、病变范围、内膜-肌层交界和全层方向。",
            40: "评估腺体结构、代表性肿瘤结构比例、浸润模式、间质反应及脉管候选灶。",
            160: "核验细胞性质、核异型、局部结构性质及真假脉管内癌栓；不得外推全片比例。",
        },
        zoom_focus_en={
            10: "Assess global endometrial-myometrial architecture, lesion extent, the junction, and wall orientation.",
            40: "Assess glandular architecture, representative tumor proportions, invasion pattern, stroma, and vascular candidates.",
            160: "Verify cell identity, cytologic atypia, local architecture, and true versus artifactual LVSI; do not extrapolate whole-slide proportions.",
        },
    ),
    "luad": CancerPromptProfile(
        key="luad",
        label_zh="肺",
        label_en="Lung",
        organ_zh="肺",
        organ_en="Lung",
        specimen_intro_zh="标本：肺（切除标本，H&E）。请仅依据本张切片可见形态回答下列问题。",
        specimen_intro_en=(
            "Specimen: lung (resection specimen, H&E). Answer the following "
            "questions using only morphology visible on this slide."
        ),
        questions_zh=_LUAD_QUESTIONS_ZH,
        questions_en=_LUAD_QUESTIONS_EN,
        question_count=7,
        knowledge_query_zh="肺肿瘤病理诊断、肺腺癌生长模式、IASLC分级、STAS、胸膜与脉管侵犯鉴别",
        zoom_focus_zh={
            10: "评估肿瘤与肺实质交界、病变范围、贴壁与浸润区分布，以及胸膜位置关系。",
            40: "评估贴壁、腺泡、乳头、微乳头、实性及复杂腺体模式，间质浸润和 STAS 线索。",
            160: "核验细胞异型、局部生长模式、气腔内细胞簇是否为 STAS，以及真假脉管侵犯。",
        },
        zoom_focus_en={
            10: "Assess the tumor-lung interface, extent, distribution of lepidic versus invasive areas, and pleural relationship.",
            40: "Assess lepidic, acinar, papillary, micropapillary, solid, and complex-gland patterns, stromal invasion, and STAS clues.",
            160: "Verify cytology, local growth pattern, true STAS versus displacement artifact, and true versus artifactual vascular invasion.",
        },
    ),
    "coad": CancerPromptProfile(
        key="coad",
        label_zh="结直肠",
        label_en="Colorectum",
        organ_zh="结直肠",
        organ_en="Colorectum",
        specimen_intro_zh="标本：结直肠（切除标本，H&E）。请仅依据本张切片可见形态回答下列问题。",
        specimen_intro_en=(
            "Specimen: colorectum (resection specimen, H&E). Answer the following "
            "questions using only morphology visible on this slide."
        ),
        questions_zh=_COAD_QUESTIONS_ZH,
        questions_en=_COAD_QUESTIONS_EN,
        question_count=7,
        knowledge_query_zh="结直肠肿瘤病理诊断、组织学分型分级、浸润深度、脉管神经侵犯与癌结节鉴别",
        zoom_focus_zh={
            10: "评估肿瘤与肠壁交界、最深受累层次、浆膜面和肠周脂肪，并定位浸润前沿。",
            40: "评估组织学类型、腺体形成、间质浸润、LVSI/EMVI、PNI及癌结节线索。",
            160: "核验细胞异型、局部组织学类型及局部脉管/神经相关结构；癌结节的最终判断须结合 10×/40× 的空间关系及周围组织结构。",
        },
        zoom_focus_en={
            10: "Assess the tumor-bowel wall interface, deepest involved layer, serosa, pericolic fat, and invasive front.",
            40: "Assess histologic type, gland formation, stromal invasion, LVSI/EMVI, PNI, and deposit candidates.",
            160: "Verify cytology, local histologic type, and local vascular or nerve-associated structures; final classification of tumor deposits requires 10×/40× spatial relationships and surrounding tissue context.",
        },
    ),
}

_ALIASES = {
    "uterus": "ucec",
    "endometrium": "ucec",
    "tcga-ucec": "ucec",
    "lung": "luad",
    "tcga-luad": "luad",
    "colon": "coad",
    "colorectum": "coad",
    "tcga-coad": "coad",
}


def get_cancer_profile(key: str) -> CancerPromptProfile:
    normalized = (key or "").strip().lower()
    normalized = _ALIASES.get(normalized, normalized)
    if normalized not in _PROFILES:
        allowed = ", ".join(sorted(_PROFILES))
        raise ValueError(f"Unknown cancer profile {key!r}; expected one of: {allowed}")
    return _PROFILES[normalized]


def get_active_cancer_profile() -> CancerPromptProfile:
    return get_cancer_profile(os.environ.get("PATHCOPILOT_CANCER_PROFILE", "ucec"))


def available_cancer_profiles():
    return tuple(sorted(_PROFILES))
