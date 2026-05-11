"""分析结果的数据模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class PrimaryDemandItem(BaseModel):
    """单条顾客主要诉求。"""

    priority: int = Field(..., ge=1, description="优先级，1 为最高")
    demand: str = Field(..., description="顾客主要诉求的归纳")
    body_part: str | None = Field(default=None, description="涉及部位")
    evidence: str = Field(..., description="原话证据，必须保留时间戳")

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, data):
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        normalized.setdefault("priority", 1)
        normalized["demand"] = normalized.get("demand") or normalized.get("need") or normalized.get("content") or ""
        normalized.setdefault("body_part", normalized.get("part") or normalized.get("area"))
        normalized.setdefault("evidence", normalized.get("quote") or normalized.get("source") or "")
        return normalized


class CustomerPrimaryDemands(BaseModel):
    """顾客主要诉求抽取结果。"""

    inference_note: str | None = Field(
        default=None,
        description="若内容主要基于咨询师表述推断，此处说明推断来源",
    )
    summary: str = Field(..., description="一句话概括顾客本次主要诉求")
    items: list[PrimaryDemandItem] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, data):
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        normalized.setdefault("items", [])
        summary = normalized.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            items = normalized.get("items")
            if isinstance(items, list):
                demand_texts = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    demand = (
                        item.get("demand")
                        or item.get("need")
                        or item.get("content")
                        or ""
                    )
                    demand = str(demand).strip()
                    if demand:
                        demand_texts.append(demand)
                normalized["summary"] = "；".join(demand_texts[:3]) if demand_texts else ""
            else:
                normalized["summary"] = ""
        return normalized


class StaffRecommendationItem(BaseModel):
    """单条员工推荐/种草。"""

    recommendation: str = Field(..., description="员工推荐动作或方案归纳")
    product_or_solution: str | None = Field(default=None, description="推荐的项目、产品或方案")
    body_part: str | None = Field(default=None, description="推荐对应的部位")
    brand: str | None = Field(default=None, description="推荐方案涉及的品牌名")
    material: str | None = Field(default=None, description="推荐方案涉及的材料、产品类型或设备类型")
    dosage: str | None = Field(default=None, description="推荐方案涉及的用量、支数、剂量或治疗范围")
    price: str | None = Field(default=None, description="推荐方案涉及的报价、成交价或套餐价格")
    course_or_frequency: str | None = Field(default=None, description="推荐方案涉及的疗程、次数或频次")
    treatment_steps: list[str] = Field(default_factory=list, description="推荐方案涉及的先后处理步骤")
    implementation_notes: str | None = Field(default=None, description="推荐方案补充执行要点")
    evidence: str = Field(..., description="原话证据，必须保留时间戳")
    customer_response: str = Field(..., description="客户对推荐的反应：接受/犹豫/拒绝/未明确回应")
    demand_priority: list[int] = Field(
        default_factory=list,
        description="对应 customer_primary_demands 中的 priority 编号列表",
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, data):
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        normalized["recommendation"] = normalized.get("recommendation") or normalized.get("content") or normalized.get("proposal") or ""
        normalized.setdefault("product_or_solution", normalized.get("product") or normalized.get("solution"))
        normalized.setdefault("body_part", normalized.get("part") or normalized.get("area"))
        normalized.setdefault("brand", normalized.get("brand_name") or normalized.get("product_brand"))
        normalized.setdefault(
            "material",
            normalized.get("material_or_product") or normalized.get("product_material") or normalized.get("material_name"),
        )
        normalized.setdefault(
            "dosage",
            normalized.get("dose") or normalized.get("quantity") or normalized.get("usage_amount"),
        )
        normalized.setdefault("price", normalized.get("quoted_price") or normalized.get("quote"))
        normalized.setdefault(
            "course_or_frequency",
            normalized.get("course") or normalized.get("frequency") or normalized.get("treatment_course"),
        )
        normalized.setdefault("treatment_steps", normalized.get("steps") or normalized.get("sequence") or [])
        if isinstance(normalized.get("treatment_steps"), str):
            value = normalized["treatment_steps"].strip()
            normalized["treatment_steps"] = [value] if value else []
        elif not isinstance(normalized.get("treatment_steps"), list):
            normalized["treatment_steps"] = []
        normalized["treatment_steps"] = [
            str(item).strip()
            for item in normalized.get("treatment_steps", [])
            if str(item or "").strip()
        ]
        normalized.setdefault(
            "implementation_notes",
            normalized.get("notes") or normalized.get("detail") or normalized.get("remark"),
        )
        normalized.setdefault("evidence", normalized.get("quote") or normalized.get("source") or "")
        normalized.setdefault("customer_response", normalized.get("response") or "未明确回应")
        # demand_priority: accept int, list[int], or None → always list[int]
        dp = normalized.get("demand_priority")
        if isinstance(dp, list):
            normalized["demand_priority"] = [int(x) for x in dp if isinstance(x, (int, float))]
        elif isinstance(dp, (int, float)):
            normalized["demand_priority"] = [int(dp)]
        else:
            normalized["demand_priority"] = []
        return normalized


class StaffRecommendations(BaseModel):
    """员工推荐/种草抽取结果。"""

    summary: str = Field(..., description="一句话概括员工本次主要推荐")
    items: list[StaffRecommendationItem] = Field(default_factory=list)


class StandardizedIndicationItem(BaseModel):
    """适应症命中项。"""

    department_code: str = Field(..., description="科室编码")
    department_name: str = Field(..., description="科室名称")
    indication_code: str = Field(..., description="适应症编码")
    indication_name: str = Field(..., description="适应症名称")
    body_part_code: str = Field(..., description="标准部位编码")
    body_part_name: str = Field(..., description="标准部位名称")
    evidence: str = Field(..., description="原话证据，必须保留时间戳")

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, data):
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        normalized.setdefault("department_code", normalized.get("dept_code") or "")
        normalized.setdefault("department_name", normalized.get("dept_name") or "")
        normalized.setdefault("indication_code", normalized.get("code") or "")
        normalized.setdefault("indication_name", normalized.get("name") or normalized.get("indication") or "")
        normalized.setdefault("body_part_code", normalized.get("part_code") or "")
        normalized.setdefault("body_part_name", normalized.get("body_part") or normalized.get("part_name") or "")
        normalized.setdefault("evidence", normalized.get("quote") or normalized.get("source") or "")
        return normalized


class StandardizedIndications(BaseModel):
    """适应症提取结果。"""

    inference_note: str | None = Field(
        default=None,
        description="若内容主要基于咨询师表述推断，此处说明推断来源",
    )
    summary: str = Field(..., description="一句话概括匹配到的适应症")
    items: list[StandardizedIndicationItem] = Field(default_factory=list)


# ── 1. 客户诉求 ──────────────────────────────────────────────
class FocusArea(BaseModel):
    """改善重点部位。"""

    area: str = Field(..., description="部位名称", examples=["眼周", "下巴"])
    surface_need: str | None = Field(
        default=None,
        description="表层需求：客户自己说出的、表面可感知的诉求",
        examples=["觉得眼睛没神", "嘴巴没有形态"],
    )
    deep_need: str | None = Field(
        default=None,
        description="深层需求：经咨询师挖掘后定位到的具体问题",
        examples=["泪沟凹陷导致疲态感", "上唇形态不对称"],
    )
    discovery_process: str | None = Field(
        default=None,
        description="需求挖掘过程：咨询师如何从客户的模糊表达或防御中锁定到该痛点",
        examples=["客户称不想整容只想保养，咨询师通过观察指出泪沟凹陷导致显老"],
    )


class ExpectationTrajectory(BaseModel):
    """期望效果与心态变化轨迹。"""

    dialogue_type: str | None = Field(
        default=None,
        description="（已废弃）对话类型",
    )
    entry_state: str | None = Field(
        default=None,
        description="入口状态：客户初始的心态/立场",
        examples=["抗拒，称自然衰老就好", "明确想打卧蚕"],
    )
    exit_state: str | None = Field(
        default=None,
        description="出口状态：对话结束时客户的心态/立场",
        examples=["接受保养概念，追求性价比", "确认方案，当场成交"],
    )
    turning_points: list[str] = Field(
        default_factory=list,
        description="关键转折点：导致客户心态变化的关键对话节点",
    )
    specific_standards: str | None = Field(
        default=None,
        description="对效果的具体标准（适用于老客/直接型）",
        examples=["和上次一样的量", "不要太假、要自然"],
    )


class ProductPreference(BaseModel):
    """产品倾向分析。"""

    preferred_products: list[str] = Field(
        default_factory=list,
        description="客户倾向的产品/材料",
        examples=["肉毒素", "玻尿酸+胶原蛋白组合"],
    )
    information_sources: list[str] = Field(
        default_factory=list,
        description="客户的信息来源或参考依据",
        examples=["小红书", "朋友推荐", "之前做过"],
    )
    comparison_factors: list[str] = Field(
        default_factory=list,
        description="客户在比较产品时关注的因素",
        examples=["价格", "品牌", "持续时间", "安全性"],
    )
    consultant_influence: str | None = Field(
        default=None,
        description="咨询师对客户产品选择的引导情况",
    )


class CustomerDemands(BaseModel):
    """客户诉求：从对话中深度提炼的结构化需求分析。"""

    inference_note: str | None = Field(
        default=None,
        description="若内容主要基于咨询师表述推断，此处说明推断来源",
    )
    focus_areas: list[FocusArea] = Field(
        default_factory=list,
        description="改善重点部位列表（区分表层与深层需求）",
    )
    expectation: ExpectationTrajectory = Field(
        ...,
        description="期望效果与心态变化轨迹",
    )
    product_preference: ProductPreference = Field(
        default_factory=ProductPreference,
        description="产品倾向分析",
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, data):
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        normalized.setdefault("focus_areas", [])
        expectation = normalized.get("expectation")
        if isinstance(expectation, str):
            text = expectation.strip()
            normalized["expectation"] = {
                "entry_state": text or None,
                "exit_state": None,
                "turning_points": [],
                "specific_standards": None,
            }
        elif expectation is None:
            normalized["expectation"] = {"turning_points": []}
        elif isinstance(expectation, dict):
            expectation = dict(expectation)
            expectation.setdefault("turning_points", [])
            normalized["expectation"] = expectation
        normalized.setdefault("product_preference", {})
        return normalized


# ── 2. 顾客顾虑点 ────────────────────────────────────────────
class ConcernItem(BaseModel):
    """单条顾虑。"""

    type: str = Field(
        ...,
        description="顾虑分类：核心抗拒点 / 深层心理负担 / 外部干扰",
    )
    content: str = Field(
        ...,
        description="顾虑内容描述",
    )
    evidence: str = Field(
        ...,
        description="对话中的证据：客户原话或行为表现（引用原文）",
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, data):
        if isinstance(data, str):
            return {"type": "未分类", "content": data, "evidence": ""}
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        normalized.setdefault("type", normalized.get("category") or "未分类")
        normalized["content"] = normalized.get("content") or normalized.get("concern") or normalized.get("summary") or normalized.get("text") or ""
        normalized.setdefault("evidence", normalized.get("quote") or normalized.get("source") or "")
        return normalized


class CustomerConcerns(BaseModel):
    """顾客顾虑点：经过反向过滤后，仅保留客户真实表达的顾虑。"""

    inference_note: str | None = Field(
        default=None,
        description="若内容主要基于咨询师表述推断，此处说明推断来源",
    )
    summary: str = Field(
        ...,
        description="一段话概述客户的整体顾虑情况，保留关键原话引用",
    )
    items: list[ConcernItem] = Field(
        default_factory=list,
        description="逐条列出的具体顾虑（已过滤掉中性咨询和未被客户确认的假设性话术）",
    )


# ── 3. 客户画像 ──────────────────────────────────────────────
class ProfileTag(BaseModel):
    """客户画像标签：category 为标签分类，value 为具体值，weight_level 为权重级别(1-4)。"""

    category: str = Field(
        ...,
        examples=["出生日期", "健康风险/禁忌", "倾向治疗方式"],
    )
    value: str | None = Field(
        default=None,
        examples=["1998-05-01", "过敏史", "微创"],
    )
    weight_level: int | None = Field(
        default=None,
        ge=1,
        le=4,
        description="权重级别：1=必须询问, 2=重要, 3=一般, 4=次要",
    )
    evidence: str | None = Field(
        default=None,
        description="对话中的证据，保留时间戳",
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, data):
        if isinstance(data, str):
            parts = data.split("_", 1)
            return {
                "category": parts[0],
                "value": parts[1] if len(parts) > 1 else None,
            }
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        if not normalized.get("category"):
            tag_text = normalized.get("tag") or normalized.get("name") or normalized.get("label")
            if isinstance(tag_text, str) and tag_text:
                parts = tag_text.split("_", 1)
                normalized["category"] = parts[0]
                normalized.setdefault("value", parts[1] if len(parts) > 1 else None)
        if not normalized.get("value"):
            normalized["value"] = normalized.get("content") or normalized.get("detail") or normalized.get("name")
        normalized.setdefault("evidence", normalized.get("quote") or normalized.get("source"))
        return normalized


class CustomerProfile(BaseModel):
    """客户画像：从对话中提取的结构化标签集合。"""

    inference_note: str | None = Field(
        default=None,
        description="若内容主要基于咨询师表述推断，此处说明推断来源",
    )
    age: str | None = Field(default=None, description="客户年龄；非画像标签，仅作补充展示")
    age_evidence: str | None = Field(default=None, description="客户年龄对应的录音证据")
    tags: list[ProfileTag] = Field(default_factory=list)


# ── 3.5 消费意向 ──────────────────────────────────────────────
class ConsumptionIntent(BaseModel):
    """顾客的消费信息：预算、决策因素等。"""

    budget: str | None = Field(
        default=None,
        description="本次消费预算（客户明确提及的金额或范围）",
    )
    willingness: str = Field(
        default="未明确",
        description="历史兼容字段，不再用于业务判断",
    )
    decision_factors: list[str] = Field(
        default_factory=list,
        description="影响决策的因素",
    )
    evidence: list[str] = Field(
        default_factory=list,
        description="证据列表，每条保留时间戳",
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, data):
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        if not isinstance(normalized.get("willingness"), str) or not normalized.get("willingness", "").strip():
            normalized["willingness"] = "未明确"
        if not isinstance(normalized.get("decision_factors"), list):
            normalized["decision_factors"] = []
        if not isinstance(normalized.get("evidence"), list):
            normalized["evidence"] = []
        return normalized


# ── 4. 接诊评价 ──────────────────────────────────────────────
class EvaluationIssue(BaseModel):
    """单个评价问题。"""

    description: str = Field(..., description="问题描述")
    evidence: str = Field(..., description="证据原话，保留时间戳 [MM:SS]")


class EvaluationDimension(BaseModel):
    """单个评价维度（6 维打分制，兼容旧格式）。"""

    name: str = Field(..., examples=["医美专业知识", "适应症获取"])
    point_score: float = Field(
        default=0,
        ge=0,
        le=1,
        description="当前维度的原始得分，满分 1 分",
    )
    max_score: float = Field(
        default=1,
        ge=1,
        description="当前维度的满分，固定为 1",
    )
    score: float = Field(
        default=0,
        ge=0,
        le=10,
        description="兼容旧页面和统计的 10 分制换算分数",
    )
    status: str = Field(
        default="未达标",
        description="达标/部分达标/未达标",
    )
    issues: list[EvaluationIssue] = Field(
        default_factory=list,
        description="该维度下发现的具体问题列表",
    )
    summary: str = Field(
        default="",
        description="该维度的简要说明",
    )

    comment: str | None = Field(default=None, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _normalize_from_score(cls, data):
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        point_score = normalized.get("point_score")
        score = normalized.get("score", 0)
        max_score = normalized.get("max_score")
        # If old score-based format, convert
        if point_score is None and "score" in normalized:
            if isinstance(score, (int, float)):
                if isinstance(max_score, (int, float)) and max_score > 1:
                    normalized["point_score"] = max(0, min(float(score) / float(max_score), 1))
                elif float(score) > 1:
                    normalized["point_score"] = max(0, min(float(score) / 10.0, 1))
                else:
                    normalized["point_score"] = max(0, min(float(score), 1))
        if point_score is None and "status" in normalized:
            status = str(normalized.get("status") or "").strip()
            if status in {"无问题", "有提及", "达标", "已获取", "已介绍", "通过", "Pass"}:
                normalized["point_score"] = 1
            elif status in {"部分达标", "部分获取", "待完善", "Partial"}:
                normalized["point_score"] = 0.5
            elif status:
                normalized["point_score"] = 0
        if "score" in normalized and "status" not in normalized:
            comment = normalized.get("comment", "")
            point_score = normalized.get("point_score", 0)
            normalized["status"] = "达标" if point_score >= 1 else "未达标"
            normalized["summary"] = comment
            if point_score < 1 and comment:
                normalized.setdefault("issues", [{"description": comment, "evidence": "（旧版评分数据，无原文引用）"}])
        normalized.setdefault("point_score", 0)
        normalized.setdefault("max_score", 1)
        normalized["score"] = round(float(normalized.get("point_score", 0)) / float(normalized.get("max_score", 1)) * 10, 2)
        if "status" not in normalized:
            point_score = float(normalized.get("point_score", 0))
            max_score = float(normalized.get("max_score", 1))
            if point_score <= 0:
                normalized["status"] = "未达标"
            elif point_score >= max_score:
                normalized["status"] = "达标"
            else:
                normalized["status"] = "部分达标"
        normalized.setdefault("issues", [])
        normalized.setdefault("summary", "")
        return normalized


class ConsultationEvaluation(BaseModel):
    """接诊评价：6 维打分 + 总结。"""

    total_score: float = Field(
        default=0,
        ge=0,
        description="6 个评价面累加后的原始得分，满分 6 分",
    )
    max_total_score: float = Field(
        default=6,
        ge=1,
        description="原始总分满分，默认 6 分",
    )
    overall_score: float = Field(
        default=0,
        ge=0,
        le=10,
        description="兼容现有列表和统计的 10 分制总分",
    )

    overall_summary: str = Field(
        default="",
        description="整体评价概述",
    )
    dimensions: list[EvaluationDimension] = Field(default_factory=list)


CONSULTATION_PROCESS_EVALUATION_BLUEPRINT = (
    {
        "code": "opening",
        "name": "开场",
        "checkpoints": (
            {"code": "1.1", "name": "称呼与开场"},
            {"code": "1.2", "name": "医院品牌和实力介绍"},
            {"code": "1.3", "name": "角色与流程说明"},
        ),
    },
    {
        "code": "demand_inquiry",
        "name": "主诉问诊",
        "checkpoints": (
            {"code": "2.1", "name": "探寻顾客诉求"},
            {"code": "2.2", "name": "诉求背后的动机与顾虑"},
        ),
    },
    {
        "code": "preliminary_plan_design",
        "name": "初步方案设计",
        "checkpoints": (
            {"code": "3.1", "name": "客户情况分析"},
            {"code": "3.2", "name": "结合顾客偏好给出专业建议"},
            {"code": "3.3", "name": "案例展示"},
        ),
    },
    {
        "code": "doctor_consultation",
        "name": "医生面诊与方案",
        "checkpoints": (
            {"code": "4.1", "name": "医生的专业化介绍"},
            {"code": "4.2", "name": "清晰转述顾客需求给医生"},
            {"code": "4.3", "name": "协助讲解并记录方案"},
        ),
    },
    {
        "code": "quotation_and_close",
        "name": "报价与成交",
        "checkpoints": (
            {"code": "5.1", "name": "探寻顾客预算与意向"},
            {"code": "5.2", "name": "讲解方案的价值和对比"},
            {"code": "5.3", "name": "联合治疗项目的介绍"},
        ),
    },
    {
        "code": "post_close_followup",
        "name": "成交后跟进",
        "checkpoints": (
            {"code": "6.1", "name": "告知术后/术前注意事项"},
            {"code": "6.2", "name": "仪器/药品验真提示"},
        ),
    },
    {
        "code": "lost_deal_followup",
        "name": "未成交跟进",
        "checkpoints": (
            {"code": "7.1", "name": "未成交时保持专业与热情"},
        ),
    },
    {
        "code": "required_actions",
        "name": "必做动作",
        "checkpoints": (
            {"code": "8.1", "name": "主动添加企业微信"},
            {"code": "8.2", "name": "老带新开口种草"},
        ),
    },
    {
        "code": "negative_feedback",
        "name": "负面评价",
        "checkpoints": (
            {"code": "9.1", "name": "负面语言"},
            {"code": "9.2", "name": "不正确的医院、医生、产品介绍"},
        ),
    },
)


class ConsultationResultChiefComplaint(BaseModel):
    """面诊结果第 1 点：探寻顾客主诉和初步适应症。"""

    summary: str = Field(default="", description="一句话概括主诉和适应症")
    primary_demands: list[str] = Field(default_factory=list, description="主诉要点列表，按优先级顺序排列")
    # 兼容保留字段：新版业务口径不再单独展示“种草点”，推荐内容统一并入第 4 点“推荐方案”。
    seeding_points: list[str] = Field(default_factory=list, description="兼容保留字段，默认留空")
    standardized_indications: list[str] = Field(
        default_factory=list,
        description="初步适应症列表，格式推荐为“科室名称（科室编码）｜适应症名称（适应症编码）｜部位名称（部位编码）”",
    )


class ConsultationResultProfileSummary(BaseModel):
    """面诊结果第 5 点：顾客标签信息摘要（权重 1-4）。"""

    summary: str = Field(default="", description="一句话概括画像标签获取情况，标签按权重 1-4 管理")
    extracted_tag_count: int = Field(default=0, ge=0, description="已提取标签数")
    age: str | None = Field(default=None, description="客户年龄；非标签项")
    age_evidence: str | None = Field(default=None, description="客户年龄对应的录音证据")
    tags: list[ProfileTag] = Field(default_factory=list, description="当前录音提取出的标签")


class ConsultationResultDealFactors(BaseModel):
    """面诊结果第 2 点：成交影响因素（预算、客户顾虑与其他影响因素）。"""

    summary: str = Field(default="", description="预算、客户顾虑和其他客观影响因素的概括")
    budget: str | None = Field(default=None, description="本次预算")
    concerns: list[str] = Field(default_factory=list, description="客户主观顾虑列表")
    decision_factors: list[str] = Field(default_factory=list, description="其他客观影响因素列表，如生理期、特殊身份、流程限制等")


class ConsultationResultRecommendedPlanItem(BaseModel):
    """面诊结果第 3 点：推荐方案单项。"""

    plan: str = Field(default="", description="推荐方案名称或概括")
    acceptance: str | None = Field(default=None, description="顾客认可程度：接受/犹豫/拒绝/未明确回应")
    evidence: str | None = Field(default=None, description="对应原话证据")
    brand: str | None = Field(default=None, description="方案涉及的品牌名")
    material: str | None = Field(default=None, description="方案涉及的材料、产品类型或设备类型")
    dosage: str | None = Field(default=None, description="方案涉及的用量、支数、剂量或治疗范围")
    price: str | None = Field(default=None, description="方案涉及的报价、成交价或套餐价格")
    course_or_frequency: str | None = Field(default=None, description="方案涉及的疗程、次数或频次")
    treatment_steps: list[str] = Field(default_factory=list, description="方案涉及的先后处理步骤")
    implementation_notes: str | None = Field(default=None, description="方案补充执行要点")

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, data):
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        normalized["plan"] = normalized.get("plan") or normalized.get("recommendation") or normalized.get("content") or ""
        normalized.setdefault(
            "acceptance",
            normalized.get("acceptance") or normalized.get("customer_response") or normalized.get("response"),
        )
        normalized.setdefault("brand", normalized.get("brand_name") or normalized.get("product_brand"))
        normalized.setdefault(
            "material",
            normalized.get("material_or_product") or normalized.get("product_material") or normalized.get("material_name"),
        )
        normalized.setdefault(
            "dosage",
            normalized.get("dose") or normalized.get("quantity") or normalized.get("usage_amount"),
        )
        normalized.setdefault("price", normalized.get("quoted_price") or normalized.get("quote"))
        normalized.setdefault(
            "course_or_frequency",
            normalized.get("course") or normalized.get("frequency") or normalized.get("treatment_course"),
        )
        normalized.setdefault("treatment_steps", normalized.get("steps") or normalized.get("sequence") or [])
        if isinstance(normalized.get("treatment_steps"), str):
            value = normalized["treatment_steps"].strip()
            normalized["treatment_steps"] = [value] if value else []
        elif not isinstance(normalized.get("treatment_steps"), list):
            normalized["treatment_steps"] = []
        normalized["treatment_steps"] = [
            str(item).strip()
            for item in normalized.get("treatment_steps", [])
            if str(item or "").strip()
        ]
        normalized.setdefault(
            "implementation_notes",
            normalized.get("notes") or normalized.get("detail") or normalized.get("remark"),
        )
        normalized.setdefault("evidence", normalized.get("evidence") or normalized.get("quote") or normalized.get("source"))
        return normalized


class ConsultationResultRecommendedPlan(BaseModel):
    """面诊结果第 3 点：推荐给顾客的方案和认可程度。"""

    summary: str = Field(default="", description="一句话概括推荐方案与认可程度")
    items: list[ConsultationResultRecommendedPlanItem] = Field(default_factory=list)


class ConsultationResultDealOutcome(BaseModel):
    """面诊结果第 4 点：成交情况总结。"""

    status: str = Field(default="未明确", description="成交状态：已成交/未成交/未明确")
    summary: str = Field(default="", description="成交或未成交的业务总结")
    deal_items: list[str] = Field(default_factory=list, description="成交方案或明确讨论的方案列表")
    amount: str | None = Field(default=None, description="成交金额；若未识别则为空")
    loss_reasons: list[str] = Field(default_factory=list, description="未成交原因列表")

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, data):
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        normalized.setdefault("status", "未明确")
        normalized.setdefault("summary", "")
        deal_items = normalized.get("deal_items")
        if isinstance(deal_items, list):
            normalized_items: list[str] = []
            for item in deal_items:
                if isinstance(item, dict):
                    text = (
                        item.get("item")
                        or item.get("plan")
                        or item.get("name")
                        or item.get("content")
                        or ""
                    )
                    text = str(text).strip()
                    amount = str(item.get("amount") or "").strip()
                    if text and amount:
                        normalized_items.append(f"{text}（{amount}）")
                    elif text:
                        normalized_items.append(text)
                elif isinstance(item, str):
                    text = item.strip()
                    if text:
                        normalized_items.append(text)
            normalized["deal_items"] = normalized_items
        else:
            normalized["deal_items"] = []
        loss_reasons = normalized.get("loss_reasons")
        if isinstance(loss_reasons, str):
            normalized["loss_reasons"] = [loss_reasons] if loss_reasons.strip() else []
        elif not isinstance(loss_reasons, list):
            normalized["loss_reasons"] = []
        return normalized

    @model_validator(mode="after")
    def _enforce_status_specific_fields(self):
        if self.status not in {"已成交", "未成交", "未明确"}:
            self.status = "未明确"
        if self.status == "已成交":
            self.loss_reasons = []
        elif self.status == "未成交":
            self.deal_items = []
            self.amount = None
        else:
            self.deal_items = []
            self.amount = None
            self.loss_reasons = []
        return self


class ConsultationResult(BaseModel):
    """新的面诊结果 5 点汇总结构。"""

    chief_complaint_and_indications: ConsultationResultChiefComplaint = Field(
        default_factory=ConsultationResultChiefComplaint
    )
    deal_factors: ConsultationResultDealFactors = Field(default_factory=ConsultationResultDealFactors)
    recommended_plan: ConsultationResultRecommendedPlan = Field(default_factory=ConsultationResultRecommendedPlan)
    deal_outcome: ConsultationResultDealOutcome = Field(default_factory=ConsultationResultDealOutcome)
    customer_profile_summary: ConsultationResultProfileSummary = Field(
        default_factory=ConsultationResultProfileSummary
    )


class SapSummarySection(BaseModel):
    """SAP 总结信息中的自然语言段落素材。"""

    name: str = Field(default="", description="总结段落名称")
    content: str = Field(default="", description="可用于 SAP 总结信息的自然语言段落")
    covered_points: list[str] = Field(default_factory=list, description="本段实际覆盖到的总结小点")

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, data):
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        normalized.setdefault("name", normalized.get("title") or normalized.get("section") or "")
        normalized.setdefault("content", normalized.get("summary") or normalized.get("text") or "")
        points = normalized.get("covered_points")
        if isinstance(points, str):
            normalized["covered_points"] = [points] if points.strip() else []
        elif not isinstance(points, list):
            normalized["covered_points"] = []
        return normalized


class SapSummaryMaterials(BaseModel):
    """面向 SAP 回传“总结信息”的自然语言素材。"""

    summary: str = Field(default="", description="按机构模板或默认模板生成的总结整体精简版，可为空")
    sections: list[SapSummarySection] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, data):
        if not isinstance(data, dict):
            return {"summary": str(data or "").strip(), "sections": []}
        normalized = dict(data)
        normalized.setdefault("summary", "")
        normalized.setdefault("sections", [])
        return normalized


class ConsultationProcessEvaluationCheckpoint(BaseModel):
    """9 点问诊过程评价中的单个检查点。"""

    code: str = Field(default="", description="检查点编号，如 1.1")
    name: str = Field(default="", description="检查点名称")
    point_score: float | None = Field(default=None, ge=0, le=1, description="检查点得分，未知可为空")
    max_score: float = Field(default=1, ge=1, description="检查点满分，固定为 1")
    status: str = Field(default="", description="达标/部分达标/未达标")
    summary: str = Field(default="", description="该检查点的简要说明")
    evidence: list[str] = Field(default_factory=list, description="该检查点对应的证据原话")
    issues: list[EvaluationIssue] = Field(default_factory=list, description="该检查点下发现的问题")

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, data):
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        normalized.setdefault("code", str(normalized.get("id") or ""))
        normalized.setdefault("name", normalized.get("label") or normalized.get("title") or "")
        if isinstance(normalized.get("evidence"), str):
            normalized["evidence"] = [normalized["evidence"]] if normalized["evidence"] else []
        normalized.setdefault("evidence", [])
        normalized.setdefault("issues", [])
        normalized.setdefault("summary", normalized.get("comment") or "")
        return normalized


class ConsultationProcessEvaluationSection(BaseModel):
    """9 点问诊过程评价中的单个大项。"""

    code: str = Field(default="", description="大项编码")
    name: str = Field(default="", description="大项名称")
    point_score: float | None = Field(default=None, ge=0, le=1, description="大项得分，未知可为空")
    max_score: float = Field(default=1, ge=1, description="大项满分，固定为 1")
    status: str = Field(default="", description="达标/部分达标/未达标")
    summary: str = Field(default="", description="该大项的简要总结")
    checkpoints: list[ConsultationProcessEvaluationCheckpoint] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, data):
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        normalized.setdefault("code", str(normalized.get("id") or ""))
        normalized.setdefault("name", normalized.get("label") or normalized.get("title") or "")
        normalized.setdefault("summary", normalized.get("comment") or "")
        normalized.setdefault("checkpoints", normalized.get("items") or [])
        return normalized


class ConsultationProcessEvaluation(BaseModel):
    """新的 9 点问诊过程评价结构。"""

    total_score: float = Field(default=0, description="9 大项累计得分")
    max_total_score: float = Field(default=9, description="9 大项满分")
    overall_score: float = Field(default=0, description="归一化后的 10 分制总分")
    overall_summary: str = Field(default="", description="整体评价总结")
    sections: list[ConsultationProcessEvaluationSection] = Field(default_factory=list)


# ── 完整分析结果 ──────────────────────────────────────────────
class AnalysisResult(BaseModel):
    """一段录音的完整 AI 分析结果。"""

    customer_primary_demands: CustomerPrimaryDemands
    standardized_indications: StandardizedIndications
    consumption_intent: ConsumptionIntent
    staff_recommendations: StaffRecommendations
    customer_demands: CustomerDemands
    customer_concerns: CustomerConcerns
    customer_profile: CustomerProfile
    consultation_evaluation: ConsultationEvaluation
    consultation_result: ConsultationResult = Field(default_factory=ConsultationResult)
    sap_summary_materials: SapSummaryMaterials = Field(default_factory=SapSummaryMaterials)
    consultation_process_evaluation: ConsultationProcessEvaluation = Field(
        default_factory=ConsultationProcessEvaluation
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, data):
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        primary_demands = normalized.setdefault("customer_primary_demands", {})
        primary_demands.setdefault("summary", "")
        primary_demands.setdefault("items", [])

        standardized = normalized.setdefault("standardized_indications", {})
        standardized.setdefault("summary", "")
        standardized.setdefault("items", [])

        normalized.setdefault("consumption_intent", {})

        recommendations = normalized.setdefault("staff_recommendations", {})
        recommendations.setdefault("summary", "")
        recommendations.setdefault("items", [])

        demands = normalized.setdefault("customer_demands", {})
        demands.setdefault("focus_areas", [])
        if demands.get("expectation") is None:
            demands["expectation"] = {"turning_points": []}
        demands.setdefault("product_preference", {})

        concerns = normalized.setdefault("customer_concerns", {})
        concerns.setdefault("summary", "")
        concerns.setdefault("items", [])

        profile = normalized.setdefault("customer_profile", {})
        profile.setdefault("tags", [])

        evaluation = normalized.setdefault("consultation_evaluation", {})
        # Backward compatibility: convert old scoring format before setting defaults
        if "overall_score" in evaluation and "overall_summary" not in evaluation:
            evaluation["overall_summary"] = f"旧版评分: {evaluation.get('overall_score', 0)}"
        evaluation.setdefault("overall_summary", "")
        evaluation.setdefault("dimensions", [])

        consultation_result = normalized.setdefault("consultation_result", {})
        consultation_result.setdefault("chief_complaint_and_indications", {})
        consultation_result.setdefault("customer_profile_summary", {})
        consultation_result.setdefault("deal_factors", {})
        consultation_result.setdefault("recommended_plan", {})
        consultation_result.setdefault("deal_outcome", {})

        sap_summary = normalized.setdefault("sap_summary_materials", {})
        sap_summary.setdefault("summary", "")
        sap_summary.setdefault("sections", [])

        process_evaluation = normalized.setdefault("consultation_process_evaluation", {})
        process_evaluation.setdefault("total_score", 0)
        process_evaluation.setdefault("max_total_score", 9)
        process_evaluation.setdefault("overall_score", 0)
        process_evaluation.setdefault("overall_summary", "")
        process_evaluation.setdefault("sections", [])

        return normalized
