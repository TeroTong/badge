import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  ArrowLeftOutlined,
  RobotOutlined,
} from '@ant-design/icons'
import { Avatar, Button, Card, Drawer, Empty, Spin, Tag } from 'antd'
import dayjs from 'dayjs'
import { useLocation, useNavigate, useParams } from 'react-router-dom'

import { fetchVisitDetail, type VisitDetail, VISIT_STATUS_MAP } from '@/api/visits'
import { sanitizeEvaluationDimensionSummary, sanitizeEvaluationSummary } from '@/utils/evaluation-summary'
import {
  buildVisitOrderLineItemMeta,
  formatVisitOrderLineItemRef,
} from '@/utils/visit-order-line-items'
import { formatBeijingTime } from '@/utils/time'

type EvaluationIssue = { description: string; evidence: string }
type AnalysisFocusArea = { area: string; surface_need?: string; deep_need?: string; discovery_process?: string }
type AnalysisConcern = { type?: string; content?: string; evidence?: string }
type AnalysisTag = { category?: string; value?: string }
type StrategyCase = { title: string; description: string; script: string }
type StaffRecommendationItem = {
  recommendation?: string
  product_or_solution?: string | null
  body_part?: string | null
  evidence?: string
  customer_response?: string
}
type StandardizedIndicationItem = {
  department_code?: string
  department_name?: string
  indication_code?: string
  indication_name?: string
  body_part_code?: string
  body_part_name?: string
  evidence?: string
}
type AnalysisResult = {
  source?: string
  customer_primary_demands?: {
    summary?: string
    items?: Array<{ demand?: string; body_part?: string | null; evidence?: string }>
  }
  staff_recommendations?: {
    summary?: string
    items?: StaffRecommendationItem[]
  }
  standardized_indications?: {
    summary?: string
    items?: StandardizedIndicationItem[]
  }
  consultation_process_evaluation?: {
    overall_score?: number
    total_score?: number
    max_total_score?: number
    overall_summary?: string
    sections?: Array<{
      name?: string
      status?: string | null
      summary?: string | null
      point_score?: number | string | null
      max_score?: number | string | null
      checkpoints?: Array<{ issues?: EvaluationIssue[] }>
    }>
  }
  customer_demands?: {
    focus_areas?: AnalysisFocusArea[]
    expectation?: {
      dialogue_type?: string
      entry_state?: string
      exit_state?: string
      turning_points?: string[]
      specific_standards?: string | null
    }
    product_preference?: {
      preferred_products?: string[]
      information_sources?: string[]
      comparison_factors?: string[]
      consultant_influence?: string
    }
  }
  customer_concerns?: {
    summary?: string
    items?: AnalysisConcern[]
  }
  customer_profile?: {
    tags?: AnalysisTag[]
  }
  consumption_intent?: {
    budget?: string | null
    willingness?: string
    decision_factors?: string[]
    evidence?: string[]
  } | null
  strategyAnalyzeResult?: {
    strategy?: {
      customer_characteristics?: Record<string, unknown>
      key_concerns?: string | string[]
      follow_up_strategy?: {
        suggestion?: string
        timing?: string
        method?: string
      }
      value_focus?: string
      recommended_script?: string
    }
  }
}

type StrategyPanelData = {
  featureSummary: string[]
  importantPoints: string[]
  followUpTiming: string
  followUpMethod: string
  followUpSuggestion: string
  valueFocus: string
  recommendedScript: string
  cases: StrategyCase[]
}

function classifyConcernType(type: string | null | undefined, detail: string | null | undefined) {
  const rawType = String(type || '').trim()
  if (rawType && rawType !== '未分类') return rawType

  const text = `${rawType} ${detail || ''}`.toLowerCase()
  if (['效果', '反弹', '自然', '恢复', '疼', '痛', '安全', '失败', '风险', '不明显'].some((kw) => text.includes(kw))) {
    return '效果类'
  }
  if (['价格', '费用', '贵', '钱', '预算', '优惠', '便宜', '划算'].some((kw) => text.includes(kw))) {
    return '价格类'
  }
  if (['机构', '医院', '别家', '对比', '其他地方', '朋友推荐', '竞争'].some((kw) => text.includes(kw))) {
    return '对比机构类'
  }
  return '其他'
}

function formatPointScore(value: number): string {
  return value.toFixed(2).replace(/\.?0+$/, '')
}

function extractTextContent(value: unknown): string | null {
  if (typeof value === 'string') {
    const text = value.trim()
    return text || null
  }
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    for (const key of ['content', 'value', 'label', 'text']) {
      const nested = extractTextContent((value as Record<string, unknown>)[key])
      if (nested) return nested
    }
  }
  return null
}

function findLabeledValue(payload: unknown, label: string): unknown {
  if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
    const record = payload as Record<string, unknown>
    if (label in record) return record[label]
    for (const value of Object.values(record)) {
      const found = findLabeledValue(value, label)
      if (found !== undefined) return found
    }
  }
  if (Array.isArray(payload)) {
    for (const item of payload) {
      const found = findLabeledValue(item, label)
      if (found !== undefined) return found
    }
  }
  return undefined
}

function extractListContent(value: unknown): string[] {
  const result: string[] = []
  const append = (item: unknown) => {
    const text = extractTextContent(item)
    if (text && !result.includes(text)) result.push(text)
  }
  if (Array.isArray(value)) {
    value.forEach(append)
  } else {
    append(value)
  }
  return result
}

function buildNeedList(analysis: AnalysisResult | null, visit: VisitDetail) {
  const focusAreas = analysis?.customer_demands?.focus_areas ?? []
  const projectNeeds = visit.project_needs ? [visit.project_needs] : []
  const lines: string[] = []

  for (const item of focusAreas) {
    const text = item.surface_need || item.deep_need || item.area
    if (text && !lines.includes(text)) lines.push(text)
  }
  for (const item of projectNeeds) {
    if (item && !lines.includes(item)) lines.push(item)
  }

  return lines.slice(0, 5)
}

function buildConcernList(analysis: AnalysisResult | null) {
  const lines: string[] = []
  if (analysis?.customer_concerns?.summary) {
    lines.push(analysis.customer_concerns.summary)
  }
  for (const item of analysis?.customer_concerns?.items ?? []) {
    const text = item.content || item.evidence
    if (text && !lines.includes(text)) lines.push(text)
  }
  return lines.slice(0, 5)
}

function buildTreatmentPlan(analysis: AnalysisResult | null, visit: VisitDetail) {
  const labeled = findLabeledValue(analysis, '治疗规划')
  const extracted = extractListContent(labeled && typeof labeled === 'object' ? (labeled as Record<string, unknown>).content ?? labeled : labeled)
  const structured = (analysis?.staff_recommendations?.items ?? [])
    .flatMap((item) => [item.recommendation, item.product_or_solution].filter(Boolean) as string[])
    .filter((text, index, array) => array.indexOf(text) === index)
  if (structured.length) return structured.slice(0, 4)
  if (extracted.length) return extracted.slice(0, 4)
  const fallback = [visit.notes, visit.latest_transcript_excerpt].filter(Boolean) as string[]
  return fallback.slice(0, 3)
}

function buildPortraitTags(analysis: AnalysisResult | null) {
  return (analysis?.customer_profile?.tags ?? [])
    .map((item) => ({
      category: item.category || '画像标签',
      value: item.value || '未标注',
    }))
    .slice(0, 8)
}

function buildBusinessSummary(visit: VisitDetail) {
  const orderContext = visit.visit_order_context
  const currentStage = visit.deal_status || orderContext?.deal_status_text || VISIT_STATUS_MAP[visit.status]?.label || visit.status
  const arrivalStatus = orderContext?.arrival_status || null

  return {
    consultationTime: visit.visit_date
      ? `${dayjs(visit.visit_date).format('YYYY-MM-DD')}${visit.visit_time ? ` ${visit.visit_time.slice(0, 5)}` : ''}`
      : formatBeijingTime(visit.created_at, 'YYYY-MM-DD HH:mm'),
    triageTime: orderContext?.triage_time || '未记录',
    createdTime: formatBeijingTime(visit.created_at, 'YYYY-MM-DD HH:mm'),
    currentStage,
    arrivalStatus: arrivalStatus && arrivalStatus !== currentStage ? arrivalStatus : null,
    consultant: visit.consultant_name || '待分配',
    doctor: visit.doctor_name || '待分配',
  }
}

function buildDemandSummary(analysis: AnalysisResult | null, visit: VisitDetail) {
  const primary = analysis?.customer_primary_demands
  const focusAreas = analysis?.customer_demands?.focus_areas ?? []
  const items = primary?.items?.length
    ? primary.items
      .map((item) => ({
        title: item.demand || '待补充',
        detail: item.body_part ? `涉及部位：${item.body_part}` : item.evidence || '暂无更多说明',
      }))
      .slice(0, 3)
    : focusAreas
      .map((item) => ({
        title: item.area || '关注部位',
        detail: item.deep_need || item.surface_need || item.discovery_process || '暂无更多说明',
      }))
      .slice(0, 3)

  const summary = primary?.summary || visit.project_needs || visit.arrival_purpose || '暂无明确诉求信息。'
  return { summary, items }
}

function buildConcernSummary(analysis: AnalysisResult | null, visit: VisitDetail) {
  const summary = analysis?.customer_concerns?.summary || null
  const concernBuckets = new Map<string, string[]>()
  for (const item of analysis?.customer_concerns?.items ?? []) {
    const detail = item.content || item.evidence || ''
    if (!detail) continue
    const title = classifyConcernType(item.type, detail)
    const current = concernBuckets.get(title) ?? []
    if (!current.includes(detail)) current.push(detail)
    concernBuckets.set(title, current)
  }
  const items = ['效果类', '价格类', '对比机构类', '其他']
    .map((title) => ({
      title,
      details: concernBuckets.get(title) ?? [],
    }))
    .filter((item) => item.details.length > 0)
    .map((item) => ({
      title: item.title,
      detail: item.details.join('；'),
    }))

  const emptyText = visit.analyzed_recording_count > 0
    ? '当前录音尚未提炼出明确顾虑。'
    : visit.recording_count > 0
      ? '录音已关联，顾虑提炼结果稍后会在这里展示。'
      : '暂未关联录音，无法结构化识别顾虑点。'

  return { summary, items, emptyText }
}

function buildProfileSummary(visit: VisitDetail, portraitTags: Array<{ category: string; value: string }>) {
  const baseItems = [
    { label: '性别', value: visit.customer_gender || '未记录' },
    { label: '年龄', value: visit.customer_age != null ? `${visit.customer_age}岁` : '未记录' },
  ]

  return {
    baseItems,
    tags: portraitTags.slice(0, 8),
  }
}

function VisitOrderLineItemsPanel({ items }: { items: NonNullable<VisitDetail['visit_order_context']>['line_items'] }) {
  if (!items.length) return null

  return (
    <div className="visit-detail-page__line-items">
      <div className="visit-detail-page__section-label">
        分诊明细{items.length > 1 ? `（合并 ${items.length} 条）` : ''}
      </div>
      <div className="visit-detail-page__line-item-grid">
        {items.map((item, index) => {
          const metaLines = buildVisitOrderLineItemMeta(item)
          return (
            <div
              key={`${item.fzdh ?? item.dzseg ?? 'line-item'}-${index}`}
              className="visit-detail-page__line-item-card"
            >
              <strong>{formatVisitOrderLineItemRef(item)}</strong>
              {metaLines.map((line) => (
                <p key={line}>{line}</p>
              ))}
              {item.note_summary ? <p className="visit-detail-page__line-item-note">备注：{item.note_summary}</p> : null}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function buildTreatmentPlanSummary(analysis: AnalysisResult | null, visit: VisitDetail, treatmentPlan: string[]) {
  const orderContext = visit.visit_order_context
  const recommendations = (analysis?.staff_recommendations?.items ?? [])
    .map((item) => ({
      title: item.product_or_solution || item.recommendation || '推荐方案',
      detail: item.recommendation || item.customer_response || '',
      bodyPart: item.body_part || null,
    }))
    .filter((item) => item.title || item.detail)
    .slice(0, 3)
  const indicationLabels = (analysis?.standardized_indications?.items ?? [])
    .map((item) => [item.indication_name, item.body_part_name].filter(Boolean).join(' · '))
    .filter(Boolean)
    .filter((text, index, array) => array.indexOf(text) === index)
    .slice(0, 4)
  const productPreference = analysis?.customer_demands?.product_preference
  const hasAnalysisPlan = Boolean(
    analysis?.staff_recommendations?.summary
    || recommendations.length
    || indicationLabels.length
    || analysis?.customer_demands?.expectation?.specific_standards
    || (productPreference?.preferred_products ?? []).length
    || (productPreference?.comparison_factors ?? []).length,
  )
  const demandRemark = orderContext?.demand_remark || null
  const summary = hasAnalysisPlan
    ? (analysis?.staff_recommendations?.summary || treatmentPlan[0] || demandRemark || '暂无明确方案摘要。')
    : (demandRemark || '暂未形成明确方案，仅保留到诊备注。')
  const planLogic = hasAnalysisPlan
    ? (
      productPreference?.consultant_influence
      || (recommendations.length ? '已结合录音中的推荐动作和客户反馈整理当前方案推进逻辑。' : null)
      || '待补充'
    )
    : null
  const effectStandard = hasAnalysisPlan
    ? (analysis?.customer_demands?.expectation?.specific_standards || '未识别到更具体的效果标准。')
    : null
  const preferredProducts = ((productPreference?.preferred_products ?? []).filter(Boolean).length
    ? (productPreference?.preferred_products ?? []).filter(Boolean)
    : []).slice(0, 4)
  const comparisonFactors = hasAnalysisPlan
    ? (productPreference?.comparison_factors ?? []).filter(Boolean).slice(0, 3)
    : []
  const rawSourceNotes = [
    orderContext?.demand_remark ? `到诊需求：${orderContext.demand_remark}` : null,
  ].filter(Boolean) as string[]
  const sourceNotes = rawSourceNotes.filter((item, index, array) => array.indexOf(item) === index)
  const contextNote = sourceNotes.length ? sourceNotes.join('；') : null

  return {
    mode: hasAnalysisPlan ? 'analysis' : 'context',
    summary,
    recommendations,
    indicationLabels,
    planLogic,
    effectStandard,
    preferredProducts,
    comparisonFactors,
    contextNote,
    caution: hasAnalysisPlan
      ? '以下内容已结合录音分析结果，可作为本次治疗规划参考。'
      : '以下内容主要来自到诊备注，只能作为接待前的方案线索，不代表已经完成正式面诊判断。',
  }
}

function buildCustomerSummary(
  visit: VisitDetail,
  needList: string[],
  concernList: string[],
  strategyPanel: StrategyPanelData | null,
) {
  const primaryNeed = needList[0] || visit.project_needs || visit.arrival_purpose || '当前求美需求'
  const primaryConcern = concernList[0] || null
  const noteReminder = visit.customer_notes || primaryConcern || '暂未补充额外提醒'
  const overview = [
    `本次接诊围绕${primaryNeed}展开。`,
    primaryConcern ? `当前最明显的顾虑是${primaryConcern}。` : '当前尚未识别到独立顾虑。',
  ].join('')

  return {
    overview,
    communicationEntry: primaryNeed,
    noteReminder,
    handoffAdvice: strategyPanel?.followUpSuggestion || '建议先确认当前最大决策障碍，再推进下一步沟通。',
    followUpTiming: strategyPanel?.followUpTiming || '建议在本次接诊后尽快完成首轮跟进。',
  }
}

function reviewLabel(pointScore?: number | null, maxScore?: number | null, status?: string | null) {
  if (typeof pointScore === 'number' && Number.isFinite(pointScore)) {
    const resolvedMaxScore = typeof maxScore === 'number' && Number.isFinite(maxScore) && maxScore > 0 ? maxScore : 1
    const ratio = pointScore / resolvedMaxScore
    const className = ratio >= 1
      ? 'visit-detail-page__review-badge visit-detail-page__review-badge--ok'
      : ratio > 0
        ? 'visit-detail-page__review-badge visit-detail-page__review-badge--neutral'
        : 'visit-detail-page__review-badge visit-detail-page__review-badge--alert'
    return { text: `${formatPointScore(pointScore)} / ${formatPointScore(resolvedMaxScore)} 分`, className }
  }
  if (status === '未达标') return { text: '0 / 1 分', className: 'visit-detail-page__review-badge visit-detail-page__review-badge--alert' }
  if (status === '部分达标') return { text: '部分得分', className: 'visit-detail-page__review-badge visit-detail-page__review-badge--neutral' }
  if (status === '达标') return { text: '1 / 1 分', className: 'visit-detail-page__review-badge visit-detail-page__review-badge--ok' }
  if (status === '有问题') return { text: '待补强', className: 'visit-detail-page__review-badge visit-detail-page__review-badge--alert' }
  if (status === '无问题' || status === '有提及') return { text: '已覆盖', className: 'visit-detail-page__review-badge visit-detail-page__review-badge--ok' }
  return { text: '未涉及', className: 'visit-detail-page__review-badge visit-detail-page__review-badge--neutral' }
}

function buildStrategyPanelData(
  visit: VisitDetail,
  analysis: AnalysisResult | null,
  needList: string[],
  concernList: string[],
  treatmentPlan: string[],
  portraitTags: Array<{ category: string; value: string }>,
): StrategyPanelData {
  const strategy = analysis?.strategyAnalyzeResult?.strategy
  const customerCharacteristics = strategy?.customer_characteristics ?? {}
  const ageText = visit.customer_age
    ? `${visit.customer_age}岁`
    : portraitTags.find((item) => item.category.includes('出生日期') || item.category.includes('年龄'))?.value
  const genderText = visit.customer_gender || portraitTags.find((item) => item.category.includes('性别'))?.value || '未标注'
  const characteristics = Object.entries(customerCharacteristics)
    .map(([key, value]) => `${key}：${Array.isArray(value) ? value.join('、') : String(value ?? '-')}`)
    .slice(0, 4)

  const featureSummary = [
    ageText ? `到诊年龄：${ageText}` : null,
    genderText ? `性别：${genderText}` : null,
    needList.length ? `需求：${needList.slice(0, 3).join('、')}` : null,
    portraitTags.length ? `职业标签：${portraitTags.slice(0, 2).map((item) => item.value).join('、')}` : null,
    ...characteristics,
  ].filter(Boolean) as string[]

  const importantPoints = [
    ...concernList.slice(0, 3),
    ...treatmentPlan.slice(0, 2),
  ].slice(0, 5)

  const followUpTiming = strategy?.follow_up_strategy?.timing || '建议在首次接诊后 24 小时内完成首轮回访，并根据客户反馈做二次跟进。'
  const followUpMethod = strategy?.follow_up_strategy?.method || '优先企业微信或电话回访，确认顾虑点后再发送案例与方案说明。'
  const followUpSuggestion = strategy?.follow_up_strategy?.suggestion || '围绕客户当前最强诉求和价格/恢复顾虑做一对一跟进，先确认决策障碍，再推进成交节点。'
  const valueFocus = strategy?.value_focus || '重点强调方案适配度、医生/机构背书、真实恢复预期和阶段性效果收益。'
  const recommendedScript = strategy?.recommended_script || `您好，${visit.customer_name}，这边根据您本次的关注点整理了更适合您的跟进建议。我们建议先围绕${needList[0] || '当前核心诉求'}做重点沟通，再把${concernList[0] || '最主要顾虑'}逐项说清，帮助您更快做决定。`

  const caseNeed = needList[0] || '同类项目需求'
  const caseConcern = concernList[0] || '术后恢复和方案适配'
  const cases: StrategyCase[] = [
    {
      title: '案例1',
      description: `同样聚焦“${caseNeed}”的客户，在确认方案适配和恢复周期后，于 48 小时内完成成交。`,
      script: `先说明她和您一样最关注“${caseNeed}”，后来主要是把“${caseConcern}”讲透之后，客户对方案的接受度就明显提升。`,
    },
    {
      title: '案例2',
      description: '另一位同类客户在对比多家机构后，最终因为医生匹配度和术后管理方案选择成交。',
      script: `她前期也在做多家比对，最后决定的关键不是单点价格，而是医生经验、方案细节和术后安排更可控。`,
    },
  ]

  return {
    featureSummary,
    importantPoints,
    followUpTiming,
    followUpMethod,
    followUpSuggestion,
    valueFocus,
    recommendedScript,
    cases,
  }
}

export function VisitDetailPage() {
  const navigate = useNavigate()
  const location = useLocation()
  const { visitId } = useParams<{ visitId: string }>()
  const [strategyOpen, setStrategyOpen] = useState(false)

  const { data, isLoading, error } = useQuery({
    queryKey: ['visit-detail-page', visitId],
    queryFn: () => fetchVisitDetail(visitId!),
    enabled: Boolean(visitId),
  })

  const analysis = (data?.latest_analysis_result ?? null) as AnalysisResult | null
  const processEvaluation = analysis?.consultation_process_evaluation
  const processSections = (processEvaluation?.sections ?? []).map((section) => ({
    name: section.name ?? '未命名大项',
    status: section.status || '未涉及',
    issues: (section.checkpoints ?? []).flatMap((checkpoint) => checkpoint.issues ?? []),
    summary: sanitizeEvaluationDimensionSummary(section.summary || ''),
    pointScore:
      typeof section.point_score === 'number'
        ? section.point_score
        : typeof section.point_score === 'string' && section.point_score.trim()
          ? Number(section.point_score)
          : null,
    maxScore:
      typeof section.max_score === 'number'
        ? section.max_score
        : typeof section.max_score === 'string' && section.max_score.trim()
          ? Number(section.max_score)
          : 1,
  }))
  const dimensions = processSections
  const evaluationTotalScore = typeof processEvaluation?.total_score === 'number' && Number.isFinite(processEvaluation.total_score)
    ? processEvaluation.total_score
    : null
  const evaluationMaxTotalScore = typeof processEvaluation?.max_total_score === 'number' && Number.isFinite(processEvaluation.max_total_score) && processEvaluation.max_total_score > 0
    ? processEvaluation.max_total_score
    : processSections.length || 9
  const needList = data ? buildNeedList(analysis, data) : []
  const concernList = buildConcernList(analysis)
  const treatmentPlan = data ? buildTreatmentPlan(analysis, data) : []
  const portraitTags = buildPortraitTags(analysis)
  const demandSummary = data ? buildDemandSummary(analysis, data) : null
  const concernSummary = data ? buildConcernSummary(analysis, data) : null
  const profileSummary = data ? buildProfileSummary(data, portraitTags) : null
  const businessSummary = data ? buildBusinessSummary(data) : null
  const visitOrderLineItems = data?.visit_order_context?.line_items ?? []
  const treatmentPlanSummary = data ? buildTreatmentPlanSummary(analysis, data, treatmentPlan) : null
  const strategyPanel = data
    ? buildStrategyPanelData(data, analysis, needList, concernList, treatmentPlan, portraitTags)
    : null
  const customerSummary = data
    ? buildCustomerSummary(data, needList, concernList, strategyPanel)
    : null

  const handleBackToVisits = () => {
    if (window.history.length > 1) {
      navigate(-1)
      return
    }
    navigate(`/admin/visits${location.search || ''}`, { replace: true })
  }

  if (isLoading) {
    return (
      <div className="visit-detail-page__loading">
        <Spin size="large" />
      </div>
    )
  }

  if (error || !data) {
    return (
      <div className="visit-detail-page__loading">
        <Empty description="接诊详情加载失败，请返回重试。" />
      </div>
    )
  }

  return (
    <div className="visit-detail-page">
      <div className="visit-detail-page__topbar">
        <Button type="text" icon={<ArrowLeftOutlined />} onClick={handleBackToVisits}>
          返回接诊记录
        </Button>
      </div>

      <section className="visit-detail-page__hero-card">
        <div className="visit-detail-page__hero-main">
          <div className="visit-detail-page__hero-identity">
            <Avatar size={54} className="visit-card__avatar">
              {data.customer_name.slice(0, 1) || '客'}
            </Avatar>
            <div>
              <div className="visit-detail-page__hero-name">{data.customer_name}</div>
              <div className="visit-detail-page__hero-subtitle">
                客户编码：{data.customer_code || '未记录'}
                {data.customer_type_label ? (
                  <Tag color={data.customer_type_code === 'V' ? 'gold' : 'green'} style={{ marginLeft: 8 }}>
                    {data.customer_type_label}
                  </Tag>
                ) : null}
              </div>
            </div>
          </div>

          <div className="visit-detail-page__hero-track-wrap">
            <div className="visit-detail-page__hero-track">
              <span />
            </div>
            <div className="visit-detail-page__hero-time">
              <div>{data.visit_date ? dayjs(data.visit_date).format('YYYY-MM-DD') : '未登记日期'}</div>
              <div>{formatBeijingTime(data.created_at, 'HH:mm:ss')}</div>
            </div>
          </div>

          <div className="visit-detail-page__hero-actions">
            <Button
              type="primary"
              icon={<RobotOutlined />}
              onClick={() => setStrategyOpen(true)}
            >
              智能跟踪策略
            </Button>
          </div>
        </div>
      </section>

      <div className="visit-detail-page__grid">
        <Card bordered={false} className="visit-detail-page__panel visit-detail-page__panel--summary">
          <div className="visit-detail-page__panel-title">客户信息总结</div>
          {customerSummary ? (
            <div className="visit-detail-page__summary-card">
              <p className="visit-detail-page__summary-lead">{customerSummary.overview}</p>
              <div className="visit-detail-page__summary-grid">
                <div className="visit-detail-page__summary-point">
                  <span>沟通切入点</span>
                  <strong>{customerSummary.communicationEntry}</strong>
                </div>
                <div className="visit-detail-page__summary-point">
                  <span>重点提醒</span>
                  <strong>{customerSummary.noteReminder}</strong>
                </div>
                <div className="visit-detail-page__summary-point">
                  <span>接手建议</span>
                  <strong>{customerSummary.handoffAdvice}</strong>
                </div>
                <div className="visit-detail-page__summary-point">
                  <span>跟进时机</span>
                  <strong>{customerSummary.followUpTiming}</strong>
                </div>
              </div>
            </div>
          ) : (
            <p className="visit-detail-page__empty-text">暂无客户信息总结。</p>
          )}
        </Card>

        <Card bordered={false} className="visit-detail-page__panel">
          <div className="visit-detail-page__panel-title">客户诉求</div>
          {demandSummary ? (
            <div className="visit-detail-page__plan-panel">
              <div className="visit-detail-page__plan-highlight">
                <span>诉求摘要</span>
                <p>{demandSummary.summary}</p>
              </div>
              {demandSummary.items.length ? (
                <>
                  <div className="visit-detail-page__section-label">重点诉求</div>
                  <div className="visit-detail-page__plan-list">
                    {demandSummary.items.map((item, index) => (
                      <div key={`${item.title}-${index}`} className="visit-detail-page__plan-item">
                        <strong>{item.title}</strong>
                        <p>{item.detail}</p>
                      </div>
                    ))}
                  </div>
                </>
              ) : (
                <p className="visit-detail-page__empty-text">当前仅同步到到诊单层面的诉求摘要，录音关联后可补充更细的结构化诉求。</p>
              )}
            </div>
          ) : (
            <p className="visit-detail-page__empty-text">暂无客户诉求提取。</p>
          )}
        </Card>

        <Card bordered={false} className="visit-detail-page__panel">
          <div className="visit-detail-page__panel-title">顾客顾虑点</div>
          {concernSummary ? (
            <div className="visit-detail-page__review-panel">
              {concernSummary.summary ? (
                <div className="visit-detail-page__plan-highlight">
                  <span>顾虑摘要</span>
                  <p>{concernSummary.summary}</p>
                </div>
              ) : null}
              {concernSummary.items.length ? (
                <>
                  <div className="visit-detail-page__section-label">分类顾虑</div>
                  <div className="visit-detail-page__review-list">
                    {concernSummary.items.map((item, index) => (
                      <div key={`${item.title}-${index}`} className="visit-detail-page__review-card">
                        <div className="visit-detail-page__review-card-head">
                          <strong>{item.title}</strong>
                        </div>
                        <p>{item.detail}</p>
                      </div>
                    ))}
                  </div>
                </>
              ) : (
                <p className="visit-detail-page__empty-text">{concernSummary.emptyText}</p>
              )}
            </div>
          ) : (
            <p className="visit-detail-page__empty-text">暂无顾虑提取。</p>
          )}
        </Card>

        <Card bordered={false} className="visit-detail-page__panel">
          <div className="visit-detail-page__panel-title">治疗规划</div>
          {treatmentPlanSummary ? (
            <div className="visit-detail-page__plan-panel">
              <div className="visit-detail-page__plan-highlight">
                <span>{treatmentPlanSummary.mode === 'analysis' ? '方案概述' : '方案线索'}</span>
                <p>{treatmentPlanSummary.summary}</p>
              </div>

              <p className="visit-detail-page__support-note">{treatmentPlanSummary.caution}</p>

              {treatmentPlanSummary.mode === 'analysis' ? (
                <>
                  <div className="visit-detail-page__section-label">方案重点</div>
                  <div className="visit-detail-page__plan-focus-grid">
                    <div className="visit-detail-page__plan-focus-card">
                      <span>核心方案</span>
                      <strong>
                        {treatmentPlanSummary.recommendations.length
                          ? treatmentPlanSummary.recommendations.map((item) => item.title).join('、')
                          : (treatmentPlan[0] || '待补充')}
                      </strong>
                    </div>
                    <div className="visit-detail-page__plan-focus-card">
                      <span>效果要求</span>
                      <strong>{treatmentPlanSummary.effectStandard || '未识别到更具体要求'}</strong>
                    </div>
                    <div className="visit-detail-page__plan-focus-card">
                      <span>客户倾向</span>
                      <strong>{treatmentPlanSummary.preferredProducts.length ? treatmentPlanSummary.preferredProducts.join('、') : '暂未识别'}</strong>
                    </div>
                    <div className="visit-detail-page__plan-focus-card">
                      <span>选择关注点</span>
                      <strong>{treatmentPlanSummary.comparisonFactors.length ? treatmentPlanSummary.comparisonFactors.join('、') : '暂未识别'}</strong>
                    </div>
                  </div>

                  {treatmentPlanSummary.indicationLabels.length ? (
                    <div className="visit-detail-page__plan-support-block">
                      <span>适应症 / 部位</span>
                      <p>以下标签用于标识本次方案匹配到的适应症与对应治疗部位，便于和后续方案建议对应查看。</p>
                      <div className="visit-detail-page__chip-row">
                        {treatmentPlanSummary.indicationLabels.map((item: string) => (
                          <Tag key={item} color="blue">{item}</Tag>
                        ))}
                      </div>
                    </div>
                  ) : null}

                  {treatmentPlanSummary.recommendations.length ? (
                    <>
                      <div className="visit-detail-page__section-label">具体推荐方案</div>
                      <div className="visit-detail-page__plan-list">
                        {treatmentPlanSummary.recommendations.map((item, index) => (
                          <div key={`${item.title}-${index}`} className="visit-detail-page__plan-item">
                            <strong>{item.title}</strong>
                            {item.bodyPart ? <Tag>{item.bodyPart}</Tag> : null}
                            {item.detail ? <p>{item.detail}</p> : null}
                          </div>
                        ))}
                      </div>
                    </>
                  ) : treatmentPlan.length ? (
                    <div className="visit-detail-page__paragraph-list">
                      {treatmentPlan.map((item) => (
                        <p key={item}>{item}</p>
                      ))}
                    </div>
                  ) : null}

                  {treatmentPlanSummary.planLogic ? (
                    <div className="visit-detail-page__plan-support-block">
                      <span>推进逻辑</span>
                      <p>{treatmentPlanSummary.planLogic}</p>
                    </div>
                  ) : null}
                </>
              ) : (
                <>
                  <div className="visit-detail-page__section-label">接待前线索</div>
                  <div className="visit-detail-page__plan-focus-grid visit-detail-page__plan-focus-grid--context">
                    <div className="visit-detail-page__plan-focus-card">
                      <span>到诊备注</span>
                      <strong>{data.visit_order_context?.demand_remark || '未记录'}</strong>
                    </div>
                  </div>

                  {treatmentPlanSummary.contextNote ? (
                    <div className="visit-detail-page__plan-support-block">
                      <span>备注线索</span>
                      <p>{treatmentPlanSummary.contextNote}</p>
                    </div>
                  ) : null}
                </>
              )}
            </div>
          ) : (
            <p className="visit-detail-page__empty-text">暂无治疗规划建议。</p>
          )}
        </Card>

        <Card bordered={false} className="visit-detail-page__panel">
          <div className="visit-detail-page__panel-title">本次业务关键信息</div>
          {businessSummary ? (
            <div className="visit-detail-page__business-panel">
              <div className="visit-detail-page__kv-grid">
                <div><span>咨询日期</span><strong>{businessSummary.consultationTime}</strong></div>
                <div><span>分诊时间</span><strong>{businessSummary.triageTime}</strong></div>
                <div><span>创建时间</span><strong>{businessSummary.createdTime}</strong></div>
                <div><span>当前阶段</span><strong>{businessSummary.currentStage}</strong></div>
              </div>
              <div className="visit-detail-page__business-grid">
                <div className="visit-detail-page__business-card">
                  <span>接待归属</span>
                  <p>咨询师：{businessSummary.consultant}</p>
                  <p>医生：{businessSummary.doctor}</p>
                </div>
                <div className="visit-detail-page__business-card">
                  <span>业务进展</span>
                  {businessSummary.arrivalStatus ? <p>到诊状态：{businessSummary.arrivalStatus}</p> : null}
                  {!businessSummary.arrivalStatus ? <p>当前暂无更细业务进展记录</p> : null}
                </div>
                <div className="visit-detail-page__business-card">
                  <span>接待备注</span>
                  <p>{data.notes || data.visit_order_context?.demand_remark || '当前暂无更多接待备注'}</p>
                </div>
              </div>
              <VisitOrderLineItemsPanel items={visitOrderLineItems} />
            </div>
          ) : (
            <p className="visit-detail-page__empty-text">暂无业务记录。</p>
          )}
        </Card>

        <Card bordered={false} className="visit-detail-page__panel">
          <div className="visit-detail-page__panel-title">客户画像</div>
          {profileSummary ? (
            <div className="visit-detail-page__business-panel">
              <div className="visit-detail-page__business-grid">
                {profileSummary.baseItems.map((item) => (
                  <div key={item.label} className="visit-detail-page__summary-point">
                    <span>{item.label}</span>
                    <strong>{item.value}</strong>
                  </div>
                ))}
              </div>
              {profileSummary.tags.length ? (
                <div className="visit-detail-page__portrait-tag-grid">
                  {profileSummary.tags.map((item) => (
                    <div key={`${item.category}-${item.value}`} className="visit-detail-page__portrait-tag">
                      <span>{item.category}</span>
                      <strong>{item.value}</strong>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="visit-detail-page__empty-text">
                  {data.analyzed_recording_count > 0
                    ? '当前尚未提炼出更多客户画像标签。'
                    : '录音画像结果稍后会在这里补充。'}
                </p>
              )}
            </div>
          ) : (
            <p className="visit-detail-page__empty-text">暂无客户画像。</p>
          )}
        </Card>

        <Card bordered={false} className="visit-detail-page__panel">
          <div className="visit-detail-page__panel-title">问诊过程评价</div>
          {dimensions.length || processEvaluation?.overall_summary ? (
            <div className="visit-detail-page__review-panel">
              {(processEvaluation?.overall_summary || evaluationTotalScore != null) ? (
                <div className="visit-detail-page__review-summary">
                  <span>整体判断</span>
                  {evaluationTotalScore != null ? (
                    <strong style={{ display: 'block', marginTop: 6, color: '#7c2d12' }}>
                      九点评分 {formatPointScore(evaluationTotalScore)} / {formatPointScore(evaluationMaxTotalScore)}
                    </strong>
                  ) : null}
                  {processEvaluation?.overall_summary ? (
                    <p>{sanitizeEvaluationSummary(processEvaluation.overall_summary)}</p>
                  ) : null}
                </div>
              ) : null}
              <div className="visit-detail-page__review-list">
                {dimensions.length ? dimensions.map((item) => {
                  const labelMeta = reviewLabel(item.pointScore, item.maxScore, item.status)
                  return (
                    <div key={item.name} className="visit-detail-page__review-card">
                      <div className="visit-detail-page__review-card-head">
                        <strong>{item.name}</strong>
                        <span className={labelMeta.className}>{labelMeta.text}</span>
                      </div>
                      <p>{sanitizeEvaluationDimensionSummary(item.summary) || (item.issues[0]?.description ?? '当前暂无进一步说明。')}</p>
                      {item.issues.length ? (
                        <ul className="visit-detail-page__review-issues">
                          {item.issues.slice(0, 2).map((issue, index) => (
                            <li key={`${item.name}-${index}`}>
                              <strong>{issue.description}</strong>
                              {issue.evidence ? <span>{issue.evidence}</span> : null}
                            </li>
                          ))}
                        </ul>
                      ) : null}
                    </div>
                  )
                }) : <p className="visit-detail-page__empty-text">暂无接诊评价。</p>}
              </div>
            </div>
          ) : (
            <p className="visit-detail-page__empty-text">
              {data.recording_count > 0 ? '录音已关联，接诊评价结果稍后会在这里展示。' : '暂未关联录音，无法生成接诊评价。'}
            </p>
          )}
        </Card>

      </div>

      <Drawer
        title="智能跟踪策略"
        placement="right"
        width={760}
        open={strategyOpen}
        onClose={() => setStrategyOpen(false)}
      >
        {strategyPanel ? (
          <div className="visit-strategy-drawer">
            <div className="visit-strategy-drawer__header">
              <div className="visit-strategy-drawer__identity">
                <Avatar size={42} className="visit-card__avatar">
                  {data.customer_name.slice(0, 1) || '客'}
                </Avatar>
                <div>
                  <div className="visit-strategy-drawer__name">{data.customer_name}</div>
                  <div className="visit-strategy-drawer__meta">客户编码: {data.customer_code || '未记录'}</div>
                </div>
              </div>
              <Tag color="blue">{data.arrival_purpose || '接诊跟进'}</Tag>
            </div>

            <div className="visit-strategy-drawer__grid">
              <section className="visit-strategy-drawer__panel">
                <div className="visit-strategy-drawer__panel-title">客户特征</div>
                <div className="visit-strategy-drawer__feature-list">
                  {strategyPanel.featureSummary.map((item) => (
                    <div key={item} className="visit-strategy-drawer__feature-item">{item}</div>
                  ))}
                </div>
                <div className="visit-strategy-drawer__sub-title">重要关注点</div>
                <ol className="visit-strategy-drawer__ordered-list">
                  {strategyPanel.importantPoints.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ol>
              </section>

              <section className="visit-strategy-drawer__panel">
                <div className="visit-strategy-drawer__panel-title">相似成功案例</div>
                <div className="visit-strategy-drawer__case-list">
                  {strategyPanel.cases.map((item) => (
                    <div key={item.title} className="visit-strategy-drawer__case-item">
                      <strong>{item.title}</strong>
                      <div className="visit-strategy-drawer__label">详情描述：</div>
                      <p>{item.description}</p>
                      <div className="visit-strategy-drawer__label">经典销售语录：</div>
                      <p>{item.script}</p>
                    </div>
                  ))}
                </div>
              </section>

              <section className="visit-strategy-drawer__panel">
                <div className="visit-strategy-drawer__panel-title">策略建议</div>
                <div className="visit-strategy-drawer__strategy-block">
                  <div className="visit-strategy-drawer__label">回访策略建议</div>
                  <p>{strategyPanel.followUpSuggestion}</p>
                </div>
                <div className="visit-strategy-drawer__kv-list">
                  <div>
                    <span>建议时机</span>
                    <strong>{strategyPanel.followUpTiming}</strong>
                  </div>
                  <div>
                    <span>建议方式</span>
                    <strong>{strategyPanel.followUpMethod}</strong>
                  </div>
                </div>
                <div className="visit-strategy-drawer__strategy-block">
                  <div className="visit-strategy-drawer__label">价值点侧重</div>
                  <p>{strategyPanel.valueFocus}</p>
                </div>
              </section>

              <section className="visit-strategy-drawer__panel">
                <div className="visit-strategy-drawer__panel-title">重要话术</div>
                <div className="visit-strategy-drawer__script-box">{strategyPanel.recommendedScript}</div>
              </section>
            </div>
          </div>
        ) : (
          <Empty description="暂无智能跟踪策略数据" />
        )}
      </Drawer>
    </div>
  )
}

export default VisitDetailPage
