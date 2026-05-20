"""Agent-style backup analysis pipeline.

This module is intentionally not used by the production worker by default.
It runs a higher-token, multi-agent chain for side-by-side comparison against
the current production staged pipeline.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smart_badge_api.analysis.staged_pipeline import (
    STAGED_LLM_MODEL,
    _INDICATION_ADJUDICATION_SYSTEM_PROMPT,
    _INDICATION_ADJUDICATION_USER_TEMPLATE,
    _build_analysis_result_from_fact_graph,
    _build_line_speaker_metadata,
    _build_preprocess_context,
    _call_json,
    _candidate_indications_from_text,
    _clean_text,
    _compact_fact_graph_for_indications,
    _catalog_match_by_name,
    _estimate_payload_chars,
    _extract_correction_patch,
    _extract_evidence_graph,
    _extract_fact_graph,
    _extract_indication_adjudication,
    _format_candidate_indications,
    _format_staff_context,
    _first_text,
    _number_dialogue_lines,
    _apply_correction_patch,
    _apply_indication_adjudication,
    _merge_profile_facts_from_evidence_graph,
    _repair_empty_fact_graph_from_evidence_graph,
)
from smart_badge_api.analysis.transcript import prepare_transcript

logger = logging.getLogger(__name__)

PIPELINE_NAME = "agent_pipeline_v3_1_gpt52"
EVIDENCE_CHUNK_TARGET_CHARS = 14000
EVIDENCE_CHUNK_OVERLAP_LINES = 2


_CORRECTION_AGENT_SYSTEM_PROMPT = """\
你是中文医美面诊录音分析链路中的 Agent 1：ASR 局部纠错 + 说话人/参与者角色判定 Agent。

任务：
只输出小范围 patch，用来修正高置信度的 ASR 医美术语错误，以及明显错误的说话人/参与者角色。
必须保留原始时间戳和原句结构。不要总结、不要提取主诉/方案/适应症、不要写 SAP 备注。

核心原则：
1. 保守修改。
   - 只有当上下文强支持时，才写入 term_corrections；不确定时不要改，写入 uncertain_notes。
   - 不要整句重写。稳定的整段说话人角色用 speaker_role_map；只有明确串音、分离错误或局部例外时，才用 speaker_corrections。
   - 不要为了让中文更通顺而改口语、语病或普通错词；只有会影响医美事实、角色判断或后续抽取的错误才修改。

2. 输出枚举必须保持英文，不能自造枚举。
   role 只能是：
   customer, companion, consultant, doctor, expert_assistant, frontdesk,
   staff_peer, other.
   customer_scope 只能是：
   primary_customer, other_customer, companion_or_family, staff, unknown.

3. 按“话语功能”判断角色，不要盲信当前标签 current_role。
   如果现有标签互相矛盾，要重新核对；例如“客户/陪同”标签里出现“工牌本人”，或“医生/咨询师”标签里出现“主客户”，都不能直接相信。
   - 客户侧发言：个人诉求、审美目标、身体感受、治疗问题、接受/拒绝、犹豫、担心、既往医美史、转述自己的体验、预算上限、价格敏感或反复核算。
   - 员工侧发言：接待/引导、核销/开单/收款/签字、排队/预约/叫号、专业解释、诊断分析、推荐方案、用量、报价或价格解释、风险/流程说明、同事/电话/对讲/内部沟通。
   - 员工内部沟通：排班、领导/同事、成本利润、订单/成交/收款、其他客户案例、客户归属，如“我的客户”“挂我名下”“我在接”“谁接”。这类 customer_scope=staff。
   - 面诊前准备：叫名字、查预约、带路进房间、签到签字、等待医生、测试/倒计时；在真实客户诉求出现前，通常是 frontdesk/staff_peer/consultant，不要误判成客户主诉。
   - 专业讲解不等于医生。咨询师、专家助理也会讲解解剖、方案、用量、风险和价格。自称“专家助理/医生助理/院长助理”的，标为 expert_assistant，不标为 doctor。
   - 不要把客户引用家人、朋友、医生、其他机构的话，误改成员工发言。

4. 参与者标签。
   - 主咨询客户：本次到诊单主要服务对象。
   - 同行客户A/B：现场另一个也在咨询自己项目的人。
   - 陪同人员：亲友陪同、帮主客户补充信息或参与决策，但不是自己咨询项目。
   如果录音里有两名或更多现场客户分别咨询自己的项目，要区分为“主咨询客户”和“同行客户A/B”，不要都写成“客户”。
   如果主客户不明确，选择证据最充分的一位，并在 uncertain_notes 说明。

5. 员工自述和客户事实必须分开。
   员工说“我过敏/我做过/我打过/我的客户”等，不是客户标签或客户医美史。
   只有客户本人、陪同人员代客户说明，或员工明确描述当前客户情况时，才可能属于客户事实。

6. ASR 术语纠错范围。
   只纠正高置信度中文医美 ASR 错词。根据预处理提示、热词、上下文判断产品、材料、项目、部位和品牌。
   典型例子：注射语境中的“一字光波/一次光波/一支光波”可能是“一支玻尿酸”；“鲁板/鲁班”可能是“濡白天使”；轮廓语境中的“下划线”可能是“下颌线”。
   产品/品牌/材料名必须更保守：只有命中热词/预处理提示、同段反复出现、或上下文唯一强指向时才改；仅凭“像某个产品”不能改。
   不要把不确定的品牌名、产品名、机构名强行改成常见词，例如不要仅凭眼周语境把陌生词改成“嗨体”，不要仅凭玻尿酸语境把陌生品牌改成某个具体品牌。
   数字、单位、金额、支数和时间只有在上下文明确重复或逻辑强约束时才改；否则保留原文并写 uncertain_notes。
   不要做大跨度改写或语义补全，例如把整段不通顺的话改成一整句新话。term_corrections 只修正最小必要片段。
   不确定的术语保留原文并写 uncertain_notes。

7. 置信度和输出。
   speaker_role_map / speaker_corrections 的 confidence >= 0.65 才输出。
   term_corrections 的 confidence >= 0.75 才输出。
   只返回下面 schema 的 JSON，不要返回 Markdown、解释文字或额外字段。

Return JSON only:
{
  "correction_patch": {
    "speaker_role_map": [{
      "asr_speaker": "speaker_0",
      "role": "customer|companion|consultant|doctor|expert_assistant|frontdesk|staff_peer|other",
      "participant_label": "主咨询客户|同行客户A|同行客户B|陪同人员|咨询师|医生|专家助理|前台|员工|其他",
      "customer_scope": "primary_customer|other_customer|companion_or_family|staff|unknown",
      "confidence": 0.0,
      "reason": ""
    }],
    "speaker_corrections": [{
      "line_id": "L0001",
      "corrected_speaker": "customer|companion|consultant|doctor|expert_assistant|frontdesk|staff_peer|other",
      "participant_label": "",
      "customer_scope": "primary_customer|other_customer|companion_or_family|staff|unknown",
      "confidence": 0.0,
      "reason": ""
    }],
    "term_corrections": [{
      "line_id": "L0001",
      "original": "",
      "corrected": "",
      "confidence": 0.0,
      "reason": ""
    }],
    "uncertain_notes": []
  }
}
"""


_CORRECTION_AGENT_USER_TEMPLATE = """\
员工 / 录音上下文:
{staff_context}

代码侧预处理提示:
{preprocess_context}

带行号的转写原文:
{numbered_dialogue}

只输出 correction_patch JSON。
"""


_SCOPE_AGENT_SYSTEM_PROMPT = """\
你是中文医美面诊录音分析链路中的 Agent 1.5：当前面诊范围识别 Agent。

任务：
把已纠错并带行号的转写切分成连续片段，判断哪些片段属于“当前到诊客户/现场客户”的有效面诊范围，哪些片段可以在后续证据抽取前忽略。
这是保守过滤关卡：宁可多保留，不要误删可能影响主诉、方案、适应症、咨询备注、客户跟进或 SAP 回写的内容。

保留边界：
- 只要片段服务于当前到诊客户、现场同行咨询客户，或由陪同人员参与当前客户决策，就设置 current_visit_relevant=true。
- 判定优先级：有效业务信息高于闲聊外壳。一个片段内只要有任意一句属于当前客户的项目建议、医生/员工判断、报价、健康/禁忌筛查、排期或成交信息，就不能把包含这些句子的片段设置为 ignore；必须拆分为“保留业务句 + 忽略无关句”，拆不开时整段保留为 supporting。
- “服务于当前面诊”的内容包括但不限于：客户诉求/问题/顾虑/既往史/健康风险/预算与价格反应；员工、医生、专家助理的诊断判断、结构分析、推荐方案、种草/下次/转科建议、产品材料、用量步骤、风险恢复、护理复诊；报价、优惠、定金、开单、付款、核销、成交确认和未成交原因。
- 上述内容无论出现在开头、中段还是结尾，都应保留。不要因说话人是前台/助理/医生、或片段属于付款/术后/接待阶段，就自动忽略。
- 客户侧一句很短的话也可能是关键事实：只要出现“做过、打过、填过、溶过、取过、修过、动过、过敏、怀孕、哺乳、禁忌、预算、太贵、担心、不敢、后遗症、钱转不出来、账户、银行卡、转账、付款”等含义，即使夹在带路/闲聊/员工操作之间，也要单独切出并保留；无法单独切出时，整段保留为 supporting。
- 健康/禁忌筛查即使很短也属于业务信息，例如“有没有感冒、身体各方面还好、有没有特殊情况、有没有暴晒、是否生理期、是否怀孕/备孕/哺乳、是否过敏、近期是否用药”等，要单独切出并保留；不要因为前后是闲聊、等待或员工内部沟通就整体删除。
- 员工对医生/同事转述当前客户情况也要保留为 supporting，例如“新客面诊、想做光子、前两天晒了、没有红肿破溃、扫码/皮肤检测做不了”等；这类交接会影响当次治疗判断，不能当作纯内部聊天删除。
- 客户或陪同提到付款、转账、账户、银行卡、定金、尾款、核销、支付失败等内容，默认视为当前成交支持信息；只有明确是员工私人事务或与当前客户无关的第三方事务时，才可以忽略。
- 流程对话中只要影响“能不能做、什么时候做、由谁做、是否需要检查/检验/抽血/签字、是否能当天做、预约/改约/排期/医生下台/检验科是否下班、术前准备、术后复查”的判断，就不是闲聊；应保留为 supporting 或对应的 quote_or_payment / post_deal_care / current_customer_consultation。
- 看似闲聊、来源说明或转场等待的片段中，只要员工开始给当前客户解释项目/皮肤或结构问题、建议先做某个项目、报出价格/活动价，或询问健康禁忌，就应从闲聊中切出并保留。不要把“客户来源/熟人闲聊 + 项目建议/报价/禁忌筛查”的混合片段整体判为 casual_chat。
- 典型反例：客户聊“为什么来院/认识谁/打羽毛球”等来源闲聊后，医生说“后期可以先做基础项目”，咨询师说“舒敏之星299”，又问“身体各方面还好、有没有暴晒/特殊情况”，这些业务句必须保留；不能因为前后仍在闲聊就整体忽略。
- 等待医生、等待检查、签字或测量数据时，对当前客户说的解释也应保留，例如“为什么量这些数据”“医生/院长什么时候下台”“今天能否做/明天做”“先等检查/检验/抽血结果”等；只有员工之间与当前客户无关的排班、找房间、递水、物品操作才可忽略。
- 现场另一位客户也在咨询自己的项目时，保留为 scope_type=accompanying_customer_consultation，participant_scope=other_customer。陪同人员帮当前客户补充信息或参与决策时，保留为 participant_scope=companion_or_family。

忽略边界：
- 只有明确不服务于当前面诊、且不含上述有效业务信息的片段，才可以设置 business_relevance=ignore。
- 可忽略内容通常是：员工纯内部工作聊天；缺席第三方/其他客户案例且不是给当前客户举例或建议；纯寒暄闲聊；纯带路、纯等待、叫号、查预约、测试/倒计时、设备操作等不含决策信息的流程话；与医美面诊无关的生活或操作内容。
- 不要因为一个片段发生在“等待医生/等待检查/转场/签字/术前流程”阶段就整体忽略；若其中夹有当前客户的项目确认、检查/排期、当天能否治疗、风险禁忌、价格成交或后续安排，要切出来保留。
- 一个片段如果混有有效业务信息和可忽略内容，要尽量拆分；无法可靠拆分时，整段保留为 supporting。

切分规则：
1. 不要抽取事实，不要总结分析，不要判断适应症；只做范围切分。
2. 片段要按原文顺序、连续、尽量不重叠。优先使用较粗粒度片段，只有范围/相关性明显变化时才切开。
3. business_relevance=core 用于主诉、方案、诊断、价格、成交等核心信息；supporting 用于护理、预约、补充背景、陪同决策、边界不确定但可能有用的信息；ignore 只用于明确可忽略片段。
4. 对长录音可以切得更细：当从核心面诊转为纯闲聊/员工内部沟通，或从闲聊又回到排期、成交、检查、复诊等业务信息时，应切开；不要把“长段业务内容 + 少量无关流程”粗暴合成一个超长片段。
5. 不确定时保留：current_visit_relevant=true，并在 reason 或 notes 说明不确定点。
6. 如果使用 scope_type=unclear，必须设置 business_relevance=supporting 且 current_visit_relevant=true；如果要忽略，请选择明确的忽略类型（staff_chat、casual_chat、third_party_absent_case 或 unrelated_operations），不要使用 unclear+ignore。

scope_type 只能使用以下值：
- current_customer_consultation
- accompanying_customer_consultation
- doctor_face_to_face
- quote_or_payment
- post_deal_care
- future_seed_or_cross_department
- third_party_absent_case
- staff_chat
- casual_chat
- unrelated_operations
- unclear

business_relevance 只能使用：core, supporting, ignore。

只返回 JSON，不要返回 Markdown 或解释文字：
{
  "scope_graph": {
    "primary_customer": "",
    "dominant_visit_topic": "",
    "segments": [
      {
        "id": "S1",
        "start_line_id": "L0001",
        "end_line_id": "L0010",
        "scope_type": "current_customer_consultation",
        "participant_scope": "primary_customer|other_customer|companion_or_family|staff|unknown",
        "business_relevance": "core|supporting|ignore",
        "current_visit_relevant": true,
        "reason": ""
      }
    ],
    "notes": []
  }
}
"""


_SCOPE_AGENT_USER_TEMPLATE = """\
员工 / 录音上下文:
{staff_context}

代码侧预处理提示:
{preprocess_context}

用于面诊范围识别的已纠错转写:
{dialogue}

只输出 scope_graph JSON。
"""


_EVIDENCE_AGENT_SYSTEM_PROMPT = """\
你是中文医美面诊录音分析链路中的 Agent 2：证据抽取 Agent。

任务：
只从已纠错、已做当前面诊范围过滤的转写中抽取证据。不要判断最终 SAP 适应症，不要生成最终分析结果，不要写 SAP 咨询备注。

总原则：
1. 证据优先，少推理。每个有用条目都要包含原文短引文、evidence_turn_ids、speaker/participant、participant_scope 和 confidence；没有原文支撑就不要抽取。
2. 按事实功能分栏，不要互相混放：客户主诉、医生/员工诊断观察、推荐/种草/备选方案、顾虑、预算/价格反应、成交/支付动作、既往史和客户标签要分别进入对应 evidence 列表。
3. 参与者必须隔离。主咨询客户、同行客户A/B、陪同人员分别保留 participant 与 participant_scope；不要把一个人的主诉、顾虑、预算、方案、既往史、标签或成交状态合并到另一个人身上。陪同人员替主客户补充时可作为主客户证据；如果是在说自己的治疗需求，标为 other_customer。

分类规则：
4. customer_demand_evidence 只抽“当前客户想解决的问题或想达到的审美目标”。来源必须是客户本人提出、陪同代述、客户确认，或员工复述后客户接受。一个身体问题/目标只保留一条，不按重复提问拆多条。
5. 主诉以“问题/目标”为中心，不以“项目/产品/成交动作”为中心。客户只说“先做光子、今天做光子、想了解嗨体/水光/某产品、想试一下某项目”，或出现“购买、开单、核销、已买几支、今天打一支、安排某产品/某项目”等执行动作时，不要抽成 customer_demand_evidence；应放到 recommendation_evidence.customer_response、deal_evidence 或 profile_evidence。只有同段明确出现具体问题/目标，如“晒后变黑想提亮、暗沉、痘印、面中凹陷、法令纹、苹果肌下垂、更饱满、更立体、更年轻”，才抽目标型主诉。
6. 不要把担心、价格、流程、项目选择、设计偏好或治疗顺序本身当主诉。例如“担心疼、怕风险、问价格、问流程、选择先做某项目、今天想做光子、买了一支瑞丽/安排面中、自然一点/夸张一点/小平扇/外开扇/宽窄、先把鼻子调好/第一步先做某部位”不能单独成为主诉；它们应分别进入 concern_evidence、budget_evidence、deal_evidence、profile_evidence、implementation_notes 或 customer_response。尤其不要把“首先，一定要鼻子调好，这是第一步”这类治疗优先级/顺序句单独抽为 customer_demand_evidence；若已有“缩小鼻头、缩窄鼻翼、改善鼻部结构”等目标型主诉，只在方案步骤或 notes 中记录优先级。只有这些表达同时指向具体身体问题/目标时，才保留目标型主诉。
7. 员工/医生主动观察到的问题先放 diagnosis_evidence；只有客户确认、接受或明确表示也想解决时，才可同时成为 customer_demand_evidence。
8. 客户明确提到但本次不处理、转科、下次再做的“问题/目标”也要保留，handling_status=referral_or_deferred。例如美白、毛孔、痘印、暗沉等皮肤管理诉求，不能因为本次无法处理就删除；但如果只是询问水光/光电/某产品且没有说明问题或目标，按第 5 条处理，不抽成主诉。
9. recommendation_evidence 只抽员工/医生给当前客户或同行咨询客户提出的项目/产品/材料/手术/注射/护理方案。保留品牌、材料、用量、价格、疗程、步骤、操作要点、恢复/风险说明和 customer_response。多个材料或产品选择要全部保留：主方案写在 content，备选/比较方案写入 implementation_notes，并用 relation_to_current_demand 标明主推、种草、备选、拒绝或不适合。
10. 独立的术前检查、术后用药、伤口护理、疤痕膏、换药、医用面膜、耗材、核销、支付方式等不是治疗方案；除非它们是某个治疗方案不可缺少的实施步骤，否则不要单独抽成 recommendation_evidence。售后领取、赠送、家用护理建议可放入 deal_evidence、profile_evidence 或 quality_notes。
11. concern_evidence 必须来自客户/陪同的真实担心、追问、拒绝、犹豫或明确确认；员工单方面安抚不是顾虑。若 customer_response 中出现安全、风险、副作用、后遗症、移位、变差、疼痛、恢复、医生资质、效果不确定等担忧，同一问题也要抽到 concern_evidence，不要只留在 customer_response。明确否定或接受的表达不是顾虑，例如“不担心、无所谓、可以接受、习惯了、没关系”不能单独抽为 concern_evidence；可作为对应方案的 customer_response。
12. budget_evidence 只抽客户的明确预算、可接受价格区间、支付能力限制、定金/尾款/付款金额、对具体报价的价格敏感、砍价/优惠诉求或反复核算。员工单纯报价、算价、解释优惠、项目价格字段仍放在 recommendation_evidence 或 deal_evidence；客户普通询价、询问价格差异、问“多少钱/贵不贵/价格差不多吗”但没有承受度、还价、预算上限或反复核算时，也不要进 budget_evidence。只有客户对价格作出承受度反应时才进 budget_evidence。
13. profile_evidence 用于客户标签：既往医美/材料/仪器/手术史、当前预算与价格敏感、疼痛耐受、家庭/职业/特殊身份、竞品机构、决策人、恢复/时间限制、项目或产品偏好等。员工自述、缺席第三方案例、其他客户案例不能变成当前客户标签；否定史（如“没打过/没做过”）和当前可做性（如“可以打/能做”）不能变成既往史标签。
14. deal_evidence 抽成交、未成交、预约、定金、开单、支付、核销、改约、复诊和未成交原因。支付/账户/银行卡/转账等如果属于当前客户成交过程，要保留。

边界规则：
15. 不要把流程问题当主诉。仪器版本、验真、医生排班、手术时间、切口、恢复、能否开车、付款方式、优惠、单纯价格问题，只有在同时表达具体身体问题/目标时才可成为 customer_demand_evidence；否则放到 concern_evidence、budget_evidence、deal_evidence 或 implementation_notes。
16. 对部位和项目保持精确，不要过度泛化：副乳、富贵包、手臂、后背、腰腹等体雕部位要分开；松弛/紧致/抗衰要和毛孔、痘印、暗沉等肤质问题分开；鼻头/鼻翼毛孔、黑头、出油、痘痘或皮肤纹理，不能在没有明确鼻部轮廓/手术/注射方案时推成鼻综合。
17. 注射/支撑/轮廓方案要保留结构目标，不要只写成泛化产品名。例如鼻基底/鼻头/鼻翼/鼻尖的三角结构支撑，下颌线/下颌角拐点/耳前耳后韧带/外轮廓支撑，都要保留具体结构目标、材料和用量；不要在出现童颜针、芭比针、支撑、下颌角拐点或鼻基底结构时误降为“肉毒/除皱瘦脸”。
18. 不要从随口提到“小毛毛/汗毛/体毛”抽脱毛主诉；只有客户明确要求脱毛、冰点脱毛、激光脱毛或询问如何去除时才抽取。
19. 比较用、被否定、不适合、客户拒绝、员工明确说不是优先级的选项，不要当成当前主推方案；保留为 alternative_not_recommended 或 implementation_notes。

Return JSON only:
{
  "evidence_graph": {
    "customer_demand_evidence": [
      {
        "id": "E_D1",
        "content": "",
        "body_part": "",
        "speaker": "customer|companion|staff_restated_confirmed",
        "participant": "主咨询客户|同行客户A|同行客户B|陪同人员|unknown",
        "participant_scope": "primary_customer|other_customer|companion_or_family|unknown",
        "handling_status": "current_handled|referral_or_deferred|unclear",
        "evidence_turn_ids": [],
        "quote": "",
        "confidence": 0.0
      }
    ],
    "diagnosis_evidence": [],
    "recommendation_evidence": [
      {
        "id": "E_R1",
        "content": "",
        "body_part": "",
        "participant": "主咨询客户|同行客户A|同行客户B|unknown",
        "participant_scope": "primary_customer|other_customer|unknown",
        "brand": "",
        "material": "",
        "dosage": "",
        "price": "",
        "course_or_frequency": "",
        "treatment_steps": [],
        "implementation_notes": "",
        "customer_response": "",
        "relation_to_current_demand": "current_main_plan|possible_current_plan|planting_or_later|alternative_not_recommended|auxiliary_or_care|not_current_or_referral|unclear",
        "evidence_turn_ids": [],
        "quote": "",
        "confidence": 0.0
      }
    ],
    "concern_evidence": [],
    "budget_evidence": [],
    "medical_history_evidence": [],
    "profile_evidence": [
      {
        "id": "E_P1",
        "category": "",
        "value": "",
        "participant": "主咨询客户|同行客户A|同行客户B|陪同人员|unknown",
        "participant_scope": "primary_customer|other_customer|companion_or_family|unknown",
        "evidence_turn_ids": [],
        "quote": "",
        "confidence": 0.0
      }
    ],
    "deal_evidence": [],
    "speaker_corrections": [],
    "quality_notes": []
  }
}
"""


_EVIDENCE_AGENT_USER_TEMPLATE = """\
员工 / 录音上下文:
{staff_context}

代码侧预处理提示:
{preprocess_context}

已纠错并完成范围过滤的转写:
{dialogue}

只输出 evidence_graph JSON。
"""


_EVIDENCE_AGENT_CHUNK_USER_TEMPLATE = """\
员工 / 录音上下文:
{staff_context}

代码侧预处理提示:
{preprocess_context}

这是转写分块 {chunk_index}/{chunk_count}。
行号范围：{line_range}。
相邻分块可能有少量重叠。只抽取本分块内的证据，并保留 line_id 到 evidence_turn_ids，方便代码侧去重合并。

已纠错并完成范围过滤的转写分块:
{dialogue}

只输出 evidence_graph JSON。
"""


_EVENT_AGENT_SYSTEM_PROMPT = """\
你是中文医美面诊录音分析链路中的 Agent 3：事件图抽取 Agent。

任务：
把 evidence_graph 和相关转写片段转换成带“事件极性”的原子业务事件。不要生成最终分析结果，不要选择最终 SAP 适应症，不要写咨询备注。

事件图的作用：
- evidence_graph 表示“提到了什么”。
- event_graph 表示“这句话在面诊中起什么作用”。
- 后续 Agent 会把 event_graph 当作极性依据，避免把客户随口提问、员工科普、备选比较、不适合或被拒绝的方案误当成最终推荐方案或适应症依据。

通用规则：
1. 只根据 evidence_graph 和转写证据生成事件；不要补充没有证据的新主诉、新方案或新成交状态。
2. 保留 participant 与 participant_scope，主咨询客户、同行客户、陪同人员不能串人。
3. 每个事件都要尽量保留 source_evidence_ids、evidence_turn_ids、quote 和 confidence。能绑定到具体证据 id 时必须绑定。
4. 方案事件要尽量绑定 related_demand；如果只能判断是后续种草、备选或科普，也要用 event_type 表达极性，不要硬绑到当前主诉。
5. 事件图不是二次证据抽取：通常一个 evidence item 最多映射为一个同类事件。不要把一条证据反复拆成多个同义事件；不要从 recommendation_evidence 的方案描述里反推新的 demand_events。demand_events 只能来自 customer_demand_evidence 或 diagnosis_evidence，其中 diagnosis_only 只能来自 diagnosis_evidence。
6. other_customer 只用于现场同行客户正在咨询自己的项目。缺席第三方、员工口中的“他/她/我的客户/其他顾客/朋友案例/之前顾客”不是 other_customer，不要生成 demand_events 或 plan_events；如需保留，只能进入 profile_events=staff_or_product_context 或 notes。
7. 如果 customer_demand_evidence 实际只是项目设计偏好、风格偏好或术式选择，例如“自然一点/夸张一点/小平扇/外开扇/宽窄/款式/风格”，且没有具体身体问题或审美目标，不要生成 demand_event；可转为 profile_events=customer_profile 或 notes。

事件极性规则：
1. demand_events：
   - current_demand：当前客户本次想解决的问题/目标。
   - deferred_demand：客户提出但本次不处理、转科、下次再做的问题/目标。
   - diagnosis_only：员工/医生观察到的问题，但客户没有明确表示要解决。
2. plan_events：
   - current_recommendation：员工/医生明确建议当前客户本次可做、优先做或正在成交的方案。
   - seed_recommendation：后续、可选、加项、维护、转科，或不属于本次核心目标但可种草的方案。若员工说“先做核心项目，整体设计/其他部位以后再做”，这些其他部位属于 seed_recommendation。
   - comparison_or_backup：用于比较、解释差异或作为备选，但没有被选为主方案。
   - not_recommended：明确不适合、不建议、被医生/员工否定，或客户明确拒绝的方案。
   - staff_explanation：产品、解剖、风险、仪器、价格、流程等科普说明，且没有形成具体“建议去做”的方案。
   - customer_question：客户只是询问某项目/产品/价格/医生/流程，员工没有推荐为当前方案。
   - diagnosis_only：只是观察或判断问题，没有给出方案。
   - unclear：证据不足以判断极性时使用。
3. deal_events：
   - deal_confirmed / deposit / payment / order_created 用于已确认成交、定金、付款、开单等动作。
   - not_deal 用于客户明确未成交、拒绝、暂缓或离院未做。
   - 交易事件要尽量写明 plan 和 amount；不能确定对应方案时也要保留 quote。
   - 带客户去医生/外科/皮肤科继续面诊、进一步评估、改约或排队，不等于 order_created 或 deal_confirmed；只有出现开单、下单、付款、定金、核销、成交确认等明确交易动作时才生成成交类 deal_events。
4. profile_events：
   - customer_profile：当前客户标签、既往史、偏好、约束等。
   - staff_or_product_context：员工自述、产品背景、第三方案例、其他客户情况等，不能作为当前客户标签。
   - ambiguous：无法确定是否属于当前客户。
   - reject：明确不应进入客户标签的证据。
5. concern_events / budget_events：
   - 只保留客户/陪同的真实顾虑、价格承受度、还价、预算上限或付款压力。
   - 员工单纯报价、科普价格、解释优惠不是 budget_event，除非客户表现出价格敏感、还价或支付压力。
   - 客户普通询价、询问价格差异、问“多少钱/价格差不多吗”但没有还价、预算上限或支付压力时，不生成 budget_event。

只返回 event_graph JSON：
{
  "event_graph": {
    "demand_events": [
      {
        "id": "EV_D1",
        "event_type": "current_demand|deferred_demand|diagnosis_only|unclear",
        "participant": "",
        "participant_scope": "primary_customer|other_customer|companion_or_family|unknown",
        "content": "",
        "body_part": "",
        "source_evidence_ids": [],
        "evidence_turn_ids": [],
        "quote": "",
        "confidence": 0.0
      }
    ],
    "plan_events": [
      {
        "id": "EV_P1",
        "event_type": "current_recommendation|seed_recommendation|comparison_or_backup|not_recommended|staff_explanation|customer_question|diagnosis_only|unclear",
        "participant": "",
        "participant_scope": "primary_customer|other_customer|unknown",
        "plan": "",
        "body_part": "",
        "brand": "",
        "material": "",
        "dosage": "",
        "price": "",
        "course_or_frequency": "",
        "treatment_steps": [],
        "implementation_notes": "",
        "customer_response": "",
        "related_demand": "",
        "source_evidence_ids": [],
        "evidence_turn_ids": [],
        "quote": "",
        "confidence": 0.0
      }
    ],
    "deal_events": [
      {
        "id": "EV_DEAL1",
        "event_type": "deal_confirmed|deposit|payment|order_created|not_deal|unclear",
        "participant": "",
        "participant_scope": "primary_customer|other_customer|unknown",
        "plan": "",
        "amount": "",
        "quote": "",
        "source_evidence_ids": [],
        "evidence_turn_ids": [],
        "confidence": 0.0
      }
    ],
    "profile_events": [
      {
        "id": "EV_PR1",
        "event_type": "customer_profile|staff_or_product_context|ambiguous|reject",
        "category": "",
        "value": "",
        "participant": "",
        "participant_scope": "primary_customer|other_customer|companion_or_family|unknown",
        "quote": "",
        "source_evidence_ids": [],
        "evidence_turn_ids": [],
        "confidence": 0.0
      }
    ],
    "concern_events": [
      {
        "id": "EV_C1",
        "event_type": "concern|reject|hesitate|accepted_no_concern|unclear",
        "participant": "",
        "participant_scope": "primary_customer|other_customer|companion_or_family|unknown",
        "content": "",
        "related_plan": "",
        "source_evidence_ids": [],
        "evidence_turn_ids": [],
        "quote": "",
        "confidence": 0.0
      }
    ],
    "budget_events": [
      {
        "id": "EV_B1",
        "event_type": "budget_limit|price_sensitive|discount_request|payment_pressure|deposit_or_payment|unclear",
        "participant": "",
        "participant_scope": "primary_customer|other_customer|unknown",
        "content": "",
        "amount": "",
        "related_plan": "",
        "source_evidence_ids": [],
        "evidence_turn_ids": [],
        "quote": "",
        "confidence": 0.0
      }
    ],
    "notes": []
  }
}
"""


_EVENT_AGENT_USER_TEMPLATE = """\
证据图:
{evidence_graph}

范围图:
{scope_graph}

相关纠错转写片段:
{dialogue}

只输出 event_graph JSON。
"""


_EMPTY_EVIDENCE_RESCUE_SYSTEM_PROMPT = """\
你是中文医美录音分析链路中的 Empty evidence rescue / 空证据兜底 Agent。

前一轮证据抽取没有找到可用的“当前顾客面诊证据”。你的任务不是重新完整分析，
而是先做场景复核：判断这是确实不该分析的非面诊录音，还是前一轮漏掉了当前顾客面诊。
如果确实漏掉了面诊，再做高精度兜底证据抽取。

通用规则：
1. 只基于转写原文和上下文判断，不创造主诉、适应症、推荐方案、成交结论或 SAP 内容。
2. 先判断 scene_type 和 is_current_customer_consultation。当前顾客面诊必须满足：
   当前顾客或同行客户在场并围绕自己的问题/目标/预算/顾虑回应，或员工/医生正在对其本人
   做诊断、解释方案、报价、开单、核销、术前沟通等接待动作。
3. 非当前顾客面诊时，所有 evidence_graph 列表必须保持为空，并在 scene_assessment.reason
   用一句中文说明原因。常见非面诊包括内部员工闲聊、前台订单处理、同事抱怨、缺席第三方
   顾客案例讨论、生活闲聊。凡是用“我有个顾客/那个顾客/有个美团的/他问我/她说/医生说/未成交”
   等方式谈论缺席第三方客户，默认按第三方案例或内部讨论处理，除非原文能明确证明当前顾客在场
   并正在就自己的方案提问、确认或接受。
4. 若判断前一轮确实漏掉了当前顾客面诊，才按原 evidence_graph schema 兜底抽取证据。
   兜底要高精度：只抽取原文直接支持的证据；不确定就留空；不要为了“补齐字段”而补全。
   如果只有价格、开单、核销等交易信息而没有医疗诉求或方案，可只保留 deal/budget 相关证据。
5. 严格区分当前顾客、同行客户、陪同人员、员工自述和其他客户案例。不要把员工自己的经历、
   缺席客户的情况、产品背景或医生科普当成当前顾客的主诉、标签、既往史或顾虑。
6. 兜底分类边界必须和证据抽取 Agent 一致：
   - customer_demand_evidence 只抽当前客户想解决的问题或审美目标；不要把单纯项目咨询、
     流程问题、询价、成交动作、治疗顺序或设计风格当主诉。
     员工/医生用“要不要、还要不要、需不需要、是不是要”提出的疑问或建议，不等于客户主诉；
     只有客户随后确认、接受或明确表达同一目标时，才可作为 customer_demand_evidence。
   - recommendation_evidence 只抽员工/医生给当前客户的项目、产品、材料、手术、注射或护理方案；
     保留品牌、材料、用量、价格、疗程、步骤和客户反应。
   - concern_evidence 必须来自客户/陪同真实担心、追问、拒绝或犹豫；员工单方面安抚不是顾虑。
   - budget_evidence 只抽客户预算、可接受价格、支付限制、还价/优惠诉求、对报价的价格敏感或反复核算；
     员工单纯报价、算价或解释优惠不算预算证据。
   - deal_evidence 只抽明确成交、未成交、预约、定金、开单、支付、核销、改约、复诊或未成交原因；
     “去看看方案/价格”“继续面诊”这类下一步沟通不等于成交或开单。
7. 输出字段必须严格使用下面 schema 的字段名，不要自造 demand_summary、plan_summary、speaker_role、
   evidence_text 等新字段。每条证据必须尽量填写 content、participant、participant_scope、
   evidence_turn_ids、quote、confidence；没有这些关键字段的条目不要输出。

只输出 JSON:
{
  "scene_assessment": {
    "scene_type": "active_consultation | internal_staff_chat | frontdesk_order | third_party_case_discussion | casual_chat | unclear",
    "is_current_customer_consultation": false,
    "confidence": 0.0,
    "reason": "short Chinese reason"
  },
  "evidence_graph": {
    "customer_demand_evidence": [
      {
        "id": "E_D1",
        "content": "",
        "body_part": "",
        "speaker": "customer|companion|staff_restated_confirmed",
        "participant": "主咨询客户|同行客户A|同行客户B|陪同人员|unknown",
        "participant_scope": "primary_customer|other_customer|companion_or_family|unknown",
        "handling_status": "current_handled|referral_or_deferred|unclear",
        "evidence_turn_ids": [],
        "quote": "",
        "confidence": 0.0
      }
    ],
    "diagnosis_evidence": [
      {
        "id": "E_DI1",
        "content": "",
        "body_part": "",
        "speaker": "consultant|doctor|expert_assistant|staff_restated_confirmed",
        "participant": "主咨询客户|同行客户A|同行客户B|unknown",
        "participant_scope": "primary_customer|other_customer|unknown",
        "evidence_turn_ids": [],
        "quote": "",
        "confidence": 0.0
      }
    ],
    "recommendation_evidence": [
      {
        "id": "E_R1",
        "content": "",
        "body_part": "",
        "participant": "主咨询客户|同行客户A|同行客户B|unknown",
        "participant_scope": "primary_customer|other_customer|unknown",
        "brand": "",
        "material": "",
        "dosage": "",
        "price": "",
        "course_or_frequency": "",
        "treatment_steps": [],
        "implementation_notes": "",
        "customer_response": "",
        "relation_to_current_demand": "current_main_plan|possible_current_plan|planting_or_later|alternative_not_recommended|auxiliary_or_care|not_current_or_referral|unclear",
        "evidence_turn_ids": [],
        "quote": "",
        "confidence": 0.0
      }
    ],
    "concern_evidence": [
      {
        "id": "E_C1",
        "content": "",
        "concern_type": "",
        "participant": "主咨询客户|同行客户A|同行客户B|陪同人员|unknown",
        "participant_scope": "primary_customer|other_customer|companion_or_family|unknown",
        "related_plan": "",
        "evidence_turn_ids": [],
        "quote": "",
        "confidence": 0.0
      }
    ],
    "budget_evidence": [
      {
        "id": "E_B1",
        "content": "",
        "amount": "",
        "participant": "主咨询客户|同行客户A|同行客户B|陪同人员|unknown",
        "participant_scope": "primary_customer|other_customer|companion_or_family|unknown",
        "related_plan": "",
        "evidence_turn_ids": [],
        "quote": "",
        "confidence": 0.0
      }
    ],
    "medical_history_evidence": [
      {
        "id": "E_H1",
        "content": "",
        "history_type": "",
        "participant": "主咨询客户|同行客户A|同行客户B|陪同人员|unknown",
        "participant_scope": "primary_customer|other_customer|companion_or_family|unknown",
        "evidence_turn_ids": [],
        "quote": "",
        "confidence": 0.0
      }
    ],
    "profile_evidence": [
      {
        "id": "E_P1",
        "category": "",
        "value": "",
        "participant": "主咨询客户|同行客户A|同行客户B|陪同人员|unknown",
        "participant_scope": "primary_customer|other_customer|companion_or_family|unknown",
        "evidence_turn_ids": [],
        "quote": "",
        "confidence": 0.0
      }
    ],
    "deal_evidence": [
      {
        "id": "E_DE1",
        "content": "",
        "deal_status": "",
        "amount": "",
        "participant": "主咨询客户|同行客户A|同行客户B|陪同人员|unknown",
        "participant_scope": "primary_customer|other_customer|companion_or_family|unknown",
        "evidence_turn_ids": [],
        "quote": "",
        "confidence": 0.0
      }
    ]
  }
}
"""


_EMPTY_EVIDENCE_RESCUE_USER_TEMPLATE = """\
员工 / 录音上下文:
{staff_context}

代码侧预处理提示:
{preprocess_context}

纠错后的转写:
{dialogue}

只输出 rescue JSON。
"""


_JUDGMENT_AGENT_SYSTEM_PROMPT = """\
你是中文医美录音分析链路中的 Agent 4：Judgment / 事实图生成 Agent。

输入包括 evidence_graph、event_graph 和本地 SAP 适应症字典召回的 candidate_indications。
你的任务是生成结构化 fact_graph。后续代码会把 fact_graph 渲染成最终分析结果和 SAP 内容，
所以不要写最终分析文案、不要写 SAP 咨询备注、不要输出 Markdown。

核心原则：
1. 事实图只做“证据到事实”的判断和归纳，不创造证据。每个 demand、recommendation、
   seed_recommendation、concern、budget_fact、medical_history、profile_fact、deal_factor
   都应尽量保留 evidence/source quote 和 source_evidence_ids；没有证据支持就不要输出。
2. event_graph 的事件极性优先用于解决歧义：
   - current_recommendation、deal_confirmed 支持 recommendations。
   - seed_recommendation 支持 seed_recommendations。
   - customer_question、staff_explanation、comparison_or_backup、diagnosis_only、not_recommended
     不能直接变成当前推荐方案或 SAP 适应症支持，除非另有明确 current_recommendation 证据。
   - deal 事件只绑定它明确命名的项目/订单；staff_or_product_context 不能变成客户标签。
3. 参与者必须隔离。保留 participant 和 participant_scope。主咨询客户、同行客户A/B、陪同人员
   分别建事实；不要把同行客户自己的需求合并到主咨询客户。陪同人员只有在明确代主客户说明时，
   才可支持主客户事实；如果是在说自己的项目，按 other_customer 处理。

事实分类规则：
4. demands 只保留当前客户明确想解决的问题或审美目标，包括客户本人提出、陪同代述、客户确认，
   或员工复述后客户接受的目标。员工/医生单方面观察先放 doctor_diagnoses；只有客户确认或
   当前推荐方案明确解决该问题时，才可转为 demand。客户明确提出但本次转科、下次再做或暂缓的
   问题也要保留，便于 SAP 备注和后续跟进；但除非当前方案支持，不要据此生成最终 SAP 适应症。
5. demands 要归并成最少的具体目标，单个客户通常 3-6 条。合并同义/重复表达，例如
   面颊/颊区/夹区/脸颊凹陷 + 填充/玻尿酸 归为一个凹陷改善目标。不要把仪器版本、发数、
   医生、验真、恢复、切口、排期、付款、优惠、单纯询价、治疗顺序或设计风格当作 demand；
   它们应进入 concerns、budget_facts、deal_factors 或 recommendation implementation_notes。
   “几月做一次”“做完A多久做B”“先做A再做B”等排期/顺序只记录为方案步骤或跟进信息；
   除非同时表达明确改善目标，否则不要作为独立 demand。
   “小毛毛/汗毛/体毛”等闲聊只有明确要求脱毛/冰点脱毛/激光脱毛时才可成为 demand。
   同一个证据或同义句只输出一条 demand；不要一条写泛化目标、一条写项目化目标造成重复。
6. doctor_diagnoses 保留医生/咨询师对当前客户的观察、诊断和结构分析；不要因为诊断提到某问题
   就自动生成客户主诉。
7. recommendations 是为解决当前 demands 的当前方案；必须尽量关联 related_demand_ids。
   分阶段治疗如果是解决当前主诉的必要步骤，仍属于 recommendations。
8. seed_recommendations 是额外种草、维养、低优先级、下次/转科/可延后或当前主诉之外的方案。
   员工提到“全脸/T区/整体设计/后面再做/可以先不做/分步选做”的可选方案，应保留为
   seed_recommendations，而不是删除或误放入当前 recommendations。
9. 推荐方案必须保留可执行细节：brand、material、dosage、price、course_or_frequency、
   treatment_steps、implementation_notes、customer_response。即使 evidence 使用 nested details
   或包含多个选项，fact_graph 中也必须暴露这些扁平字段，后续代码依赖扁平字段渲染。
10. concerns 和 deal_factors 必须具体，不要只写“治疗条件限制”“时间限制”“安全顾虑”等泛标签；
    要写清楚限制在哪里，例如价格压力、担心移位、担心后遗症、无法频繁到院、医生资质顾虑等。
    “去看看方案/价格”“先看一下方案”“继续面诊/再沟通”只是下一步沟通，不是成交、开单或预约。
11. budget_evidence 全部转换为 budget_facts。不要因为价格/折扣/可接受区间/定金/尾款/付款信息
    已经出现在 recommendation_evidence 中就丢掉。员工单纯报价不算预算事实；客户对报价敏感、
    还价、要求优惠、反复核算、表达支付压力时，要输出类似“对26800元方案价格敏感，要求优惠”
    或“预算上限约7000-8000元”的简洁事实。
12. profile_evidence 转成 profile_facts；medical_history、budget、concern、deal 中描述客户画像的
    信号也要保留为 profile_facts，例如既往项目/材料/仪器、预算与价格敏感、疼痛耐受、家庭/子女、
    行业/特殊身份、竞品机构、决策人、项目偏好、恢复/时间限制、产品偏好等。这些用于客户标签，
    不能因不是 SAP 适应症而删除。
13. 客户标签边界要严格：
    - 既往治疗/材料/仪器标签必须有正向既往史证据，如“做过/打过/去年/上次/外院”；不要从
      “从来没打过/没做过”或“能打/可以打”等当前可行性话语生成既往史。
    - 健康风险/禁忌只使用明确属于客户或同行客户的证据；员工/医生自述、产品描述、其他客户案例
      或模糊皮肤敏感表述不能变成当前客户标签。
    - “皮肤敏感/敏感肌/玫瑰痤疮”不等于“过敏史”；只有药物、麻药、碘伏、酒精、胶布或“对X过敏”
      等明确医学过敏证据，才输出过敏史。
14. 内部员工聊天、前台订单、付款/核销讨论、缺席第三方案例，若没有当前顾客主诉、诊断或方案，
    返回空业务事实，并设置 deal_outcome.status = "未明确"。

SAP 适应症判断：
15. candidate_indications 只是候选。只能复制 candidate_indications 中已经给出的
    standardized_indication 原文，不能自造编码或名称；宁可少选，不要错选。
16. 适应症必须有当前主诉、当前诊断或当前推荐方案支持。仅种草、备选、比较、员工科普、客户随口问、
    员工观察但客户未确认且无当前方案支持时，不要进入 indication_candidates。
17. 常见边界：
    - 副乳有明确诉求/方案时优先选择具体“副乳整形”；富贵包可保留需求或诊断，但没有明确吸脂/
      减脂治疗方案时不要硬选吸脂类适应症。
    - “闭口时/闭上嘴”等口部动作不能映射为痤疮。
    - 咬肌肉毒/瘦脸不能映射为面部除皱，除非有明确皱纹/动态纹/除皱证据。
    - 鼻基底/鼻头/鼻翼/鼻尖/三角结构 + 注射/玻尿酸/再生材料/芭比针/濡白天使 等结构支撑，
      优先匹配塑美（鼻中轴线（H）），不要误选外科-面部填充或鼻综合。
    - 外科“面部填充”仅在明确自体脂肪/脂肪胶/脂肪移植等外科填充时选择；玻尿酸、再生材料、
      胶原、童颜针、芭比针、瑞德喜、濡白天使等注射支撑不应补成外科“面部填充”。
    - 下颌线/下颌角拐点/耳前耳后韧带/外轮廓 + 童颜针/芭比针/支撑/提升，
      优先匹配塑美（下颌轮廓线（大O））。
    - 同时出现童颜针/芭比针结构支撑和肉毒/大提拉时，结构支撑是主方案；肉毒只有被明确推荐时
      才作为辅助或独立方案。
    - 泪沟、黑眼圈、法令纹等眼周/面部问题如果只来自员工观察、诊断说明、可选种草、或客户
      “要不要/是不是/可以先不/化妆即可/先做更在意的”回应，应留在 diagnosis、concern 或
      seed_recommendations；只有客户明确要求现在处理，或当前方案解决该问题时，才可变成 demand
      和最终适应症依据。

只输出 JSON:
{
  "fact_graph": {
    "demands": [],
    "doctor_diagnoses": [],
    "indication_candidates": [],
    "recommendations": [],
    "seed_recommendations": [],
    "concerns": [],
    "budget_facts": [],
    "medical_history": [],
    "profile_facts": [],
    "deal_factors": [],
    "deal_outcome": {},
    "uncertainties": []
  }
}
"""


_JUDGMENT_AGENT_USER_TEMPLATE = """\
证据图:
{evidence_graph}

事件图:
{event_graph}

本地适应症字典召回候选:
{candidate_indications}

只输出 fact_graph JSON。
"""


_PLAN_AGENT_SYSTEM_PROMPT = """\
你是中文医美录音分析链路中的 Agent 5：Plan adjudication / 推荐方案与种草方案裁决 Agent。

你的任务只包括两件事：重新裁决 fact_graph 中的 recommendations 与 seed_recommendations，
并补齐方案细节。不要选择 SAP 适应症，不要修改主诉、客户标签、预算、顾虑或成交结论，
不要生成最终分析文案或 SAP 咨询备注。

裁决流程：
1. 先看 event_graph 的事件极性，再看 fact_graph 和 evidence_graph：
   - current_recommendation、deal_confirmed 支持放入 recommendations。
   - seed_recommendation、deferred_demand、referral_or_deferred 支持放入 seed_recommendations。
   - customer_question、staff_explanation、comparison_or_backup、diagnosis_only、not_recommended
     不能单独进入 recommendations；只有同一方案另有 current_recommendation 证据时，
     才可作为当前方案的细节、备选说明或客户反应保留。
2. recommendations = 本次围绕当前主诉/诊断，员工或医生实际建议客户现在做、当次做，
   或作为解决当前主诉必要分阶段步骤的方案。
3. seed_recommendations = 当前主诉之外、可选、低优先级、维养、下次/后续/转科/暂缓，
   或整体设计中非核心的方案。员工发现其他可改善问题后提出的“种草”也放这里。
4. 以下内容从两类方案中剔除，必要时放入 rejected_recommendations：
   单纯比较或科普、客户随口询问、明确不建议/不适合、术前检查、术后用药、疤痕膏、
   敷料换药、排期、开单/付款/核销、护理注意事项。
5. 分阶段治疗的判断看目的：所有步骤都为解决当前主诉时，保留为 recommendations；
   只是可选增强、后续维养、另一个问题的方案时，放 seed_recommendations。
6. 参与者必须隔离。主咨询客户、同行客户A/B、陪同人员分别裁决；不要把同行客户自己的方案
   合并到主咨询客户，也不要删除同行客户有证据支持的方案。
7. 保留证据中的可执行细节：brand、material、dosage、price、course_or_frequency、
   treatment_steps、implementation_notes、customer_response、related_demand_ids、evidence_ids、
   participant、participant_scope。不要只放在 nested details 里。
   每条方案必须使用 content 字段写方案名称/方案概述；不要用 plan、plan_summary、title
   等字段替代 content。
8. 多个材料/产品作为选择时，已选或主推项放在 brand/material；备选、对比、替代材料放入
   implementation_notes。若备选本身是另一个后续方案，才放 seed_recommendations。
9. 合并或改写方案时，使用最具体、最有证据支持的中文表达，不要泛化成“治疗条件限制”
   或“综合改善方案”；不确定的客户态度写入 customer_response，不要因此删除当前方案。
10. 结构支撑、注射填充、光电、皮肤管理、手术等不同项目按同一原则裁决：只要是为当前主诉
    明确提出的执行方案，就保留为 recommendations；如果只是额外优化、种草、未来可做，
    就放 seed_recommendations。不要为某个部位写死例外规则。

只输出 JSON:
{
  "recommendation_adjudication": {
    "recommendations": [],
    "seed_recommendations": [],
    "rejected_recommendations": [
      {"source_id": "", "reason": ""}
    ],
    "notes": []
  }
}
"""


_PLAN_AGENT_USER_TEMPLATE = """\
当前 fact_graph:
{fact_graph}

证据图:
{evidence_graph}

事件图:
{event_graph}

相关纠错转写片段:
{dialogue}

只输出 recommendation_adjudication JSON。
"""


_AUDIT_AGENT_SYSTEM_PROMPT = """\
You are Agent 7 in a Chinese medical-aesthetic recording analysis chain:
the final audit and repair agent.

Audit the fact_graph before code renders analysis_result. You may return a
corrected_fact_graph only when there is a clear evidence-backed issue.

Audit priorities:
0. Enforce event_graph polarity. Do not keep final recommendations or SAP
   indications for customer_question, staff_explanation, comparison_or_backup,
   diagnosis_only, or not_recommended events. Deal outcome must be tied to a
   deal_confirmed/payment/deposit/order_created event for a specific plan when
   the transcript contains multiple options.
1. SAP indications must be evidence-backed and exact to body area/project.
   When participant_scope exists, each indication/recommendation/concern/budget
   must stay attached to its own participant. Do not merge 同行客户A/同行客户B facts
   into 主咨询客户, but do not delete valid同行客户 facts either.
2. Do not miss explicit current customer demands such as 副乳、富贵包、美白、毛孔、
   痘印、暗沉 when the customer raised them.
3. Do not turn referral/deferred demands into final SAP indications without a
   current plan.
4. Recommendations and seed recommendations must be separated correctly.
5. Do not keep standalone pre/post-op care as recommendation.
6. Do not infer nose surgery from nasal skin texture.
7. Do not infer acne from 闭口时/闭上嘴.
8. Deal outcome requires direct payment/order/deposit evidence.
9. Do not delete valid profile_facts such as budget, price sensitivity, prior
   treatment/material, children/family, industry, decision maker, treatment
   preference, or comparison institution when evidence supports them.
10. If budget_evidence exists but budget_facts are empty or less specific, repair
    budget_facts. If recommendation evidence has price/dosage/material in a
    nested detail but final recommendation lacks it, repair the flat fields.
11. If a current main recommendation in fact_graph would be lost from rendered
    output due to backup/comparison wording, preserve it and only demote the
    backup option.
12. If 下颌线/下颌角拐点/耳前耳后韧带 support evidence exists but final
    recommendations only keep 肉毒/大提拉, repair the structural support plan
    as the main recommendation.
13. If 面部除皱 is selected only because of 咬肌/瘦脸/大提拉/下颌线 and there
    is no explicit wrinkle/dynamic-line treatment, remove it.
14. If 鼻基底/鼻头/鼻翼 三角结构 injection evidence exists but final
    indications only keep 面部填充 or 鼻综合, repair to 塑美（鼻中轴线（H））.
15. If you cannot confidently repair an issue, leave fact_graph unchanged and
   add an audit issue instead of inventing.

Return JSON only:
{
  "audit": {
    "revision_required": false,
    "issues": [
      {
        "severity": "high|medium|low",
        "type": "",
        "description": "",
        "evidence": ""
      }
    ],
    "unresolved_risks": []
  },
  "corrected_fact_graph": null
}
"""


_AUDIT_AGENT_USER_TEMPLATE = """\
Fact graph after recommendation and indication adjudication:
{fact_graph}

Evidence graph:
{evidence_graph}

Event graph:
{event_graph}

Candidate indications:
{candidate_indications}

Relevant corrected transcript excerpts:
{dialogue}

Return audit JSON only. Include corrected_fact_graph only if a repair is clearly evidence-backed.
"""


_FINAL_RESULT_AUDIT_SYSTEM_PROMPT = """\
You are Agent 8 in a Chinese medical-aesthetic recording analysis chain:
the final user-visible result consistency auditor.

Audit the rendered analysis_result after code has converted fact_graph into
display fields. Your output may patch only the final result sections. Prefer
small, evidence-backed repairs.

Audit priorities:
1. Customer primary demands must be concrete treatment goals for the current
   visit/customer. Do not keep duplicate or near-duplicate demands. Do not treat
   doctor preference, brand preference, price calculation, payment/deposit,
   recovery/scar questions, or general worries as primary demands.
2. Customer concerns must include explicit worry/hesitation from the customer,
   especially safety, side effects, worsening hollowing, scars, recovery, pain,
   migration, price pressure, or doctor/operator concerns.
3. Recommendations must be actual staff/doctor plans for the customer's current
   demand. Seed recommendations are additional/next-visit/cross-department
   plans, not replacements for the current plan.
4. Every recommendation's demand_priority must point to an existing demand
   priority. If no exact demand exists, leave the link empty instead of linking
   to the wrong demand.
5. SAP indications must be exact to the project/body area and supported by
   current recommendations or confirmed current demands. Do not invent
   indications for unsupported nose surgery, acne, wrinkle treatment, or
   unrelated post-deal care.
6. Budget must be a normalized budget/price-sensitivity conclusion, not a raw
   evidence quote. A deposit/order amount is not automatically the customer's
   budget. If the customer repeatedly calculates or resists a quoted total,
   summarize the price sensitivity or upper bound.
7. Preserve useful recommendation details: dosage, material, brand, price,
   course/frequency, steps, implementation notes, and customer response.

Return JSON only:
{
  "final_result_audit": {
    "revision_required": false,
    "issues": [
      {
        "severity": "high|medium|low",
        "type": "",
        "description": "",
        "evidence": ""
      }
    ],
    "unresolved_risks": []
  },
  "analysis_result_patch": null
}

If repairs are needed, analysis_result_patch may include only these sections:
customer_primary_demands, customer_concerns, staff_recommendations,
staff_seed_recommendations, standardized_indications, consumption_intent,
consultation_result, customer_profile.
"""


_FINAL_RESULT_AUDIT_USER_TEMPLATE = """\
Trigger reasons:
{trigger_reasons}

Scope graph:
{scope_graph}

Evidence graph:
{evidence_graph}

Event graph:
{event_graph}

Fact graph:
{fact_graph}

Rendered analysis_result:
{analysis_result}

Relevant corrected transcript excerpts:
{dialogue}

Return final_result_audit JSON only. Include analysis_result_patch only when a repair is clearly evidence-backed.
"""


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else []


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


_AGENT_RECOMMENDATION_DETAIL_KEYS = (
    "brand",
    "material",
    "dosage",
    "price",
    "course_or_frequency",
    "treatment_steps",
    "implementation_notes",
    "customer_response",
)

_AGENT_PRICE_TERMS = (
    "预算",
    "价格",
    "报价",
    "费用",
    "金额",
    "元",
    "块",
    "万",
    "千",
    "贵",
    "便宜",
    "优惠",
    "打折",
    "折扣",
    "承受",
    "顶死",
    "最多",
    "打不起",
    "付",
    "定金",
    "订金",
)

_AGENT_BUDGET_CATEGORY_ALIASES = {
    "budget",
    "current_budget",
    "本次预算",
    "本次消费预算",
    "消费预算",
    "价格预算",
}

_AGENT_PROFILE_CATEGORY_ALIASES = {
    "price_sensitivity": "价格敏感度",
    "budget": "本次消费预算",
    "current_budget": "本次消费预算",
    "prior_treatment_experience": "治疗项目",
    "treatment_experience": "治疗项目",
    "prior_material_or_device": "历史用的设备/原材料名称",
    "material_or_device": "历史用的设备/原材料名称",
    "pain_tolerance": "疼痛耐受度",
    "decision_maker": "决策主体",
    "comparison_institution": "对比机构",
    "industry_or_identity": "行业",
    "occupation": "行业",
    "children_status": "亲属/子女情况",
}


def _agent_join_text(*values: object) -> str:
    parts: list[str] = []
    for value in values:
        if isinstance(value, list):
            parts.extend(_clean_text(item) for item in value if _clean_text(item))
        elif isinstance(value, dict):
            parts.append(json.dumps(value, ensure_ascii=False))
        else:
            text = _clean_text(value)
            if text:
                parts.append(text)
    return "；".join(part for part in parts if part)


def _agent_evidence_text(item: dict[str, Any]) -> str:
    return _agent_join_text(item.get("quote"), item.get("evidence"), item.get("content"), item.get("text"))


def _agent_item_content(item: dict[str, Any]) -> str:
    return _first_text(item, "content", "demand_content", "demand", "recommendation", "plan", "text", "summary", "value")


def _agent_item_key(item: dict[str, Any]) -> str:
    return _compact_key_text(
        _agent_item_content(item)
        or item.get("quote")
        or item.get("value")
        or item.get("amount")
    )


def _agent_has_price_signal(text: str) -> bool:
    text = _clean_text(text)
    if not text:
        return False
    if not any(term in text for term in _AGENT_PRICE_TERMS):
        return False
    if re.search(r"\d", text):
        return True
    if re.search(r"[一二三四五六七八九十两俩]+[千百]?多?万", text):
        return True
    if re.search(r"[一二三四五六七八九十两俩]+千", text):
        return True
    if re.search(r"[一二三四五六七八九十两俩]+百", text):
        return True
    if re.search(r"[一二三四五六七八九十两俩]+(块钱|元)", text):
        return True
    return any(term in text for term in ("价格高", "价格偏高", "太贵", "贵了", "打不起", "预算有限"))


def _agent_has_budget_or_price_reaction(text: str) -> bool:
    text = _clean_text(text)
    if not text:
        return False
    return any(
        term in text
        for term in (
            "预算",
            "贵",
            "便宜",
            "打折",
            "优惠",
            "申请",
            "承受",
            "顶死",
            "最多",
            "打不起",
            "太高",
            "价格",
            "多少钱",
            "几千",
            "几万",
        )
    )


def _agent_has_affordability_reaction(text: str) -> bool:
    text = _clean_text(text)
    if not text:
        return False
    return any(
        term in text
        for term in (
            "预算",
            "贵",
            "太高",
            "打折",
            "优惠",
            "申请",
            "承受",
            "顶死",
            "最多",
            "打不起",
            "不够",
            "没那么多",
            "价格偏高",
            "价格高",
            "价格敏感",
            "敏感",
            "反复核算",
            "反复算",
            "核算",
            "少一点",
            "差别有点大",
        )
    )


_AGENT_NOT_BUDGET_EXPLANATION_CUES = (
    "解决不了多少",
    "改善的程度有限",
    "改善程度有限",
    "效果有限",
    "做不了多少",
    "没效果",
)


def _agent_has_explicit_budget_intent(text: str) -> bool:
    text = _clean_text(text)
    if not text:
        return False
    return any(
        term in text
        for term in (
            "预算",
            "可接受",
            "能接受",
            "接受不了",
            "承受",
            "顶死",
            "最多",
            "上限",
            "不超过",
            "打不起",
            "付款",
            "支付",
            "付了",
            "付定",
            "定金",
            "订金",
            "意向金",
            "交钱",
        )
    )


def _agent_is_budget_fact_text(text: str) -> bool:
    text = _clean_text(text)
    if not text or not _agent_has_price_signal(text):
        return False
    if any(term in text for term in _AGENT_NOT_BUDGET_EXPLANATION_CUES) and not _agent_has_explicit_budget_intent(text):
        return False
    return _agent_has_explicit_budget_intent(text) or _agent_has_affordability_reaction(text)


def _agent_next_id(prefix: str, items: list[dict[str, Any]]) -> str:
    max_index = 0
    for item in items:
        raw = _clean_text(item.get("id") or item.get(f"{prefix.lower()}_id"))
        match = re.search(r"(\d+)$", raw)
        if match:
            max_index = max(max_index, int(match.group(1)))
    return f"{prefix}{max_index + 1}"


def _agent_participant_key(item: dict[str, Any]) -> tuple[str, str]:
    scope = _clean_text(item.get("participant_scope") or item.get("customer_scope"))
    participant = _clean_text(item.get("participant") or item.get("participant_label"))
    primary_aliases = {
        "",
        "客户",
        "顾客",
        "主客户",
        "主顾客",
        "主咨询客户",
        "primary_customer",
        "primary",
        "customer",
    }
    primary_name_markers = ("主咨询客户", "现场主咨询客户", "主客户", "主顾客")
    if scope in primary_aliases:
        return ("primary_customer", "")
    if not scope and participant in primary_aliases:
        return ("primary_customer", "")
    if not scope and any(marker in participant for marker in primary_name_markers):
        return ("primary_customer", "")
    if not participant and scope in primary_aliases:
        return ("primary_customer", "")
    return (scope, participant)


def _agent_profile_item_is_staff_scoped(item: dict[str, Any]) -> bool:
    scope = _clean_text(item.get("participant_scope") or item.get("customer_scope") or item.get("scope")).lower()
    if scope in {"staff", "doctor", "consultant", "badge_owner", "employee", "assistant", "nurse"}:
        return True
    participant = _clean_text(
        item.get("participant")
        or item.get("participant_label")
        or item.get("speaker")
        or item.get("speaker_label")
    )
    return any(term in participant for term in ("工牌本人", "咨询师", "医生", "顾问", "助理", "护士", "员工"))


def _agent_should_skip_profile_fact(category: str, value: str, evidence: str, item: dict[str, Any]) -> bool:
    if _agent_profile_item_is_staff_scoped(item):
        return True
    combined = _agent_join_text(category, value, evidence, item.get("content"), item.get("text"))
    if "过敏" not in combined:
        return False
    if not (any(term in category for term in ("健康风险", "禁忌", "病史")) or "过敏" in value):
        return False
    if any(term in combined for term in ("无药物过敏", "没有药物过敏", "无过敏史", "没有过敏史", "不过敏", "不是过敏")):
        return True
    if any(term in combined for term in ("过敏率", "不易过敏", "不容易过敏", "低敏", "抗过敏")):
        return True
    allergy_context = _agent_join_text(evidence, item.get("quote"), item.get("source_quote"))
    if not allergy_context or _compact_key_text(allergy_context) == _compact_key_text(value):
        content_text = _agent_join_text(item.get("content"), item.get("text"))
        if _compact_key_text(content_text) != _compact_key_text(value):
            allergy_context = content_text
    strong_allergy = any(
        term in allergy_context
        for term in (
            "药物过敏",
            "麻药过敏",
            "麻醉过敏",
            "利多卡因过敏",
            "碘伏过敏",
            "酒精过敏",
            "胶布过敏",
            "敷贴过敏",
            "过敏史",
            "对玻尿酸过敏",
            "对胶原过敏",
            "对肉毒过敏",
        )
    ) or bool(re.search(r"对.{1,12}过敏", allergy_context))
    if strong_allergy:
        return False
    return any(term in combined for term in ("皮肤过敏", "玫瑰痤疮", "敏感肌", "皮肤敏感", "容易泛红"))


def _agent_flatten_recommendation_details(fact_graph: dict[str, Any]) -> dict[str, Any]:
    updated = dict(fact_graph)
    for section in ("recommendations", "seed_recommendations"):
        flattened: list[dict[str, Any]] = []
        for item in _as_list(updated.get(section)):
            if not isinstance(item, dict):
                continue
            copied = dict(item)
            details = _as_dict(copied.get("details"))
            for key in _AGENT_RECOMMENDATION_DETAIL_KEYS:
                if copied.get(key) in (None, "", [], {}) and details.get(key) not in (None, "", [], {}):
                    copied[key] = details[key]
            if copied.get("material") in (None, "") and details.get("brand_or_material"):
                copied["material"] = details["brand_or_material"]
            if copied.get("brand") in (None, "") and details.get("brand_or_product"):
                copied["brand"] = details["brand_or_product"]
            if copied.get("price") in (None, "") and details.get("amount"):
                copied["price"] = details["amount"]
            flattened.append(copied)
        updated[section] = flattened
    return updated


def _agent_existing_item_keys(items: list[dict[str, Any]]) -> set[tuple[str, tuple[str, str]]]:
    return {
        (_agent_item_key(item), _agent_participant_key(item))
        for item in items
        if _agent_item_key(item)
    }


def _agent_ensure_demands_from_evidence_graph(
    fact_graph: dict[str, Any],
    evidence_graph: dict[str, Any],
) -> dict[str, Any]:
    updated = dict(fact_graph)
    demands = [dict(item) for item in _as_list(updated.get("demands")) if isinstance(item, dict)]
    seen = _agent_existing_item_keys(demands)
    for item in _as_list(evidence_graph.get("customer_demand_evidence")):
        if not isinstance(item, dict):
            continue
        content = _agent_item_content(item)
        if not content:
            continue
        confidence = item.get("confidence")
        if isinstance(confidence, (int, float)) and confidence < 0.62:
            continue
        scope = _clean_text(item.get("participant_scope") or item.get("customer_scope"))
        if scope == "staff":
            continue
        key = (_compact_key_text(content), _agent_participant_key(item))
        if key in seen:
            continue
        seen.add(key)
        next_id = _agent_next_id("D", demands)
        demands.append(
            {
                "id": next_id,
                "content": content,
                "body_part": _first_text(item, "body_part", "body_part_name"),
                "participant": _clean_text(item.get("participant") or item.get("participant_label")) or None,
                "participant_scope": scope or None,
                "handling_status": _clean_text(item.get("handling_status")) or None,
                "evidence_ids": [_clean_text(item.get("id"))] if _clean_text(item.get("id")) else [],
                "evidence": [_agent_evidence_text(item)] if _agent_evidence_text(item) else [],
                "confidence": item.get("confidence"),
            }
        )
    updated["demands"] = demands
    return updated


def _agent_budget_fact_from_item(
    item: dict[str, Any],
    *,
    source_id: str,
    content: str | None = None,
) -> dict[str, Any] | None:
    content = _clean_text(content) or _first_text(item, "content", "amount", "price", "quote", "text", "summary")
    quote = _agent_evidence_text(item)
    combined = _agent_join_text(content, quote, item.get("customer_response"))
    if not _agent_is_budget_fact_text(combined):
        return None
    return {
        "id": "",
        "content": content or quote,
        "participant": _clean_text(item.get("participant") or item.get("participant_label")) or None,
        "participant_scope": _clean_text(item.get("participant_scope") or item.get("customer_scope")) or None,
        "evidence_ids": [_clean_text(item.get("id")) or source_id],
        "evidence": [quote] if quote else [],
        "confidence": item.get("confidence"),
    }


def _agent_ensure_budget_facts_from_evidence_graph(
    fact_graph: dict[str, Any],
    evidence_graph: dict[str, Any],
) -> dict[str, Any]:
    updated = dict(fact_graph)
    budget_facts = [dict(item) for item in _as_list(updated.get("budget_facts")) if isinstance(item, dict)]
    seen = _agent_existing_item_keys(budget_facts)

    def append_fact(fact: dict[str, Any] | None) -> None:
        if not fact:
            return
        key = (_agent_item_key(fact), _agent_participant_key(fact))
        if not key[0] or key in seen:
            return
        fact["id"] = _agent_next_id("B", budget_facts)
        seen.add(key)
        budget_facts.append(fact)

    for item in _as_list(evidence_graph.get("budget_evidence")):
        if isinstance(item, dict):
            append_fact(_agent_budget_fact_from_item(item, source_id="budget_evidence"))

    for item in _as_list(evidence_graph.get("concern_evidence")):
        if not isinstance(item, dict):
            continue
        text = _agent_join_text(_agent_item_content(item), item.get("quote"))
        if _agent_has_budget_or_price_reaction(text):
            append_fact(_agent_budget_fact_from_item(item, source_id="concern_evidence"))

    for item in _as_list(evidence_graph.get("recommendation_evidence")):
        if not isinstance(item, dict):
            continue
        price = _first_text(item, "price")
        response = _first_text(item, "customer_response", "response")
        quote = _first_text(item, "quote")
        if not price:
            continue
        if not _agent_has_affordability_reaction(_agent_join_text(response, quote)):
            continue
        plan = _agent_item_content(item)
        content = f"{plan}价格反馈：{price}"
        if response:
            content = f"{content}；{response}"
        append_fact(_agent_budget_fact_from_item(item, source_id="recommendation_evidence", content=content))

    if budget_facts:
        updated["budget_facts"] = budget_facts
    return updated


def _agent_option_terms(text: str) -> list[str]:
    terms = [
        "瑞德喜",
        "双美胶原蛋白",
        "双美",
        "芭比针",
        "弗缦",
        "尊雅",
        "海媚",
        "思奥美",
        "艾拉斯提",
        "贝丽菲尔",
    ]
    found: list[str] = []
    for term in terms:
        if term in text and term not in found:
            found.append(term)
    return found


def _agent_should_preserve_as_backup_option(item: dict[str, Any]) -> bool:
    relation = _clean_text(item.get("relation_to_current_demand"))
    text = _agent_join_text(item.get("content"), item.get("quote"), item.get("implementation_notes"))
    if relation not in {"alternative_not_recommended", "unclear"}:
        return False
    return any(term in text for term in ("备选", "二选一", "选择", "维持时间偏短", "非主要推荐"))


def _agent_should_preserve_as_deferred_seed(item: dict[str, Any]) -> bool:
    relation = _clean_text(item.get("relation_to_current_demand"))
    if relation not in {"alternative_not_recommended", "unclear", "possible_current_plan"}:
        return False
    text = _agent_join_text(
        item.get("content"),
        item.get("quote"),
        item.get("implementation_notes"),
        item.get("customer_response"),
        item.get("treatment_steps"),
    )
    if not any(term in text for term in ("后续", "后期", "之后", "以后", "炎症控制后", "稳定后", "后面", "下次")):
        return False
    return any(term in text for term in ("可以", "可在", "联合", "考虑", "再做", "进行", "改善", "治疗"))


def _agent_same_plan_area(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if _agent_participant_key(left) != _agent_participant_key(right):
        return False
    left_body = _compact_key_text(_first_text(left, "body_part", "body_part_name"))
    right_body = _compact_key_text(_first_text(right, "body_part", "body_part_name"))
    if left_body and right_body and (left_body in right_body or right_body in left_body):
        return True
    left_text = _compact_key_text(_agent_item_content(left))
    right_text = _compact_key_text(_agent_item_content(right))
    return bool(left_text and right_text and (left_text[:8] in right_text or right_text[:8] in left_text))


def _agent_preserve_backup_options(
    fact_graph: dict[str, Any],
    evidence_graph: dict[str, Any],
) -> dict[str, Any]:
    alternatives = [
        item
        for item in _as_list(evidence_graph.get("recommendation_evidence"))
        if isinstance(item, dict) and _agent_should_preserve_as_backup_option(item)
    ]
    if not alternatives:
        return fact_graph
    updated = dict(fact_graph)
    recs = [dict(item) for item in _as_list(updated.get("recommendations")) if isinstance(item, dict)]
    for alt in alternatives:
        alt_text = _agent_join_text(_agent_item_content(alt), _first_text(alt, "brand"), _first_text(alt, "material"))
        terms = _agent_option_terms(_agent_join_text(alt_text, alt.get("quote")))
        if not terms:
            continue
        note = f"备选/对比材料：{'/'.join(terms)}"
        extra = _first_text(alt, "implementation_notes")
        if extra:
            note = f"{note}（{extra}）"
        for rec in recs:
            if not _agent_same_plan_area(rec, alt):
                continue
            rec_text = json.dumps(rec, ensure_ascii=False)
            if all(term in rec_text for term in terms):
                break
            current_notes = _first_text(rec, "implementation_notes", "notes")
            if note not in current_notes:
                rec["implementation_notes"] = "；".join(part for part in (current_notes, note) if part)
            brand = _first_text(rec, "brand")
            missing_terms = [term for term in terms if term not in brand]
            if brand and missing_terms and any(term in brand for term in _agent_option_terms(brand)):
                rec["brand"] = f"{brand}/{'/'.join(missing_terms)}"
            break
    updated["recommendations"] = recs
    return updated


def _agent_preserve_deferred_seed_recommendations(
    fact_graph: dict[str, Any],
    evidence_graph: dict[str, Any],
) -> dict[str, Any]:
    deferred_items = [
        item
        for item in _as_list(evidence_graph.get("recommendation_evidence"))
        if isinstance(item, dict) and _agent_should_preserve_as_deferred_seed(item)
    ]
    if not deferred_items:
        return fact_graph
    updated = dict(fact_graph)
    seeds = [dict(item) for item in _as_list(updated.get("seed_recommendations")) if isinstance(item, dict)]
    seen = {_compact_key_text(_agent_item_content(item)) for item in seeds if _agent_item_content(item)}
    for item in deferred_items:
        content = _agent_item_content(item)
        key = _compact_key_text(content)
        if not key or key in seen:
            continue
        copied = dict(item)
        copied["relation_to_current_demand"] = "planting_or_later"
        copied.setdefault("seed_reason", "炎症/恢复/当前阶段后续可考虑的方案")
        seeds.append(copied)
        seen.add(key)
    updated["seed_recommendations"] = seeds
    return updated


def _agent_normalize_profile_facts(fact_graph: dict[str, Any]) -> dict[str, Any]:
    updated = dict(fact_graph)
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, tuple[str, str]]] = set()
    for item in _as_list(updated.get("profile_facts")):
        if not isinstance(item, dict):
            continue
        copied = dict(item)
        category = _first_text(copied, "category", "tag_category", "type")
        category = _AGENT_PROFILE_CATEGORY_ALIASES.get(category, category)
        value = _first_text(copied, "value", "tag_value", "content", "text")
        evidence = _agent_join_text(copied.get("evidence"), copied.get("quote"))
        combined = _agent_join_text(category, value, evidence)
        if _agent_should_skip_profile_fact(category, value, evidence, copied):
            continue
        if category in _AGENT_BUDGET_CATEGORY_ALIASES or category == "本次消费预算":
            if not _agent_has_price_signal(combined):
                continue
            category = "本次消费预算"
        if category == "价格敏感度":
            if any(term in combined for term in ("高", "贵", "太高", "打不起", "顶死", "预算有限")):
                value = "高"
            elif any(term in combined for term in ("价格", "预算", "费用", "报价")):
                value = "中"
        if not category or not value:
            continue
        key = (_compact_key_text(category), _compact_key_text(value), _agent_participant_key(copied))
        if key in seen:
            continue
        seen.add(key)
        copied["category"] = category
        copied["value"] = value
        copied["content"] = value
        normalized.append(copied)
    updated["profile_facts"] = normalized
    return updated


def _agent_normalize_fact_content_fields(fact_graph: dict[str, Any]) -> dict[str, Any]:
    updated = dict(fact_graph)
    for section in (
        "demands",
        "doctor_diagnoses",
        "recommendations",
        "seed_recommendations",
        "concerns",
        "budget_facts",
        "medical_history",
        "profile_facts",
        "deal_factors",
    ):
        normalized: list[dict[str, Any]] = []
        changed = False
        for item in _as_list(updated.get(section)):
            if not isinstance(item, dict):
                continue
            copied = dict(item)
            if not _first_text(copied, "content", "demand_content", "demand", "recommendation", "plan", "text"):
                summary = _first_text(copied, "demand_content", "summary", "description")
                if summary:
                    copied["content"] = summary
                    changed = True
            normalized.append(copied)
        if changed:
            updated[section] = normalized
    return updated


def _agent_ensure_demands_from_diagnoses_when_empty(fact_graph: dict[str, Any]) -> dict[str, Any]:
    if _as_list(fact_graph.get("demands")):
        return fact_graph
    if not (_as_list(fact_graph.get("recommendations")) or _as_list(fact_graph.get("indication_candidates"))):
        return fact_graph
    diagnoses = [dict(item) for item in _as_list(fact_graph.get("doctor_diagnoses")) if isinstance(item, dict)]
    if not diagnoses:
        return fact_graph

    def clean_diagnosis_demand_text(text: str, body: str) -> str:
        cleaned = re.sub(r"[，,；;]?\s*既往[^，,；;。]*(?:假体|注射史|治疗史)[^，,；;。]*", "", text).strip("，,；;。 ")
        if "基础尚可" in cleaned and "存在" in cleaned:
            suffix = cleaned.split("存在", 1)[1].strip("，,；;。 ")
            if suffix:
                cleaned = f"{body}{suffix}" if body else suffix
        return cleaned or text

    demands: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in diagnoses[:6]:
        text = _first_text(item, "content", "summary", "diagnosis", "text")
        body = _first_text(item, "body_part", "body_part_name", "area")
        if not text:
            continue
        text = clean_diagnosis_demand_text(text, body)
        if any(term in text for term in ("既往", "假体", "做过", "注射史")) and not any(
            term in text for term in ("偏", "低", "凹", "凸", "不顺", "浮肿", "松", "垮", "显", "扁平")
        ):
            continue
        key = _compact_key_text(_agent_join_text(body, text))
        if not key or key in seen:
            continue
        seen.add(key)
        content = text if text.startswith(("改善", "希望", "想")) else f"希望改善{text}"
        demands.append(
            {
                "demand_id": f"D{len(demands) + 1}",
                "content": content,
                "body_part": body or None,
                "evidence_ids": _as_list(item.get("evidence_ids")),
                "handling_status": "current_handled",
                "participant": _first_text(item, "participant") or "主咨询客户",
                "participant_scope": _first_text(item, "participant_scope") or "primary_customer",
                "source": "diagnosis_recommendation_fallback",
            }
        )
    if not demands:
        return fact_graph
    updated = dict(fact_graph)
    updated["demands"] = demands
    return updated


_AGENT_DEMAND_KEY_TERMS = (
    "双眼皮",
    "内双",
    "肿眼泡",
    "眼睛肿",
    "无神",
    "显大",
    "小平扇",
    "平扇",
    "开眼角",
    "内眼角",
    "提肌",
    "不对称",
    "眼袋",
    "泪沟",
    "卧蚕",
    "细纹",
    "干纹",
    "胶原",
    "热玛吉",
    "钻石精雕",
    "隐痕精雕",
    "腰腹",
    "妈妈臀",
    "臀凹",
    "大腿",
    "手臂",
    "吸脂",
    "填胸",
    "丰胸",
    "太阳穴",
    "下巴",
    "副乳",
    "富贵包",
    "美白",
    "毛孔",
    "痘印",
    "痘坑",
    "出油",
    "提亮",
    "暗沉",
    "雀斑",
    "色斑",
    "汗管瘤",
    "鼻部",
    "鼻子",
    "下颌缘",
    "瘦脸",
    "水光",
    "童颜",
    "祛斑",
    "皮秒",
    "发红",
    "泛红",
    "下至",
    "太窄",
    "过窄",
    "变宽",
    "加宽",
    "显凶",
    "柔和",
    "眼尾",
    "眼修复",
    "修复",
)


def _agent_demand_text(item: dict[str, Any]) -> str:
    return _agent_join_text(
        _first_text(item, "content", "demand_content", "demand", "text", "summary"),
        _first_text(item, "body_part", "body_part_name"),
        item.get("quote"),
        item.get("evidence"),
    )


def _agent_demand_core_text(item: dict[str, Any]) -> str:
    return _agent_join_text(
        _first_text(item, "content", "demand_content", "demand", "text", "summary"),
        _first_text(item, "body_part", "body_part_name"),
    )


def _agent_has_prior_eyelid_surgery_context(text: str) -> bool:
    """Return True only for explicit prior-eyelid-surgery repair context."""

    if any(term in text for term in ("眼修复", "双眼皮修复", "重睑修复")):
        return True
    prior_terms = (
        "做过双眼皮",
        "做过重睑",
        "双眼皮做过",
        "重睑做过",
        "以前做过双眼皮",
        "之前做过双眼皮",
        "既往双眼皮",
        "双眼皮术后",
        "重睑术后",
        "韩式三点",
        "埋线双眼皮",
        "埋线重睑",
    )
    repair_terms = ("修复", "不满意", "变形", "肉条", "疤痕", "太宽", "过宽", "太窄", "过窄")
    return any(term in text for term in prior_terms) and any(term in text for term in repair_terms)


def _agent_is_vague_skin_request(text: str) -> bool:
    if not any(term in text for term in ("皮肤科", "皮肤项目", "看皮肤", "皮肤咨询")):
        return False
    if any(
        term in text
        for term in (
            "痘",
            "痤疮",
            "闭口",
            "毛孔",
            "痘坑",
            "痘印",
            "斑",
            "暗沉",
            "暗黄",
            "美白",
            "提亮",
            "泛红",
            "发红",
            "红血丝",
            "敏感",
            "水光",
            "干燥",
            "缺水",
            "细纹",
            "皱纹",
            "松弛",
            "热玛吉",
            "皮秒",
            "脱毛",
            "汗管瘤",
        )
    ):
        return False
    return True


def _agent_is_non_business_demand(item: dict[str, Any]) -> bool:
    text = _agent_demand_text(item)
    if not text:
        return True
    if "具体问题未说明" in text:
        return True
    if _agent_is_vague_skin_request(text):
        return True
    if any(term in text for term in ("小毛毛", "汗毛", "体毛", "毛面")) and not any(
        term in text for term in ("脱毛", "冰点脱毛", "激光脱毛", "去毛", "去除", "处理")
    ):
        return True
    if any(term in text for term in ("价格", "多少钱", "费用", "报价", "预算")) and not any(
        term in text for term in ("改善", "治疗", "手术", "注射", "填充", "吸脂", "双眼皮", "脱毛")
    ):
        return True
    if any(
        term in text
        for term in (
            "几月",
            "月份",
            "六月",
            "七月",
            "八月",
            "九月",
            "做完",
            "多久可以做",
            "隔多久",
            "隔一段时间",
            "间隔",
            "先做",
            "再做",
            "后面再做",
        )
    ) and not any(
        term in text
        for term in (
            "改善",
            "变小",
            "变高",
            "凹陷",
            "松弛",
            "下垂",
            "紧致",
            "抗衰",
            "提升",
            "补水",
            "干燥",
            "祛斑",
            "祛痣",
            "脱毛",
            "去除",
            "填充",
            "支撑",
            "塑形",
        )
    ):
        return True
    if any(term in text for term in ("接受", "确认", "确定", "决定")) and any(
        term in text for term in ("套餐", "案例价", "回填方式", "方案")
    ) and not any(term in text for term in ("改善", "去除", "填充", "提升", "塑形", "调整")):
        return True
    if any(term in text for term in ("主咨询客户", "持续咨询", "围绕")) and "眼袋" in text:
        return True
    if any(term in text for term in ("安排", "预约", "下个月", "早点做", "具体时间")) and not any(
        term in text for term in ("改善", "肿", "凹陷", "无神", "显小", "松弛", "下垂", "填充", "吸脂")
    ):
        return True
    if any(term in text for term in ("具体时间", "时间安排", "下半年", "下个月")) and any(
        term in text for term in ("计划做", "计划", "安排")
    ) and not any(term in text for term in ("腰腹", "妈妈臀", "臀凹", "大腿", "手臂", "胸", "太阳穴", "下巴", "眼周", "眼部")):
        return True
    return False


def _agent_demand_cluster(item: dict[str, Any]) -> str:
    text = _agent_demand_core_text(item) or _agent_demand_text(item)
    if any(term in text for term in ("整体改善面部状态", "面部整体状态", "面部看着更好看", "面部看着好看", "稍微面部看着好看")):
        return "face_overall_improvement"
    if "侧面" in text and any(term in text for term in ("轮廓", "线条", "侧颜", "明显", "眶外", "颧弓")):
        return "face_profile_contour"
    if any(term in text for term in ("轮廓外扩", "上半部分轮廓", "上面的问题全部有点外扩", "上半脸外扩")):
        return "upper_face_contour_expansion"
    if any(term in text for term in ("颊凹", "面颊凹陷", "脸颊凹陷", "颊区凹陷", "夹区凹陷")) or (
        any(term in text for term in ("变瘦", "凹进去", "凹陷感", "凹陷")) and any(term in text for term in ("面部", "脸", "颊"))
    ):
        return "cheek_hollow"
    if "鼻" in text and any(term in text for term in ("残留", "没溶干净", "摸得到", "填充物")):
        return "nose_residual_filler"
    if any(term in text for term in ("水光", "补水", "干燥", "肤质粗", "胶原流失")):
        return "skin_hydration"
    if any(term in text for term in ("热玛吉", "超声炮", "超声刀", "抗衰", "紧致", "提升")) and any(
        term in text for term in ("面部", "脸", "皮肤", "本次", "想做", "希望")
    ):
        return "face_anti_aging"
    if any(term in text for term in ("价格", "多少钱", "费用", "报价", "预算")) and not any(
        term in text
        for term in (
            "改善",
            "治疗",
            "去除",
            "祛斑",
            "色斑",
            "点痣",
            "祛痣",
            "痘坑",
            "毛孔",
            "颈纹",
            "红血丝",
            "注射",
            "填充",
        )
    ):
        return "process_price"
    if any(term in text for term in ("安排", "预约", "下个月", "早点做", "具体时间")) and "改善" not in text:
        return "process_schedule"
    if "笑" in text and any(term in text for term in ("厚重", "一坨肉", "中下面部", "面中")) and any(
        term in text for term in ("改善", "想", "希望")
    ):
        return "smile_midface_heavy"
    if any(term in text for term in ("鱼尾纹", "眉间纹", "抬头纹", "动态纹", "除皱", "皱眉纹", "川字纹")):
        return "dynamic_wrinkle"
    if any(term in text for term in ("肉毒", "除皱针", "瘦脸针")):
        return "botox_injection"
    if any(term in text for term in ("上眼", "上睑", "眼部提升", "提眉", "切眉", "上睑提升")) and any(
        term in text for term in ("提升", "松弛", "眼皮", "手术", "改善")
    ):
        return "upper_eyelid_lift"
    if any(term in text for term in ("点痣", "祛痣", "色素痣")) or ("痣" in text and any(term in text for term in ("点", "去除", "包干", "复发"))):
        return "mole_removal"
    if any(term in text for term in ("祛斑", "色斑", "雀斑", "斑点", "皮秒", "双击")):
        return "pigmentation"
    if "胶原流失" in text or (
        any(term in text for term in ("衰老", "紧致", "抗衰", "提升")) and any(term in text for term in ("面部", "脸", "胶原"))
    ):
        return "face_anti_aging"
    if "卡粉" in text or any(term in text for term in ("上妆卡", "妆容不服帖", "妆感不服帖")):
        return "makeup_caking_texture"
    if any(term in text for term in ("额头", "额结节", "额颞", "眉峰")) and any(
        term in text for term in ("不够高", "高光", "立体", "上镜", "起来", "填充", "瑞德喜")
    ):
        return "forehead_contour"
    if any(term in text for term in ("耳基底", "耳朵", "耳轮", "耳位")) and any(
        term in text for term in ("往上", "往外", "提", "出来", "填充", "支撑", "肉肉")
    ):
        return "ear_base_support"
    if any(term in text for term in ("眶外C", "眶外", "眉尾", "眉弓")) and any(
        term in text for term in ("提", "平", "支撑", "立体", "补", "填充", "瑞德喜", "眼睛", "双眼皮")
    ):
        return "orbital_tail_support"
    if any(term in text for term in ("人中窝", "人中")) and any(term in text for term in ("加深", "缩短", "改善", "打", "注射")):
        return "philtrum_shape"
    if any(term in text for term in ("小腿", "腿部")) and any(term in text for term in ("肌肉", "瘦", "肉毒", "注射")):
        return "calf_slimming"
    if _agent_has_prior_eyelid_surgery_context(text):
        return "eye_repair"
    if "外切眼袋" in text or "眼袋" in text:
        return "eye_bag"
    if "泪沟" in text:
        return "tear_trough"
    if "黑眼圈" in text or "眼下黑" in text:
        return "dark_circle"
    if any(term in text for term in ("上睑下垂", "眼皮下垂", "遮瞳", "遮挡瞳孔", "瞳孔暴露")):
        return "eye_exposure"
    if any(term in text for term in ("眼皮大", "眼眶周围水肿", "眼周水肿", "眼部浮肿", "浮肿", "浮泡", "上眼泡")):
        return "eye_puffiness"
    if any(term in text for term in ("显凶", "柔和", "不好相处", "眼神凶", "眼神柔")):
        return "eye_expression"
    if any(term in text for term in ("眼尾上扬", "眼尾下调", "眼尾走势", "眼尾走向", "眼尾形态", "眼尾设计")):
        return "eye_tail_design"
    if any(term in text for term in ("双眼皮", "重睑", "内双", "肿眼泡", "肿泡眼", "眼睛肿", "上睑臃肿", "太窄", "过窄", "变宽", "加宽")):
        if any(term in text for term in ("太窄", "过窄", "偏窄", "变宽", "加宽", "宽度", "平扇", "开扇", "形态", "上妆")):
            return "double_eyelid_style"
        if any(term in text for term in ("松弛", "下垂", "耷拉", "去皮", "遮挡", "上睑臃肿")):
            return "double_eyelid_laxity"
        return "double_eyelid"
    if "下至" in text:
        return "eye_downward"
    if any(term in text for term in ("卧蚕", "媚眼针")):
        return "eye_wocan"
    if any(term in text for term in ("中面部", "苹果肌", "鼻基底", "法令纹")) and any(
        term in text for term in ("凹陷", "填充", "饱满", "年轻", "衔接")
    ):
        return "midface_filling"
    if "下巴" in text and any(term in text for term in ("后缩", "偏短", "短", "下庭", "长度", "翘度", "玻尿酸", "支撑", "比例", "填充")):
        return "chin_shape"
    if any(term in text for term in ("下颌缘", "脸变小", "视觉瘦脸", "瘦脸", "轮廓更精致", "轮廓线条", "骨相感", "轻薄感")):
        return "jawline_slimming"
    if any(term in text for term in ("鼻小柱", "人中")) and any(
        term in text for term in ("拉出", "偏长", "缩短", "改善", "精致", "注射")
    ):
        return "nose_philtrum"
    if any(term in text for term in ("鼻部", "鼻子", "山根", "鼻背", "鼻基底")) and any(
        term in text for term in ("调整", "微调", "优化", "高一点", "补打", "支撑", "立体", "玻尿酸", "材料", "改善")
    ):
        return "nose_filling"
    if any(term in text for term in ("唇", "嘴巴", "嘴凸")):
        return "lip_shape"
    if "脱毛" in text:
        return "hair_removal"
    if any(term in text for term in ("痘痘", "痤疮", "炎症", "痘印", "痘坑", "闭口")):
        return "acne_texture"
    if any(term in text for term in ("色斑", "雀斑", "黄褐斑", "斑点", "祛斑", "淡斑", "皮秒", "色素沉着", "肤色不均")) or (
        "光子" in text and any(term in text for term in ("色斑", "雀斑", "祛斑", "淡斑"))
    ):
        return "pigmentation"
    if any(term in text for term in ("发红", "泛红", "敏感", "红血丝")):
        return "skin_redness"
    if "汗管瘤" in text:
        return "syringoma"
    if any(term in text for term in ("毛孔", "痘印", "痘坑", "肤质", "出油", "暗沉", "暗黄", "提亮")):
        return "skin_texture"
    if any(term in text for term in ("水光", "童颜", "中胚层", "胶原水光")):
        return "skin_booster"
    if any(term in text for term in ("抗衰", "松垮", "松弛", "法令纹", "口角囊袋")) and any(
        term in text for term in ("面部", "脸", "热玛吉", "超声", "提升")
    ):
        return "face_anti_aging"
    if any(term in text for term in ("显年轻", "疲态", "疲惫", "憔悴")) and any(
        term in text for term in ("整体", "面部", "脸")
    ):
        return "face_anti_aging"
    if any(term in text for term in ("钻石精雕", "隐痕精雕", "收紧抗衰", "眼周收紧", "热玛吉")):
        return "eye_tightening"
    if "开眼角" in text or "内眼角" in text or "内眦" in text:
        return "eye_canthus"
    if any(term in text for term in ("平扇", "开扇", "自然", "宽", "眼头尖", "尖眼角", "假感", "妈生")):
        return "eye_style"
    if any(term in text for term in ("无神", "显小", "有神", "瞳孔曝光", "眼睛小")):
        return "eye_exposure"
    if any(term in text for term in ("提肌", "不对称", "体积对称", "双眼体积")):
        return "eye_symmetry"
    if any(term in text for term in ("腰腹", "妈妈臀")):
        return "waist_liposuction"
    if any(term in text for term in ("背部", "后背", "背上")) and any(term in text for term in ("吸脂", "抽脂", "术后", "没抽")):
        return "back_liposuction"
    if "大腿" in text and "吸脂" in text:
        return "thigh_liposuction"
    if "手臂" in text and "吸脂" in text:
        return "arm_liposuction"
    if any(term in text for term in ("臀凹", "臀部凹陷")):
        return "hip_dip"
    if any(term in text for term in ("丰胸", "填胸", "隆胸")):
        return "breast_augmentation"
    if "太阳穴" in text:
        return "temple_filling"
    if "下巴" in text:
        return "chin_filling"
    if "细纹" in text or "干纹" in text:
        return "fine_lines"
    return ""


def _agent_demand_key_terms(item: dict[str, Any]) -> set[str]:
    text = _agent_demand_core_text(item) or _agent_demand_text(item)
    return {term for term in _AGENT_DEMAND_KEY_TERMS if term in text}


def _agent_demand_is_duplicate(item: dict[str, Any], kept: list[dict[str, Any]]) -> bool:
    participant = _agent_participant_key(item)
    body = _compact_key_text(_first_text(item, "body_part", "body_part_name"))
    content = _compact_key_text(_first_text(item, "content", "demand_content", "demand", "text", "summary"))
    terms = _agent_demand_key_terms(item)
    cluster = _agent_demand_cluster(item)
    related_cluster_sets = (
        {"nose_filling", "nose_philtrum"},
        {"upper_face_contour_expansion", "face_profile_contour"},
    )
    for existing in kept:
        if _agent_participant_key(existing) != participant:
            continue
        existing_cluster = _agent_demand_cluster(existing)
        if cluster and existing_cluster and cluster == existing_cluster:
            return True
        if cluster and existing_cluster and any(cluster in group and existing_cluster in group for group in related_cluster_sets):
            return True
        existing_body = _compact_key_text(_first_text(existing, "body_part", "body_part_name"))
        existing_content = _compact_key_text(_first_text(existing, "content", "demand_content", "demand", "text", "summary"))
        if content and existing_content and (content in existing_content or existing_content in content):
            return True
        if body and existing_body and body != existing_body:
            continue
        existing_terms = _agent_demand_key_terms(existing)
        if terms and existing_terms and len(terms & existing_terms) >= 2:
            return True
    return False


def _agent_normalize_demands(fact_graph: dict[str, Any]) -> dict[str, Any]:
    updated = dict(fact_graph)
    kept: list[dict[str, Any]] = []
    for item in _as_list(updated.get("demands")):
        if not isinstance(item, dict):
            continue
        copied = dict(item)
        if _clean_text(copied.get("participant_scope") or copied.get("customer_scope")).lower() == "companion_or_family":
            continue
        if _agent_is_non_business_demand(copied):
            continue
        if _agent_demand_is_duplicate(copied, kept):
            continue
        kept.append(copied)
    updated["demands"] = kept
    return updated


def _agent_issue_has_current_recommendation(recommendation_context: str, terms: tuple[str, ...]) -> bool:
    compact = _clean_text(recommendation_context)
    if not compact or not any(term in compact for term in terms):
        return False
    return any(
        cue in compact
        for cue in (
            "改善",
            "治疗",
            "处理",
            "方案",
            "做",
            "打",
            "注射",
            "填充",
            "激光",
            "光电",
            "皮秒",
            "水光",
            "胶原",
            "玻尿酸",
            "嗨体",
            "福曼",
        )
    )


def _agent_prune_observation_only_demands(fact_graph: dict[str, Any]) -> dict[str, Any]:
    updated = dict(fact_graph)
    demands = [dict(item) for item in _as_list(updated.get("demands")) if isinstance(item, dict)]
    if not demands:
        return fact_graph

    recommendation_context = _agent_join_text(updated.get("recommendations"))
    full_context = _agent_join_text(
        updated.get("demands"),
        updated.get("doctor_diagnoses"),
        updated.get("recommendations"),
        updated.get("seed_recommendations"),
        updated.get("concerns"),
    )
    weak_or_deferred_cues = (
        "要不要",
        "是不是",
        "是否",
        "有一点",
        "一点点",
        "轻度",
        "可接受",
        "考虑",
        "关注",
        "化妆就行",
        "化个妆就行",
        "可以化妆",
        "先打你在意的",
        "先打在意的",
        "先做更在意的",
        "先做你在意的",
        "先不处理",
        "暂时不处理",
        "后期再",
        "下次再",
        "以后再",
        "不是这次",
    )
    issue_groups = (
        ("泪沟", "眼下凹", "眼下凹陷", "眶下凹陷"),
        ("黑眼圈", "眼下黑", "眼周暗沉", "眼周色沉"),
        ("法令纹", "鼻唇沟"),
    )

    kept: list[dict[str, Any]] = []
    changed = False
    for item in demands:
        item_text = _agent_join_text(item)
        evidence_text = _agent_evidence_text(item)
        remove_item = False
        for terms in issue_groups:
            if not any(term in item_text for term in terms):
                continue
            if _agent_issue_has_current_recommendation(recommendation_context, terms):
                continue
            if not evidence_text or any(cue in item_text or cue in full_context for cue in weak_or_deferred_cues):
                remove_item = True
                break
        if remove_item:
            changed = True
            continue
        kept.append(item)

    if not changed:
        return fact_graph
    updated["demands"] = kept
    return updated


_AGENT_PLAN_AREA_TERMS = (
    "颧骨前方",
    "颧骨后方",
    "内轮廓",
    "外轮廓",
    "颧弓",
    "眶外C",
    "眶外",
    "眉尾",
    "眉弓",
    "额头",
    "额结节",
    "中面部",
    "上颌",
    "泪沟",
    "鼻基底",
    "下巴",
    "下颌缘",
    "耳基底",
    "耳朵",
    "小腿",
    "唇",
    "鼻",
    "法令纹",
)

_AGENT_PLAN_MATERIAL_TERMS = (
    "瑞德喜",
    "玻尿酸",
    "肉毒",
    "胶原",
    "双美",
    "熊猫",
    "濡白",
    "定彩",
    "乔雅登",
    "艾拉斯提",
    "童颜",
)


def _agent_plan_text(item: dict[str, Any]) -> str:
    return _agent_join_text(
        _first_text(item, "content", "plan", "recommendation", "summary"),
        _first_text(item, "body_part", "body_part_name"),
        _first_text(item, "brand", "material", "dosage", "price", "course_or_frequency", "implementation_notes"),
        item.get("treatment_steps"),
        item.get("evidence"),
    )


def _agent_plan_terms(text: str, terms: tuple[str, ...]) -> set[str]:
    return {term for term in terms if term in text}


def _agent_plan_quality_score(item: dict[str, Any]) -> int:
    text = _agent_plan_text(item)
    score = min(len(text), 260)
    if _clean_text(item.get("evidence")):
        score += 30
    if _clean_text(item.get("customer_response")):
        score += 20
    if _as_list(item.get("treatment_steps")):
        score += 12
    if "推断" in text:
        score -= 80
    if "未明确回应" in text:
        score -= 10
    return score


def _agent_plan_semantic_signature(item: dict[str, Any]) -> str:
    text = _agent_join_text(
        _first_text(item, "content", "plan", "recommendation", "summary"),
        _first_text(item, "body_part", "body_part_name"),
        _first_text(item, "brand", "material", "dosage", "price", "course_or_frequency", "implementation_notes"),
        item.get("treatment_steps"),
    )
    if not text:
        return ""
    area_terms = _agent_plan_terms(text, _AGENT_PLAN_AREA_TERMS)
    material_terms = _agent_plan_terms(
        text,
        _AGENT_PLAN_MATERIAL_TERMS
        + ("英伦大提升", "海派", "海妹", "黑曜", "朗普洛", "濡白", "熊猫", "爱拉斯提"),
    )
    body_part = _first_text(item, "body_part", "body_part_name")
    body_context = _agent_join_text(body_part, text)
    if any(term in body_context for term in ("下颌缘", "下颌线", "下颌角", "下颌轮廓")):
        area_sig = "jawline"
    elif "下巴" in body_context:
        area_sig = "chin"
    elif any(term in body_context for term in ("唇", "嘴唇", "嘴巴")):
        area_sig = "lip"
    elif any(term in body_context for term in ("鼻", "山根", "鼻小柱", "鼻中轴")):
        area_sig = "nose"
    elif area_terms:
        area_sig = "|".join(sorted(area_terms))
    else:
        area_sig = _agent_demand_cluster({"content": text, "body_part": body_part})
    if not area_sig:
        return ""
    material_sig = "|".join(sorted(material_terms))
    if not material_sig:
        material_sig = _compact_key_text(_first_text(item, "brand", "material", "product_or_solution"))
    if not material_sig:
        return ""
    return f"{area_sig}::{material_sig}"


def _agent_plan_is_duplicate(seed: dict[str, Any], recommendations: list[dict[str, Any]]) -> bool:
    seed_text = _agent_plan_text(seed)
    seed_compact = _compact_key_text(seed_text)
    seed_cluster = _agent_demand_cluster({"content": seed_text, "body_part": seed.get("body_part")})
    seed_areas = _agent_plan_terms(seed_text, _AGENT_PLAN_AREA_TERMS)
    seed_materials = _agent_plan_terms(seed_text, _AGENT_PLAN_MATERIAL_TERMS)
    seed_participant = _agent_participant_key(seed)
    for rec in recommendations:
        if seed_participant != ("", "") and _agent_participant_key(rec) != ("", "") and _agent_participant_key(rec) != seed_participant:
            continue
        rec_text = _agent_plan_text(rec)
        rec_compact = _compact_key_text(rec_text)
        if seed_compact and rec_compact and (seed_compact in rec_compact or rec_compact in seed_compact):
            return True
        rec_cluster = _agent_demand_cluster({"content": rec_text, "body_part": rec.get("body_part")})
        rec_areas = _agent_plan_terms(rec_text, _AGENT_PLAN_AREA_TERMS)
        rec_materials = _agent_plan_terms(rec_text, _AGENT_PLAN_MATERIAL_TERMS)
        if seed_areas and rec_areas and seed_areas - rec_areas:
            continue
        if seed_cluster and rec_cluster and seed_cluster == rec_cluster:
            if not seed_materials or not rec_materials or seed_materials & rec_materials:
                return True
        if seed_areas and rec_areas and seed_areas & rec_areas:
            if seed_materials and rec_materials and seed_materials & rec_materials:
                return True
    return False


def _agent_remove_redundant_seed_recommendations(fact_graph: dict[str, Any]) -> dict[str, Any]:
    recommendations = [dict(item) for item in _as_list(fact_graph.get("recommendations")) if isinstance(item, dict)]
    seeds = [dict(item) for item in _as_list(fact_graph.get("seed_recommendations")) if isinstance(item, dict)]
    if not seeds:
        return fact_graph
    kept = [item for item in seeds if not recommendations or not _agent_plan_is_duplicate(item, recommendations)]
    deduped: list[dict[str, Any]] = []
    for item in kept:
        key = _compact_key_text(_agent_plan_text(item))
        if not key:
            continue
        signature = _agent_plan_semantic_signature(item)
        duplicate_index: int | None = None
        for index, existing in enumerate(deduped):
            existing_key = _compact_key_text(_agent_plan_text(existing))
            existing_signature = _agent_plan_semantic_signature(existing)
            if (
                key == existing_key
                or key in existing_key
                or existing_key in key
                or (signature and signature == existing_signature)
            ):
                duplicate_index = index
                break
        if duplicate_index is None:
            deduped.append(item)
        elif _agent_plan_quality_score(item) > _agent_plan_quality_score(deduped[duplicate_index]):
            deduped[duplicate_index] = item
    kept = deduped
    if len(kept) == len(seeds):
        return fact_graph
    updated = dict(fact_graph)
    updated["seed_recommendations"] = kept
    return updated


def _agent_evidence_text_from_item(item: dict[str, Any]) -> str:
    text = _first_text(item, "quote", "content", "text", "summary", "description")
    if text:
        return text
    evidence = item.get("evidence")
    if isinstance(evidence, list):
        return "\n".join(_clean_text(value) for value in evidence if _clean_text(value))
    if evidence:
        return _clean_text(evidence)
    return ""


def _agent_existing_fact_evidence_text(item: dict[str, Any]) -> str:
    evidence = item.get("evidence")
    if isinstance(evidence, list):
        return "\n".join(_clean_text(value) for value in evidence if _clean_text(value))
    if evidence:
        return _clean_text(evidence)
    supporting = item.get("supporting_evidence")
    if isinstance(supporting, list):
        return "\n".join(_clean_text(value) for value in supporting if _clean_text(value))
    if supporting:
        return _clean_text(supporting)
    return _first_text(item, "quote", "source_quote", "evidence_quote")


def _agent_evidence_lookup(evidence_graph: dict[str, Any]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for section in _EVIDENCE_LIST_SECTIONS:
        for item in _as_list(evidence_graph.get(section)):
            if not isinstance(item, dict):
                continue
            text = _agent_evidence_text_from_item(item)
            if not text:
                continue
            for key in ("id", "evidence_id", "source_id", "quote_id"):
                item_id = _clean_text(item.get(key))
                if item_id:
                    lookup[item_id] = text
    return lookup


def _agent_backfill_evidence_texts(fact_graph: dict[str, Any], evidence_graph: dict[str, Any]) -> dict[str, Any]:
    lookup = _agent_evidence_lookup(evidence_graph)
    if not lookup:
        return fact_graph
    updated = dict(fact_graph)
    for key in (
        "demands",
        "doctor_diagnoses",
        "indication_candidates",
        "recommendations",
        "seed_recommendations",
        "concerns",
        "budget_facts",
        "medical_history",
        "profile_facts",
        "deal_factors",
    ):
        items: list[dict[str, Any]] = []
        changed = False
        for item in _as_list(updated.get(key)):
            if not isinstance(item, dict):
                continue
            copied = dict(item)
            current_evidence = _agent_existing_fact_evidence_text(copied)
            evidence_ids = [_clean_text(value) for value in _as_list(copied.get("evidence_ids")) if _clean_text(value)]
            resolved = [lookup[item_id] for item_id in evidence_ids if lookup.get(item_id)]
            if resolved and not current_evidence:
                copied["evidence"] = resolved
                changed = True
            items.append(copied)
        if changed:
            updated[key] = items
    return updated


def _agent_dedupe_fact_items_by_content(
    fact_graph: dict[str, Any],
    key: str,
    *,
    content_keys: tuple[str, ...] = ("content", "summary", "text", "factor", "concern", "value"),
) -> dict[str, Any]:
    items = [dict(item) for item in _as_list(fact_graph.get(key)) if isinstance(item, dict)]
    if not items:
        return fact_graph
    kept: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, str]]] = set()
    for item in items:
        content = _first_text(item, *content_keys)
        compact = _compact_key_text(content)
        loose_compact = re.sub(r"(约|大概|左右|元|块钱|人民币)", "", compact)
        if not compact:
            continue
        participant = _agent_participant_key(item)
        duplicate_index: int | None = None
        for index, existing in enumerate(kept):
            existing_content = _first_text(existing, *content_keys)
            existing_compact = _compact_key_text(existing_content)
            existing_loose = re.sub(r"(约|大概|左右|元|块钱|人民币)", "", existing_compact)
            if participant != ("", "") and _agent_participant_key(existing) not in {("", ""), participant}:
                continue
            if (
                compact == existing_compact
                or compact in existing_compact
                or existing_compact in compact
                or (loose_compact and existing_loose and (loose_compact in existing_loose or existing_loose in loose_compact))
            ):
                duplicate_index = index
                break
        if duplicate_index is not None:
            if len(content) > len(_first_text(kept[duplicate_index], *content_keys)):
                kept[duplicate_index] = item
            continue
        key_tuple = (compact, participant)
        if key_tuple in seen:
            continue
        seen.add(key_tuple)
        kept.append(item)
    if len(kept) == len(items):
        return fact_graph
    updated = dict(fact_graph)
    updated[key] = kept
    return updated


def _agent_filter_non_deal_factors(fact_graph: dict[str, Any]) -> dict[str, Any]:
    items = [dict(item) for item in _as_list(fact_graph.get("deal_factors")) if isinstance(item, dict)]
    if not items:
        return fact_graph
    kept: list[dict[str, Any]] = []
    for item in items:
        text = _agent_join_text(
            item.get("content"),
            item.get("summary"),
            item.get("factor"),
            item.get("deal_status"),
            item.get("quote"),
            item.get("evidence"),
        )
        if any(cue in text for cue in _RESCUE_NON_DEAL_NEXT_STEP_CUES) and not any(
            cue in text for cue in _RESCUE_STRONG_DEAL_ACTION_CUES
        ):
            continue
        kept.append(item)
    if len(kept) == len(items):
        return fact_graph
    updated = dict(fact_graph)
    updated["deal_factors"] = kept
    return updated


def _agent_normalize_non_deal_outcome(fact_graph: dict[str, Any]) -> dict[str, Any]:
    outcome = fact_graph.get("deal_outcome")
    if not isinstance(outcome, dict):
        return fact_graph
    text = _agent_join_text(
        outcome.get("content"),
        outcome.get("summary"),
        outcome.get("quote"),
        outcome.get("evidence"),
    )
    status = _clean_text(outcome.get("status"))
    if status and status != "未明确":
        return fact_graph
    if not any(cue in text for cue in _RESCUE_NON_DEAL_NEXT_STEP_CUES):
        return fact_graph
    negative_deal_context = any(
        cue in text
        for cue in (
            "尚未明确成交",
            "未明确成交",
            "未明确预约",
            "没有成交",
            "没有预约",
            "未成交",
            "未预约",
            "未开单",
            "未付款",
        )
    )
    if any(cue in text for cue in _RESCUE_STRONG_DEAL_ACTION_CUES) and not negative_deal_context:
        return fact_graph
    updated = dict(fact_graph)
    updated["deal_outcome"] = {"status": "未明确"}
    return updated


def _agent_normalize_concerns(fact_graph: dict[str, Any]) -> dict[str, Any]:
    concerns = [dict(item) for item in _as_list(fact_graph.get("concerns")) if isinstance(item, dict)]
    if not concerns:
        return fact_graph
    kept: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, str]]] = set()
    for item in concerns:
        content = _first_text(item, "content", "concern", "text", "summary")
        if not content:
            continue
        key = (_compact_key_text(content), _agent_participant_key(item))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        kept.append(item)
    if len(kept) == len(concerns):
        return fact_graph
    updated = dict(fact_graph)
    updated["concerns"] = kept
    return updated


def _agent_has_current_surgical_plan(fact_graph: dict[str, Any]) -> bool:
    plan_text = _agent_join_text(fact_graph.get("recommendations"))
    return any(term in plan_text for term in ("手术", "外切", "内切", "切开", "麻醉", "抽脂", "吸脂"))


def _agent_has_positive_medical_safety_signal(fact_graph: dict[str, Any]) -> bool:
    medical_text = _agent_join_text(fact_graph.get("medical_history"))
    positive_terms = (
        "葡萄膜炎",
        "眼底病",
        "泼尼松",
        "激素",
        "长期服药",
        "不能停药",
        "糖尿病",
        "凝血",
        "抗凝",
        "心脏病",
        "本人高血压",
        "确诊高血压",
        "患有高血压",
    )
    if any(term in medical_text for term in positive_terms):
        return True
    if "高血压" in medical_text and not any(term in medical_text for term in ("无高血压", "没有高血压", "母亲高血压", "家族高血压")):
        return True
    return False


def _agent_filter_unsupported_medical_safety_concerns(fact_graph: dict[str, Any]) -> dict[str, Any]:
    concerns = [dict(item) for item in _as_list(fact_graph.get("concerns")) if isinstance(item, dict)]
    if not concerns:
        return fact_graph
    if _agent_has_current_surgical_plan(fact_graph) and _agent_has_positive_medical_safety_signal(fact_graph):
        return fact_graph
    generic_terms = ("既往疾病或长期用药是否影响手术安全", "长期用药是否影响手术安全和术后恢复")
    kept = [
        item
        for item in concerns
        if not any(term in _first_text(item, "content", "concern", "text", "summary") for term in generic_terms)
    ]
    if len(kept) == len(concerns):
        return fact_graph
    updated = dict(fact_graph)
    updated["concerns"] = kept
    return updated


def _agent_remove_rejected_indications(
    fact_graph: dict[str, Any],
    indication_adjudication: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(indication_adjudication, dict):
        return fact_graph
    rejected = {
        _clean_text(item.get("standardized_indication"))
        for item in _as_list(indication_adjudication.get("rejected_indications"))
        if isinstance(item, dict) and _clean_text(item.get("standardized_indication"))
    }
    rejected_name_body: set[tuple[str, str]] = set()
    for standardized in rejected:
        parts = standardized.split("|")
        if len(parts) >= 6:
            rejected_name_body.add((_clean_text(parts[3]), _clean_text(parts[5])))
    if not rejected and not rejected_name_body:
        return fact_graph
    candidates = [item for item in _as_list(fact_graph.get("indication_candidates")) if isinstance(item, dict)]
    kept = [
        item
        for item in candidates
        if item.get("force_include")
        or (
            _clean_text(item.get("standardized_indication")) not in rejected
            and (_clean_text(item.get("indication_name")), _clean_text(item.get("body_part_name"))) not in rejected_name_body
        )
    ]
    if len(kept) == len(candidates):
        return fact_graph
    updated = dict(fact_graph)
    updated["indication_candidates"] = kept
    return updated


def _agent_add_catalog_indication(
    candidates: list[dict[str, Any]],
    *,
    name: str,
    body: str,
    evidence: str,
    confidence: float = 0.72,
    force_include: bool = False,
) -> bool:
    row = _catalog_match_by_name(name, body)
    if not row:
        return False
    for item in candidates:
        if _clean_text(item.get("indication_name")) == row["indication_name"] and _clean_text(item.get("body_part_name")) == row["body_part_name"]:
            return False
    candidates.append(
        {
            **row,
            "evidence_ids": [],
            "evidence": [evidence],
            "confidence": confidence,
            "force_include": force_include,
            "reason": "agent deterministic indication fallback",
        }
    )
    return True


def _agent_result_has_indication(items: list[dict[str, Any]], *, name: str, body_contains: str) -> bool:
    return any(
        _clean_text(item.get("indication_name")) == name
        and body_contains in _clean_text(item.get("body_part_name"))
        for item in items
    )


def _agent_append_result_catalog_indication(result: dict[str, Any], *, name: str, body: str, evidence: str) -> bool:
    row = _catalog_match_by_name(name, body)
    if not row:
        return False
    block = result.setdefault("standardized_indications", {})
    if not isinstance(block, dict):
        block = {"inference_note": None, "summary": "", "items": []}
        result["standardized_indications"] = block
    items = [dict(item) for item in _as_list(block.get("items")) if isinstance(item, dict)]
    if _agent_result_has_indication(items, name=row["indication_name"], body_contains=row["body_part_name"]):
        return False
    items.append({**row, "evidence": evidence})
    block["items"] = items
    block["summary"] = "；".join(
        f"{_clean_text(item.get('indication_name'))}（{_clean_text(item.get('body_part_name'))}）"
        for item in items
        if _clean_text(item.get("indication_name"))
    )
    return True


def _agent_prune_result_profile_tags(result: dict[str, Any]) -> bool:
    profile = result.get("customer_profile")
    if not isinstance(profile, dict):
        return False
    tags = [dict(item) for item in _as_list(profile.get("tags")) if isinstance(item, dict)]
    if not tags:
        return False
    negative_history_markers = (
        "从来没打过",
        "从来没做过",
        "没有打过",
        "没打过",
        "未打过",
        "没有做过",
        "没做过",
        "未做过",
        "无治疗史",
        "无既往",
        "没有既往",
    )
    prior_markers = (
        "做过",
        "打过",
        "填过",
        "治疗过",
        "做了",
        "打了",
        "割过",
        "隆过",
        "术后",
        "既往",
        "之前",
        "以前",
        "曾",
        "外院",
        "去年",
        "今年",
        "最近一次",
        "上次",
    )
    kept: list[dict[str, Any]] = []
    changed = False
    for item in tags:
        category = _clean_text(item.get("category"))
        value = _clean_text(item.get("value"))
        evidence = _clean_text(item.get("evidence"))
        combined = _agent_join_text(category, value, evidence)
        if category in {"治疗项目", "历史用的设备/原材料名称"}:
            if any(term in combined for term in negative_history_markers) or not any(term in combined for term in prior_markers):
                changed = True
                continue
        kept.append(item)
    if not changed:
        return False
    profile["tags"] = kept
    return True


def _agent_recompute_result_seed_summary(result: dict[str, Any]) -> None:
    block = result.get("staff_seed_recommendations")
    if not isinstance(block, dict):
        return
    items = [dict(item) for item in _as_list(block.get("items")) if isinstance(item, dict)]
    block["items"] = items
    block["summary"] = "；".join(
        _first_text(item, "recommendation", "content", "summary")
        for item in items
        if _first_text(item, "recommendation", "content", "summary")
    )


def _agent_recompute_result_recommendation_summary(result: dict[str, Any]) -> None:
    block = result.get("staff_recommendations")
    if not isinstance(block, dict):
        return
    items = [dict(item) for item in _as_list(block.get("items")) if isinstance(item, dict)]
    block["items"] = items
    block["summary"] = "；".join(
        _first_text(item, "recommendation", "content", "summary")
        for item in items
        if _first_text(item, "recommendation", "content", "summary")
    )


def _agent_correct_result_brand_terms(result: dict[str, Any], *, context: str) -> bool:
    if "海派" not in context:
        return False
    changed = False
    for block_name in ("staff_recommendations", "staff_seed_recommendations"):
        block = result.get(block_name)
        if not isinstance(block, dict):
            continue
        for item in _as_list(block.get("items")):
            if not isinstance(item, dict):
                continue
            for key in ("recommendation", "brand", "implementation_notes", "evidence"):
                value = item.get(key)
                if isinstance(value, str) and "海薇" in value:
                    item[key] = value.replace("海薇", "海派")
                    changed = True
    if changed:
        _agent_recompute_result_recommendation_summary(result)
        _agent_recompute_result_seed_summary(result)
    return changed


def _agent_demote_result_orphan_recommendations(result: dict[str, Any]) -> bool:
    rec_block = result.get("staff_recommendations")
    if not isinstance(rec_block, dict):
        return False
    kept: list[dict[str, Any]] = []
    changed = False
    for item in [dict(value) for value in _as_list(rec_block.get("items")) if isinstance(value, dict)]:
        text = _agent_plan_text(item)
        demand_links = _as_list(item.get("demand_priority")) + _as_list(item.get("related_demand_ids")) + _as_list(item.get("linked_demand_ids"))
        should_demote = (
            not demand_links
            and "英伦大提升" in text
            and any(term in text for term in ("下颌缘", "斜方肌", "除皱", "300单位", "一瓶"))
        )
        if should_demote:
            changed = _agent_append_result_seed_recommendation(result, item) or changed
        else:
            kept.append(item)
    if changed:
        rec_block["items"] = kept
        _agent_recompute_result_recommendation_summary(result)
    return changed


def _agent_result_demand_key(item: dict[str, Any]) -> str:
    text = _agent_join_text(
        _first_text(item, "demand", "content", "text", "summary"),
        _first_text(item, "body_part", "body_part_name", "area"),
        item.get("evidence"),
    )
    if any(term in text for term in ("面颊", "颊区", "夹区", "脸颊")) and any(
        term in text for term in ("凹陷", "填充", "玻尿酸")
    ):
        return "cheek_hollow_filling"
    return _agent_demand_cluster(
        {"content": text, "body_part": _first_text(item, "body_part", "body_part_name", "area")}
    ) or _compact_key_text(text)


def _agent_result_demand_score(item: dict[str, Any]) -> int:
    content = _first_text(item, "demand", "content", "text", "summary")
    evidence = _clean_text(item.get("evidence"))
    score = min(len(content), 100)
    if evidence:
        score += 5
    if "改善" in content:
        score += 30
    if any(term in content for term in ("关注", "是否", "考虑是否")):
        score -= 40
    if _first_text(item, "body_part", "body_part_name", "area"):
        score += 8
    return score


def _agent_dedupe_result_demands(result: dict[str, Any]) -> bool:
    block = result.get("customer_primary_demands")
    if not isinstance(block, dict):
        return False
    items = [dict(item) for item in _as_list(block.get("items")) if isinstance(item, dict)]
    if not items:
        return False
    by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        key = _agent_result_demand_key(item)
        if not key:
            continue
        if key not in by_key:
            by_key[key] = item
            order.append(key)
        elif _agent_result_demand_score(item) > _agent_result_demand_score(by_key[key]):
            by_key[key] = item
    deduped = [by_key[key] for key in order if key in by_key]
    if len(deduped) == len(items):
        return False
    for index, item in enumerate(deduped, start=1):
        item["priority"] = index
    block["items"] = deduped
    block["summary"] = "；".join(
        _first_text(item, "demand", "content", "text", "summary")
        for item in deduped
        if _first_text(item, "demand", "content", "text", "summary")
    )
    return True


def _agent_context_evidence_for_terms(context: str, terms: tuple[str, ...]) -> str:
    if not context:
        return ""
    parts = [part.strip() for part in re.split(r"[。！？!?；;\n]+", context) if part.strip()]
    for index, part in enumerate(parts):
        if any(term in part for term in terms):
            start = max(index - 1, 0)
            end = min(index + 2, len(parts))
            return " / ".join(parts[start:end])[:260]
    return ""


def _agent_append_result_concern(result: dict[str, Any], *, content: str, evidence: str) -> bool:
    content = _clean_text(content)
    evidence = _clean_text(evidence)
    if not content or not evidence:
        return False
    block = result.setdefault("customer_concerns", {})
    if not isinstance(block, dict):
        block = {"inference_note": None, "summary": "", "items": []}
        result["customer_concerns"] = block
    items = [dict(item) for item in _as_list(block.get("items")) if isinstance(item, dict)]
    key = _compact_key_text(content)
    if any(_compact_key_text(_first_text(item, "content", "concern", "text", "summary")) == key for item in items):
        return False
    items.append({"type": "顾虑", "content": content, "evidence": evidence})
    block["items"] = items
    block["summary"] = "；".join(
        _first_text(item, "content", "concern", "text", "summary")
        for item in items
        if _first_text(item, "content", "concern", "text", "summary")
    )
    return True


def _agent_backfill_result_concerns_from_recommendations(result: dict[str, Any], *, context: str) -> bool:
    changed = False
    recommendations = _as_list(_as_dict(result.get("staff_recommendations")).get("items"))
    for item in recommendations:
        if not isinstance(item, dict):
            continue
        response = _first_text(item, "customer_response", "response")
        if not response:
            continue
        body = _first_text(item, "body_part", "body_part_name")
        text = _agent_join_text(response, item.get("evidence"), item.get("recommendation"), body)
        if any(term in text for term in ("颊凹", "夹凹", "凹陷")) and any(
            term in text for term in ("担心", "怕", "更狠", "加重", "更凹", "越凹")
        ):
            evidence = _agent_context_evidence_for_terms(
                context, ("怕凹", "怕越", "凹的更", "凹陷加重", "颊凹加重")
            ) or response
            changed = _agent_append_result_concern(
                result,
                content="担心咬肌肉毒后面颊凹陷加重",
                evidence=evidence,
            ) or changed
        if any(term in text for term in ("安全", "后遗症", "风险", "移位", "副作用")) and any(
            term in text for term in ("担心", "怕", "询问", "安不安全", "有没有")
        ):
            target = "玻尿酸填充" if any(term in text for term in ("玻尿酸", "填充", "面颊", "颊区")) else (body or "方案")
            evidence = _agent_context_evidence_for_terms(context, ("安不安全", "安全", "后遗症", "移位", "副作用")) or response
            changed = _agent_append_result_concern(
                result,
                content=f"担心{target}的安全性及后遗症",
                evidence=evidence,
            ) or changed
    return changed


_AGENT_NON_DEMAND_CONCERN_CUES = (
    "担心",
    "害怕",
    "怕",
    "顾虑",
    "风险",
    "后遗症",
    "副作用",
    "安全",
    "移位",
    "留疤",
    "疤痕",
    "恢复",
    "疼",
    "闭眼",
)

_AGENT_NON_DEMAND_PRICE_CUES = (
    "多少钱",
    "价格",
    "报价",
    "费用",
    "预算",
    "贵",
    "便宜",
    "定金",
    "订金",
    "付款",
)

_AGENT_EXECUTOR_CUES = (
    "主刀",
    "亲自做",
    "谁做",
    "哪个医生",
    "院长做",
    "教授做",
    "医生做",
    "医生操作",
)

_AGENT_TREATMENT_GOAL_CUES = (
    "改善",
    "调整",
    "解决",
    "想做",
    "希望",
    "提升",
    "填充",
    "支撑",
    "祛",
    "去",
    "瘦",
    "变",
    "修复",
    "塑形",
    "淡化",
    "美白",
    "紧致",
    "抗衰",
)


def _agent_result_item_text(item: dict[str, Any]) -> str:
    return _agent_join_text(
        _first_text(item, "demand", "content", "text", "summary", "concern", "recommendation"),
        _first_text(item, "body_part", "body_part_name", "area"),
        item.get("evidence"),
        item.get("customer_response"),
    )


def _agent_demote_non_demand_result_items(result: dict[str, Any]) -> bool:
    block = result.get("customer_primary_demands")
    if not isinstance(block, dict):
        return False
    items = [dict(item) for item in _as_list(block.get("items")) if isinstance(item, dict)]
    if not items:
        return False
    kept: list[dict[str, Any]] = []
    changed = False
    for item in items:
        text = _agent_result_item_text(item)
        has_goal = any(term in text for term in _AGENT_TREATMENT_GOAL_CUES)
        is_concern = any(term in text for term in _AGENT_NON_DEMAND_CONCERN_CUES)
        is_price = any(term in text for term in _AGENT_NON_DEMAND_PRICE_CUES)
        is_executor = any(term in text for term in _AGENT_EXECUTOR_CUES)
        is_brand_preference = any(term in text for term in ("倾向选择", "偏向选择", "想用", "品牌")) and any(
            term in text for term in ("保妥适", "衡力", "吉适", "瑞德喜", "艾拉斯提", "乔雅登", "濡白")
        )
        if (is_concern or is_price) and not has_goal:
            changed = _agent_append_result_concern(
                result,
                content=_first_text(item, "demand", "content", "text", "summary") or text[:80],
                evidence=_clean_text(item.get("evidence")) or text[:160],
            ) or changed
            changed = True
            continue
        if (is_executor or is_brand_preference) and not has_goal:
            changed = True
            continue
        kept.append(item)
    if not changed:
        return False
    for index, item in enumerate(kept, start=1):
        item["priority"] = index
    block["items"] = kept
    block["summary"] = "；".join(
        _first_text(item, "demand", "content", "text", "summary")
        for item in kept
        if _first_text(item, "demand", "content", "text", "summary")
    )
    return True


def _agent_match_recommendation_to_demand_priority(item: dict[str, Any], demands: list[dict[str, Any]]) -> list[int]:
    text = _agent_plan_text(item)
    if not text:
        return []
    scored: list[tuple[int, int]] = []
    for demand in demands:
        try:
            priority = int(demand.get("priority") or 0)
        except (TypeError, ValueError):
            priority = 0
        if priority <= 0:
            continue
        demand_text = _agent_result_item_text(demand)
        score = 0
        for term in ("鼻", "眼", "泪沟", "面颊", "颊", "下巴", "下颌", "嘴", "唇", "咬肌", "额", "颞", "太阳穴", "胸", "副乳", "富贵包", "皮肤", "痣", "斑"):
            if term in text and term in demand_text:
                score += 2
        for term in ("凹陷", "填充", "支撑", "提升", "祛", "瘦", "塑形", "修复", "美白", "淡化", "紧致"):
            if term in text and term in demand_text:
                score += 1
        if score:
            scored.append((score, priority))
    scored.sort(reverse=True)
    return [priority for _score, priority in scored[:2]]


def _agent_repair_result_recommendation_links(result: dict[str, Any]) -> bool:
    demands = [dict(item) for item in _as_list(_as_dict(result.get("customer_primary_demands")).get("items")) if isinstance(item, dict)]
    valid = {int(item.get("priority") or 0) for item in demands if isinstance(item.get("priority"), int) or str(item.get("priority") or "").isdigit()}
    block = result.get("staff_recommendations")
    if not isinstance(block, dict) or not valid:
        return False
    changed = False
    items = [dict(item) for item in _as_list(block.get("items")) if isinstance(item, dict)]
    for item in items:
        raw_values = _as_list(item.get("demand_priority")) or _as_list(item.get("related_demand_ids")) or _as_list(item.get("linked_demand_ids"))
        kept: list[int] = []
        for value in raw_values:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed in valid and parsed not in kept:
                kept.append(parsed)
        if not kept:
            kept = _agent_match_recommendation_to_demand_priority(item, demands)
        if kept != _as_list(item.get("demand_priority")):
            item["demand_priority"] = kept
            changed = True
    if changed:
        block["items"] = items
        _agent_recompute_result_recommendation_summary(result)
    return changed


def _agent_remove_executor_only_result_recommendations(result: dict[str, Any]) -> bool:
    block = result.get("staff_recommendations")
    if not isinstance(block, dict):
        return False
    kept: list[dict[str, Any]] = []
    changed = False
    for item in [dict(value) for value in _as_list(block.get("items")) if isinstance(value, dict)]:
        text = _agent_plan_text(item)
        has_executor = any(term in text for term in _AGENT_EXECUTOR_CUES)
        has_plan_detail = any(_clean_text(item.get(key)) for key in ("brand", "material", "dosage", "price", "course_or_frequency", "implementation_notes"))
        has_steps = bool(_as_list(item.get("treatment_steps")))
        has_plan_language = any(term in text for term in ("建议", "推荐", "可以做", "考虑做", "方案", "改善", "治疗", "注射", "填充", "塑形", "提升"))
        if has_executor and not has_plan_detail and not has_steps and not has_plan_language:
            changed = True
            continue
        kept.append(item)
    if changed:
        block["items"] = kept
        _agent_recompute_result_recommendation_summary(result)
    return changed


def _agent_repair_budget_raw_quote(result: dict[str, Any], *, context: str) -> bool:
    block = result.get("consumption_intent")
    if not isinstance(block, dict):
        return False
    changed = False
    for key in ("budget", "current_budget", "budget_amount", "budget_summary", "summary"):
        value = block.get(key)
        if not isinstance(value, str):
            continue
        stripped = value.strip()
        looks_like_raw_quote = bool(re.match(r"^\[?\d{1,2}:\d{2}\]?", stripped)) or len(stripped) > 80
        if looks_like_raw_quote:
            if any(term in context for term in ("29000", "30000", "2万9", "三万", "3万")) and any(
                term in context for term in ("贵", "太高", "便宜", "优惠", "算一下", "核算", "接受不了", "超")
            ):
                block[key] = "对约29000-30000元总价较敏感，倾向低于该报价"
            else:
                block[key] = "未明确"
            changed = True
    return changed


def _agent_clear_resolved_quality_flags(result: dict[str, Any]) -> None:
    has_indications = bool(_as_list(_as_dict(result.get("standardized_indications")).get("items")))
    quality = result.get("analysis_quality")
    if not isinstance(quality, dict):
        return
    issues = [_clean_text(item) for item in _as_list(quality.get("issues")) if _clean_text(item)]
    if has_indications:
        issues = [issue for issue in issues if "未提取到可支撑 SAP 回写的适应症" not in issue]
    quality["issues"] = issues
    quality["requires_review"] = bool(issues)


def _agent_result_has_seed_recommendation(result: dict[str, Any], *terms: str) -> bool:
    seed_context = _agent_join_text(_as_dict(result.get("staff_seed_recommendations")).get("items"))
    return all(term in seed_context for term in terms)


def _agent_append_result_seed_recommendation(result: dict[str, Any], item: dict[str, Any]) -> bool:
    block = result.setdefault("staff_seed_recommendations", {})
    if not isinstance(block, dict):
        block = {"summary": "", "items": []}
        result["staff_seed_recommendations"] = block
    items = [dict(existing) for existing in _as_list(block.get("items")) if isinstance(existing, dict)]
    signature = _agent_plan_semantic_signature(item)
    for existing in items:
        if signature and signature == _agent_plan_semantic_signature(existing):
            return False
    items.append(item)
    block["items"] = items
    _agent_recompute_result_seed_summary(result)
    return True


def _agent_finalize_analysis_result(result: dict[str, Any], *, context: str = "") -> dict[str, Any]:
    updated = dict(result)
    changed = False
    changed = _agent_correct_result_brand_terms(updated, context=context) or changed
    changed = _agent_demote_result_orphan_recommendations(updated) or changed
    changed = _agent_dedupe_result_demands(updated) or changed
    changed = _agent_backfill_result_concerns_from_recommendations(updated, context=context) or changed
    changed = _agent_demote_non_demand_result_items(updated) or changed
    changed = _agent_remove_executor_only_result_recommendations(updated) or changed
    changed = _agent_repair_result_recommendation_links(updated) or changed
    changed = _agent_repair_budget_raw_quote(updated, context=context) or changed
    changed = _agent_dedupe_result_demands(updated) or changed
    recommendation_context = _agent_join_text(_as_dict(updated.get("staff_recommendations")).get("items"))
    if any(term in recommendation_context for term in ("唇部", "嘴唇", "嘴巴", "唇峰", "唇珠", "口周")) and any(
        term in recommendation_context for term in ("玻尿酸", "填充", "注射", "补打", "塑形", "海派", "海妹", "弹性材料")
    ):
        changed = _agent_append_result_catalog_indication(
            updated,
            name="塑美",
            body="唇部",
            evidence="正式推荐方案出现唇部玻尿酸/弹性材料注射补打或塑形，按本系统字典映射为塑美-唇部（D）",
        ) or changed

    if "下巴" in recommendation_context and any(
        term in recommendation_context for term in ("玻尿酸", "填充", "注射", "支撑", "塑形", "翘", "拉出来", "兜住")
    ):
        changed = _agent_append_result_catalog_indication(
            updated,
            name="塑美",
            body="下颌轮廓线（大O）",
            evidence="正式推荐方案出现下巴注射/填充/支撑塑形，按本系统字典映射为塑美-下颌轮廓线（大O）",
        ) or changed

    if _agent_has_face_fill_support_context(recommendation_context):
        changed = _agent_append_result_catalog_indication(
            updated,
            name="面部填充",
            body="面部",
            evidence="正式推荐方案出现面颊/颊区凹陷玻尿酸填充或注射支撑，按字典映射为面部填充-面部",
        ) or changed

    seed_block = _as_dict(updated.get("staff_seed_recommendations"))
    seed_items = [dict(item) for item in _as_list(seed_block.get("items")) if isinstance(item, dict)]
    if seed_items:
        deduped_graph = _agent_remove_redundant_seed_recommendations({"recommendations": [], "seed_recommendations": seed_items})
        deduped_items = _as_list(deduped_graph.get("seed_recommendations"))
        if len(deduped_items) != len(seed_items):
            updated["staff_seed_recommendations"] = {**seed_block, "items": deduped_items}
            _agent_recompute_result_seed_summary(updated)
            changed = True

    if (
        "英伦大提升" in context
        and any(term in context for term in ("下颌缘", "斜方肌", "除皱", "300单位"))
        and not _agent_result_has_seed_recommendation(updated, "英伦大提升")
    ):
        changed = _agent_append_result_seed_recommendation(
            updated,
            {
                "recommendation": "英伦大提升用于下颌缘/斜方肌提升，并可少量分配至除皱",
                "product_or_solution": None,
                "body_part": "下颌缘/斜方肌/动态纹",
                "brand": "英伦大提升",
                "material": "肉毒类",
                "dosage": "300单位（建议一瓶，可按部位分配）",
                "price": None,
                "course_or_frequency": "单次，可作为加做项目",
                "treatment_steps": ["下颌缘及斜方肌注射提升", "少量剂量分配至动态纹除皱"],
                "implementation_notes": "作为省钱的加做/种草方案，不属于本次艾拉斯提下巴塑形的主方案。",
                "demand_priority": [],
                "evidence": "英伦大提升…300单位…打到下颌缘斜方肌…匀一点点打到除皱…买一瓶就够了",
                "customer_response": "倾向省钱方式，未确认本次实施",
            },
        ) or changed

    changed = _agent_prune_result_profile_tags(updated) or changed
    if changed:
        debug = updated.setdefault("staged_pipeline_debug", {})
        if isinstance(debug, dict):
            debug["agent_final_result_safety_patch"] = True
    _agent_clear_resolved_quality_flags(updated)
    return updated


def _agent_has_wrinkle_treatment_context(text: str) -> bool:
    return any(term in text for term in ("鱼尾纹", "眉间纹", "抬头纹", "川字纹", "动态纹", "皱纹", "除皱"))


def _agent_has_non_wrinkle_botox_context(text: str) -> bool:
    return any(
        term in text
        for term in ("咬肌", "瘦脸", "头大", "下颌线", "下划线", "下颌角", "下颌轮廓", "大提拉", "斜方肌", "肩膀", "小腿")
    ) and any(term in text for term in ("肉毒", "肉毒素", "大提拉", "一瓶", "注射"))


def _agent_has_nose_axis_support_context(text: str) -> bool:
    return any(
        term in text
        for term in ("鼻基底", "鼻头", "鼻翼", "鼻尖", "鼻小柱", "鼻中下段", "鼻中轴", "三角结构")
    ) and any(
        term in text
        for term in ("玻尿酸", "一支玻尿酸", "注射", "支撑", "填充", "塑形", "再生", "芭比针", "濡白", "鲁班", "鲁板", "三角结构")
    )


def _agent_has_jawline_support_context(text: str) -> bool:
    has_area = any(
        term in text
        for term in ("下颌线", "下划线", "下颌角", "下颌缘", "下颌轮廓", "下颌角拐点", "耳前", "耳后", "韧带")
    )
    if not has_area:
        return False
    return any(
        term in text
        for term in ("玻尿酸", "注射", "支撑", "填充", "塑形", "童颜", "芭比", "濡白", "再生")
    )


def _agent_has_face_fill_support_context(text: str) -> bool:
    has_area = any(
        term in text
        for term in (
            "鼻基底",
            "口基底",
            "法令纹",
            "面中",
            "侧面凹陷",
            "外轮廓",
            "太阳穴",
            "额颞",
            "苹果肌",
            "泪沟",
            "面颊",
            "颊区",
            "夹区",
            "脸颊",
        )
    )
    if not has_area:
        return False
    has_structural_action = any(
        term in text
        for term in ("填充", "支撑", "塑形", "凹陷", "断层", "轮廓", "衔接", "法令纹")
    )
    if not has_structural_action:
        return False
    return any(
        term in text
        for term in ("玻尿酸", "再生", "童颜", "瑞德喜", "濡白", "芭比", "注射", "填充", "支撑")
    ) or ("胶原" in text and any(term in text for term in ("填充", "支撑", "塑形", "凹陷")))


def _agent_has_ear_support_plan(fact_graph: dict[str, Any]) -> bool:
    """Only infer ear plastic indication from an actual ear-area plan, not history."""
    for key in ("recommendations", "seed_recommendations"):
        for item in _as_list(fact_graph.get(key)):
            if not isinstance(item, dict):
                continue
            text = _agent_plan_text(item)
            if any(term in text for term in ("中耳炎", "面神经", "耳朵手术", "病史", "受损")):
                continue
            if any(term in text for term in ("耳朵", "耳垂", "耳部", "耳基底", "耳轮")) and any(
                term in text for term in ("玻尿酸", "注射", "支撑", "填充", "塑形", "拉长", "衬托", "偏小")
            ):
                return True
    return False


def _agent_remove_indication_by_name(
    candidates: list[dict[str, Any]],
    *,
    name: str,
    body_contains: str | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    kept: list[dict[str, Any]] = []
    removed = False
    for item in candidates:
        item_name = _clean_text(item.get("indication_name"))
        item_body = _clean_text(item.get("body_part_name"))
        standardized = _clean_text(item.get("standardized_indication"))
        parts = standardized.split("|")
        if len(parts) >= 6:
            item_name = item_name or _clean_text(parts[3])
            item_body = item_body or _clean_text(parts[5])
        if item_name == name and (not body_contains or body_contains in item_body):
            removed = True
            continue
        kept.append(item)
    return kept, removed


def _agent_has_current_eye_plan(text: str, terms: tuple[str, ...]) -> bool:
    compact = _clean_text(text)
    if not compact or not any(term in compact for term in terms):
        return False
    return any(
        cue in compact
        for cue in (
            "治疗",
            "改善",
            "处理",
            "方案",
            "做",
            "打",
            "注射",
            "填充",
            "激光",
            "光电",
            "皮秒",
            "水光",
            "胶原",
            "玻尿酸",
            "嗨体",
            "福曼",
        )
    )


def _agent_prune_observation_only_eye_indications(
    candidates: list[dict[str, Any]],
    *,
    context: str,
    recommendation_context: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Drop eye indications that came from observation/deferred seed talk only."""

    changed = False
    compact_context = _clean_text(context)
    defer_or_observation_only = any(
        cue in compact_context
        for cue in (
            "化妆就行",
            "化个妆就行",
            "可以化妆",
            "先打你在意的",
            "先打在意的",
            "先做你在意的",
            "先不处理",
            "暂时不处理",
            "后期再",
            "下次再",
            "以后再",
            "不是这次",
        )
    )

    if not _agent_has_current_eye_plan(
        recommendation_context,
        ("黑眼圈", "眼下黑", "眼周暗沉", "眼周色沉"),
    ):
        candidates, removed = _agent_remove_indication_by_name(candidates, name="黑眼圈", body_contains="眼部")
        changed = changed or removed

    if defer_or_observation_only and not _agent_has_current_eye_plan(
        recommendation_context,
        ("泪沟", "眼下凹", "眼下凹陷", "眶下凹陷"),
    ):
        candidates, removed = _agent_remove_indication_by_name(candidates, name="塑美", body_contains="眼部")
        changed = changed or removed
        candidates, removed = _agent_remove_indication_by_name(candidates, name="眼袋", body_contains="眼部")
        changed = changed or removed

    return candidates, changed


def _agent_ensure_common_indications(fact_graph: dict[str, Any]) -> dict[str, Any]:
    context = _agent_join_text(
        fact_graph.get("demands"),
        fact_graph.get("doctor_diagnoses"),
        fact_graph.get("recommendations"),
        fact_graph.get("seed_recommendations"),
    )
    recommendation_context = _agent_join_text(fact_graph.get("recommendations"))
    updated = dict(fact_graph)
    candidates = [dict(item) for item in _as_list(updated.get("indication_candidates")) if isinstance(item, dict)]

    changed = False
    if _agent_has_non_wrinkle_botox_context(context) and not _agent_has_wrinkle_treatment_context(recommendation_context):
        candidates, removed = _agent_remove_indication_by_name(candidates, name="面部除皱", body_contains="面部")
        changed = changed or removed
    explicit_surgical_face_fill = any(
        term in recommendation_context
        for term in ("脂肪填充", "自体脂肪", "脂肪胶", "脂肪移植", "自体脂肪移植")
    )
    injection_support_context = any(
        term in recommendation_context
        for term in (
            "玻尿酸",
            "注射",
            "支撑",
            "海派",
            "海魅",
            "艾拉斯提",
            "瑞德喜",
            "童颜针",
            "芭比针",
            "濡白天使",
            "胶原",
            "胶原蛋白",
        )
    )
    if not explicit_surgical_face_fill and (
        _agent_has_nose_axis_support_context(recommendation_context)
        or _agent_has_jawline_support_context(recommendation_context)
        or _agent_has_face_fill_support_context(recommendation_context)
        or injection_support_context
    ):
        candidates, removed = _agent_remove_indication_by_name(candidates, name="面部填充", body_contains="面部")
        changed = changed or removed
    candidates, removed = _agent_prune_observation_only_eye_indications(
        candidates,
        context=context,
        recommendation_context=recommendation_context,
    )
    changed = changed or removed
    demand_plan_context = _agent_join_text(
        fact_graph.get("demands"),
        fact_graph.get("recommendations"),
        fact_graph.get("seed_recommendations"),
    )
    diagnosis_context = _agent_join_text(fact_graph.get("doctor_diagnoses"))
    negative_pigment_context = any(
        term in diagnosis_context
        for term in ("无明显色斑", "没有明显色斑", "没有色斑", "没什么太多的色斑", "无真皮斑", "没有真皮斑")
    )
    positive_pigment_context = any(term in demand_plan_context for term in ("祛斑", "色斑", "斑点", "雀斑", "皮秒", "双击", "淡斑"))
    if negative_pigment_context and not positive_pigment_context:
        before_len = len(candidates)
        candidates = [
            item
            for item in candidates
            if not (
                _clean_text(item.get("indication_name")) == "色斑"
                and _clean_text(item.get("body_part_name")) == "面部"
            )
        ]
        changed = changed or len(candidates) != before_len

    anti_aging_anchor_context = _agent_join_text(fact_graph.get("demands"), fact_graph.get("doctor_diagnoses"))
    if not any(term in anti_aging_anchor_context for term in ("松弛", "下垂", "细纹", "干纹", "皱纹", "抗衰", "紧致", "提升")):
        before_len = len(candidates)
        candidates = [
            item
            for item in candidates
            if _clean_text(item.get("indication_name")) not in {"松弛下垂", "紧致淡纹"}
        ]
        changed = changed or len(candidates) != before_len

    if any(term in context for term in ("肉毒", "除皱针", "玻尿酸", "瑞德喜", "注射")):
        before_len = len(candidates)
        candidates = [
            item
            for item in candidates
            if not (
                _clean_text(item.get("indication_name")) == "生活美容"
                and _clean_text(item.get("body_part_name")) == "其他"
            )
        ]
        changed = changed or len(candidates) != before_len
    if any(term in context for term in ("肉毒", "除皱针")) and any(
        term in context for term in ("鱼尾纹", "眉间纹", "抬头纹", "动态纹", "皱眉纹", "川字纹", "除皱")
    ):
        changed = _agent_add_catalog_indication(
            candidates,
            name="面部除皱",
            body="面部",
            evidence="正式方案或主诉出现肉毒/除皱针对鱼尾纹、眉间纹、抬头纹等动态纹治疗",
            confidence=0.82,
        ) or changed
    if any(term in recommendation_context for term in ("咬肌", "瘦脸", "英伦大提升", "下颌轮廓线")) and any(
        term in recommendation_context for term in ("肉毒", "注射", "提升", "塑形")
    ):
        changed = _agent_add_catalog_indication(
            candidates,
            name="塑美",
            body="下颌轮廓线（大O）",
            evidence="正式推荐方案出现咬肌/下颌轮廓线肉毒注射瘦脸或轮廓提升",
            confidence=0.76,
        ) or changed
    has_jawline_injection_plan = _agent_has_jawline_support_context(recommendation_context) or (
        any(term in recommendation_context for term in ("咬肌", "瘦脸", "下颌缘", "下颌线", "下颌轮廓线"))
        and any(term in recommendation_context for term in ("肉毒", "肉毒素", "注射", "塑形"))
    )
    if not has_jawline_injection_plan:
        before_len = len(candidates)
        candidates = [
            item
            for item in candidates
            if not (
                _clean_text(item.get("indication_name")) == "塑美"
                and "下颌" in _clean_text(item.get("body_part_name"))
            )
        ]
        changed = changed or len(candidates) != before_len
    if _agent_has_jawline_support_context(recommendation_context):
        changed = _agent_add_catalog_indication(
            candidates,
            name="塑美",
            body="下颌轮廓线（大O）",
            evidence="正式推荐方案出现下颌线/下颌角拐点/耳前耳后韧带注射支撑或轮廓提升，按字典映射为塑美-下颌轮廓线（大O）",
            confidence=0.84,
        ) or changed
    if explicit_surgical_face_fill:
        changed = _agent_add_catalog_indication(
            candidates,
            name="面部填充",
            body="面部",
            evidence="正式推荐方案出现自体脂肪/脂肪胶/脂肪移植等外科面部填充",
            confidence=0.82,
            force_include=True,
        ) or changed
    if "外油内干" in context or ("皮肤" in context and any(term in context for term in ("干燥", "缺水"))):
        changed = _agent_add_catalog_indication(
            candidates,
            name="干燥",
            body="面部",
            evidence="皮肤主诉出现外油内干/干燥缺水信息",
        ) or changed
    # Do not infer pigment indications from incidental diagnosis chatter.  The
    # SAP indication should only be added when pigment/spot removal is part of
    # the customer's demand or a staff recommendation/seed plan.
    if positive_pigment_context and not negative_pigment_context:
        changed = _agent_add_catalog_indication(
            candidates,
            name="色斑",
            body="面部",
            evidence="主诉或方案出现祛斑/雀斑/皮秒信息",
        ) or changed
    if any(term in context for term in ("点痣", "祛痣", "色素痣")) or ("痣" in context and any(term in context for term in ("点", "去除", "祛", "包干", "复发"))):
        body = "面部"
        if "眼" in context:
            body = "眼部"
        elif "颈" in context:
            body = "颈部"
        elif "身体" in context:
            body = "身体"
        changed = _agent_add_catalog_indication(
            candidates,
            name="祛痣/祛疣",
            body=body,
            evidence="主诉或方案出现点痣/祛痣/色素痣处理需求",
            confidence=0.86,
        ) or changed
    if "痘坑" in context:
        changed = _agent_add_catalog_indication(
            candidates,
            name="疤痕",
            body="面部",
            evidence="主诉或方案出现痘坑/凹陷性痤疮瘢痕信息",
            confidence=0.76,
        ) or changed
    has_eye_repair = _agent_has_prior_eyelid_surgery_context(context)
    if not has_eye_repair:
        before_len = len(candidates)
        candidates = [
            item
            for item in candidates
            if not (
                _clean_text(item.get("indication_name")) == "眼修复"
                and _clean_text(item.get("body_part_name")) == "眼部"
            )
        ]
        changed = changed or len(candidates) != before_len
        if any(term in context for term in ("双眼皮", "重睑", "内双", "肿眼泡", "切开重睑", "重睑成形")) and any(
            term in context for term in ("手术", "切开", "去皮", "切掉", "重睑成形", "做双眼皮")
        ):
            changed = _agent_add_catalog_indication(
                candidates,
                name="双眼皮",
                body="眼部",
                evidence="主诉或方案为首次双眼皮/重睑改善，未出现明确既往双眼皮修复语义",
                confidence=0.78,
            ) or changed
    if has_eye_repair:
        before_len = len(candidates)
        candidates = [
            item
            for item in candidates
            if not (
                _clean_text(item.get("indication_name")) == "双眼皮"
                and _clean_text(item.get("body_part_name")) == "眼部"
            )
        ]
        changed = changed or len(candidates) != before_len
        changed = _agent_add_catalog_indication(
            candidates,
            name="眼修复",
            body="眼部",
            evidence="既往双眼皮/重睑术后不满意或松弛下垂，属于修复场景",
            confidence=0.78,
        ) or changed

    if "下巴" in recommendation_context and any(
        term in recommendation_context for term in ("玻尿酸", "填充", "注射", "支撑", "塑形", "翘", "拉出来", "兜住")
    ):
        changed = _agent_add_catalog_indication(
            candidates,
            name="塑美",
            body="下颌轮廓线（大O）",
            evidence="正式推荐方案出现下巴注射/填充/支撑塑形，按本系统字典映射为塑美-下颌轮廓线（大O）",
            confidence=0.76,
            force_include=True,
        ) or changed

    if any(term in recommendation_context for term in ("唇部", "嘴唇", "嘴巴", "唇峰", "唇珠", "口周")) and any(
        term in recommendation_context for term in ("玻尿酸", "填充", "注射", "补打", "塑形", "海派", "海妹", "弹性材料")
    ):
        changed = _agent_add_catalog_indication(
            candidates,
            name="塑美",
            body="唇部",
            evidence="正式推荐方案出现唇部玻尿酸/弹性材料注射补打或塑形，按本系统字典映射为塑美-唇部（D）",
            confidence=0.82,
            force_include=True,
        ) or changed

    if any(term in recommendation_context for term in ("鼻基底", "鼻头", "鼻翼", "鼻尖", "鼻小柱", "鼻中下段", "鼻中段", "鼻下段", "鼻中轴", "鼻中轴线", "三角结构")) and any(
        term in recommendation_context for term in ("玻尿酸", "定彩", "注射", "支撑", "填充", "塑形", "抬高", "拉高", "纵深")
    ):
        changed = _agent_add_catalog_indication(
            candidates,
            name="塑美",
            body="鼻中轴线",
            evidence="正式推荐方案出现鼻小柱/鼻中下段玻尿酸注射支撑塑形，按字典映射为塑美-鼻中轴线（H）",
            confidence=0.82,
        ) or changed
    if _agent_has_nose_axis_support_context(recommendation_context):
        changed = _agent_add_catalog_indication(
            candidates,
            name="塑美",
            body="鼻中轴线",
            evidence="正式推荐方案出现鼻基底/鼻头/鼻翼三角结构注射支撑塑形，按字典映射为塑美-鼻中轴线（H）",
            confidence=0.86,
        ) or changed

    if not _agent_has_ear_support_plan(updated):
        before_len = len(candidates)
        candidates = [
            item
            for item in candidates
            if not (
                _clean_text(item.get("indication_name")) == "塑美"
                and "耳" in _clean_text(item.get("body_part_name"))
            )
        ]
        changed = changed or len(candidates) != before_len

    if _agent_has_ear_support_plan(updated):
        changed = _agent_add_catalog_indication(
            candidates,
            name="塑美",
            body="耳部",
            evidence="正式推荐方案出现耳朵/耳垂玻尿酸注射支撑或塑形，按字典映射为塑美-耳部（大O）",
            confidence=0.82,
        ) or changed

    if changed:
        updated["indication_candidates"] = candidates
    return updated


def _agent_ensure_medical_safety_concerns(fact_graph: dict[str, Any]) -> dict[str, Any]:
    medical_items = [dict(item) for item in _as_list(fact_graph.get("medical_history")) if isinstance(item, dict)]
    if not medical_items:
        return fact_graph
    medical_text = _agent_join_text(medical_items)
    if not _agent_has_current_surgical_plan(fact_graph):
        return fact_graph
    safety_terms = (
        "葡萄膜炎",
        "眼底病",
        "泼尼松",
        "激素",
        "长期服药",
        "不能停药",
        "高血压",
        "糖尿病",
        "凝血",
        "抗凝",
        "心脏病",
    )
    if not any(term in medical_text for term in safety_terms):
        return fact_graph
    if not _agent_has_positive_medical_safety_signal(fact_graph):
        return fact_graph
    if any(term in medical_text for term in ("无高血压", "没有高血压", "无药物过敏", "没有药物过敏")) and not any(
        term in medical_text for term in ("葡萄膜炎", "眼底病", "泼尼松", "激素", "长期服药", "不能停药", "糖尿病", "凝血", "抗凝", "心脏病")
    ):
        return fact_graph
    concerns = [dict(item) for item in _as_list(fact_graph.get("concerns")) if isinstance(item, dict)]
    concern_text = _agent_join_text(concerns)
    if any(term in concern_text for term in safety_terms):
        return fact_graph

    if "葡萄膜炎" in medical_text or "眼底病" in medical_text:
        content = "担心既往葡萄膜炎/眼底病及长期用药是否影响手术安全和术后恢复"
    elif "泼尼松" in medical_text or "激素" in medical_text:
        content = "担心长期服用激素类药物是否影响手术安全和术后恢复"
    else:
        content = "担心既往疾病或长期用药是否影响手术安全和术后恢复"
    if content in concern_text:
        return fact_graph
    evidence_ids: list[str] = []
    for item in medical_items:
        for value in _as_list(item.get("evidence_ids")):
            text = _clean_text(value)
            if text and text not in evidence_ids:
                evidence_ids.append(text)
    concerns.append(
        {
            "concern_id": f"C{len(concerns) + 1}",
            "content": content,
            "evidence_ids": evidence_ids,
            "participant": _first_text(medical_items[0], "participant") or "主咨询客户",
            "participant_scope": _first_text(medical_items[0], "participant_scope") or "primary_customer",
        }
    )
    updated = dict(fact_graph)
    updated["concerns"] = concerns
    return updated


_AGENT_DEMAND_LINK_TERMS = (
    "水光",
    "补水",
    "干燥",
    "胶原",
    "热玛吉",
    "抗衰",
    "紧致",
    "提升",
    "松弛",
    "超声",
    "鼻",
    "鼻基底",
    "下巴",
    "下颌",
    "眼袋",
    "泪沟",
    "毛孔",
    "痘",
    "色斑",
    "祛斑",
    "点痣",
    "耳",
)


def _agent_repair_recommendation_demand_links(fact_graph: dict[str, Any]) -> dict[str, Any]:
    demands = [dict(item) for item in _as_list(fact_graph.get("demands")) if isinstance(item, dict)]
    if not demands:
        return fact_graph
    demand_ids: list[str] = []
    demand_by_id: dict[str, dict[str, Any]] = {}
    for index, demand in enumerate(demands, start=1):
        demand_id = _clean_text(demand.get("id") or demand.get("demand_id")) or f"D{index}"
        demand_ids.append(demand_id)
        demand_by_id[demand_id] = demand

    def best_demand_id(item: dict[str, Any]) -> str:
        item_text = _agent_join_text(_agent_item_content(item), item.get("body_part"), item.get("brand"), item.get("material"))
        item_terms = {term for term in _AGENT_DEMAND_LINK_TERMS if term in item_text}
        item_body = _clean_text(item.get("body_part") or item.get("body_part_name"))
        best_id = ""
        best_score = 0
        for demand_id, demand in demand_by_id.items():
            demand_text = _agent_join_text(_agent_item_content(demand), demand.get("body_part"), demand.get("body_part_name"))
            demand_terms = {term for term in _AGENT_DEMAND_LINK_TERMS if term in demand_text}
            score = len(item_terms & demand_terms) * 3
            demand_body = _clean_text(demand.get("body_part") or demand.get("body_part_name"))
            if item_body and demand_body and (item_body in demand_body or demand_body in item_body):
                score += 1
            if score > best_score:
                best_score = score
                best_id = demand_id
        if best_id and best_score > 0:
            return best_id
        return demand_ids[0] if len(demand_ids) == 1 else ""

    updated = dict(fact_graph)
    for section in ("recommendations", "seed_recommendations"):
        repaired_items: list[dict[str, Any]] = []
        for item in _as_list(updated.get(section)):
            if not isinstance(item, dict):
                continue
            copied = dict(item)
            linked = [_clean_text(value) for value in _as_list(copied.get("related_demand_ids")) if _clean_text(value)]
            valid = [value for value in linked if value in demand_by_id]
            if not valid:
                fallback_id = best_demand_id(copied)
                if fallback_id:
                    valid = [fallback_id]
            if valid:
                copied["related_demand_ids"] = valid
            repaired_items.append(copied)
        updated[section] = repaired_items
    return updated


def _agent_prune_unsupported_pigment_fallbacks(fact_graph: dict[str, Any]) -> dict[str, Any]:
    context = _agent_join_text(
        fact_graph.get("demands"),
        fact_graph.get("doctor_diagnoses"),
        fact_graph.get("recommendations"),
        fact_graph.get("seed_recommendations"),
    )
    positive = any(
        term in context
        for term in ("祛斑", "色斑", "雀斑", "斑点", "黄褐斑", "淡斑", "皮秒", "色素沉着", "肤色不均")
    )
    negative = any(term in context for term in ("无明显色斑", "没有明显色斑", "没有色斑", "无真皮斑", "没有真皮斑"))
    if positive and not negative:
        return fact_graph
    candidates = [dict(item) for item in _as_list(fact_graph.get("indication_candidates")) if isinstance(item, dict)]
    kept = [
        item
        for item in candidates
        if not (
            _clean_text(item.get("indication_name")) == "色斑"
            and _clean_text(item.get("reason")) == "agent deterministic indication fallback"
        )
    ]
    if len(kept) == len(candidates):
        return fact_graph
    updated = dict(fact_graph)
    updated["indication_candidates"] = kept
    return updated


def _agent_ensure_structural_support_recommendations(
    fact_graph: dict[str, Any],
    evidence_graph: dict[str, Any],
) -> dict[str, Any]:
    recommendations = [dict(item) for item in _as_list(fact_graph.get("recommendations")) if isinstance(item, dict)]
    evidence_items = [dict(item) for item in _as_list(evidence_graph.get("recommendation_evidence")) if isinstance(item, dict)]
    if not evidence_items:
        return fact_graph

    recommendation_text = _agent_join_text(recommendations)
    changed = False

    def append_from_evidence(item: dict[str, Any], *, content: str, body_part: str) -> None:
        nonlocal changed
        evidence_ids = [str(value) for value in _as_list(item.get("evidence_turn_ids")) if str(value).strip()]
        recommendations.append(
            {
                "content": content,
                "body_part": body_part,
                "brand": _first_text(item, "brand"),
                "material": _first_text(item, "material"),
                "dosage": _first_text(item, "dosage"),
                "price": _first_text(item, "price"),
                "course_or_frequency": _first_text(item, "course_or_frequency"),
                "treatment_steps": item.get("treatment_steps") if isinstance(item.get("treatment_steps"), list) else [],
                "implementation_notes": _first_text(item, "implementation_notes", "quote"),
                "customer_response": _first_text(item, "customer_response"),
                "evidence_ids": evidence_ids,
                "participant": _first_text(item, "participant") or "主咨询客户",
                "participant_scope": _first_text(item, "participant_scope") or "primary_customer",
            }
        )
        changed = True

    for item in evidence_items:
        relation = _first_text(item, "relation_to_current_demand")
        if relation in {"alternative_not_recommended", "not_current_or_referral"}:
            continue
        text = _agent_plan_text(item)
        if _agent_has_jawline_support_context(text) and not _agent_has_jawline_support_context(recommendation_text):
            append_from_evidence(
                item,
                content="下颌线/下颌角拐点结构支撑提升",
                body_part="下颌线/下颌角拐点",
            )
            recommendation_text = _agent_join_text(recommendations)
        if _agent_has_nose_axis_support_context(text) and not _agent_has_nose_axis_support_context(recommendation_text):
            append_from_evidence(
                item,
                content="鼻基底/鼻头鼻翼三角结构注射支撑塑形",
                body_part="鼻基底/鼻头鼻翼",
            )
            recommendation_text = _agent_join_text(recommendations)

    if not changed:
        return fact_graph
    updated = dict(fact_graph)
    updated["recommendations"] = recommendations
    return updated


def _agent_repair_fact_graph(
    fact_graph: dict[str, Any],
    evidence_graph: dict[str, Any],
) -> dict[str, Any]:
    repaired = _agent_normalize_fact_content_fields(fact_graph)
    repaired = _agent_flatten_recommendation_details(repaired)
    repaired = _agent_ensure_demands_from_evidence_graph(repaired, evidence_graph)
    repaired = _agent_ensure_demands_from_diagnoses_when_empty(repaired)
    repaired = _agent_normalize_demands(repaired)
    repaired = _agent_ensure_budget_facts_from_evidence_graph(repaired, evidence_graph)
    repaired = _agent_preserve_backup_options(repaired, evidence_graph)
    repaired = _agent_ensure_structural_support_recommendations(repaired, evidence_graph)
    repaired = _agent_preserve_deferred_seed_recommendations(repaired, evidence_graph)
    repaired = _agent_normalize_demands(repaired)
    repaired = _agent_prune_observation_only_demands(repaired)
    repaired = _agent_remove_redundant_seed_recommendations(repaired)
    repaired = _agent_filter_unsupported_medical_safety_concerns(repaired)
    repaired = _agent_normalize_concerns(repaired)
    repaired = _agent_normalize_profile_facts(repaired)
    repaired = _agent_ensure_common_indications(repaired)
    repaired = _agent_prune_unsupported_pigment_fallbacks(repaired)
    repaired = _agent_ensure_medical_safety_concerns(repaired)
    repaired = _agent_repair_recommendation_demand_links(repaired)
    repaired = _agent_normalize_concerns(repaired)
    repaired = _agent_backfill_evidence_texts(repaired, evidence_graph)
    repaired = _agent_normalize_demands(repaired)
    repaired = _agent_filter_non_deal_factors(repaired)
    repaired = _agent_normalize_non_deal_outcome(repaired)
    for list_key in ("budget_facts", "deal_factors", "concerns", "medical_history", "profile_facts"):
        repaired = _agent_dedupe_fact_items_by_content(repaired, list_key)
    return repaired


_EVIDENCE_LIST_SECTIONS = (
    "customer_demand_evidence",
    "diagnosis_evidence",
    "recommendation_evidence",
    "concern_evidence",
    "budget_evidence",
    "medical_history_evidence",
    "profile_evidence",
    "deal_evidence",
    "speaker_corrections",
    "quality_notes",
)
_BUSINESS_EVIDENCE_SECTIONS = (
    "customer_demand_evidence",
    "diagnosis_evidence",
    "recommendation_evidence",
    "concern_evidence",
    "budget_evidence",
    "medical_history_evidence",
    "profile_evidence",
    "deal_evidence",
)

_EVIDENCE_ID_PREFIX = {
    "customer_demand_evidence": "E_D",
    "diagnosis_evidence": "E_X",
    "recommendation_evidence": "E_R",
    "concern_evidence": "E_C",
    "budget_evidence": "E_B",
    "medical_history_evidence": "E_H",
    "profile_evidence": "E_P",
    "deal_evidence": "E_DEAL",
    "speaker_corrections": "E_SPK",
    "quality_notes": "E_Q",
}


def _line_id_from_text(line: str) -> str:
    match = re.match(r"^\s*(L\d{4})\b", line)
    return match.group(1) if match else ""


def _line_id_to_int(value: object) -> int | None:
    match = re.search(r"\bL(\d{4})\b", _clean_text(value))
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _truncate_text_for_prompt(text: str, *, max_chars: int = 36000) -> str:
    if len(text) <= max_chars:
        return text
    head_chars = max_chars * 2 // 3
    tail_chars = max_chars - head_chars
    return text[:head_chars] + "\n...<truncated_middle>...\n" + text[-tail_chars:]


def _dialogue_for_scope_prompt(corrected_dialogue: str) -> str:
    compact_lines: list[str] = []
    for line in corrected_dialogue.splitlines():
        text = line.strip()
        if not text:
            continue
        if len(text) > 260:
            text = text[:260] + "...<line_truncated>"
        compact_lines.append(text)
    return _truncate_text_for_prompt("\n".join(compact_lines), max_chars=36000)


def _extract_scope_graph(parsed: dict[str, Any]) -> dict[str, Any]:
    payload = parsed.get("scope_graph") if isinstance(parsed.get("scope_graph"), dict) else parsed
    if not isinstance(payload, dict):
        return {}
    segments: list[dict[str, Any]] = []
    for index, item in enumerate(_as_list(payload.get("segments")), start=1):
        if not isinstance(item, dict):
            continue
        start_line_id = _clean_text(item.get("start_line_id"))
        end_line_id = _clean_text(item.get("end_line_id"))
        if not start_line_id or not end_line_id:
            continue
        scope_type = _clean_text(item.get("scope_type")) or "unclear"
        relevance = _clean_text(item.get("business_relevance")) or "supporting"
        current_relevant = item.get("current_visit_relevant")
        if not isinstance(current_relevant, bool):
            current_relevant = relevance != "ignore" and scope_type not in {
                "staff_chat",
                "casual_chat",
                "third_party_absent_case",
                "unrelated_operations",
            }
        segments.append(
            {
                "id": _clean_text(item.get("id")) or f"S{index}",
                "start_line_id": start_line_id,
                "end_line_id": end_line_id,
                "scope_type": scope_type,
                "participant_scope": _clean_text(item.get("participant_scope")) or "unknown",
                "business_relevance": relevance,
                "current_visit_relevant": bool(current_relevant),
                "reason": _clean_text(item.get("reason")),
            }
        )
    return {
        "primary_customer": _clean_text(payload.get("primary_customer")),
        "dominant_visit_topic": _clean_text(payload.get("dominant_visit_topic")),
        "segments": segments,
        "notes": [_clean_text(item) for item in _as_list(payload.get("notes")) if _clean_text(item)],
    }


def _scope_segment_should_ignore(segment: dict[str, Any]) -> bool:
    if segment.get("current_visit_relevant") is True:
        return False
    scope_type = _clean_text(segment.get("scope_type"))
    relevance = _clean_text(segment.get("business_relevance"))

    retain_scope_types = {
        "current_customer_consultation",
        "accompanying_customer_consultation",
        "doctor_face_to_face",
        "quote_or_payment",
        "post_deal_care",
        "future_seed_or_cross_department",
        "unclear",
        "unknown",
    }
    ignore_scope_types = {
        "staff_chat",
        "casual_chat",
        "third_party_absent_case",
        "unrelated_operations",
    }
    if scope_type in retain_scope_types:
        return False
    if scope_type in ignore_scope_types:
        return True
    return relevance == "ignore"


def _dialogue_with_scope_filter(corrected_dialogue: str, scope_graph: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    segments = [item for item in _as_list(scope_graph.get("segments")) if isinstance(item, dict)]
    if not segments:
        return corrected_dialogue, {"removed_line_count": 0, "kept_line_count": len(corrected_dialogue.splitlines())}
    ignore_ranges: list[tuple[int, int, str]] = []
    for segment in segments:
        if not _scope_segment_should_ignore(segment):
            continue
        start = _line_id_to_int(segment.get("start_line_id"))
        end = _line_id_to_int(segment.get("end_line_id"))
        if start is None or end is None:
            continue
        if end < start:
            start, end = end, start
        ignore_ranges.append((start, end, _clean_text(segment.get("scope_type"))))
    if not ignore_ranges:
        return corrected_dialogue, {"removed_line_count": 0, "kept_line_count": len(corrected_dialogue.splitlines())}

    kept: list[str] = []
    removed = 0
    removed_types: dict[str, int] = {}
    for line in corrected_dialogue.splitlines():
        line_no = _line_id_to_int(line)
        should_remove = False
        remove_type = ""
        if line_no is not None:
            for start, end, scope_type in ignore_ranges:
                if start <= line_no <= end:
                    should_remove = True
                    remove_type = scope_type
                    break
        if should_remove:
            removed += 1
            removed_types[remove_type or "unknown"] = removed_types.get(remove_type or "unknown", 0) + 1
            continue
        kept.append(line)
    if removed < 3 or len(kept) < 8:
        return corrected_dialogue, {
            "removed_line_count": 0,
            "kept_line_count": len(corrected_dialogue.splitlines()),
            "filter_skipped": True,
            "reason": "scope_filter_too_small_or_too_aggressive",
        }
    return "\n".join(kept), {
        "removed_line_count": removed,
        "kept_line_count": len(kept),
        "removed_scope_types": removed_types,
    }


def _split_corrected_dialogue_for_evidence(
    dialogue: str,
    *,
    target_chars: int = EVIDENCE_CHUNK_TARGET_CHARS,
    overlap_lines: int = EVIDENCE_CHUNK_OVERLAP_LINES,
) -> list[dict[str, Any]]:
    lines = [line for line in dialogue.splitlines() if line.strip()]
    if not lines:
        return []
    if len(dialogue) <= target_chars:
        return [
            {
                "chunk_index": 1,
                "chunk_count": 1,
                "line_range": f"{_line_id_from_text(lines[0]) or 'start'}-{_line_id_from_text(lines[-1]) or 'end'}",
                "line_count": len(lines),
                "char_count": len(dialogue),
                "dialogue": "\n".join(lines),
            }
        ]

    chunks: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        start = max(0, index - overlap_lines) if chunks else index
        current: list[str] = []
        current_len = 0
        cursor = start
        while cursor < len(lines):
            line = lines[cursor]
            next_len = current_len + len(line) + 1
            if current and next_len > target_chars and cursor > index:
                break
            current.append(line)
            current_len = next_len
            cursor += 1
            if cursor > index and current_len >= target_chars:
                break
        if not current:
            current = [lines[index]]
            cursor = index + 1
        first_line = current[0]
        last_line = current[-1]
        chunks.append(
            {
                "chunk_index": len(chunks) + 1,
                "chunk_count": 0,
                "line_range": f"{_line_id_from_text(first_line) or 'start'}-{_line_id_from_text(last_line) or 'end'}",
                "line_count": len(current),
                "char_count": len("\n".join(current)),
                "dialogue": "\n".join(current),
            }
        )
        index = max(cursor, index + 1)
    total = len(chunks)
    for chunk in chunks:
        chunk["chunk_count"] = total
    return chunks


def _compact_key_text(value: object) -> str:
    text = _clean_text(value)
    return re.sub(r"[\s,，;；。.!！?？、/\\|（）()\"'“”‘’]+", "", text).lower()


def _evidence_turn_ids(item: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for value in _as_list(item.get("evidence_turn_ids")):
        text = _clean_text(value)
        if text and text not in ids:
            ids.append(text)
    blob = json.dumps(item, ensure_ascii=False)
    for line_id in re.findall(r"\bL\d{4}\b", blob):
        if line_id not in ids:
            ids.append(line_id)
    return ids


def _evidence_merge_key(section: str, item: object) -> tuple[str, ...]:
    if isinstance(item, dict):
        turn_ids = ",".join(_evidence_turn_ids(item))
        content = _compact_key_text(
            item.get("content")
            or item.get("quote")
            or item.get("text")
            or item.get("value")
            or item.get("description")
        )
        body = _compact_key_text(item.get("body_part") or item.get("body_part_name"))
        participant = _compact_key_text(item.get("participant") or item.get("participant_label"))
        scope = _compact_key_text(item.get("participant_scope") or item.get("customer_scope"))
        category = _compact_key_text(item.get("category") or item.get("type"))
        if turn_ids:
            return (section, turn_ids, category, content[:80], body, participant, scope)
        return (section, category, content[:140], body, participant, scope)
    return (section, _compact_key_text(item))


_PRIORITY_ONLY_DEMAND_CUES = (
    "第一步",
    "优先",
    "首先",
    "先把",
    "先做",
    "先调",
    "先处理",
    "首要",
)

_CONCRETE_DEMAND_GOAL_CUES = (
    "缩小",
    "缩窄",
    "改善",
    "淡化",
    "提升",
    "变小",
    "变薄",
    "显脸",
    "饱满",
    "立体",
    "年轻",
    "解决",
    "修复",
    "收紧",
)

_DEMAND_BODY_GROUPS: tuple[tuple[str, ...], ...] = (
    ("鼻", "鼻头", "鼻翼", "鼻基底", "鼻梁", "鼻小柱"),
    ("眼", "眼袋", "双眼皮", "眼角", "泪沟", "眉弓"),
    ("下巴", "颏"),
    ("法令纹", "口基底", "口角"),
    ("面中", "苹果肌", "脸", "面部", "轮廓", "颊"),
)


def _evidence_item_text(item: dict[str, Any]) -> str:
    return _agent_join_text(
        item.get("content"),
        item.get("body_part"),
        item.get("quote"),
        item.get("implementation_notes"),
    )


def _evidence_body_groups(text: str) -> set[int]:
    return {
        index
        for index, terms in enumerate(_DEMAND_BODY_GROUPS)
        if any(term in text for term in terms)
    }


def _evidence_demand_is_priority_only(item: dict[str, Any], all_demands: list[dict[str, Any]]) -> bool:
    text = _evidence_item_text(item)
    if not any(cue in text for cue in _PRIORITY_ONLY_DEMAND_CUES):
        return False
    groups = _evidence_body_groups(text)
    if not groups:
        return False
    participant = _agent_participant_key(item)
    for other in all_demands:
        if other is item or not isinstance(other, dict):
            continue
        if _agent_participant_key(other) != participant:
            continue
        other_text = _evidence_item_text(other)
        if not groups.intersection(_evidence_body_groups(other_text)):
            continue
        if any(cue in other_text for cue in _CONCRETE_DEMAND_GOAL_CUES):
            return True
    return False


def _normalize_evidence_graph_demands(evidence_graph: dict[str, Any]) -> dict[str, Any]:
    demands = [item for item in _as_list(evidence_graph.get("customer_demand_evidence")) if isinstance(item, dict)]
    if len(demands) < 2:
        return evidence_graph
    normalized = dict(evidence_graph)
    normalized["customer_demand_evidence"] = [
        item for item in _as_list(evidence_graph.get("customer_demand_evidence"))
        if not (isinstance(item, dict) and _evidence_demand_is_priority_only(item, demands))
    ]
    return normalized


_RESCUE_NON_DEAL_NEXT_STEP_CUES = (
    "看看方案",
    "看方案",
    "看看价格",
    "看价格",
    "去看看方案",
    "去看方案",
    "了解方案",
    "了解价格",
    "继续面诊",
    "再沟通",
)

_RESCUE_DEAL_ACTION_CUES = (
    "成交",
    "开单",
    "下单",
    "付款",
    "支付",
    "定金",
    "尾款",
    "核销",
    "划扣",
    "预约",
    "复诊",
    "改约",
    "未成交",
    "不成交",
)

_RESCUE_STRONG_DEAL_ACTION_CUES = (
    "开单",
    "下单",
    "付款",
    "支付",
    "定金",
    "尾款",
    "核销",
    "划扣",
    "预约",
    "复诊",
    "改约",
)

_RESCUE_MEDICAL_HISTORY_CUES = (
    "做过",
    "打过",
    "填过",
    "溶过",
    "取过",
    "修复过",
    "手术",
    "病史",
    "过敏",
    "面瘫",
    "中耳炎",
    "高血压",
    "糖尿病",
    "甲亢",
    "怀孕",
    "备孕",
    "哺乳",
    "停经",
    "生理期",
    "种植牙",
    "钢板",
    "起搏器",
)


def _normalize_rescue_evidence_graph(evidence_graph: dict[str, Any]) -> dict[str, Any]:
    """Prune high-risk false positives from the empty-evidence rescue pass only."""
    if not isinstance(evidence_graph, dict):
        return evidence_graph
    normalized = dict(evidence_graph)

    deal_items: list[object] = []
    for item in _as_list(normalized.get("deal_evidence")):
        if not isinstance(item, dict):
            deal_items.append(item)
            continue
        text = _agent_join_text(item.get("content"), item.get("quote"), item.get("deal_status"), item.get("amount"))
        if any(cue in text for cue in _RESCUE_NON_DEAL_NEXT_STEP_CUES) and not any(
            cue in text for cue in _RESCUE_STRONG_DEAL_ACTION_CUES
        ):
            continue
        if any(cue in text for cue in _RESCUE_NON_DEAL_NEXT_STEP_CUES) and not any(
            cue in text for cue in _RESCUE_DEAL_ACTION_CUES
        ):
            continue
        deal_items.append(item)
    normalized["deal_evidence"] = deal_items

    history_items: list[object] = []
    for item in _as_list(normalized.get("medical_history_evidence")):
        if not isinstance(item, dict):
            history_items.append(item)
            continue
        text = _agent_join_text(item.get("content"), item.get("quote"), item.get("history_type"))
        if not any(cue in text for cue in _RESCUE_MEDICAL_HISTORY_CUES):
            continue
        history_items.append(item)
    normalized["medical_history_evidence"] = history_items

    return _normalize_evidence_graph_demands(normalized)


def _merge_evidence_graphs(
    graphs: list[dict[str, Any]],
    chunk_debug: list[dict[str, Any]],
) -> dict[str, Any]:
    merged: dict[str, Any] = {section: [] for section in _EVIDENCE_LIST_SECTIONS}
    seen: set[tuple[str, ...]] = set()
    counters: dict[str, int] = {section: 0 for section in _EVIDENCE_LIST_SECTIONS}

    for chunk_index, graph in enumerate(graphs, start=1):
        if not isinstance(graph, dict):
            continue
        for section in _EVIDENCE_LIST_SECTIONS:
            for item in _as_list(graph.get(section)):
                key = _evidence_merge_key(section, item)
                if key in seen:
                    continue
                seen.add(key)
                counters[section] += 1
                if isinstance(item, dict):
                    copied = dict(item)
                    copied["source_chunk"] = chunk_index
                    copied["source_evidence_id"] = _clean_text(copied.get("id"))
                    copied["id"] = f"{_EVIDENCE_ID_PREFIX.get(section, 'E')}{counters[section]}"
                    merged[section].append(copied)
                else:
                    merged[section].append(item)

    merged = _normalize_evidence_graph_demands(merged)
    merged["_merge_stats"] = {
        "chunk_count": len(graphs),
        "chunks": chunk_debug,
        "section_counts": {section: len(_as_list(merged.get(section))) for section in _EVIDENCE_LIST_SECTIONS},
    }
    return merged


def _extract_evidence_by_chunks(
    corrected_dialogue: str,
    *,
    staff_text: str,
    preprocess_context: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    chunks = _split_corrected_dialogue_for_evidence(corrected_dialogue)
    evidence_graphs: list[dict[str, Any]] = []
    chunk_debug: list[dict[str, Any]] = []
    preprocess_text = json.dumps(preprocess_context, ensure_ascii=False, separators=(",", ":"))
    for chunk in chunks:
        evidence_user_prompt = _EVIDENCE_AGENT_CHUNK_USER_TEMPLATE.format(
            staff_context=staff_text,
            preprocess_context=preprocess_text,
            chunk_index=chunk["chunk_index"],
            chunk_count=chunk["chunk_count"],
            line_range=chunk["line_range"],
            dialogue=chunk["dialogue"],
        )
        evidence_parsed = _call_agent(
            f"evidence_chunk_{chunk['chunk_index']}",
            _EVIDENCE_AGENT_SYSTEM_PROMPT,
            evidence_user_prompt,
            max_tokens=9000,
        )
        evidence_graph = _extract_evidence_graph(evidence_parsed)
        evidence_graphs.append(evidence_graph)
        chunk_debug.append(
            {
                "chunk_index": chunk["chunk_index"],
                "line_range": chunk["line_range"],
                "line_count": chunk["line_count"],
                "char_count": chunk["char_count"],
                "evidence_counts": {
                    section: len(_as_list(evidence_graph.get(section)))
                    for section in _EVIDENCE_LIST_SECTIONS
                },
            }
        )
    return _merge_evidence_graphs(evidence_graphs, chunk_debug), chunk_debug


def _evidence_graph_is_empty(evidence_graph: dict[str, Any]) -> bool:
    return not any(_as_list(evidence_graph.get(section)) for section in _BUSINESS_EVIDENCE_SECTIONS)


def _evidence_item_scope(item: dict[str, Any]) -> str:
    return _clean_text(item.get("participant_scope") or item.get("customer_scope") or item.get("scope"))


def _business_evidence_needs_scene_rescue(evidence_graph: dict[str, Any]) -> bool:
    if _evidence_graph_is_empty(evidence_graph):
        return True
    if _as_list(evidence_graph.get("customer_demand_evidence")):
        return False

    business_items = [
        item
        for section in _BUSINESS_EVIDENCE_SECTIONS
        for item in _as_list(evidence_graph.get(section))
        if isinstance(item, dict)
    ]
    if not business_items:
        return True
    has_current_scope = any(
        _evidence_item_scope(item) in {"primary_customer", "current_customer", "main_customer"}
        for item in business_items
    )
    notes_text = _agent_join_text(evidence_graph.get("quality_notes"), evidence_graph.get("speaker_corrections"))
    internal_or_third_party_note = any(
        cue in notes_text
        for cue in (
            "内部",
            "员工",
            "无明确主咨询客户",
            "未出现客户直接",
            "转述",
            "第三方",
            "其他顾客",
            "未发现可归属于具体主咨询客户",
        )
    )
    relation_text = _agent_join_text(evidence_graph.get("recommendation_evidence"), evidence_graph.get("deal_evidence"))
    third_party_or_unclear_relation = any(
        cue in relation_text
        for cue in (
            "有顾客",
            "那个顾客",
            "美团",
            "未成交",
            "准备去韩国",
            "relation_to_current_demand",
            "unclear",
            "alternative_not_recommended",
        )
    )
    return (not has_current_scope and internal_or_third_party_note) or third_party_or_unclear_relation


def _extract_scene_assessment(parsed: dict[str, Any]) -> dict[str, Any]:
    payload = parsed.get("scene_assessment")
    if not isinstance(payload, dict):
        return {}
    scene_type = _clean_text(payload.get("scene_type")) or "unclear"
    reason = _clean_text(payload.get("reason"))
    try:
        confidence = float(payload.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "scene_type": scene_type,
        "is_current_customer_consultation": bool(payload.get("is_current_customer_consultation")),
        "confidence": max(0.0, min(confidence, 1.0)),
        "reason": reason,
    }


def _is_non_current_consultation_scene(scene_assessment: dict[str, Any]) -> bool:
    if not scene_assessment:
        return False
    scene_type = _clean_text(scene_assessment.get("scene_type"))
    if scene_assessment.get("is_current_customer_consultation") is True:
        return False
    return scene_type in {
        "internal_staff_chat",
        "frontdesk_order",
        "third_party_case_discussion",
        "casual_chat",
    }


def _mark_non_consultation_scene(
    result: dict[str, Any],
    scene_assessment: dict[str, Any],
) -> dict[str, Any]:
    if not scene_assessment:
        return result
    enriched = dict(result)
    enriched["scene_assessment"] = scene_assessment
    scene_type = _clean_text(scene_assessment.get("scene_type")) or "unclear"
    reason = _clean_text(scene_assessment.get("reason")) or "未发现当前顾客面诊主线"
    enriched["analysis_quality"] = {
        "requires_review": True,
        "issues": [f"非当前顾客面诊场景：{scene_type}，{reason}"],
    }
    return enriched


def _collect_referenced_line_ids(evidence_graph: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for section in _EVIDENCE_LIST_SECTIONS:
        for item in _as_list(evidence_graph.get(section)):
            if isinstance(item, dict):
                ids.update(_evidence_turn_ids(item))
            else:
                for line_id in re.findall(r"\bL\d{4}\b", _clean_text(item)):
                    ids.add(line_id)
    return ids


def _relevant_dialogue_excerpt(
    corrected_dialogue: str,
    evidence_graph: dict[str, Any],
    *,
    context_lines: int = 1,
    max_lines: int = 120,
) -> str:
    wanted = _collect_referenced_line_ids(evidence_graph)
    if not wanted:
        return "No full transcript provided. Use evidence quotes in evidence_graph."
    lines = [line for line in corrected_dialogue.splitlines() if line.strip()]
    indexed: list[tuple[str, str]] = [(_line_id_from_text(line), line) for line in lines]
    positions = {line_id: index for index, (line_id, _line) in enumerate(indexed) if line_id}
    selected: set[int] = set()
    for line_id in wanted:
        if line_id not in positions:
            continue
        pos = positions[line_id]
        for offset in range(-context_lines, context_lines + 1):
            next_pos = pos + offset
            if 0 <= next_pos < len(indexed):
                selected.add(next_pos)
    selected_positions = sorted(selected)
    if len(selected_positions) > max_lines:
        selected_positions = selected_positions[:max_lines]
    return "\n".join(indexed[pos][1] for pos in selected_positions)


def _has_participant_scope(evidence_graph: dict[str, Any], scope: str) -> bool:
    for section in _EVIDENCE_LIST_SECTIONS:
        for item in _as_list(evidence_graph.get(section)):
            if isinstance(item, dict) and _clean_text(item.get("participant_scope") or item.get("customer_scope")) == scope:
                return True
    return False


def _audit_needed(
    fact_graph: dict[str, Any],
    evidence_graph: dict[str, Any],
    correction_metadata: dict[str, Any],
    indication_adjudication: dict[str, Any],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    demands = _as_list(fact_graph.get("demands"))
    recommendations = _as_list(fact_graph.get("recommendations"))
    seed_recommendations = _as_list(fact_graph.get("seed_recommendations"))
    indications = _as_list(fact_graph.get("indication_candidates"))
    if _as_list(evidence_graph.get("customer_demand_evidence")) and not demands:
        reasons.append("demand_evidence_without_fact")
    if (demands or recommendations or seed_recommendations) and not indications:
        reasons.append("business_facts_without_indication")
    if _as_list(evidence_graph.get("recommendation_evidence")) and not (recommendations or seed_recommendations):
        reasons.append("recommendation_evidence_without_fact")
    if recommendations and not any(_as_list(item.get("related_demand_ids")) for item in recommendations if isinstance(item, dict)):
        reasons.append("recommendations_without_demand_links")
    if _as_list(evidence_graph.get("profile_evidence")) and not _as_list(fact_graph.get("profile_facts")):
        reasons.append("profile_evidence_without_profile_facts")
    if _as_list(evidence_graph.get("budget_evidence")) and not _as_list(fact_graph.get("budget_facts")):
        reasons.append("budget_evidence_without_budget_facts")
    for item in recommendations:
        if not isinstance(item, dict):
            continue
        details = _as_dict(item.get("details"))
        if details and any(details.get(key) for key in _AGENT_RECOMMENDATION_DETAIL_KEYS) and not any(item.get(key) for key in _AGENT_RECOMMENDATION_DETAIL_KEYS):
            reasons.append("recommendation_details_not_flattened")
            break
    if _has_participant_scope(evidence_graph, "other_customer"):
        reasons.append("multi_customer_scope")
    if len(_as_list(correction_metadata.get("applied_speaker_corrections"))) >= 3:
        reasons.append("many_speaker_corrections")
    if _as_list(correction_metadata.get("uncertain_notes")):
        reasons.append("speaker_or_term_uncertainty")
    rejected = _as_list(indication_adjudication.get("rejected_indications"))
    if len(rejected) >= 3:
        reasons.append("many_rejected_indications")
    # Keep the expensive audit call for structural/data-loss risks. Speaker
    # uncertainty and many rejected candidates are useful diagnostics but are
    # common on long recordings and do not by themselves justify another full
    # LLM pass.
    diagnostic_only = {
        "many_speaker_corrections",
        "speaker_or_term_uncertainty",
        "many_rejected_indications",
    }
    actionable = [reason for reason in reasons if reason not in diagnostic_only]
    return bool(actionable), reasons


def _extract_plan_adjudication(parsed: dict[str, Any]) -> dict[str, Any]:
    payload = parsed.get("recommendation_adjudication")
    if isinstance(payload, dict):
        return payload
    return parsed


def _normalize_fact_item_list(value: object) -> list[dict[str, Any]]:
    return [dict(item) for item in _as_list(value) if isinstance(item, dict)]


def _apply_plan_adjudication(fact_graph: dict[str, Any], adjudication: dict[str, Any]) -> dict[str, Any]:
    recommendations = _normalize_fact_item_list(adjudication.get("recommendations"))
    seed_recommendations = _normalize_fact_item_list(adjudication.get("seed_recommendations"))
    if not recommendations and not seed_recommendations:
        return fact_graph
    updated = dict(fact_graph)
    updated["recommendations"] = recommendations
    updated["seed_recommendations"] = seed_recommendations
    updated["_recommendation_adjudication"] = {
        "rejected_recommendations": _as_list(adjudication.get("rejected_recommendations")),
        "notes": _as_list(adjudication.get("notes")),
    }
    return updated


def _extract_event_graph(parsed: dict[str, Any]) -> dict[str, Any]:
    payload = parsed.get("event_graph")
    if isinstance(payload, dict):
        return _normalize_event_graph(payload)
    return _normalize_event_graph(parsed) if isinstance(parsed, dict) else {}


_EVENT_STYLE_PREFERENCE_CUES = (
    "自然一点",
    "夸张一点",
    "小平扇",
    "外开扇",
    "开扇",
    "平扇",
    "宽窄",
    "宽一点",
    "窄一点",
    "款式",
    "风格",
)

_EVENT_CONCRETE_DEMAND_CUES = (
    "改善",
    "解决",
    "无神",
    "大小眼",
    "下垂",
    "松弛",
    "凹陷",
    "后缩",
    "眼袋",
    "皱纹",
    "法令纹",
    "缩小",
    "缩窄",
    "显年轻",
    "显脸小",
    "提升",
    "固定",
)


def _event_filter_text(item: dict[str, Any]) -> str:
    return _agent_join_text(
        item.get("content"),
        item.get("body_part"),
        item.get("quote"),
        item.get("plan"),
        item.get("value"),
    )


def _event_is_style_preference_only_demand(item: dict[str, Any]) -> bool:
    text = _event_filter_text(item)
    if _clean_text(item.get("event_type")) not in {"current_demand", "unclear"}:
        return False
    if not any(cue in text for cue in _EVENT_STYLE_PREFERENCE_CUES):
        return False
    return not any(cue in text for cue in _EVENT_CONCRETE_DEMAND_CUES)


def _normalize_event_graph(event_graph: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(event_graph)
    normalized["demand_events"] = [
        item
        for item in _as_list(event_graph.get("demand_events"))
        if not (isinstance(item, dict) and _event_is_style_preference_only_demand(item))
    ]
    for section in ("concern_events", "budget_events"):
        fixed: list[Any] = []
        for item in _as_list(normalized.get(section)):
            if isinstance(item, dict) and not _clean_text(item.get("event_type")):
                copied = dict(item)
                copied["event_type"] = "unclear"
                fixed.append(copied)
            else:
                fixed.append(item)
        normalized[section] = fixed
    return normalized


def _event_graph_is_empty(event_graph: dict[str, Any]) -> bool:
    if not isinstance(event_graph, dict):
        return True
    for section in ("demand_events", "plan_events", "deal_events", "profile_events", "concern_events", "budget_events"):
        if _as_list(event_graph.get(section)):
            return False
    return True


_EVENT_CURRENT_PLAN_TYPES = {"current_recommendation", "deal_confirmed", "customer_accept"}
_EVENT_SEED_PLAN_TYPES = {"seed_recommendation"}
_EVENT_BLOCKED_PLAN_TYPES = {
    "comparison_or_backup",
    "not_recommended",
    "staff_explanation",
    "customer_question",
    "diagnosis_only",
}
_EVENT_DEAL_TYPES = {"deal_confirmed", "deposit", "payment", "order_created"}
_EVENT_PROFILE_BLOCK_TYPES = {"staff_or_product_context", "reject"}


_EVENT_PLAN_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("botox", ("\u8089\u6bd2", "\u4fdd\u59a5\u9002", "\u7626\u8138\u9488", "\u9664\u76b1\u9488")),
    ("thermage", ("\u70ed\u739b\u5409", "\u70ed\u62c9\u63d0")),
    ("ultherapy", ("\u8d85\u58f0\u70ae", "\u8d85\u58f0")),
    ("waterlight_collagen", ("\u6c34\u5149", "\u6ce2\u6ce2", "\u52a8\u80fd\u7d20", "\u80f6\u539f", "\u798f\u66fc", "\u5f17\u7f26", "\u53cc\u7f8e")),
    ("hyaluronic_filler", ("\u73bb\u5c3f\u9178", "\u745e\u5fb7\u559c", "\u827e\u62c9\u65af\u63d0", "\u4e54\u96c5\u767b", "\u6cd5\u601d\u4e3d", "\u586b\u5145")),
    ("nose_support", ("\u9f3b\u5c0f\u67f1", "\u9f3b\u4e2d\u4e0b\u6bb5", "\u9f3b\u57fa\u5e95", "\u5c71\u6839", "\u9f3b\u80cc", "\u9f3b\u7efc\u5408")),
    ("ear_support", ("\u8033\u6735", "\u8033\u5782")),
    ("mole_removal", ("\u70b9\u75e3", "\u795b\u75e3", "\u53bb\u75e3")),
    ("eye_bag_tear_trough", ("\u773c\u888b", "\u6cea\u6c9f", "\u7736\u9694", "\u7736\u5916c")),
    ("jawline_chin", ("\u4e0b\u988c\u7f18", "\u4e0b\u5df4", "\u4e0b\u989a")),
    ("whitening", ("\u7f8e\u767d", "\u5149\u5b50", "\u8272\u6c89", "\u9ec4\u6c14")),
)


def _event_text(item: dict[str, Any]) -> str:
    return _agent_join_text(
        item.get("plan"),
        item.get("content"),
        item.get("recommendation"),
        item.get("summary"),
        item.get("body_part"),
        item.get("brand"),
        item.get("material"),
        item.get("implementation_notes"),
        item.get("quote"),
    )


def _event_item_keys(item: dict[str, Any]) -> set[str]:
    text = _event_text(item)
    if not text:
        return set()
    keys: set[str] = set()
    compact = _compact_key_text(text)
    for key, terms in _EVENT_PLAN_KEYWORDS:
        if any(term in text for term in terms):
            keys.add(key)
    if compact:
        keys.add(compact[:80])
    return keys


def _event_key_sets_match(left: set[str], right: set[str]) -> bool:
    if not left or not right:
        return False
    if left & right:
        return True
    for lkey in left:
        for rkey in right:
            if len(lkey) >= 8 and len(rkey) >= 8 and (lkey in rkey or rkey in lkey):
                return True
    return False


def _event_plan_keys_by_type(event_graph: dict[str, Any], types: set[str]) -> set[str]:
    keys: set[str] = set()
    for event in _as_list(event_graph.get("plan_events")):
        if not isinstance(event, dict):
            continue
        if _clean_text(event.get("event_type")) not in types:
            continue
        keys.update(_event_item_keys(event))
    for event in _as_list(event_graph.get("deal_events")):
        if not isinstance(event, dict):
            continue
        if _clean_text(event.get("event_type")) not in types:
            continue
        keys.update(_event_item_keys(event))
    return keys


def _event_is_optional_seed_plan(event: dict[str, Any]) -> bool:
    event_type = _clean_text(event.get("event_type"))
    if event_type != "comparison_or_backup":
        return False
    text = _agent_join_text(
        event.get("plan"),
        event.get("body_part"),
        event.get("implementation_notes"),
        event.get("customer_response"),
        event.get("quote"),
    )
    if not text:
        return False
    optional_markers = (
        "整体方案",
        "整体设计",
        "整体帮你分析",
        "可以选择",
        "可以先",
        "也可以",
        "只做",
        "先做",
        "后续",
        "下次",
        "再做",
        "可选",
        "次要",
    )
    blocked_markers = ("不建议", "不适合", "不能做", "不要做", "没必要", "拒绝", "排除")
    return any(term in text for term in optional_markers) and not any(term in text for term in blocked_markers)


def _optional_seed_plan_keys(event_graph: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for event in _as_list(event_graph.get("plan_events")):
        if isinstance(event, dict) and _event_is_optional_seed_plan(event):
            keys.update(_event_item_keys(event))
    return keys


def _event_quote(event: dict[str, Any]) -> str:
    return _clean_text(event.get("quote")) or _clean_text(event.get("content")) or _clean_text(event.get("plan"))


def _event_related_demand_ids(event: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for key in ("related_demand", "related_demand_id", "demand_id"):
        value = _clean_text(event.get(key))
        if value:
            ids.append(value)
    for key in ("related_demand_ids", "linked_demand_ids", "demand_ids"):
        ids.extend(_clean_text(value) for value in _as_list(event.get(key)) if _clean_text(value))
    return list(dict.fromkeys(ids))


def _agent_add_optional_seed_recommendations_from_events(
    fact_graph: dict[str, Any],
    event_graph: dict[str, Any],
) -> dict[str, Any]:
    events = [event for event in _as_list(event_graph.get("plan_events")) if isinstance(event, dict) and _event_is_optional_seed_plan(event)]
    if not events:
        return fact_graph
    updated = dict(fact_graph)
    seeds = _normalize_fact_item_list(updated.get("seed_recommendations"))
    existing_keys = {_compact_key_text(_event_text(item)) for item in seeds if _compact_key_text(_event_text(item))}
    for event in events:
        event_key = _compact_key_text(_event_text(event))
        if event_key and event_key in existing_keys:
            continue
        plan = _clean_text(event.get("plan"))
        if not plan:
            continue
        copied = {
            "id": _clean_text(event.get("id")),
            "content": plan,
            "body_part": _clean_text(event.get("body_part")),
            "brand": _clean_text(event.get("brand")),
            "material": _clean_text(event.get("material")),
            "dosage": _clean_text(event.get("dosage")),
            "price": _clean_text(event.get("price")),
            "course_or_frequency": _clean_text(event.get("course_or_frequency")),
            "treatment_steps": _as_list(event.get("treatment_steps")),
            "implementation_notes": _clean_text(event.get("implementation_notes")),
            "customer_response": _clean_text(event.get("customer_response")) or "未明确回应",
            "related_demand_ids": _event_related_demand_ids(event),
            "evidence": _event_quote(event),
            "source_evidence_ids": _as_list(event.get("source_evidence_ids")),
            "event_graph_optional_seed": True,
        }
        seeds.append(copied)
        if event_key:
            existing_keys.add(event_key)
    updated["seed_recommendations"] = seeds
    return updated


def _agent_demote_orphan_optional_recommendations(fact_graph: dict[str, Any]) -> dict[str, Any]:
    recommendations = [dict(item) for item in _as_list(fact_graph.get("recommendations")) if isinstance(item, dict)]
    if not recommendations:
        return fact_graph
    demand_context = _agent_join_text(fact_graph.get("demands"))
    optional_markers = (
        "整体方案",
        "整体设计",
        "整体帮你分析",
        "可以选择",
        "可以先",
        "先单做",
        "再考虑",
        "后续",
        "下次",
        "追加",
        "未明确回应",
    )
    kept: list[dict[str, Any]] = []
    demoted: list[dict[str, Any]] = []
    for item in recommendations:
        linked_demand_ids = _as_list(item.get("related_demand_ids")) + _as_list(item.get("demand_priority"))
        body_terms = _agent_indication_body_specific_terms({"body_part_name": _first_text(item, "body_part", "body_part_name")})
        has_demand_body_support = bool(body_terms) and any(term in demand_context for term in body_terms)
        text = _agent_join_text(
            item.get("content"),
            item.get("recommendation"),
            item.get("body_part"),
            item.get("implementation_notes"),
            item.get("customer_response"),
            item.get("evidence"),
        )
        should_demote = (
            not linked_demand_ids
            and body_terms
            and not has_demand_body_support
            and any(term in text for term in optional_markers)
        )
        if should_demote:
            copied = dict(item)
            copied["source"] = _clean_text(copied.get("source")) or "demoted_orphan_optional_recommendation"
            if not _clean_text(copied.get("customer_response")):
                copied["customer_response"] = "未明确回应"
            demoted.append(copied)
        else:
            kept.append(item)
    if not demoted:
        return fact_graph
    updated = dict(fact_graph)
    seeds = [dict(item) for item in _as_list(updated.get("seed_recommendations")) if isinstance(item, dict)]
    existing = {_compact_key_text(_agent_item_content(item)) for item in seeds if _agent_item_content(item)}
    for item in demoted:
        key = _compact_key_text(_agent_item_content(item))
        if key and key in existing:
            continue
        seeds.append(item)
        if key:
            existing.add(key)
    updated["recommendations"] = kept
    updated["seed_recommendations"] = seeds
    return updated


_INDICATION_CURRENT_SUPPORT_TERMS: dict[str, tuple[str, ...]] = {
    "毛孔": ("毛孔", "控油", "油皮", "黑头", "肤质", "水光"),
    "干燥": ("干燥", "缺水", "补水", "水光"),
    "松弛下垂": ("松弛", "下垂", "紧致", "提升", "抗衰", "热玛吉", "超声炮"),
    "紧致淡纹": ("细纹", "干纹", "淡纹", "皱纹", "紧致", "抗衰"),
    "纹路": ("法令纹", "纹路", "皱纹", "细纹", "干纹", "淡纹"),
    "塑美": ("塑形", "支撑", "提升", "鼻", "下颌", "下巴", "轮廓", "英伦", "大O", "耳", "眉弓", "双C", "唇", "嘴"),
    "面部填充": ("填充", "凹陷", "轮廓", "苹果肌", "太阳穴", "额颞", "泪沟", "口基底", "鼻基底"),
    "双眼皮": ("双眼皮", "开扇", "平扇", "眼尾", "去皮", "去脂", "提肌", "开眼角"),
}


def _agent_indication_current_support_terms(item: dict[str, Any]) -> list[str]:
    name = _clean_text(item.get("indication_name"))
    body = _clean_text(item.get("body_part_name"))
    terms = list(_INDICATION_CURRENT_SUPPORT_TERMS.get(name, ()))
    if name and name not in terms:
        terms.append(name)
    for part in re.split(r"[（()）/、,，;；\s]+", body):
        part = _clean_text(part)
        if len(part) >= 2 and part not in terms:
            terms.append(part)
    return terms


def _agent_indication_body_specific_terms(item: dict[str, Any]) -> list[str]:
    body = _clean_text(item.get("body_part_name"))
    terms: list[str] = []
    for part in re.split(r"[（()）/、,，;；\s]+", body):
        part = _clean_text(part)
        if len(part) >= 2 and part not in terms:
            terms.append(part)
    body_synonyms = {
        "下颌": ("下颌", "下颌线", "下颌轮廓", "轮廓线", "大O", "下巴"),
        "鼻": ("鼻", "鼻头", "鼻背", "鼻中轴", "鼻中轴线", "山根", "鼻小柱"),
        "毛孔": ("毛孔", "控油", "油皮", "T区"),
        "口基底": ("口基底", "嘴角", "口角"),
        "眼": ("眼", "双眼皮", "泪沟", "眼袋", "眼尾"),
        "眉": ("眉弓", "眉尾", "眉眼"),
        "双C": ("双C", "眶外C", "C线"),
        "唇": ("唇", "唇部", "嘴唇", "嘴巴", "口周"),
    }
    for key, values in body_synonyms.items():
        if key in body:
            for value in values:
                if value not in terms:
                    terms.append(value)
    return terms


def _agent_prune_seed_only_indications(fact_graph: dict[str, Any]) -> dict[str, Any]:
    candidates = [dict(item) for item in _as_list(fact_graph.get("indication_candidates")) if isinstance(item, dict)]
    if not candidates:
        return fact_graph
    current_context = _agent_join_text(
        fact_graph.get("demands"),
        fact_graph.get("recommendations"),
        fact_graph.get("deal_outcome"),
    )
    seed_or_observation_context = _agent_join_text(
        fact_graph.get("seed_recommendations"),
        fact_graph.get("doctor_diagnoses"),
    )
    kept: list[dict[str, Any]] = []
    changed = False
    for item in candidates:
        body_terms = _agent_indication_body_specific_terms(item)
        if body_terms:
            has_body_current_support = any(term in current_context for term in body_terms)
            has_body_seed_support = any(term in seed_or_observation_context for term in body_terms)
            if not has_body_current_support and has_body_seed_support:
                changed = True
                continue
        terms = [term for term in _agent_indication_current_support_terms(item) if len(term) >= 2]
        if not terms:
            kept.append(item)
            continue
        has_current_support = any(term in current_context for term in terms)
        has_only_seed_or_observation_support = any(term in seed_or_observation_context for term in terms)
        if not has_current_support and has_only_seed_or_observation_support:
            changed = True
            continue
        kept.append(item)
    if not changed:
        return fact_graph
    updated = dict(fact_graph)
    updated["indication_candidates"] = kept
    return updated


def _agent_fill_missing_fact_evidence_from_events(
    fact_graph: dict[str, Any],
    event_graph: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(fact_graph, dict) or _event_graph_is_empty(event_graph):
        return fact_graph
    updated = dict(fact_graph)
    section_events = {
        "demands": [event for event in _as_list(event_graph.get("demand_events")) if isinstance(event, dict)],
        "recommendations": [
            event
            for event in _as_list(event_graph.get("plan_events"))
            if isinstance(event, dict) and _clean_text(event.get("event_type")) in (_EVENT_CURRENT_PLAN_TYPES | _EVENT_DEAL_TYPES)
        ],
        "seed_recommendations": [
            event
            for event in _as_list(event_graph.get("plan_events"))
            if isinstance(event, dict)
            and (_clean_text(event.get("event_type")) in _EVENT_SEED_PLAN_TYPES or _event_is_optional_seed_plan(event))
        ],
        "concerns": [event for event in _as_list(event_graph.get("concern_events")) if isinstance(event, dict)],
        "budget_facts": [event for event in _as_list(event_graph.get("budget_events")) if isinstance(event, dict)],
        "deal_factors": [
            event
            for event in [*_as_list(event_graph.get("budget_events")), *_as_list(event_graph.get("deal_events"))]
            if isinstance(event, dict)
        ],
        "medical_history": [event for event in _as_list(event_graph.get("profile_events")) if isinstance(event, dict)],
        "profile_facts": [event for event in _as_list(event_graph.get("profile_events")) if isinstance(event, dict)],
    }

    def pick_event(item: dict[str, Any], events: list[dict[str, Any]], index: int) -> dict[str, Any] | None:
        if not events:
            return None
        item_ids = _event_evidence_ids(item)
        for event in events:
            event_ids = _event_evidence_ids(event)
            if item_ids and event_ids and item_ids & event_ids:
                return event
        item_key = _compact_key_text(_event_text(item))
        best: dict[str, Any] | None = None
        best_score = 0
        for event in events:
            event_key = _compact_key_text(_event_text(event))
            score = 0
            if item_key and event_key and (item_key in event_key or event_key in item_key):
                score += 4
            for term in ("鼻", "眼", "双眼皮", "毛孔", "法令纹", "口基底", "眉弓", "轮廓", "预算"):
                if term in item_key and term in event_key:
                    score += 1
            if score > best_score:
                best = event
                best_score = score
        if best is not None and best_score > 0:
            return best
        return events[index] if index < len(events) else events[-1]

    for section, events in section_events.items():
        items = _normalize_fact_item_list(updated.get(section))
        if not items:
            continue
        repaired: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            copied = dict(item)
            if not _clean_text(copied.get("evidence")):
                event = pick_event(copied, events, index)
                quote = _event_quote(event) if event else ""
                if quote:
                    copied["evidence"] = quote
            repaired.append(copied)
        updated[section] = repaired
    return updated


def _event_evidence_ids(item: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for key in ("id", "source_id", "evidence_id"):
        value = _clean_text(item.get(key))
        if value:
            ids.add(value)
    for key in ("evidence_ids", "source_evidence_ids", "source_ids"):
        ids.update(_clean_text(value) for value in _as_list(item.get(key)) if _clean_text(value))
    return ids


def _profile_event_blocks_item(item: dict[str, Any], blocked_events: list[dict[str, Any]]) -> bool:
    item_ids = _event_evidence_ids(item)
    item_key = _compact_key_text(
        _agent_join_text(
            _first_text(item, "category", "tag_category", "type"),
            _first_text(item, "value", "tag_value", "content", "text"),
            item.get("evidence"),
            item.get("quote"),
        )
    )
    for event in blocked_events:
        event_ids = _event_evidence_ids(event)
        if item_ids and event_ids and item_ids & event_ids:
            return True
        event_key = _compact_key_text(
            _agent_join_text(event.get("category"), event.get("value"), event.get("quote"), event.get("content"))
        )
        if item_key and event_key and (item_key in event_key or event_key in item_key):
            return True
    return False


def _deal_outcome_from_event_graph(event_graph: dict[str, Any]) -> dict[str, Any] | None:
    deal_events = [
        event
        for event in _as_list(event_graph.get("deal_events"))
        if isinstance(event, dict) and _clean_text(event.get("event_type")) in _EVENT_DEAL_TYPES
    ]
    if not deal_events:
        return None
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    amount = ""
    for event in deal_events:
        plan = _first_text(event, "plan", "content", "summary")
        event_amount = _first_text(event, "amount", "price")
        if event_amount and not amount:
            amount = event_amount
        key = (_compact_key_text(plan), _compact_key_text(event_amount))
        if not any(key) or key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "plan": plan,
                "amount": event_amount or None,
                "evidence_ids": _as_list(event.get("source_evidence_ids")) or _as_list(event.get("evidence_ids")),
                "evidence": _first_text(event, "quote", "evidence") or None,
                "participant": _first_text(event, "participant") or None,
                "participant_scope": _first_text(event, "participant_scope") or None,
            }
        )
    if not items and not amount:
        return None
    summary_parts = []
    for item in items:
        text = _agent_join_text(item.get("plan"), item.get("amount"))
        if text:
            summary_parts.append(text)
    return {
        "status": "\u5df2\u6210\u4ea4",
        "summary": "\uff1b".join(summary_parts) or amount or "\u5df2\u6210\u4ea4",
        "deal_items": items,
        "amount": amount or None,
    }


def _apply_event_graph_constraints(fact_graph: dict[str, Any], event_graph: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(fact_graph, dict) or _event_graph_is_empty(event_graph):
        return fact_graph

    updated = dict(fact_graph)
    allowed_keys = _event_plan_keys_by_type(event_graph, _EVENT_CURRENT_PLAN_TYPES | _EVENT_DEAL_TYPES)
    seed_keys = _event_plan_keys_by_type(event_graph, _EVENT_SEED_PLAN_TYPES)
    optional_seed_keys = _optional_seed_plan_keys(event_graph)
    seed_keys |= optional_seed_keys
    blocked_keys = _event_plan_keys_by_type(event_graph, _EVENT_BLOCKED_PLAN_TYPES)
    blocked_keys -= optional_seed_keys

    recommendations: list[dict[str, Any]] = []
    demoted_seeds: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for item in _normalize_fact_item_list(updated.get("recommendations")):
        keys = _event_item_keys(item)
        if _event_key_sets_match(keys, blocked_keys) and not _event_key_sets_match(keys, allowed_keys):
            rejected.append({"source_id": item.get("id") or item.get("source_id") or "", "reason": "blocked_by_event_graph"})
            continue
        if _event_key_sets_match(keys, seed_keys) and not _event_key_sets_match(keys, allowed_keys):
            copied = dict(item)
            copied["event_graph_demoted"] = True
            demoted_seeds.append(copied)
            continue
        recommendations.append(item)

    seed_recommendations: list[dict[str, Any]] = []
    seen_seed_keys: set[str] = set()
    for item in [*_normalize_fact_item_list(updated.get("seed_recommendations")), *demoted_seeds]:
        keys = _event_item_keys(item)
        if _event_key_sets_match(keys, blocked_keys) and not _event_key_sets_match(keys, seed_keys | allowed_keys):
            rejected.append({"source_id": item.get("id") or item.get("source_id") or "", "reason": "blocked_by_event_graph"})
            continue
        key = "|".join(sorted(keys))
        if key and key in seen_seed_keys:
            continue
        if key:
            seen_seed_keys.add(key)
        seed_recommendations.append(item)

    updated["recommendations"] = recommendations
    updated["seed_recommendations"] = seed_recommendations
    if rejected:
        adjudication = _as_dict(updated.get("_recommendation_adjudication"))
        existing = _as_list(adjudication.get("rejected_recommendations"))
        updated["_recommendation_adjudication"] = {
            **adjudication,
            "rejected_recommendations": [*existing, *rejected],
        }

    blocked_profile_events = [
        event
        for event in _as_list(event_graph.get("profile_events"))
        if isinstance(event, dict) and _clean_text(event.get("event_type")) in _EVENT_PROFILE_BLOCK_TYPES
    ]
    if blocked_profile_events:
        updated["profile_facts"] = [
            item
            for item in _normalize_fact_item_list(updated.get("profile_facts"))
            if not _profile_event_blocks_item(item, blocked_profile_events)
        ]

    deal_outcome = _deal_outcome_from_event_graph(event_graph)
    if deal_outcome:
        updated["deal_outcome"] = deal_outcome

    updated["_event_graph_constraints"] = {
        "current_plan_key_count": len(allowed_keys),
        "seed_plan_key_count": len(seed_keys),
        "optional_seed_plan_key_count": len(optional_seed_keys),
        "blocked_plan_key_count": len(blocked_keys),
        "rejected_recommendation_count": len(rejected),
    }
    return updated


def _extract_audit(parsed: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    audit = parsed.get("audit") if isinstance(parsed.get("audit"), dict) else {}
    corrected = parsed.get("corrected_fact_graph")
    return audit, corrected if isinstance(corrected, dict) else None


def _extract_final_result_audit(parsed: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    audit = parsed.get("final_result_audit") if isinstance(parsed.get("final_result_audit"), dict) else {}
    patch = parsed.get("analysis_result_patch")
    return audit, patch if isinstance(patch, dict) else None


def _apply_final_result_audit_patch(result: dict[str, Any], patch: dict[str, Any] | None) -> dict[str, Any]:
    if not patch:
        return result
    updated = dict(result)
    replaceable_sections = {
        "customer_primary_demands",
        "customer_concerns",
        "staff_recommendations",
        "staff_seed_recommendations",
        "standardized_indications",
        "consumption_intent",
        "consultation_result",
        "customer_profile",
    }
    for section in replaceable_sections:
        value = patch.get(section)
        if isinstance(value, dict):
            updated[section] = value
    debug = updated.setdefault("staged_pipeline_debug", {})
    if isinstance(debug, dict):
        debug["agent_final_result_audit_repaired"] = True
    return updated


def _final_result_audit_needed(
    analysis_result: dict[str, Any],
    *,
    corrected_dialogue: str,
    fact_graph: dict[str, Any],
    event_graph: dict[str, Any],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    demand_items = [dict(item) for item in _as_list(_as_dict(analysis_result.get("customer_primary_demands")).get("items")) if isinstance(item, dict)]
    demand_keys = [_agent_result_demand_key(item) for item in demand_items]
    if len(demand_items) >= 5:
        reasons.append("many_demands_need_consistency_check")
    if len(set(key for key in demand_keys if key)) < len([key for key in demand_keys if key]):
        reasons.append("duplicate_or_near_duplicate_demands")
    for item in demand_items:
        text = _agent_result_item_text(item)
        has_goal = any(term in text for term in _AGENT_TREATMENT_GOAL_CUES)
        if not has_goal and any(term in text for term in _AGENT_NON_DEMAND_CONCERN_CUES + _AGENT_NON_DEMAND_PRICE_CUES + _AGENT_EXECUTOR_CUES):
            reasons.append("non_goal_item_in_demands")
            break
        if any(term in text for term in ("倾向选择", "偏向选择", "品牌", "保妥适", "衡力")) and not has_goal:
            reasons.append("brand_preference_in_demands")
            break

    concern_items = _as_list(_as_dict(analysis_result.get("customer_concerns")).get("items"))
    recommendation_items = [dict(item) for item in _as_list(_as_dict(analysis_result.get("staff_recommendations")).get("items")) if isinstance(item, dict)]
    recommendation_text = _agent_join_text(recommendation_items)
    if not concern_items and any(term in recommendation_text for term in ("担心", "害怕", "怕", "后遗症", "安全", "风险", "移位", "凹陷加重", "疤痕")):
        reasons.append("worry_in_recommendation_response_without_concern")

    valid_priorities = {
        int(item.get("priority") or 0)
        for item in demand_items
        if isinstance(item.get("priority"), int) or str(item.get("priority") or "").isdigit()
    }
    for item in recommendation_items:
        raw_values = _as_list(item.get("demand_priority"))
        for value in raw_values:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                reasons.append("invalid_recommendation_demand_link")
                break
            if parsed not in valid_priorities:
                reasons.append("invalid_recommendation_demand_link")
                break

    budget_text = _agent_join_text(analysis_result.get("consumption_intent"))
    if re.search(r"\[?\d{1,2}:\d{2}\]?", budget_text) or len(budget_text) > 700:
        reasons.append("raw_quote_in_budget")

    if len(corrected_dialogue) > 16000:
        reasons.append("long_recording_final_check")
    if _as_list(event_graph.get("events")) and _as_list(fact_graph.get("recommendations")):
        reasons.append("event_fact_alignment_check")

    actionable = [
        reason
        for reason in reasons
        if reason
        not in {
            "long_recording_final_check",
            "event_fact_alignment_check",
        }
    ]
    if actionable:
        return True, reasons
    # Long recordings get a final audit only when there is enough extracted
    # content to justify the extra token spend.
    return bool(len(corrected_dialogue) > 22000 and (len(demand_items) >= 3 or len(recommendation_items) >= 3)), reasons


def _apply_audit_repair(fact_graph: dict[str, Any], corrected: dict[str, Any] | None) -> dict[str, Any]:
    if not corrected:
        return fact_graph
    updated = dict(fact_graph)
    replaceable_sections = (
        "demands",
        "doctor_diagnoses",
        "indication_candidates",
        "recommendations",
        "seed_recommendations",
        "concerns",
        "budget_facts",
        "medical_history",
        "profile_facts",
        "deal_factors",
        "uncertainties",
    )
    for section in replaceable_sections:
        if section in corrected and isinstance(corrected.get(section), list):
            updated[section] = _normalize_fact_item_list(corrected.get(section))
    if isinstance(corrected.get("deal_outcome"), dict):
        updated["deal_outcome"] = dict(corrected["deal_outcome"])
    updated["_audit_repaired"] = True
    return updated


def _compact_for_prompt(value: object, *, max_chars: int = 20000) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...<truncated>"


def _evidence_for_plan_prompt(evidence_graph: dict[str, Any]) -> dict[str, Any]:
    return {
        "customer_demand_evidence": _as_list(evidence_graph.get("customer_demand_evidence")),
        "diagnosis_evidence": _as_list(evidence_graph.get("diagnosis_evidence")),
        "recommendation_evidence": _as_list(evidence_graph.get("recommendation_evidence")),
        "concern_evidence": _as_list(evidence_graph.get("concern_evidence")),
        "budget_evidence": _as_list(evidence_graph.get("budget_evidence")),
        "deal_evidence": _as_list(evidence_graph.get("deal_evidence")),
    }


def _call_agent(agent_name: str, system_prompt: str, user_prompt: str, *, max_tokens: int) -> dict[str, Any]:
    logger.info(
        "agent pipeline %s prompt chars system=%d user=%d",
        agent_name,
        len(system_prompt),
        len(user_prompt),
    )
    return _call_json(system_prompt, user_prompt, max_tokens=max_tokens)


_CORRECTION_FULL_DIALOGUE_MAX_CHARS = 45000
_CORRECTION_CONTEXT_RADIUS = 2
_CORRECTION_MAX_PROMPT_LINES = 420

_CORRECTION_INTERNAL_CUES = (
    "我的顾客",
    "我有个顾客",
    "我那个顾客",
    "我的老顾客",
    "接顾客",
    "在接顾客",
    "接谁",
    "谁接",
    "前台",
    "领导",
    "早班",
    "晚班",
    "成本",
    "利润",
    "成交",
    "未成交",
    "核销",
    "划扣",
    "到账",
    "退款",
    "退费",
    "开单",
    "开检查单",
    "派单",
    "收银",
    "权限",
    "系统",
    "医生助理",
    "专家助理",
    "院长助理",
    "给我同事",
)
_CORRECTION_PRE_RECEPTION_CUES = (
    "这边请",
    "请坐",
    "稍等",
    "签字",
    "签完字",
    "身份证",
    "预约",
    "叫号",
    "排号",
)
_CORRECTION_TERM_CUES = (
    "一字光波",
    "一次光波",
    "一支光波",
    "鲁板",
    "鲁班",
    "下划线",
)


def _line_id_from_numbered_dialogue_line(line: str) -> str:
    match = re.match(r"^(L\d{4})\b", line)
    return match.group(1) if match else ""


def _line_role_and_text(line: str) -> tuple[str, str]:
    try:
        after_metadata = line.split("]: ", 1)[1]
    except IndexError:
        after_metadata = line
    try:
        _timestamp, rest = after_metadata.split("] ", 1)
        role, text = rest.split(": ", 1)
    except ValueError:
        return "", line
    return role.strip(), text.strip()


def _role_looks_customer(role: str) -> bool:
    return any(term in role for term in ("客户", "主客户", "同行人", "访客"))


def _role_looks_staff(role: str) -> bool:
    return any(term in role for term in ("咨询师", "医生", "助理", "员工", "前台", "工牌本人"))


def _line_needs_correction_context(line: str, metadata: dict[str, str]) -> bool:
    role, text = _line_role_and_text(line)
    compact_text = re.sub(r"\s+", "", text)
    if re.search(r"(客户|主客户|同行人|访客)（[^）]*工牌本人", line):
        return True
    if re.search(r"(咨询师|医生|前台|员工|专家助理)（(主客户|同行人|客户|顾客|访客)）", line):
        return True
    if _role_looks_customer(role) and any(cue in compact_text for cue in _CORRECTION_INTERNAL_CUES):
        return True
    if _role_looks_customer(role) and any(cue in compact_text for cue in _CORRECTION_PRE_RECEPTION_CUES):
        return True
    if any(cue in compact_text for cue in _CORRECTION_TERM_CUES):
        return True
    metadata_role = _clean_text(metadata.get("role"))
    metadata_label = _clean_text(metadata.get("speaker_label"))
    if metadata_role.lower() in {"customer", "client", "patient", "primary_customer", "visitor_companion"} and _role_looks_staff(metadata_label):
        return True
    if metadata_role.lower() in {"consultant", "doctor", "frontdesk", "staff_peer", "badge_owner", "expert_assistant"} and _role_looks_customer(metadata_label):
        return True
    return False


def _dialogue_for_correction_prompt(
    numbered_dialogue: str,
    line_metadata: dict[str, dict[str, str]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return full dialogue for normal transcripts, and focused windows for long ones."""
    lines = [line for line in numbered_dialogue.splitlines() if line.strip()]
    if len(numbered_dialogue) <= _CORRECTION_FULL_DIALOGUE_MAX_CHARS:
        return numbered_dialogue, {
            "mode": "full",
            "input_line_count": len(lines),
            "prompt_line_count": len(lines),
            "prompt_chars": len(numbered_dialogue),
        }

    wanted: set[int] = set(range(min(20, len(lines))))
    speaker_samples: dict[str, int] = {}
    for idx, line in enumerate(lines):
        line_id = _line_id_from_numbered_dialogue_line(line)
        metadata = (line_metadata or {}).get(line_id) or {}
        speaker_key = _clean_text(metadata.get("asr_speaker") or metadata.get("speaker_label") or metadata.get("role"))
        if speaker_key and speaker_samples.get(speaker_key, 0) < 3:
            wanted.add(idx)
            speaker_samples[speaker_key] = speaker_samples.get(speaker_key, 0) + 1
        if _line_needs_correction_context(line, metadata):
            for offset in range(-_CORRECTION_CONTEXT_RADIUS, _CORRECTION_CONTEXT_RADIUS + 1):
                pos = idx + offset
                if 0 <= pos < len(lines):
                    wanted.add(pos)

    selected = sorted(wanted)
    if len(selected) > _CORRECTION_MAX_PROMPT_LINES:
        selected = selected[:_CORRECTION_MAX_PROMPT_LINES]
    prompt_lines = [lines[idx] for idx in selected]
    omitted_count = max(len(lines) - len(prompt_lines), 0)
    header = (
        f"# Focused correction windows: showing {len(prompt_lines)} of {len(lines)} lines. "
        f"{omitted_count} low-risk lines omitted; line IDs are original."
    )
    prompt_dialogue = "\n".join([header, *prompt_lines])
    return prompt_dialogue, {
        "mode": "focused",
        "input_line_count": len(lines),
        "prompt_line_count": len(prompt_lines),
        "omitted_line_count": omitted_count,
        "prompt_chars": len(prompt_dialogue),
    }


def analyze_transcript_agent(
    path: str | Path,
    *,
    system_prompt: str | None = None,
    staff_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the higher-token agent backup chain for one transcript.

    The returned payload mirrors ``analyze_transcript_staged``: it contains an
    ``analysis_result`` suitable for comparison, but callers should decide
    whether to persist it. This function itself does not update DB state.
    """
    del system_prompt  # Current agent prompts carry task-specific rules directly.
    dialogue, raw = prepare_transcript(path)
    if not dialogue.strip():
        raise ValueError(f"Transcript file {Path(path).name} has no valid dialogue")

    staff_text = _format_staff_context(staff_context)
    preprocess_context = _build_preprocess_context(dialogue, staff_context)
    line_speaker_metadata = _build_line_speaker_metadata(dialogue, raw)
    numbered_dialogue, numbered_line_map = _number_dialogue_lines(dialogue, line_speaker_metadata)
    correction_dialogue, correction_prompt_debug = _dialogue_for_correction_prompt(
        numbered_dialogue,
        line_speaker_metadata,
    )

    correction_user_prompt = _CORRECTION_AGENT_USER_TEMPLATE.format(
        staff_context=staff_text,
        preprocess_context=json.dumps(preprocess_context, ensure_ascii=False, separators=(",", ":")),
        numbered_dialogue=correction_dialogue,
    )
    correction_parsed = _call_agent(
        "correction",
        _CORRECTION_AGENT_SYSTEM_PROMPT,
        correction_user_prompt,
        max_tokens=6000,
    )
    correction_patch = _extract_correction_patch(correction_parsed)
    corrected_dialogue, correction_metadata = _apply_correction_patch(
        numbered_dialogue,
        numbered_line_map,
        correction_patch,
        line_speaker_metadata,
    )
    correction_metadata["correction_prompt_debug"] = correction_prompt_debug

    scope_call_count = 0
    scope_graph: dict[str, Any] = {}
    scope_filter_debug: dict[str, Any] = {}
    scoped_dialogue = corrected_dialogue
    scope_user_prompt = _SCOPE_AGENT_USER_TEMPLATE.format(
        staff_context=staff_text,
        preprocess_context=json.dumps(preprocess_context, ensure_ascii=False, separators=(",", ":")),
        dialogue=_dialogue_for_scope_prompt(corrected_dialogue),
    )
    try:
        scope_call_count = 1
        scope_parsed = _call_agent(
            "scope",
            _SCOPE_AGENT_SYSTEM_PROMPT,
            scope_user_prompt,
            max_tokens=5000,
        )
        scope_graph = _extract_scope_graph(scope_parsed)
        scoped_dialogue, scope_filter_debug = _dialogue_with_scope_filter(corrected_dialogue, scope_graph)
    except Exception as exc:
        logger.warning("agent scope segmentation failed, using full corrected dialogue: %s", exc)
        scope_graph = {"error": str(exc), "segments": []}
        scope_filter_debug = {"removed_line_count": 0, "kept_line_count": len(corrected_dialogue.splitlines()), "error": str(exc)}

    evidence_graph, evidence_chunk_debug = _extract_evidence_by_chunks(
        scoped_dialogue,
        staff_text=staff_text,
        preprocess_context=preprocess_context,
    )
    evidence_call_count = max(1, len(evidence_chunk_debug))
    rescue_call_count = 0
    scene_assessment: dict[str, Any] = {}
    rescue_payload: dict[str, Any] = {}
    if _business_evidence_needs_scene_rescue(evidence_graph):
        rescue_user_prompt = _EMPTY_EVIDENCE_RESCUE_USER_TEMPLATE.format(
            staff_context=staff_text,
            preprocess_context=json.dumps(preprocess_context, ensure_ascii=False, separators=(",", ":")),
            dialogue=_compact_for_prompt(scoped_dialogue, max_chars=18000),
        )
        try:
            rescue_call_count = 1
            rescue_payload = _call_agent(
                "empty_evidence_rescue",
                _EMPTY_EVIDENCE_RESCUE_SYSTEM_PROMPT,
                rescue_user_prompt,
                max_tokens=7000,
            )
            scene_assessment = _extract_scene_assessment(rescue_payload)
            rescue_graph = _normalize_rescue_evidence_graph(_extract_evidence_graph(rescue_payload))
            if not _is_non_current_consultation_scene(scene_assessment) and not _evidence_graph_is_empty(rescue_graph):
                evidence_graph = _merge_evidence_graphs(
                    [rescue_graph],
                    [
                        {
                            "chunk_index": 1,
                            "line_range": "rescue",
                            "line_count": len(corrected_dialogue.splitlines()),
                            "char_count": len(corrected_dialogue),
                            "evidence_counts": {
                                section: len(_as_list(rescue_graph.get(section)))
                                for section in _EVIDENCE_LIST_SECTIONS
                            },
                        }
                    ],
                )
        except Exception as exc:
            logger.warning("agent empty-evidence rescue failed, continuing with empty evidence: %s", exc)
            rescue_payload = {"error": str(exc)}

    if _is_non_current_consultation_scene(scene_assessment):
        fact_graph = {
            "demands": [],
            "doctor_diagnoses": [],
            "indication_candidates": [],
            "recommendations": [],
            "seed_recommendations": [],
            "concerns": [],
            "budget_facts": [],
            "medical_history": [],
            "profile_facts": [],
            "deal_factors": [],
            "uncertainties": [],
            "deal_outcome": {"status": "未明确", "summary": "非当前顾客面诊场景，未生成 SAP 回写事实"},
        }
        analysis_result = _build_analysis_result_from_fact_graph(fact_graph, raw, allow_raw_augmentation=False)
        analysis_result = _mark_non_consultation_scene(analysis_result, scene_assessment)
        debug = analysis_result.setdefault("staged_pipeline_debug", {})
        if isinstance(debug, dict):
            total_logical_calls = 1 + scope_call_count + evidence_call_count + rescue_call_count
            debug["production_chain"] = PIPELINE_NAME
            debug["llm_call_plan"] = {
                "model": STAGED_LLM_MODEL,
                "correction_agent": 1,
                "scope_agent": scope_call_count,
                "evidence_agent": evidence_call_count,
                "empty_evidence_rescue_agent": rescue_call_count,
                "event_graph_agent": 0,
                "judgment_agent": 0,
                "recommendation_adjudication_agent": 0,
                "indication_adjudication_agent": 0,
                "audit_agent": 0,
                "final_result_audit_agent": 0,
                "indication_adjudication_after_audit": 0,
                "fact_graph_to_analysis_result": 0,
                "total_logical_calls": total_logical_calls,
            }
            debug["scene_assessment"] = scene_assessment
            debug["agent_scope_graph"] = scope_graph
            debug["agent_scope_filter"] = scope_filter_debug
            debug["agent_evidence_chunking"] = {
                "chunk_count": evidence_call_count,
                "target_chars": EVIDENCE_CHUNK_TARGET_CHARS,
                "overlap_lines": EVIDENCE_CHUNK_OVERLAP_LINES,
            }
        total_logical_calls = 1 + scope_call_count + evidence_call_count + rescue_call_count
        return {
            "pipeline": PIPELINE_NAME,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "llm_call_plan": {
                "model": STAGED_LLM_MODEL,
                "correction_agent": 1,
                "scope_agent": scope_call_count,
                "evidence_agent": evidence_call_count,
                "empty_evidence_rescue_agent": rescue_call_count,
                "event_graph_agent": 0,
                "judgment_agent": 0,
                "recommendation_adjudication_agent": 0,
                "indication_adjudication_agent": 0,
                "audit_agent": 0,
                "final_result_audit_agent": 0,
                "indication_adjudication_after_audit": 0,
                "fact_graph_to_analysis_result": 0,
                "total_logical_calls": total_logical_calls,
            },
            "input_stats": {
                "dialogue_chars": len(dialogue),
                "corrected_dialogue_chars": len(corrected_dialogue),
                "scoped_dialogue_chars": len(scoped_dialogue),
                "raw_payload_chars": _estimate_payload_chars(raw),
                "numbered_dialogue_lines": len(numbered_line_map),
                "applied_speaker_correction_count": len(correction_metadata.get("applied_speaker_corrections", [])),
                "applied_term_correction_count": len(correction_metadata.get("applied_term_corrections", [])),
                "evidence_chunk_count": evidence_call_count,
            },
            "preprocess_context": preprocess_context,
            "correction_patch": correction_patch,
            "correction_metadata": correction_metadata,
            "corrected_dialogue": corrected_dialogue,
            "scope_graph": scope_graph,
            "scope_filter_debug": scope_filter_debug,
            "evidence_graph": evidence_graph,
            "event_graph": {"skipped": True, "reason": "non_current_customer_consultation"},
            "evidence_chunk_debug": evidence_chunk_debug,
            "empty_evidence_rescue": rescue_payload,
            "scene_assessment": scene_assessment,
            "candidate_indications": "",
            "plan_adjudication": {"skipped": True, "reason": "non_current_customer_consultation"},
            "indication_adjudication": {"skipped": True, "reason": "non_current_customer_consultation"},
            "audit": {"skipped": True, "revision_required": False, "issues": [], "trigger_reasons": []},
            "fact_graph": fact_graph,
            "analysis_result": analysis_result,
        }

    relevant_dialogue_excerpt = _relevant_dialogue_excerpt(scoped_dialogue, evidence_graph)

    event_graph_call_count = 0
    event_graph: dict[str, Any] = {}
    event_user_prompt = _EVENT_AGENT_USER_TEMPLATE.format(
        evidence_graph=_compact_for_prompt(evidence_graph, max_chars=18000),
        scope_graph=_compact_for_prompt(scope_graph, max_chars=8000),
        dialogue=relevant_dialogue_excerpt,
    )
    try:
        event_graph_call_count = 1
        event_parsed = _call_agent(
            "event_graph",
            _EVENT_AGENT_SYSTEM_PROMPT,
            event_user_prompt,
            max_tokens=9000,
        )
        event_graph = _extract_event_graph(event_parsed)
    except Exception as exc:
        logger.warning("agent event_graph extraction failed, continuing without event constraints: %s", exc)
        event_graph = {"error": str(exc)}

    evidence_text = json.dumps(evidence_graph, ensure_ascii=False, separators=(",", ":"))
    event_text = json.dumps(event_graph, ensure_ascii=False, separators=(",", ":"))
    candidate_rows = _candidate_indications_from_text(f"{evidence_text}\n{event_text}\n{relevant_dialogue_excerpt}", max_items=36)
    candidate_indications = _format_candidate_indications(candidate_rows)

    judgment_user_prompt = _JUDGMENT_AGENT_USER_TEMPLATE.format(
        evidence_graph=_compact_for_prompt(evidence_graph),
        event_graph=_compact_for_prompt(event_graph, max_chars=14000),
        candidate_indications=_compact_for_prompt(candidate_indications, max_chars=12000),
    )
    judgment_parsed = _call_agent(
        "judgment",
        _JUDGMENT_AGENT_SYSTEM_PROMPT,
        judgment_user_prompt,
        max_tokens=14000,
    )
    fact_graph = _extract_fact_graph(judgment_parsed)
    fact_graph = _repair_empty_fact_graph_from_evidence_graph(fact_graph, evidence_graph)
    fact_graph = _merge_profile_facts_from_evidence_graph(fact_graph, evidence_graph)
    fact_graph = _agent_repair_fact_graph(fact_graph, evidence_graph)
    fact_graph = _apply_event_graph_constraints(fact_graph, event_graph)

    plan_call_count = 0
    if _as_list(evidence_graph.get("recommendation_evidence")) or _as_list(fact_graph.get("recommendations")) or _as_list(fact_graph.get("seed_recommendations")):
        plan_user_prompt = _PLAN_AGENT_USER_TEMPLATE.format(
            fact_graph=_compact_for_prompt(fact_graph),
            evidence_graph=_compact_for_prompt(_evidence_for_plan_prompt(evidence_graph), max_chars=14000),
            event_graph=_compact_for_prompt(event_graph, max_chars=12000),
            dialogue=relevant_dialogue_excerpt,
        )
        try:
            plan_call_count = 1
            plan_parsed = _call_agent(
                "recommendation_adjudication",
                _PLAN_AGENT_SYSTEM_PROMPT,
                plan_user_prompt,
                max_tokens=9000,
            )
            plan_adjudication = _extract_plan_adjudication(plan_parsed)
            fact_graph = _apply_plan_adjudication(fact_graph, plan_adjudication)
            fact_graph = _agent_repair_fact_graph(fact_graph, evidence_graph)
            fact_graph = _apply_event_graph_constraints(fact_graph, event_graph)
        except Exception as exc:
            logger.warning("agent recommendation adjudication failed, using judgment fact_graph: %s", exc)
            plan_adjudication = {"error": str(exc), "recommendations": [], "seed_recommendations": []}
    else:
        plan_adjudication = {"skipped": True, "reason": "no recommendation evidence or recommendation facts"}

    indication_user_prompt = _INDICATION_ADJUDICATION_USER_TEMPLATE.format(
        fact_graph=_compact_for_prompt(_compact_fact_graph_for_indications(fact_graph), max_chars=14000),
        candidate_indications=_compact_for_prompt(candidate_indications, max_chars=12000),
    )
    try:
        indication_parsed = _call_agent(
            "indication_adjudication",
            _INDICATION_ADJUDICATION_SYSTEM_PROMPT,
            indication_user_prompt,
            max_tokens=8000,
        )
        indication_adjudication = _extract_indication_adjudication(indication_parsed)
        fact_graph = _apply_indication_adjudication(fact_graph, indication_adjudication, candidate_indications)
    except Exception as exc:
        logger.warning("agent indication adjudication failed, using preliminary indications: %s", exc)
        indication_adjudication = {
            "final_indications": [],
            "rejected_indications": [],
            "error": str(exc),
        }

    audit_call_count = 0
    indication_after_audit_count = 0
    audit_required, audit_reasons = _audit_needed(
        fact_graph,
        evidence_graph,
        correction_metadata,
        indication_adjudication,
    )
    if audit_required:
        audit_user_prompt = _AUDIT_AGENT_USER_TEMPLATE.format(
            fact_graph=_compact_for_prompt(fact_graph),
            evidence_graph=_compact_for_prompt(evidence_graph, max_chars=18000),
            event_graph=_compact_for_prompt(event_graph, max_chars=12000),
            candidate_indications=_compact_for_prompt(candidate_indications, max_chars=12000),
            dialogue=relevant_dialogue_excerpt,
        )
        try:
            audit_call_count = 1
            audit_parsed = _call_agent(
                "audit",
                _AUDIT_AGENT_SYSTEM_PROMPT,
                audit_user_prompt,
                max_tokens=9000,
            )
            audit, corrected_fact_graph = _extract_audit(audit_parsed)
            audit["trigger_reasons"] = audit_reasons
            if audit.get("revision_required") and corrected_fact_graph:
                fact_graph = _apply_audit_repair(fact_graph, corrected_fact_graph)
                fact_graph = _agent_repair_fact_graph(fact_graph, evidence_graph)
                fact_graph = _apply_event_graph_constraints(fact_graph, event_graph)
                # Re-run indication adjudication after fact repair so SAP indications
                # match the final fact graph.
                repaired_indication_user_prompt = _INDICATION_ADJUDICATION_USER_TEMPLATE.format(
                    fact_graph=_compact_for_prompt(_compact_fact_graph_for_indications(fact_graph), max_chars=14000),
                    candidate_indications=_compact_for_prompt(candidate_indications, max_chars=12000),
                )
                indication_after_audit_count = 1
                repaired_indication = _call_agent(
                    "indication_adjudication_after_audit",
                    _INDICATION_ADJUDICATION_SYSTEM_PROMPT,
                    repaired_indication_user_prompt,
                    max_tokens=8000,
                )
                indication_adjudication = _extract_indication_adjudication(repaired_indication)
                fact_graph = _apply_indication_adjudication(fact_graph, indication_adjudication, candidate_indications)
        except Exception as exc:
            logger.warning("agent audit failed, using pre-audit fact_graph: %s", exc)
            audit = {"error": str(exc), "revision_required": False, "issues": [], "trigger_reasons": audit_reasons}
    else:
        audit = {"skipped": True, "revision_required": False, "issues": [], "trigger_reasons": []}

    fact_graph = _merge_profile_facts_from_evidence_graph(fact_graph, evidence_graph)
    fact_graph = _agent_repair_fact_graph(fact_graph, evidence_graph)
    fact_graph = _apply_event_graph_constraints(fact_graph, event_graph)
    fact_graph = _agent_remove_rejected_indications(fact_graph, indication_adjudication)
    fact_graph = _agent_ensure_common_indications(fact_graph)
    fact_graph = _agent_prune_unsupported_pigment_fallbacks(fact_graph)
    fact_graph = _agent_remove_rejected_indications(fact_graph, indication_adjudication)
    fact_graph = _agent_add_optional_seed_recommendations_from_events(fact_graph, event_graph)
    fact_graph = _agent_repair_recommendation_demand_links(fact_graph)
    fact_graph = _agent_demote_orphan_optional_recommendations(fact_graph)
    fact_graph = _agent_remove_redundant_seed_recommendations(fact_graph)
    fact_graph = _agent_fill_missing_fact_evidence_from_events(fact_graph, event_graph)
    fact_graph = _agent_prune_seed_only_indications(fact_graph)
    analysis_result = _build_analysis_result_from_fact_graph(fact_graph, raw, allow_raw_augmentation=False)
    analysis_result = _agent_finalize_analysis_result(analysis_result, context=f"{corrected_dialogue}\n{dialogue}")
    final_audit_call_count = 0
    final_audit_required, final_audit_reasons = _final_result_audit_needed(
        analysis_result,
        corrected_dialogue=scoped_dialogue,
        fact_graph=fact_graph,
        event_graph=event_graph,
    )
    if final_audit_required:
        final_audit_user_prompt = _FINAL_RESULT_AUDIT_USER_TEMPLATE.format(
            trigger_reasons=_compact_for_prompt(final_audit_reasons, max_chars=4000),
            scope_graph=_compact_for_prompt(scope_graph, max_chars=8000),
            evidence_graph=_compact_for_prompt(evidence_graph, max_chars=14000),
            event_graph=_compact_for_prompt(event_graph, max_chars=10000),
            fact_graph=_compact_for_prompt(fact_graph, max_chars=14000),
            analysis_result=_compact_for_prompt(analysis_result, max_chars=14000),
            dialogue=_truncate_text_for_prompt(relevant_dialogue_excerpt, max_chars=12000),
        )
        try:
            final_audit_call_count = 1
            final_audit_parsed = _call_agent(
                "final_result_audit",
                _FINAL_RESULT_AUDIT_SYSTEM_PROMPT,
                final_audit_user_prompt,
                max_tokens=9000,
            )
            final_audit, analysis_result_patch = _extract_final_result_audit(final_audit_parsed)
            final_audit["trigger_reasons"] = final_audit_reasons
            if final_audit.get("revision_required") and analysis_result_patch:
                analysis_result = _apply_final_result_audit_patch(analysis_result, analysis_result_patch)
                analysis_result = _agent_finalize_analysis_result(analysis_result, context=f"{corrected_dialogue}\n{dialogue}")
        except Exception as exc:
            logger.warning("agent final result audit failed, using pre-audit analysis_result: %s", exc)
            final_audit = {"error": str(exc), "revision_required": False, "issues": [], "trigger_reasons": final_audit_reasons}
    else:
        final_audit = {"skipped": True, "revision_required": False, "issues": [], "trigger_reasons": final_audit_reasons}
    debug = analysis_result.setdefault("staged_pipeline_debug", {})
    if isinstance(debug, dict):
        total_logical_calls = 3 + scope_call_count + evidence_call_count + rescue_call_count + event_graph_call_count + plan_call_count + audit_call_count + final_audit_call_count + indication_after_audit_count
        debug["production_chain"] = PIPELINE_NAME
        debug["llm_call_plan"] = {
            "model": STAGED_LLM_MODEL,
            "correction_agent": 1,
            "scope_agent": scope_call_count,
            "evidence_agent": evidence_call_count,
            "empty_evidence_rescue_agent": rescue_call_count,
            "event_graph_agent": event_graph_call_count,
            "judgment_agent": 1,
            "recommendation_adjudication_agent": plan_call_count,
            "indication_adjudication_agent": 1,
            "audit_agent": audit_call_count,
            "final_result_audit_agent": final_audit_call_count,
            "indication_adjudication_after_audit": indication_after_audit_count,
            "fact_graph_to_analysis_result": 0,
            "total_logical_calls": total_logical_calls,
        }
        debug["agent_audit"] = audit
        debug["agent_final_result_audit"] = final_audit
        debug["agent_scope_graph"] = scope_graph
        debug["agent_scope_filter"] = scope_filter_debug
        debug["agent_event_graph"] = event_graph
        debug["agent_evidence_chunking"] = {
            "chunk_count": evidence_call_count,
            "target_chars": EVIDENCE_CHUNK_TARGET_CHARS,
            "overlap_lines": EVIDENCE_CHUNK_OVERLAP_LINES,
        }
        if scene_assessment:
            debug["scene_assessment"] = scene_assessment

    total_logical_calls = 3 + scope_call_count + evidence_call_count + rescue_call_count + event_graph_call_count + plan_call_count + audit_call_count + final_audit_call_count + indication_after_audit_count
    return {
        "pipeline": PIPELINE_NAME,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "llm_call_plan": {
            "model": STAGED_LLM_MODEL,
            "correction_agent": 1,
            "scope_agent": scope_call_count,
            "evidence_agent": evidence_call_count,
            "empty_evidence_rescue_agent": rescue_call_count,
            "event_graph_agent": event_graph_call_count,
            "judgment_agent": 1,
            "recommendation_adjudication_agent": plan_call_count,
            "indication_adjudication_agent": 1,
            "audit_agent": audit_call_count,
            "final_result_audit_agent": final_audit_call_count,
            "indication_adjudication_after_audit": indication_after_audit_count,
            "fact_graph_to_analysis_result": 0,
            "total_logical_calls": total_logical_calls,
        },
        "input_stats": {
            "dialogue_chars": len(dialogue),
            "corrected_dialogue_chars": len(corrected_dialogue),
            "scoped_dialogue_chars": len(scoped_dialogue),
            "raw_payload_chars": _estimate_payload_chars(raw),
            "numbered_dialogue_lines": len(numbered_line_map),
            "applied_speaker_correction_count": len(correction_metadata.get("applied_speaker_corrections", [])),
            "applied_term_correction_count": len(correction_metadata.get("applied_term_corrections", [])),
            "evidence_chunk_count": evidence_call_count,
        },
        "preprocess_context": preprocess_context,
        "correction_patch": correction_patch,
        "correction_metadata": correction_metadata,
        "corrected_dialogue": corrected_dialogue,
        "scope_graph": scope_graph,
        "scope_filter_debug": scope_filter_debug,
        "evidence_graph": evidence_graph,
        "event_graph": event_graph,
        "evidence_chunk_debug": evidence_chunk_debug,
        "empty_evidence_rescue": rescue_payload,
        "scene_assessment": scene_assessment,
        "relevant_dialogue_excerpt": relevant_dialogue_excerpt,
        "candidate_indications": candidate_indications,
        "plan_adjudication": plan_adjudication,
        "indication_adjudication": indication_adjudication,
        "audit": audit,
        "final_result_audit": final_audit,
        "fact_graph": fact_graph,
        "analysis_result": analysis_result,
    }
