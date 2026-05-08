import { AudioOutlined, RobotOutlined } from '@ant-design/icons'
import { Button, Card } from 'antd'
import dayjs from 'dayjs'

import type { VisitDetail } from '@/api/visits'
import { VISIT_STATUS_MAP } from '@/api/visits'
import { formatBeijingTime } from '@/utils/time'

type AnalysisFocusArea = { area: string; surface_need?: string; deep_need?: string }
type AnalysisConcern = { type?: string; content?: string; evidence?: string }
type AnalysisResult = {
  customer_demands?: {
    focus_areas?: AnalysisFocusArea[]
  }
  customer_concerns?: {
    summary?: string
    items?: AnalysisConcern[]
  }
  strategyAnalyzeResult?: {
    strategy?: {
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

type InsightStrategyPanelData = {
  importantPoints: string[]
  followUpTiming: string
  followUpMethod: string
  followUpSuggestion: string
  valueFocus: string
  recommendedScript: string
}

type CustomerInsightBoardProps = {
  visit: VisitDetail
  onOpenVisitDetail: (visitId: string) => void
  onOpenRecording: (recordingId: string) => void
}

function formatVisitMoment(visitDate: string | null, visitTime: string | null, createdAt: string) {
  if (visitDate && visitTime) {
    return `${dayjs(visitDate).format('YYYY-MM-DD')} ${visitTime.slice(0, 5)}`
  }
  if (createdAt) {
    return formatBeijingTime(createdAt, 'YYYY-MM-DD HH:mm')
  }
  if (visitDate) {
    return dayjs(visitDate).format('YYYY-MM-DD')
  }
  return '未登记'
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
  const extracted = extractListContent(
    labeled && typeof labeled === 'object' ? (labeled as Record<string, unknown>).content ?? labeled : labeled,
  )
  if (extracted.length) return extracted.slice(0, 4)
  const fallback = [visit.notes, visit.latest_transcript_excerpt].filter(Boolean) as string[]
  return fallback.slice(0, 3)
}

function buildStrategyPanelData(
  visit: VisitDetail,
  analysis: AnalysisResult | null,
  needList: string[],
  concernList: string[],
  treatmentPlan: string[],
) : InsightStrategyPanelData {
  const strategy = analysis?.strategyAnalyzeResult?.strategy
  const importantPoints = [
    ...concernList.slice(0, 3),
    ...treatmentPlan.slice(0, 2),
  ].slice(0, 5)

  return {
    importantPoints,
    followUpTiming: strategy?.follow_up_strategy?.timing || '建议在首次接诊后 24 小时内完成首轮回访，并根据客户反馈做二次跟进。',
    followUpMethod: strategy?.follow_up_strategy?.method || '优先企业微信或电话回访，确认顾虑点后再发送案例与方案说明。',
    followUpSuggestion: strategy?.follow_up_strategy?.suggestion || '围绕客户当前最强诉求和价格/恢复顾虑做一对一跟进，先确认决策障碍，再推进成交节点。',
    valueFocus: strategy?.value_focus || '重点强调方案适配度、医生/机构背书、真实恢复预期和阶段性效果收益。',
    recommendedScript: strategy?.recommended_script || `您好，${visit.customer_name}，这边根据您本次的关注点整理了更适合您的跟进建议。我们建议先围绕${needList[0] || '当前核心诉求'}做重点沟通，再把${concernList[0] || '最主要顾虑'}逐项说清，帮助您更快做决定。`,
  }
}

function buildCustomerSummary(
  visit: VisitDetail,
  needList: string[],
  concernList: string[],
  treatmentPlan: string[],
  strategyPanel: InsightStrategyPanelData,
) {
  const stageLabel = visit.deal_status || VISIT_STATUS_MAP[visit.status]?.label || visit.status
  const timing = strategyPanel.followUpTiming.split(/[，。]/)[0]?.trim() || '下一次跟进窗口'
  const method = strategyPanel.followUpMethod.split(/[，。]/)[0]?.trim() || '企业微信或电话'
  const focus = strategyPanel.valueFocus || treatmentPlan[0] || needList[0] || visit.project_needs || '当前重点诉求'
  const concern = concernList[0]
  const parts = [
    `当前客户处于${stageLabel}阶段。`,
    concern ? `跟进时要优先化解“${concern}”这一阻力。` : '当前没有明确的单一阻力，可直接围绕核心方案推进。',
    `建议在${timing}通过${method}继续推进，重点围绕${focus}展开。`,
  ]

  return parts.join('')
}

function buildReviewBasis(visit: VisitDetail) {
  if (visit.recordings.length > 0) return '录音资料'
  return '接诊档案'
}

export function CustomerInsightBoard({
  visit,
  onOpenVisitDetail,
  onOpenRecording,
}: CustomerInsightBoardProps) {
  const analysis = (visit.latest_analysis_result ?? null) as AnalysisResult | null
  const needList = buildNeedList(analysis, visit)
  const concernList = buildConcernList(analysis)
  const treatmentPlan = buildTreatmentPlan(analysis, visit)
  const strategyPanel = buildStrategyPanelData(visit, analysis, needList, concernList, treatmentPlan)
  const currentVisitTime = formatVisitMoment(visit.visit_date, visit.visit_time, visit.created_at)
  const reviewBasis = buildReviewBasis(visit)
  const recordingSummaryLabel = visit.recordings.length > 0 ? `${visit.recordings.length} 条` : '暂无'
  const summary = buildCustomerSummary(visit, needList, concernList, treatmentPlan, strategyPanel)

  return (
    <div className="customer-insight-board">
      <div className="visit-detail-page__hero-card customer-insight-board__hero-panel">
        <div className="visit-detail-page__panel-title">最近一次接待摘要</div>
        <div className="customer-insight-board__hero-layout">
          <div className="customer-insight-board__hero-main">
            <p className="customer-insight-board__hero-text">{summary}</p>

            <div className="customer-insight-board__hero-metrics">
              <div className="customer-insight-board__hero-metric">
                <span>最近接待</span>
                <strong>{currentVisitTime}</strong>
              </div>
              <div className="customer-insight-board__hero-metric">
                <span>当前阶段</span>
                <strong>{visit.deal_status || VISIT_STATUS_MAP[visit.status]?.label || visit.status}</strong>
              </div>
              <div className="customer-insight-board__hero-metric">
                <span>复盘依据</span>
                <strong>{reviewBasis}</strong>
              </div>
              <div className="customer-insight-board__hero-metric">
                <span>关联录音</span>
                <strong>{recordingSummaryLabel}</strong>
              </div>
            </div>
          </div>

          <div className="visit-detail-page__hero-actions customer-insight-board__hero-actions">
            <Button type="primary" icon={<RobotOutlined />} onClick={() => onOpenVisitDetail(visit.id)}>
              查看接诊详情
            </Button>
            <Button
              icon={<AudioOutlined />}
              onClick={() => visit.latest_recording_id && onOpenRecording(visit.latest_recording_id)}
              disabled={!visit.latest_recording_id}
            >
              录音详情
            </Button>
          </div>
        </div>
      </div>

      <div className="customer-insight-board__compact-grid">
        <Card bordered={false} className="visit-detail-page__panel customer-insight-board__primary-card">
          <div className="visit-detail-page__panel-title">最近一次重点</div>
          <div className="customer-insight-board__mini-block">
            <span>当前主诉</span>
            <p>{needList[0] || visit.project_needs || visit.arrival_purpose || '暂无客户诉求提取。'}</p>
          </div>
          <div className="customer-insight-board__mini-block">
            <span>最大顾虑</span>
            <p>{concernList[0] || '暂未识别明显顾虑。'}</p>
          </div>
          <div className="customer-insight-board__mini-block">
            <span>方案线索</span>
            <p>{treatmentPlan[0] || '暂无治疗规划建议。'}</p>
          </div>
        </Card>

        <Card bordered={false} className="visit-detail-page__panel customer-insight-board__business-card">
          <div className="visit-detail-page__panel-title">接手建议</div>
          <div className="customer-insight-board__business-panel">
            <div className="customer-insight-board__action-grid">
              <div className="customer-insight-board__action-card">
                <span>跟进方式</span>
                <p>{strategyPanel.followUpMethod}</p>
              </div>
              <div className="customer-insight-board__action-card">
                <span>最佳跟进时机</span>
                <p>{strategyPanel.followUpTiming}</p>
              </div>
              <div className="customer-insight-board__action-card">
                <span>推进重点</span>
                <p>{strategyPanel.valueFocus}</p>
              </div>
              <div className="customer-insight-board__action-card customer-insight-board__action-card--wide">
                <span>下次动作</span>
                <p>{strategyPanel.followUpSuggestion}</p>
              </div>
            </div>
          </div>
        </Card>
      </div>
    </div>
  )
}
