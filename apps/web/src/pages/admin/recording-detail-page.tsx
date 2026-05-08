import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AudioOutlined,
  ScissorOutlined,
} from '@ant-design/icons'
import {
  Alert,
  Button,
  Card,
  Collapse,
  Descriptions,
  Empty,
  InputNumber,
  message,
  Modal,
  Progress,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
} from 'antd'
import { useParams } from 'react-router-dom'

import {
  buildCustomerCharacteristics,
  CUSTOMER_CHARACTERISTIC_LABELS,
} from '@/app/analysis-display'
import { fetchAnalysisDetail } from '@/api/analysis'
import { AnalysisDetailContent } from '@/components/analysis-detail-content'

import {
  analyzeRecording,
  confirmRecordingMultiCustomerReview,
  fetchRecording,
  fetchRecordingAnalysis,
  fetchRecordingMediaBlob,
  fetchRecordingMultiCustomerReview,
  resetRecordingMultiCustomerReview,
  splitRecording,
  type RecordingAnalysisTask,
  type RecordingSplitResult,
} from '@/api/recordings'
import { SPEAKER_MAP } from '@/api/segments'
import { fetchTranscripts, triggerTranscription } from '@/api/transcripts'
import type { Transcript } from '@/api/transcripts'
import { formatRecordingDisplayName } from '@/utils/recording-display'
import { keepElementInScrollContainerView } from '@/utils/scroll'
import { isHospitalAdminOrAbove } from '@/app/roles'
import { useAuth } from '@/app/use-auth'
import { formatBeijingTime, toBeijingTime } from '@/utils/time'

type ProcessEvaluationSummary = {
  overall_score?: number
  sections?: Array<{ checkpoints?: Array<{ issues?: unknown[] }> }>
}
type AnalysisFocusArea = { area: string; surface_need: string; deep_need: string; discovery_process: string }
type AnalysisConcern = { type: string; content: string; evidence: string }
type AnalysisTag = { category: string; value: string }
type AnalysisHighlight = { label: string; value: string; detail: string }
type FaceAnalysisDetailData = {
  name: string
  score: number
  status?: string
  evidence?: string
  reasoning?: string
  suggestion?: string
}

function formatMs(ms: number): string {
  const totalSeconds = Math.floor(ms / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${minutes}:${seconds.toString().padStart(2, '0')}`
}

function formatVisitRef(orderNo: string | null | undefined, orderSeg: string | null | undefined) {
  const normalizedOrderNo = String(orderNo || '').trim()
  const normalizedOrderSeg = String(orderSeg || '').trim()
  if (!normalizedOrderNo) return '已关联接诊'
  return normalizedOrderSeg ? `${normalizedOrderNo}-${normalizedOrderSeg}` : normalizedOrderNo
}

function getVisitAnalysisStatusMeta(status: string) {
  if (status === 'done') return { label: '分析完成', color: 'success' }
  if (status === 'running') return { label: '分析中', color: 'processing' }
  if (status === 'pending') return { label: '待分析', color: 'warning' }
  if (status === 'failed') return { label: '分析失败', color: 'error' }
  return { label: '待确认', color: 'default' }
}

function splitNumberedSegments(text: string): string[] {
  const matches = Array.from(text.matchAll(/(?:^|\s)((?:\d+(?:\.\d+)?|[一二三四五六七八九十]+)[、.．]\s*[^\n]+?)(?=\s+(?:\d+(?:\.\d+)?|[一二三四五六七八九十]+)[、.．]\s*|$)/g))

  if (matches.length < 2) {
    return []
  }

  return matches
    .map((match) => match[1]?.trim())
    .filter((segment): segment is string => Boolean(segment))
}

function splitReadableSegments(text: string): string[] {
  const normalizedText = text.replace(/\r/g, '').trim()
  if (!normalizedText) {
    return []
  }

  const lines = normalizedText
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)

  if (lines.length > 1) {
    return lines
  }

  const numberedSegments = splitNumberedSegments(normalizedText)
  if (numberedSegments.length > 0) {
    return numberedSegments
  }

  const sentenceSegments = normalizedText
    .split(/(?<=[。！？])/)
    .map((segment) => segment.trim())
    .filter(Boolean)

  if (
    normalizedText.length >= 60
    && sentenceSegments.length >= 2
    && sentenceSegments.every((segment) => segment.length >= 10)
  ) {
    return sentenceSegments
  }

  const clauseSegments = normalizedText
    .split(/[；;]/)
    .map((segment) => segment.trim())
    .filter(Boolean)

  if (
    normalizedText.length >= 80
    && clauseSegments.length >= 3
    && clauseSegments.every((segment) => segment.length >= 8)
  ) {
    return clauseSegments
  }

  return [normalizedText]
}

function StructuredText({ text, minListLength = 32 }: { text: string; minListLength?: number }) {
  const segments = splitReadableSegments(text)
  const normalizedText = text.trim()
  const asList = segments.length > 1 && normalizedText.length >= minListLength

  if (asList) {
    return (
      <ul className="recording-detail-structured-list">
        {segments.map((segment, index) => (
          <li key={`${segment}-${index}`}>{segment}</li>
        ))}
      </ul>
    )
  }

  return <div className="recording-detail-structured-paragraph">{normalizedText}</div>
}

/* ---------- helpers for _original rendering ---------- */

const { Text } = Typography

/** Render a content+evidence pair from consultAnalyzeResult / requirementAnalyzeResult */
function ContentEvidence({ content, evidence }: { content: unknown; evidence?: string }) {
  const renderContent = (c: unknown): React.ReactNode => {
    if (c == null) return <Text type="secondary">-</Text>
    if (typeof c === 'string') return <StructuredText text={c} />
    if (Array.isArray(c)) {
      return (
        <ul style={{ margin: '4px 0', paddingLeft: 20 }}>
          {c.map((item, i) => (
            <li key={i}>{typeof item === 'object' ? JSON.stringify(item) : String(item)}</li>
          ))}
        </ul>
      )
    }
    if (typeof c === 'object') {
      return (
        <Descriptions column={1} size="small" bordered={false} style={{ marginTop: 4 }}>
          {Object.entries(c as Record<string, unknown>).map(([k, v]) => (
            <Descriptions.Item key={k} label={k}>{typeof v === 'object' ? JSON.stringify(v) : String(v ?? '-')}</Descriptions.Item>
          ))}
        </Descriptions>
      )
    }
    return String(c)
  }
  return (
    <div>
      <div>{renderContent(content)}</div>
      {evidence && <div style={{ color: '#999', fontSize: 12, marginTop: 4 }}>{evidence}</div>}
    </div>
  )
}

function buildAnalysisHighlights(task: RecordingAnalysisTask): AnalysisHighlight[] {
  const result = task.result ?? {}
  const processEvaluation = result.consultation_process_evaluation as
    | ProcessEvaluationSummary
    | undefined
  const demands = result.customer_demands as { focus_areas?: AnalysisFocusArea[]; expectation?: { dialogue_type?: string } } | undefined
  const concerns = result.customer_concerns as { summary?: string; items?: AnalysisConcern[] } | undefined
  const profile = result.customer_profile as { tags?: AnalysisTag[] } | undefined
  const original = result._original as Record<string, unknown> | undefined

  const processIssueCount = (processEvaluation?.sections ?? []).reduce(
    (sum, section) => sum + (section.checkpoints ?? []).reduce((inner, checkpoint) => inner + (Array.isArray(checkpoint.issues) ? checkpoint.issues.length : 0), 0),
    0,
  )
  const preferredOverallScore = typeof processEvaluation?.overall_score === 'number' ? processEvaluation.overall_score : null
  const processSectionCount = processEvaluation?.sections?.length ?? 0

  return [
    {
      label: '接诊评价',
      value: processIssueCount > 0 ? `${processIssueCount} 个问题` : (preferredOverallScore != null ? preferredOverallScore.toFixed(1) : '无问题'),
      detail: processSectionCount ? `${processSectionCount} 个问诊评价大项` : '评价尚未生成',
    },
    {
      label: '沟通类型',
      value: demands?.expectation?.dialogue_type || '未识别',
      detail: demands?.focus_areas?.length ? `识别出 ${demands.focus_areas.length} 项主要诉求` : '暂无诉求提取',
    },
    {
      label: '主要顾虑',
      value: concerns?.items?.length ? `${concerns.items.length} 项` : '暂无',
      detail: concerns?.summary || '暂未提炼顾虑摘要',
    },
    {
      label: '客户画像',
      value: profile?.tags?.length ? `${profile.tags.length} 个标签` : '暂无',
      detail: original ? `原始复盘含 ${Object.keys(original).length} 个分析模块` : '未返回原始复盘模块',
    },
  ]
}

/** Face analysis item */
function FaceItem({ id, data }: { id: string; data: FaceAnalysisDetailData }) {
  const statusColor = data.status === 'Pass' ? 'green' : data.status === 'Fail' ? 'red' : 'gold'
  const barColor = data.score >= 7 ? '#2a9d8f' : data.score >= 5 ? '#e9c46a' : '#e76f51'
  return (
    <div style={{ marginBottom: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
        <Space size={8} wrap>
          <Tag color="blue">{id}</Tag>
          <strong>{data.name}</strong>
        </Space>
        <Space size={4}>
          {data.status && <Tag color={statusColor}>{data.status}</Tag>}
          <span>{data.score.toFixed(1)}</span>
        </Space>
      </div>
      <Progress percent={data.score * 10} showInfo={false} strokeColor={barColor} size="small" />
      {data.reasoning && (
        <div style={{ color: '#555', fontSize: 12, marginTop: 4 }}>
          <StructuredText text={data.reasoning} />
        </div>
      )}
      {data.suggestion && (
        <div style={{ color: '#2a9d8f', fontSize: 12, marginTop: 4 }}>
          <strong>建议：</strong>
          <StructuredText text={data.suggestion} />
        </div>
      )}
    </div>
  )
}

function isContentEvidenceValue(value: unknown): value is { content?: unknown; evidence?: string } {
  return typeof value === 'object' && value !== null && ('content' in value || 'evidence' in value)
}

function extractFaceSummaryMeta(text: string, details?: Record<string, FaceAnalysisDetailData>) {
  const idMatch = text.match(/ID\s*([\d.]+)/i)
  const statusMatch = text.match(/：\s*(Pass|Partial|Fail)\s*）?$/i)
  const derivedId = idMatch?.[1] ?? Object.entries(details ?? {}).find(([, item]) => text.includes(item.name))?.[0]
  const label = text
    .replace(/（\s*ID\s*[\d.]+(?:：\s*(?:Pass|Partial|Fail))?\s*）/gi, '')
    .trim()

  return {
    id: derivedId,
    status: statusMatch?.[1],
    label,
  }
}

function FaceSummaryItem({
  text,
  color,
  details,
}: {
  text: string
  color: 'green' | 'red'
  details?: Record<string, FaceAnalysisDetailData>
}) {
  const meta = extractFaceSummaryMeta(text, details)
  const statusColor = meta.status === 'Pass' ? 'green' : meta.status === 'Fail' ? 'red' : meta.status === 'Partial' ? 'gold' : undefined

  return (
    <Tag color={color} style={{ margin: '2px 4px', paddingInline: 8 }}>
      <Space size={4} wrap>
        {meta.id ? <span style={{ fontWeight: 700 }}>{meta.id}</span> : null}
        <span>{meta.label}</span>
        {meta.status ? <Tag color={statusColor}>{meta.status}</Tag> : null}
      </Space>
    </Tag>
  )
}

/** Full original analysis detail panel */
function OriginalAnalysisDetail({
  original,
}: {
  original: Record<string, unknown>
}) {
  const consult = original.consultAnalyzeResult as { summary?: Record<string, { content?: unknown; evidence?: string }> } | undefined
  const requirement = original.requirementAnalyzeResult as { summary?: Record<string, unknown> } | undefined
  const face = original.faceAnalyzeResult as {
    analysis_details?: Record<string, { name: string; score: number; status?: string; evidence?: string; reasoning?: string; suggestion?: string }>
    overall_summary?: { total_score?: number; consultant_level?: string; key_strengths?: string[]; critical_misses?: string[] }
  } | undefined
  const strategy = original.strategyAnalyzeResult as {
    strategy?: {
      customer_characteristics?: Record<string, unknown>
      key_concerns?: string
      follow_up_strategy?: { suggestion?: string; timing?: string; method?: string }
      value_focus?: string
      recommended_script?: string
    }
  } | undefined
  const tags = original.tagsAnalyzeResult as {
    extracted_data?: { category: string; sub_tag: string; confidence: string; evidence: string }[]
    summary?: string
  } | undefined

  const sections: { key: string; label: string; children: React.ReactNode; wide?: boolean }[] = []

  /* --- 咨询档案 (consultAnalyzeResult) --- */
  if (consult?.summary && Object.keys(consult.summary).length > 0) {
    sections.push({
      key: 'consult',
      label: '咨询档案（16维度）',
      wide: true,
      children: (
        <Descriptions column={1} size="small" bordered labelStyle={{ width: 140, fontWeight: 600 }}>
          {Object.entries(consult.summary).map(([key, val]) => (
            <Descriptions.Item key={key} label={key}>
              {isContentEvidenceValue(val) ? (
                <ContentEvidence content={val.content} evidence={val.evidence} />
              ) : typeof val === 'object' && val !== null ? (
                <Descriptions column={1} size="small" bordered={false} style={{ marginTop: 4 }}>
                  {Object.entries(val as Record<string, unknown>).map(([subKey, subVal]) => (
                    <Descriptions.Item key={subKey} label={subKey}>
                      {isContentEvidenceValue(subVal) ? (
                        <ContentEvidence content={subVal.content} evidence={subVal.evidence} />
                      ) : (
                        <span>{JSON.stringify(subVal)}</span>
                      )}
                    </Descriptions.Item>
                  ))}
                </Descriptions>
              ) : (
                <span>{String(val ?? '-')}</span>
              )}
            </Descriptions.Item>
          ))}
        </Descriptions>
      ),
    })
  }

  /* --- 需求深度分析 (requirementAnalyzeResult) --- */
  if (requirement?.summary && Object.keys(requirement.summary).length > 0) {
    const requirementSections = requirement.summary as Record<string, Record<string, { content?: unknown; evidence?: string }>>
    sections.push({
      key: 'requirement',
      label: '需求深度分析',
      wide: true,
      children: (
        <div>
          {Object.entries(requirementSections).map(([sectionName, sectionData]) => (
            <Card key={sectionName} size="small" type="inner" title={sectionName} style={{ marginBottom: 12 }}>
              {typeof sectionData === 'object' && sectionData !== null ? (
                <Descriptions column={1} size="small" bordered={false}>
                  {Object.entries(sectionData).map(([field, val]) => (
                    <Descriptions.Item key={field} label={field}>
                      {typeof val === 'object' && val !== null && 'content' in (val as Record<string, unknown>)
                        ? <ContentEvidence content={(val as { content?: unknown; evidence?: string }).content} evidence={(val as { content?: unknown; evidence?: string }).evidence} />
                        : <span>{JSON.stringify(val)}</span>
                      }
                    </Descriptions.Item>
                  ))}
                </Descriptions>
              ) : (
                <span>{String(sectionData)}</span>
              )}
            </Card>
          ))}
        </div>
      ),
    })
  }

  /* --- 面诊能力评估 (faceAnalyzeResult) --- */
  if (face?.analysis_details) {
    const summary = face.overall_summary
    sections.push({
      key: 'face',
      label: `面诊能力评估${summary ? `（${summary.total_score}分 · ${summary.consultant_level}）` : ''}`,
      wide: true,
      children: (
        <div>
          {summary && (
            <div style={{ marginBottom: 16, padding: 12, background: '#f0f5ff', borderRadius: 8 }}>
              <Space wrap>
                <Tag color="blue">{summary.consultant_level}</Tag>
                <span>总分：<strong>{summary.total_score}</strong></span>
              </Space>
              {summary.key_strengths?.length ? (
                <div style={{ marginTop: 8 }}>
                  <Text type="success">核心优势：</Text>
                  {summary.key_strengths.map((s, i) => (
                    <FaceSummaryItem key={i} text={s} color="green" details={face.analysis_details} />
                  ))}
                </div>
              ) : null}
              {summary.critical_misses?.length ? (
                <div style={{ marginTop: 8 }}>
                  <Text type="danger">关键不足：</Text>
                  {summary.critical_misses.map((s, i) => (
                    <FaceSummaryItem key={i} text={s} color="red" details={face.analysis_details} />
                  ))}
                </div>
              ) : null}
            </div>
          )}
          {Object.entries(face.analysis_details).map(([id, data]) => (
            <FaceItem key={id} id={id} data={data} />
          ))}
        </div>
      ),
    })
  }

  /* --- 跟进策略 (strategyAnalyzeResult) --- */
  if (strategy?.strategy) {
    const s = strategy.strategy
    const customerCharacteristics = buildCustomerCharacteristics(s.customer_characteristics)
    sections.push({
      key: 'strategy',
      label: '跟进策略与推荐话术',
      children: (
        <div>
          {customerCharacteristics.length > 0 && (
            <Card size="small" type="inner" title="客户特征" style={{ marginBottom: 12 }}>
              <Descriptions column={1} size="small">
                {customerCharacteristics.map(([k, v]) => (
                  <Descriptions.Item key={k} label={CUSTOMER_CHARACTERISTIC_LABELS[k] ?? k}>
                    {Array.isArray(v) ? v.join('、') : String(v ?? '-')}
                  </Descriptions.Item>
                ))}
              </Descriptions>
            </Card>
          )}
          {s.key_concerns && (
            <Card size="small" type="inner" title="核心顾虑" style={{ marginBottom: 12 }}>
              <StructuredText text={s.key_concerns} />
            </Card>
          )}
          {s.follow_up_strategy && (
            <Card size="small" type="inner" title="跟进计划" style={{ marginBottom: 12 }}>
              {s.follow_up_strategy.timing && <div style={{ marginBottom: 8 }}><Tag color="blue">时机</Tag>{s.follow_up_strategy.timing}</div>}
              {s.follow_up_strategy.method && <div style={{ marginBottom: 8 }}><Tag color="cyan">方式</Tag>{s.follow_up_strategy.method}</div>}
              {s.follow_up_strategy.suggestion && <StructuredText text={s.follow_up_strategy.suggestion} />}
            </Card>
          )}
          {s.value_focus && (
            <Card size="small" type="inner" title="价值聚焦" style={{ marginBottom: 12 }}>
              <StructuredText text={s.value_focus} />
            </Card>
          )}
          {s.recommended_script && (
            <Card size="small" type="inner" title="推荐话术" style={{ marginBottom: 12 }}>
              <div style={{ whiteSpace: 'pre-wrap', lineHeight: 1.8, background: '#fffbe6', padding: 12, borderRadius: 8, fontSize: 13 }}>
                {s.recommended_script}
              </div>
            </Card>
          )}
        </div>
      ),
    })
  }

  /* --- 标签分析明细 (tagsAnalyzeResult) --- */
  if (tags?.extracted_data?.length) {
    sections.push({
      key: 'tags_detail',
      label: `标签分析明细（${tags.extracted_data.length}项）`,
      children: (
        <div>
          {tags.summary && <StructuredText text={tags.summary} />}
          {tags.extracted_data.map((t, i) => (
            <div key={i} style={{ marginBottom: 12, padding: 8, background: '#fafafa', borderRadius: 6 }}>
              <Space wrap>
                <Tag color="gold">{t.category}</Tag>
                <Tag>{t.sub_tag}</Tag>
                <Tag color={t.confidence === 'High' ? 'green' : t.confidence === 'Medium' ? 'gold' : 'default'}>{t.confidence}</Tag>
              </Space>
              {t.evidence && <div style={{ color: '#999', fontSize: 12, marginTop: 4 }}>{t.evidence}</div>}
            </div>
          ))}
        </div>
      ),
    })
  }

  if (sections.length === 0) return null

  return (
    <Card size="small" title="详细分析报告" style={{ marginTop: 16 }} className="recording-detail-analysis-card">
      <Collapse
        className="recording-detail-collapse"
        accordion
        items={sections.map((section) => ({
          key: section.key,
          label: section.label,
          className: section.wide ? 'recording-detail-collapse__item recording-detail-collapse__item--wide' : 'recording-detail-collapse__item',
          children: section.children,
        }))}
      />
    </Card>
  )
}

function AnalysisSummary({ task }: { task: RecordingAnalysisTask }) {
  const result = task.result ?? {}
  const demands = result.customer_demands as
    | { focus_areas?: AnalysisFocusArea[]; expectation?: { dialogue_type?: string } }
    | undefined
  const concerns = result.customer_concerns as
    | { summary?: string; items?: AnalysisConcern[] }
    | undefined
  const profile = result.customer_profile as
    | { tags?: AnalysisTag[] }
    | undefined
  const original = result._original as Record<string, unknown> | undefined

  if (task.status === 'pending' || task.status === 'running') {
    return (
      <div>
        <div style={{ marginBottom: 12 }}>分析任务已创建，系统正在处理整段对话。</div>
        <Progress percent={task.progress} status="active" />
      </div>
    )
  }

  if (task.status === 'failed') {
    return <div style={{ color: '#e76f51' }}>分析失败：{task.error_message || '未知错误'}</div>
  }

  if (task.status !== 'done' || !task.result) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无分析结果" />
  }

  const highlights = buildAnalysisHighlights(task)

  return (
    <div>
      <div className="recording-detail-summary-strip">
        {highlights.map((item) => (
          <div key={item.label} className="recording-detail-summary-chip">
            <span>{item.label}</span>
            <strong>{item.value}</strong>
            <small>{item.detail}</small>
          </div>
        ))}
      </div>

      <Collapse
        className="recording-detail-collapse recording-detail-collapse--summary"
        accordion
        items={[
          {
            key: 'demands',
            label: '客户诉求',
            children: (
              <div>
                {demands?.expectation?.dialogue_type && (
                  <div style={{ marginBottom: 12 }}>
                    <Tag color="blue">{demands.expectation.dialogue_type}</Tag>
                  </div>
                )}
                {demands?.focus_areas?.length ? (
                  demands.focus_areas.map((item, index) => (
                    <div key={`${item.area}-${index}`} style={{ marginBottom: 12 }}>
                      <strong>{item.area}</strong>
                      <div style={{ color: '#666', marginTop: 4 }}>表层诉求：{item.surface_need || '-'}</div>
                      <div style={{ color: '#666', marginTop: 4 }}>深层诉求：{item.deep_need || '-'}</div>
                      <div style={{ color: '#999', marginTop: 4 }}>
                        <strong>发现依据：</strong>
                        {item.discovery_process ? <StructuredText text={item.discovery_process} /> : '-'}
                      </div>
                    </div>
                  ))
                ) : (
                  <div style={{ color: '#999' }}>暂无客户诉求提取</div>
                )}
              </div>
            ),
          },
          {
            key: 'concerns',
            label: '主要顾虑',
            children: (
              <div>
                {concerns?.summary && <StructuredText text={concerns.summary} />}
                {concerns?.items?.length ? (
                  concerns.items.map((item, index) => (
                    <div key={`${item.type}-${index}`} style={{ marginBottom: 12 }}>
                      <strong>{item.type}</strong>
                      <div style={{ color: '#666', marginTop: 4 }}>
                        <StructuredText text={item.content} />
                      </div>
                      <div style={{ color: '#999', marginTop: 4 }}>
                        <StructuredText text={item.evidence} />
                      </div>
                    </div>
                  ))
                ) : (
                  <div style={{ color: '#999' }}>暂无顾虑提取</div>
                )}
              </div>
            ),
          },
          {
            key: 'profile',
            label: '客户画像',
            children: profile?.tags?.length ? (
              <Space wrap>
                {profile.tags.map((item, index) => (
                  <Tag key={`${item.category}-${item.value}-${index}`} color="gold">
                    {item.category}：{item.value}
                  </Tag>
                ))}
              </Space>
            ) : (
              <div style={{ color: '#999' }}>暂无客户画像</div>
            ),
          },
        ]}
      />

      {original && Object.keys(original).length > 0 && (
        <OriginalAnalysisDetail original={original as Record<string, unknown>} />
      )}
    </div>
  )
}

void AnalysisSummary

export function RecordingDetailPage() {
  const { recordingId } = useParams<{ recordingId: string }>()
  const qc = useQueryClient()
  const auth = useAuth()
  const [manualPolling, setManualPolling] = useState(false)
  const [manualAnalysisPolling, setManualAnalysisPolling] = useState(false)
  const [isAudioPlaying, setIsAudioPlaying] = useState(false)
  const audioRef = useRef<HTMLAudioElement>(null)
  const [playbackMs, setPlaybackMs] = useState<number | null>(null)
  const [splitModalOpen, setSplitModalOpen] = useState(false)
  const [splitAtSeconds, setSplitAtSeconds] = useState<number | null>(null)
  const [multiCustomerMappingDraft, setMultiCustomerMappingDraft] = useState<Record<string, string>>({})
  const bubbleListRef = useRef<HTMLDivElement>(null)
  const activeBubbleRef = useRef<HTMLDivElement>(null)

  // Auto-scroll to active bubble on playback
  useEffect(() => {
    keepElementInScrollContainerView(bubbleListRef.current, activeBubbleRef.current, {
      topPadding: 72,
      bottomPadding: 96,
    })
  }, [playbackMs])

  const { data: recording, isLoading: loadingRec } = useQuery({
    queryKey: ['recording', recordingId],
    queryFn: () => fetchRecording(recordingId!),
    enabled: !!recordingId,
    refetchIntervalInBackground: false,
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status === 'transcribing' || manualPolling ? 3000 : false
    },
  })

  const pollingTranscript = manualPolling || recording?.status === 'transcribing'

  const {
    data: multiCustomerReview,
    isLoading: multiCustomerReviewLoading,
  } = useQuery({
    queryKey: ['recording-multi-customer-review', recordingId],
    queryFn: () => fetchRecordingMultiCustomerReview(recordingId!),
    enabled: !!recordingId && (recording?.linked_visit_ids?.length ?? 0) > 1,
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status === 'analyzing' ? 5000 : false
    },
  })

  const { data: transcriptsData } = useQuery({
    queryKey: ['transcripts', recordingId],
    queryFn: () => fetchTranscripts({ recording_id: recordingId, page_size: 100 }),
    enabled: !!recordingId,
    refetchIntervalInBackground: false,
    refetchInterval: pollingTranscript ? 3000 : false,
  })

  const transcript: Transcript | undefined = transcriptsData?.items?.[0]

  const { data: analysisTask } = useQuery({
    queryKey: ['recording-analysis', recordingId],
    queryFn: () => fetchRecordingAnalysis(recordingId!),
    enabled: !!recordingId,
    refetchIntervalInBackground: false,
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status === 'pending' || status === 'running' || manualAnalysisPolling ? 3000 : false
    },
  })

  const analysisFileId = recordingId ? `recording_${recordingId}` : null

  const {
    data: analysisDetail,
    isLoading: loadingAnalysisDetail,
    error: analysisDetailError,
  } = useQuery({
    queryKey: ['analysis-detail-by-recording', analysisFileId],
    queryFn: () => fetchAnalysisDetail(analysisFileId!),
    enabled: !!analysisFileId && analysisTask?.status === 'done',
  })

  const {
    data: audioBlob,
    isFetching: loadingAudio,
  } = useQuery({
    queryKey: ['recording-audio', recordingId],
    queryFn: () => fetchRecordingMediaBlob(recordingId!),
    enabled: !!recordingId,
    retry: false,
  })

  const audioUrl = useMemo(() => (audioBlob ? URL.createObjectURL(audioBlob) : null), [audioBlob])

  useEffect(() => {
    if (!audioUrl) return
    return () => {
      URL.revokeObjectURL(audioUrl)
    }
  }, [audioUrl])

  useEffect(() => {
    if (!manualPolling) return

    const recordingBusy = recording?.status === 'transcribing'
    const transcriptBusy = transcript?.status === 'pending' || transcript?.status === 'processing'
    if (recordingBusy || transcriptBusy) return

    const timer = window.setTimeout(() => {
      setManualPolling(false)
    }, 0)

    return () => window.clearTimeout(timer)
  }, [manualPolling, recording?.status, transcript?.status])

  useEffect(() => {
    if (!manualAnalysisPolling) return

    const analysisBusy = analysisTask?.status === 'pending' || analysisTask?.status === 'running'
    if (analysisBusy) return

    const timer = window.setTimeout(() => {
      setManualAnalysisPolling(false)
    }, 0)

    return () => window.clearTimeout(timer)
  }, [manualAnalysisPolling, analysisTask?.status])

  const multiCustomerDefaultMapping = useMemo(() => {
    if (!multiCustomerReview?.required) return {}
    const next: Record<string, string> = {}
    const segmentIds = multiCustomerReview.segments.map((segment) => segment.id)

    for (const visitAnalysis of multiCustomerReview.visit_analyses) {
      if (visitAnalysis.customer_segment_id && segmentIds.includes(visitAnalysis.customer_segment_id)) {
        next[visitAnalysis.visit_id] = visitAnalysis.customer_segment_id
        continue
      }
      const usedSegmentIds = new Set(Object.values(next).filter(Boolean))
      next[visitAnalysis.visit_id] = segmentIds.find((segmentId) => !usedSegmentIds.has(segmentId)) || ''
    }

    return next
  }, [multiCustomerReview])

  const resolvedMultiCustomerMapping = useMemo(
    () => ({ ...multiCustomerDefaultMapping, ...multiCustomerMappingDraft }),
    [multiCustomerDefaultMapping, multiCustomerMappingDraft],
  )

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['recording', recordingId] })
    qc.invalidateQueries({ queryKey: ['transcripts', recordingId] })
    qc.invalidateQueries({ queryKey: ['recording-analysis', recordingId] })
    qc.invalidateQueries({ queryKey: ['analysis-detail-by-recording', analysisFileId] })
    qc.invalidateQueries({ queryKey: ['recording-multi-customer-review', recordingId] })
  }

  const triggerMut = useMutation({
    mutationFn: () => triggerTranscription(recordingId!),
    onSuccess: () => {
      message.success('已触发转写')
      setManualPolling(true)
      invalidate()
    },
    onError: () => message.error('触发转写失败'),
  })

  const analyzeMut = useMutation({
    mutationFn: () => analyzeRecording(recordingId!),
    onSuccess: () => {
      message.success('已触发分析')
      setManualAnalysisPolling(true)
      invalidate()
    },
    onError: (err: unknown) => {
      const msg = (err as { message?: string })?.message || '触发分析失败'
      message.error(msg)
    },
  })

  const splitMut = useMutation({
    mutationFn: () => {
      if (!recordingId || splitAtSeconds == null) {
        throw new Error('请先填写裁切时间点')
      }
      return splitRecording(recordingId, { split_at_seconds: splitAtSeconds, confirm: true })
    },
    onSuccess: async (result: RecordingSplitResult) => {
      setSplitModalOpen(false)
      message.success(result.message || '录音裁切完成')
      await qc.invalidateQueries({ queryKey: ['recording', recordingId] })
      await qc.invalidateQueries({ queryKey: ['recording-audio', recordingId] })
      await qc.invalidateQueries({ queryKey: ['transcripts', recordingId] })
      await qc.invalidateQueries({ queryKey: ['recordings'] })
      for (const part of result.parts) {
        await qc.invalidateQueries({ queryKey: ['recording', part.recording.id] })
        await qc.invalidateQueries({ queryKey: ['transcripts', part.recording.id] })
      }
      Modal.success({
        title: '裁切完成',
        content: (
          <div>
            <p>已生成 {result.parts.length} 段新录音，原录音已隐藏。</p>
            <ul style={{ paddingLeft: 18, marginBottom: 0 }}>
              {result.parts.map((part) => (
                <li key={part.recording.id}>
                  第 {part.part_index} 段：{formatRecordingDisplayName(part.recording.file_name, part.recording.created_at)}
                </li>
              ))}
            </ul>
          </div>
        ),
      })
    },
    onError: (err: unknown) => {
      const msg = err instanceof Error ? err.message : '录音裁切失败'
      message.error(msg)
    },
  })

  const multiCustomerConfirmMut = useMutation({
    mutationFn: () => {
      if (!recordingId || !multiCustomerReview) {
        throw new Error('当前录音还没有多客户确认数据')
      }
      return confirmRecordingMultiCustomerReview(
        recordingId,
        multiCustomerReview.visit_analyses.map((item) => ({
          visit_id: item.visit_id,
          customer_segment_id: resolvedMultiCustomerMapping[item.visit_id] || '',
        })),
      )
    },
    onSuccess: async () => {
      message.success('客户对应关系已确认，系统已开始按到诊单分别生成分析结果')
      await qc.invalidateQueries({ queryKey: ['recording-multi-customer-review', recordingId] })
      await qc.invalidateQueries({ queryKey: ['recording-analysis', recordingId] })
      await qc.invalidateQueries({ queryKey: ['analysis-detail-by-recording', analysisFileId] })
    },
    onError: (err: unknown) => {
      const msg = err instanceof Error ? err.message : '确认失败，请检查客户段是否重复选择'
      message.error(msg)
    },
  })

  const multiCustomerResetMut = useMutation({
    mutationFn: () => {
      if (!recordingId) {
        throw new Error('当前录音还没有多客户确认数据')
      }
      return resetRecordingMultiCustomerReview(recordingId)
    },
    onSuccess: async () => {
      setMultiCustomerMappingDraft({})
      message.success('已解除客户对应确认，可重新匹配客户段')
      await qc.invalidateQueries({ queryKey: ['recording-multi-customer-review', recordingId] })
      await qc.invalidateQueries({ queryKey: ['recording-analysis', recordingId] })
      await qc.invalidateQueries({ queryKey: ['analysis-detail-by-recording', analysisFileId] })
    },
    onError: (err: unknown) => {
      const msg = err instanceof Error ? err.message : '解除失败，请稍后再试'
      message.error(msg)
    },
  })

  if (loadingRec || !recording) {
    return <Spin style={{ display: 'block', margin: '80px auto' }} size="large" />
  }

  const startedAt = recording.created_at ? formatBeijingTime(recording.created_at, 'YYYY-MM-DD HH:mm:ss') : '-'
  const endedAt = recording.created_at && recording.duration_seconds
    ? toBeijingTime(recording.created_at).add(recording.duration_seconds, 'second').format('YYYY-MM-DD HH:mm:ss')
    : '-'
  const durationLabel = recording.duration_seconds != null
    ? `${Math.floor(recording.duration_seconds / 60)}分${recording.duration_seconds % 60}秒`
    : '-'
  const currentUser = auth.status === 'authenticated' ? auth.user : null
  const canSplitRecording = Boolean(
    currentUser
    && recording.status !== 'filtered'
    && (recording.duration_seconds ?? 0) > 1
    && (
      isHospitalAdminOrAbove(currentUser.role)
      || (recording.staff_id && recording.staff_id === currentUser.staff_id)
    ),
  )
  const openSplitModal = () => {
    const durationSeconds = recording.duration_seconds ?? 0
    const currentSeconds = playbackMs != null ? Math.floor(playbackMs / 1000) : 0
    const defaultSeconds = currentSeconds > 0 && currentSeconds < durationSeconds
      ? currentSeconds
      : Math.max(1, Math.floor(durationSeconds / 2))
    setSplitAtSeconds(defaultSeconds)
    setSplitModalOpen(true)
  }
  const canAnalyze = transcript?.status === 'completed'
  const shouldShowAnalyzeAction = canAnalyze && (!analysisTask || analysisTask.status === 'failed')
  const activeUtterance = transcript?.utterances?.find(
    (utterance) => playbackMs != null && playbackMs >= utterance.begin_ms && playbackMs < utterance.end_ms,
  )
  const activeSpeakerLabel = activeUtterance ? (SPEAKER_MAP[activeUtterance.speaker]?.label ?? activeUtterance.speaker) : null
  const activeSpeakerColor = activeUtterance ? (SPEAKER_MAP[activeUtterance.speaker]?.color ?? 'default') : 'default'
  const activePlaybackPercent = activeUtterance && playbackMs != null
    ? Math.max(
      0,
      Math.min(
        100,
        ((playbackMs - activeUtterance.begin_ms) / Math.max(1, activeUtterance.end_ms - activeUtterance.begin_ms)) * 100,
      ),
    )
    : 0
  const multiCustomerSelectedSegmentIds = multiCustomerReview
    ? multiCustomerReview.visit_analyses.map((item) => resolvedMultiCustomerMapping[item.visit_id]).filter(Boolean)
    : []
  const hasDuplicateMultiCustomerMapping = new Set(multiCustomerSelectedSegmentIds).size !== multiCustomerSelectedSegmentIds.length
  const multiCustomerMappingComplete = Boolean(
    multiCustomerReview?.required
    && multiCustomerReview.visit_analyses.length > 0
    && multiCustomerReview.visit_analyses.every((item) => Boolean(resolvedMultiCustomerMapping[item.visit_id]))
    && !hasDuplicateMultiCustomerMapping,
  )
  const shouldShowMultiCustomerReview = (recording.linked_visit_ids?.length ?? 0) > 1

  return (
    <div className="recording-detail-page">
      <header className="recording-detail-page__header">
        <div>
          <p className="recording-detail-page__eyebrow">录音中心 / 录音详情</p>
          <h1>
            <AudioOutlined style={{ marginRight: 10 }} />
            录音与分析详情
          </h1>
          <p className="recording-detail-page__summary">
            在同一页串联音频、ASR 逐字稿和分析结果，方便边听边核对模型判断。
          </p>
        </div>
      </header>

      <div className="recording-detail-page__top-grid">
        <Card size="small" className="recording-detail-panel recording-detail-panel--overview" title="录音概览">
          <Descriptions column={{ xs: 1, sm: 2, md: 3 }} size="small">
            <Descriptions.Item label="录音文件名">{formatRecordingDisplayName(recording.file_name, recording.created_at)}</Descriptions.Item>
            <Descriptions.Item label="员工">{recording.staff_name || '-'}</Descriptions.Item>
            <Descriptions.Item label="设备 ID">{recording.device_id || '-'}</Descriptions.Item>
            <Descriptions.Item label="录音开始时间">{startedAt}</Descriptions.Item>
            <Descriptions.Item label="录音结束时间">{endedAt}</Descriptions.Item>
            <Descriptions.Item label="录音时长">{durationLabel}</Descriptions.Item>
          </Descriptions>

          <div className="recording-detail-audio-block">
            <strong className="recording-detail-audio-block__title">音频播放</strong>
            {loadingAudio ? (
              <Spin size="small" />
            ) : audioUrl ? (
              <audio
                ref={audioRef}
                controls
                preload="metadata"
                src={audioUrl}
                style={{ width: '100%' }}
                onPlay={() => setIsAudioPlaying(true)}
                onPause={() => setIsAudioPlaying(false)}
                onTimeUpdate={(e) => setPlaybackMs(Math.round(e.currentTarget.currentTime * 1000))}
                onEnded={() => {
                  setPlaybackMs(null)
                  setIsAudioPlaying(false)
                }}
              >
                您的浏览器暂不支持音频播放。
              </audio>
            ) : (
              <span style={{ color: '#999', fontSize: 13 }}>暂无可播放的音频文件</span>
            )}

            {canSplitRecording ? (
              <div className="recording-detail-split-tools">
                <Button icon={<ScissorOutlined />} onClick={openSplitModal}>
                  按时间点裁切
                </Button>
                <span>
                  当前定位：{playbackMs != null ? formatMs(playbackMs) : '未定位'}
                </span>
              </div>
            ) : null}

            <div className={`recording-detail-now-playing${activeUtterance ? ' recording-detail-now-playing--active' : ''}`}>
              <div className="recording-detail-now-playing__header">
                <strong>{activeUtterance ? (isAudioPlaying ? '正在播放' : '当前定位') : '播放提示'}</strong>
                {activeUtterance ? (
                  <div className="recording-detail-now-playing__meta">
                    <Tag color={activeSpeakerColor} style={{ margin: 0 }}>{activeSpeakerLabel}</Tag>
                    <span>
                      {formatMs(activeUtterance.begin_ms)} - {formatMs(activeUtterance.end_ms)}
                    </span>
                  </div>
                ) : (
                  <span>点击下方任一句转写可从对应位置开始播放</span>
                )}
              </div>
              <div className="recording-detail-now-playing__body">
                {activeUtterance ? activeUtterance.text : '播放器开始播放后，这里会实时显示当前高亮句子。'}
              </div>
              {activeUtterance && (
                <div className="recording-detail-now-playing__progress" aria-hidden="true">
                  <span style={{ width: `${activePlaybackPercent}%` }} />
                </div>
              )}
            </div>
          </div>

          <div className="recording-detail-inline-section">
            <Collapse
              className="recording-detail-inline-collapse"
              items={[
                {
                  key: 'transcript',
                  label: <strong>ASR 对话全文</strong>,
                  extra: (
                    <Space size={8} wrap>
                      {(!transcript || transcript.status === 'failed') && (
                        <Button
                          type="primary"
                          loading={triggerMut.isPending}
                          onClick={(event) => {
                            event.stopPropagation()
                            triggerMut.mutate()
                          }}
                        >
                          触发转写
                        </Button>
                      )}
                    </Space>
                  ),
                  children: (
                    <>
                      {!transcript && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无对话全文" />}

                      {transcript?.status === 'processing' && (
                        <Spin tip="对话全文生成中..." style={{ display: 'block', padding: 32 }} />
                      )}

                      {transcript?.status === 'failed' && (
                        <div style={{ color: '#e76f51' }}>当前暂无可用对话全文。</div>
                      )}

                      {transcript?.status === 'completed' && (
                        transcript.utterances?.length ? (
                          <div ref={bubbleListRef} className="chat-bubble-list recording-detail-transcript-list recording-detail-transcript-list--embedded">
                            {transcript.utterances.map((utterance, index) => {
                              const isRight = utterance.speaker === 'customer' || utterance.speaker === '客户'
                              const speakerLabel = SPEAKER_MAP[utterance.speaker]?.label ?? utterance.speaker
                              const speakerColor = SPEAKER_MAP[utterance.speaker]?.color ?? 'default'
                              const isActive = playbackMs != null && playbackMs >= utterance.begin_ms && playbackMs < utterance.end_ms
                              return (
                                <div
                                  key={index}
                                  ref={isActive ? activeBubbleRef : undefined}
                                  className={`chat-bubble-row${isRight ? ' chat-bubble-row--right' : ' chat-bubble-row--left'}${isActive ? ' chat-bubble-row--active' : ''}`}
                                  onClick={() => {
                                    if (audioRef.current && audioUrl) {
                                      audioRef.current.currentTime = utterance.begin_ms / 1000
                                      audioRef.current.play()
                                    }
                                  }}
                                  style={{ cursor: audioUrl ? 'pointer' : undefined }}
                                >
                                  <div className="chat-bubble">
                                    <div className="chat-bubble__header">
                                      <Tag color={speakerColor} style={{ fontSize: 11 }}>{speakerLabel}</Tag>
                                      <span className="chat-bubble__time">{formatMs(utterance.begin_ms)}</span>
                                      {isActive && (
                                        <span className="chat-bubble__active-pill">
                                          {isAudioPlaying ? '正在播放' : '当前定位'}
                                        </span>
                                      )}
                                    </div>
                                    <div className="chat-bubble__text">{utterance.text}</div>
                                    {isActive && (
                                      <div className="chat-bubble__progress" aria-hidden="true">
                                        <span
                                          style={{
                                            width: `${Math.max(
                                              0,
                                              Math.min(
                                                100,
                                                ((playbackMs! - utterance.begin_ms) / Math.max(1, utterance.end_ms - utterance.begin_ms)) * 100,
                                              ),
                                            )}%`,
                                          }}
                                        />
                                      </div>
                                    )}
                                  </div>
                                </div>
                              )
                            })}
                          </div>
                        ) : (
                          <div style={{ color: '#999' }}>暂无逐句转写内容</div>
                        )
                      )}
                    </>
                  ),
                },
              ]}
            />
          </div>
        </Card>

      </div>

      {shouldShowMultiCustomerReview && (
        <Card
          size="small"
          className="recording-detail-panel recording-detail-panel--multi-customer"
          title={(
            <Space size={10} wrap>
              <span>多客户对应确认</span>
              <Tag color="blue">{recording.linked_visit_ids.length} 张到诊单</Tag>
            </Space>
          )}
        >
          {multiCustomerReviewLoading || !multiCustomerReview ? (
            <Spin tip="正在生成客户段候选..." style={{ display: 'block', padding: 28 }} />
          ) : (
            <div className="recording-detail-multi-customer">
              <Alert
                showIcon
                type={
                  multiCustomerReview.status === 'failed'
                    ? 'error'
                    : multiCustomerReview.status === 'ready'
                      ? 'success'
                      : multiCustomerReview.status === 'analyzing'
                        ? 'warning'
                        : 'info'
                }
                message={multiCustomerReview.message}
                description="一条录音关联多个到诊单时，需要先确认录音里的客户段分别对应哪张到诊单；确认后系统会按客户分别生成分析结果，再进入 SAP 自动回传等待。"
              />

              <div className="recording-detail-multi-customer__segments">
                {multiCustomerReview.segments.map((segment) => (
                  <div key={segment.id} className="recording-detail-multi-customer__segment">
                    <div className="recording-detail-multi-customer__segment-head">
                      <strong>{segment.label}</strong>
                      <span>
                        {formatMs(segment.begin_ms)}-{formatMs(segment.end_ms)} · {segment.utterance_count}句
                      </span>
                    </div>
                    <p>{segment.summary || '暂无摘要，可结合 ASR 原文判断。'}</p>
                  </div>
                ))}
              </div>

              <div className="recording-detail-multi-customer__mapping-list">
                {multiCustomerReview.visit_analyses.map((visitAnalysis) => {
                  const statusMeta = getVisitAnalysisStatusMeta(visitAnalysis.analysis_status)
                  return (
                    <div key={visitAnalysis.visit_id} className="recording-detail-multi-customer__mapping-row">
                      <div className="recording-detail-multi-customer__visit">
                        <strong>{formatVisitRef(visitAnalysis.visit_order_no, visitAnalysis.visit_order_seg)}</strong>
                        <span>{visitAnalysis.customer_name || '未识别客户'}</span>
                        {visitAnalysis.customer_code ? <em>{visitAnalysis.customer_code}</em> : null}
                      </div>
                      <Select
                        allowClear
                        placeholder="选择录音中的客户段"
                        value={resolvedMultiCustomerMapping[visitAnalysis.visit_id] || undefined}
                        onChange={(selectedSegmentId) => {
                          setMultiCustomerMappingDraft((current) => ({
                            ...current,
                            [visitAnalysis.visit_id]: selectedSegmentId || '',
                          }))
                        }}
                        options={multiCustomerReview.segments.map((segment) => ({
                          value: segment.id,
                          label: `${segment.label}（${formatMs(segment.begin_ms)}-${formatMs(segment.end_ms)}）`,
                        }))}
                      />
                      <Tag color={statusMeta.color}>{statusMeta.label}</Tag>
                      {visitAnalysis.analysis_error ? (
                        <small className="recording-detail-multi-customer__error">{visitAnalysis.analysis_error}</small>
                      ) : null}
                    </div>
                  )
                })}
              </div>

              {hasDuplicateMultiCustomerMapping ? (
                <Alert showIcon type="warning" message="每个客户段只能对应一张到诊单，请调整重复选择。" />
              ) : null}

              <div className="recording-detail-actions recording-detail-multi-customer__actions">
                <Button
                  type="primary"
                  loading={multiCustomerConfirmMut.isPending}
                  disabled={!multiCustomerMappingComplete || multiCustomerResetMut.isPending}
                  onClick={() => multiCustomerConfirmMut.mutate()}
                >
                  确认对应关系并重新分析
                </Button>
                {multiCustomerReview.status !== 'pending_mapping' ? (
                  <Button
                    danger
                    loading={multiCustomerResetMut.isPending}
                    disabled={multiCustomerConfirmMut.isPending}
                    onClick={() => {
                      Modal.confirm({
                        title: '解除多客户对应确认？',
                        content: '解除后会清空当前客户段映射和到诊单级分析结果，并停止这批结果进入后续自动回传；如果已有 SAP 回传日志，则不会自动撤回 SAP 中已生成的咨询单。',
                        okText: '确认解除',
                        cancelText: '取消',
                        okButtonProps: { danger: true },
                        onOk: () => multiCustomerResetMut.mutateAsync(),
                      })
                    }}
                  >
                    解除确认，重新匹配
                  </Button>
                ) : null}
                <Button onClick={invalidate}>刷新状态</Button>
              </div>
            </div>
          )}
        </Card>
      )}

      <div className="recording-detail-page__main">
        {!analysisTask && (
          <Card
            size="small"
            className="recording-detail-panel recording-detail-panel--analysis"
            title="分析结果"
          >
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={canAnalyze ? '暂无分析结果，可按需重新生成。' : '暂无可展示的分析结果。'}
            />
            {shouldShowAnalyzeAction && (
              <div className="recording-detail-actions recording-detail-actions--analysis">
                <Button type="primary" loading={analyzeMut.isPending} onClick={() => analyzeMut.mutate()}>
                  开始分析
                </Button>
                <Button onClick={invalidate}>刷新分析状态</Button>
              </div>
            )}
          </Card>
        )}

        {(analysisTask?.status === 'pending' || analysisTask?.status === 'running') && (
          <Card
            size="small"
            className="recording-detail-panel recording-detail-panel--analysis"
            title="分析结果"
          >
            <div style={{ color: '#666', marginBottom: 16 }}>分析结果整理中，请稍后刷新。</div>
            <AnalysisSummary task={analysisTask} />
          </Card>
        )}

        {analysisTask?.status === 'failed' && (
          <Card
            size="small"
            className="recording-detail-panel recording-detail-panel--analysis"
            title="分析结果"
          >
            <div style={{ color: '#e76f51', marginBottom: 16 }}>
              当前暂无可用分析结果。
            </div>
            <div className="recording-detail-actions recording-detail-actions--analysis">
              <Button type="primary" loading={analyzeMut.isPending} onClick={() => analyzeMut.mutate()}>
                重新分析
              </Button>
              <Button onClick={invalidate}>刷新分析状态</Button>
            </div>
          </Card>
        )}

        {analysisTask?.status === 'done' && loadingAnalysisDetail && (
          <Card
            size="small"
            className="recording-detail-panel recording-detail-panel--analysis"
            title="分析结果"
          >
            <Spin tip="分析详情加载中..." style={{ display: 'block', padding: 32 }} />
          </Card>
        )}

        {analysisTask?.status === 'done' && analysisDetailError && (
          <Card
            size="small"
            className="recording-detail-panel recording-detail-panel--analysis"
            title="分析结果"
          >
            <div style={{ color: '#e76f51', marginBottom: 16 }}>
              分析详情加载失败：{String(analysisDetailError)}
            </div>
            <div className="recording-detail-actions recording-detail-actions--analysis">
              <Button onClick={invalidate}>刷新分析状态</Button>
            </div>
          </Card>
        )}

        {analysisTask?.status === 'done' && analysisDetail && (
          <AnalysisDetailContent
            data={analysisDetail}
            recordingId={recordingId}
            embedded
            showHeader
            title="分析结果"
          />
        )}
      </div>

      <Modal
        open={splitModalOpen}
        title="确认裁切录音"
        okText="确认裁切"
        cancelText="取消"
        okButtonProps={{ danger: true, loading: splitMut.isPending }}
        onOk={() => splitMut.mutate()}
        onCancel={() => {
          if (!splitMut.isPending) setSplitModalOpen(false)
        }}
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Alert
            showIcon
            type="warning"
            message="裁切后原录音会被隐藏，新生成的两段录音需要按实际客户重新关联到诊单。"
          />
          <div>
            <div style={{ marginBottom: 8 }}>裁切时间点</div>
            <InputNumber
              min={1}
              max={Math.max(1, (recording.duration_seconds ?? 2) - 1)}
              value={splitAtSeconds}
              precision={0}
              addonAfter="秒"
              style={{ width: '100%' }}
              onChange={(value) => setSplitAtSeconds(typeof value === 'number' ? value : null)}
            />
            <div style={{ color: '#999', fontSize: 12, marginTop: 6 }}>
              将在 {splitAtSeconds != null ? formatMs(splitAtSeconds * 1000) : '-'} 处分成前后两段。
            </div>
          </div>
        </Space>
      </Modal>
    </div>
  )
}

export default RecordingDetailPage
