import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AudioOutlined,
  ReloadOutlined,
  ScissorOutlined,
} from '@ant-design/icons'
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Drawer,
  Empty,
  Input,
  InputNumber,
  message,
  Modal,
  Segmented,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from 'antd'
import type { TableProps } from 'antd'

import * as adminApi from '@/api/admin'
import {
  type ArchiveRecording as DingtalkArchiveRecording,
  type ArchiveRecordingEnsureResult as DingtalkArchiveEnsureRecordingResult,
  ensureArchiveRecording,
  fetchArchiveRecordingDetail,
  fetchArchiveRecordingMediaBlob,
  fetchArchiveRecordings,
} from '@/api/archive-recordings'
import { getApiErrorMessage } from '@/api/errors'
import {
  fetchDailyVisitOrdersForRecording,
  fetchRecordingVisitOrderMatch,
  STAFF_ROLE_MAP,
  splitRecording,
  type DailyVisitOrderItem,
  type RecordingVisitOrderMatch,
  type RecordingSplitResult,
  updateRecording,
  type VisitOrderMatchCandidate,
} from '@/api/recordings'
import { SPEAKER_MAP } from '@/api/segments'
import { sanitizeEvaluationDimensionSummary, sanitizeEvaluationSummary } from '@/utils/evaluation-summary'
import {
  buildCompanionVisitPromptMessage,
  buildLinkedVisitIds,
  hasCompanionVisitOptions,
} from '@/utils/companion-visit-linking'
import { buildRecordingVisitLinkRiskLines } from '@/utils/recording-visit-link-confirmation'
import { getDisplayMatchEvidenceLines } from '@/utils/match-evidence'
import {
  buildVisitOrderLineItemMeta,
  formatMergedVisitOrderTitle,
  formatVisitOrderLineItemRef,
} from '@/utils/visit-order-line-items'
import { formatRecordingDisplayName } from '@/utils/recording-display'
import { isHospitalAdminOrAbove } from '@/app/roles'
import { useAuth } from '@/app/use-auth'
import { formatBeijingTime } from '@/utils/time'

const { Paragraph, Text } = Typography

const STATUS_OPTIONS = [
  { value: 'all', label: '全部状态' },
  { value: 'transcribing', label: '转写中' },
  { value: 'analyzed', label: '已分析' },
  { value: 'filtered', label: '已过滤' },
  { value: 'failed', label: '失败' },
]

const FOCUS_OPTIONS = [
  { value: 'needs_link', label: '待关联优先' },
  { value: 'processable', label: '全部有效录音' },
  { value: 'linked', label: '已关联到诊单' },
  { value: 'filtered', label: '已过滤/失败' },
  { value: 'all', label: '全部录音' },
]

type DailyVisitOrdersMode = 'self' | 'org'

const STATUS_META: Record<string, { label: string; color: string }> = {
  archived: { label: '仅归档', color: 'default' },
  downloaded: { label: '已暂存', color: 'default' },
  transcribing: { label: '转写中', color: 'processing' },
  transcribed: { label: '已转写', color: 'blue' },
  analyzing: { label: '分析中', color: 'processing' },
  analyzed: { label: '已分析', color: 'success' },
  filtered: { label: '已过滤', color: 'gold' },
  failed: { label: '失败', color: 'error' },
}

function formatDateTime(value?: string | null): string {
  return formatBeijingTime(value, 'YYYY-MM-DD HH:mm:ss')
}

function formatArchiveRecordingName(
  fileName: string | null | undefined,
  createdAt?: string | null,
) {
  return formatRecordingDisplayName(fileName, createdAt)
}

function formatStaffRoleLabel(role?: string | null): string | null {
  if (!role) return null
  return STAFF_ROLE_MAP[role] || role
}

function formatDurationMs(durationMs?: number | null, durationSeconds?: number | null): string {
  const ms = typeof durationMs === 'number' && durationMs > 0
    ? durationMs
    : typeof durationSeconds === 'number' && durationSeconds > 0
      ? durationSeconds * 1000
      : null
  if (ms == null) return '--:--'
  const totalSeconds = Math.floor(ms / 1000)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  if (hours > 0) {
    return `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`
  }
  return `${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`
}

function formatFileSize(bytes?: number | null): string {
  if (typeof bytes !== 'number' || bytes <= 0) return '-'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(2)} MB`
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`
}

function formatMs(ms?: number | null): string {
  if (typeof ms !== 'number' || ms < 0) return '--:--'
  const totalSeconds = Math.floor(ms / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${minutes}:${seconds.toString().padStart(2, '0')}`
}

function fmtClock(s?: string | null): string {
  if (!s || s.length < 4) return s || ''
  const normalized = s.replace(/[^0-9]/g, '')
  if (normalized.length < 4) return s
  const padded = normalized.padStart(6, '0')
  return `${padded.slice(0, 2)}:${padded.slice(2, 4)}:${padded.slice(4, 6)}`
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  return value as Record<string, unknown>
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function asText(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function formatScore(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '-'
  return value.toFixed(2).replace(/\.?0+$/, '')
}

function getMatchTag(decision: string) {
  if (decision === 'auto') return <Tag color="success">自动关联</Tag>
  if (decision === 'recommend') return <Tag color="processing">推荐关联</Tag>
  return <Tag>待确认</Tag>
}

function getMethodLabel(method: string) {
  if (method === 'direct_dzdh') return 'DZDH 直连'
  if (method === 'strict_customer_day_advisor') return '客户编码+日期+顾问'
  if (method === 'llm') return 'LLM 判定'
  return '规则筛选'
}

function getConfidenceTag(confidence: number) {
  const percent = Math.round(confidence * 100)
  const color = confidence >= 0.95 ? 'success' : confidence >= 0.75 ? 'processing' : confidence >= 0.58 ? 'gold' : 'default'
  return <Tag color={color}>推荐置信度 {percent}%</Tag>
}

const VERY_LOW_TOP1_CONFIDENCE = 0.45
const MIN_VISIBLE_CANDIDATE_CONFIDENCE = 0.35
const MAX_VISIBLE_CONFIDENCE_GAP = 0.2

function getDisplayCandidates(candidates: VisitOrderMatchCandidate[], preserveAll = false) {
  if (!candidates.length) {
    return {
      items: [] as VisitOrderMatchCandidate[],
      hiddenCount: 0,
    }
  }

  if (preserveAll) {
    return {
      items: candidates,
      hiddenCount: 0,
    }
  }

  const top1 = candidates[0]
  if (top1.confidence < VERY_LOW_TOP1_CONFIDENCE) {
    return {
      items: candidates,
      hiddenCount: 0,
    }
  }

  const items = candidates.filter((candidate, index) => {
    if (index === 0) return true
    const confidenceGap = top1.confidence - candidate.confidence
    return candidate.confidence >= MIN_VISIBLE_CANDIDATE_CONFIDENCE && confidenceGap <= MAX_VISIBLE_CONFIDENCE_GAP
  })

  return {
    items,
    hiddenCount: Math.max(candidates.length - items.length, 0),
  }
}

function renderConflictBlock(conflicts: string[], manualReviewReason?: string | null) {
  if (!conflicts.length && !manualReviewReason) return null
  const visibleConflicts = manualReviewReason
    ? conflicts.filter((conflict) => conflict !== manualReviewReason)
    : conflicts
  return (
    <div
      style={{
        display: 'grid',
        gap: 6,
        padding: 10,
        borderRadius: 8,
        background: '#fffbe6',
        border: '1px solid #ffe58f',
      }}
    >
      <strong style={{ color: '#ad6800' }}>需人工确认</strong>
      {manualReviewReason ? <span style={{ color: '#8c6d1f' }}>{manualReviewReason}</span> : null}
      {visibleConflicts.length ? (
        <div style={{ display: 'grid', gap: 4, color: '#8c6d1f' }}>
          {visibleConflicts.map((conflict) => (
            <span key={conflict}>{conflict}</span>
          ))}
        </div>
      ) : null}
    </div>
  )
}

function renderEvidenceBlock(candidate: VisitOrderMatchCandidate) {
  const lines = getDisplayMatchEvidenceLines(candidate)
  if (!lines.length) return '-'
  return (
    <div style={{ display: 'grid', gap: 4 }}>
      {lines.map((line) => (
        <span key={line}>{line}</span>
      ))}
    </div>
  )
}

function renderMergedLineItemBlock(candidate: VisitOrderMatchCandidate) {
  if ((candidate.merged_line_items?.length ?? 0) <= 1) return null

  return (
    <div
      style={{
        display: 'grid',
        gap: 6,
        padding: 10,
        borderRadius: 10,
        border: '1px dashed #bfdbfe',
        background: '#f8fbff',
      }}
    >
      {candidate.merged_line_items.map((item, index) => {
        const metaLines = buildVisitOrderLineItemMeta(item)
        return (
          <div
            key={`${item.fzdh ?? item.dzseg ?? 'line-item'}-${index}`}
            style={{
              display: 'grid',
              gap: 4,
              padding: '8px 10px',
              borderRadius: 8,
              background: '#fff',
            }}
          >
            <strong style={{ fontSize: 12, color: '#1d4ed8' }}>{formatVisitOrderLineItemRef(item)}</strong>
            {metaLines.map((line) => (
              <span key={line} style={{ color: '#475569', fontSize: 12 }}>{line}</span>
            ))}
            {item.note_summary ? <span style={{ color: '#64748b', fontSize: 12 }}>备注：{item.note_summary}</span> : null}
          </div>
        )
      })}
    </div>
  )
}

function formatVisitOrderRef(orderNo?: string | null, orderSeg?: string | null) {
  const normalizedOrderNo = String(orderNo || '').trim()
  const normalizedOrderSeg = String(orderSeg || '').trim()
  if (!normalizedOrderNo) return null
  return normalizedOrderSeg ? `${normalizedOrderNo}-${normalizedOrderSeg}` : normalizedOrderNo
}

function renderLinkedVisitOrderSummary(
  linkedVisitOrderRefs: string[] = [],
  primaryOrderNo?: string | null,
  primaryOrderSeg?: string | null,
) {
  const primaryRef = formatVisitOrderRef(primaryOrderNo, primaryOrderSeg) || linkedVisitOrderRefs[0] || null
  const secondaryRefs = linkedVisitOrderRefs.filter((ref) => ref && ref !== primaryRef)
  if (!primaryRef) return '未关联'
  return (
    <div style={{ display: 'grid', gap: 6 }}>
      <div>
        <strong>主到诊单：</strong>
        <span>{primaryRef}</span>
      </div>
      {secondaryRefs.length > 0 ? (
        <div>
          <strong>同行辅单：</strong>
          <span>{secondaryRefs.join(' / ')}</span>
        </div>
      ) : null}
    </div>
  )
}

async function confirmCompanionVisitLinking(
  companionVisitIds: string[] = [],
  companionVisitOrderRefs: string[] = [],
  companionCustomerCodes: string[] = [],
) {
  if (!hasCompanionVisitOptions(companionVisitIds)) {
    return false
  }
  return new Promise<boolean>((resolve) => {
    Modal.confirm({
      title: '检测到同行辅单',
      content: buildCompanionVisitPromptMessage(companionVisitOrderRefs, companionCustomerCodes),
      okText: '一并关联',
      cancelText: '仅关联当前',
      onOk: () => resolve(true),
      onCancel: () => resolve(false),
    })
  })
}

async function confirmRecordingVisitLinkRisk({
  nextLinkedVisitIds,
  targetLinkedRecordingNames,
  targetLinkedRecordingCount,
}: {
  nextLinkedVisitIds: string[]
  targetLinkedRecordingNames?: string[]
  targetLinkedRecordingCount?: number
}) {
  const lines = buildRecordingVisitLinkRiskLines({
    nextLinkedVisitIds,
    targetLinkedRecordingNames,
    targetLinkedRecordingCount,
  })
  if (!lines.length) return true

  return new Promise<boolean>((resolve) => {
    Modal.confirm({
      title: '确认关联关系',
      content: (
        <div style={{ display: 'grid', gap: 8 }}>
          {lines.map((line) => (
            <span key={line}>{line}</span>
          ))}
        </div>
      ),
      okText: '确认关联',
      cancelText: '取消',
      onOk: () => resolve(true),
      onCancel: () => resolve(false),
    })
  })
}

function canRecommendLink(record: DingtalkArchiveRecording): boolean {
  return record.has_transcript && record.pipeline_status !== 'filtered' && record.pipeline_status !== 'failed'
}

function recommendDisabledReason(record: DingtalkArchiveRecording): string | null {
  if (!record.has_transcript) return '当前录音还没有 ASR 转写结果'
  if (record.pipeline_status === 'filtered') return '当前录音已被质检过滤，不适合推荐关联'
  if (record.pipeline_status === 'failed') return '当前录音处理失败，暂不能推荐关联'
  return null
}

function getLinkStatus(record: DingtalkArchiveRecording) {
  if (record.has_visit_link) {
    return {
      label: '已关联',
      color: 'success' as const,
    }
  }
  if (record.needs_visit_link) {
    return {
      label: '待关联',
      color: 'processing' as const,
    }
  }
  if (record.pipeline_status === 'filtered') {
    return {
      label: '已过滤',
      color: 'gold' as const,
    }
  }
  if (record.pipeline_status === 'failed') {
    return {
      label: '处理失败',
      color: 'error' as const,
    }
  }
  return {
    label: '暂不可关联',
    color: 'default' as const,
  }
}

function getStatusTags(record: DingtalkArchiveRecording) {
  const meta = STATUS_META[record.pipeline_status ?? 'archived'] ?? STATUS_META.archived
  const tags = [<Tag key="pipeline" color={meta.color}>{meta.label}</Tag>]
  const pipelineStatus = record.pipeline_status ?? 'archived'

  if (
    record.has_transcript
    && !['transcribed', 'analyzing', 'analyzed'].includes(pipelineStatus)
  ) {
    tags.push(<Tag key="transcript" color="blue">已转写</Tag>)
  } else if (
    !record.has_transcript
    && ['archived', 'downloaded'].includes(pipelineStatus)
  ) {
    tags.push(<Tag key="pending">待转写</Tag>)
  }

  return tags
}

function parseEvaluationDimensions(value: unknown) {
  return asArray(value)
    .map((item) => asRecord(item))
    .filter((item): item is Record<string, unknown> => Boolean(item))
    .map((item) => ({
      name: asText(item.name) || '未命名维度',
      pointScore: asNumber(item.point_score),
      maxScore: asNumber(item.max_score) ?? 1,
      summary: sanitizeEvaluationDimensionSummary(asText(item.summary) ?? asText(item.comment)),
      issues: asArray(item.issues)
        .map((issue) => asRecord(issue))
        .filter((issue): issue is Record<string, unknown> => Boolean(issue))
        .map((issue) => ({
          description: asText(issue.description),
          evidence: asText(issue.evidence),
        })),
    }))
}

function renderPrettyJson(value: unknown) {
  if (!value) return null
  return (
    <pre
      style={{
        margin: 0,
        padding: 16,
        borderRadius: 12,
        background: '#0f172a',
        color: '#e2e8f0',
        overflowX: 'auto',
        fontSize: 12,
        lineHeight: 1.6,
      }}
    >
      {JSON.stringify(value, null, 2)}
    </pre>
  )
}

export default function DingtalkAudioArchivePage() {
  const auth = useAuth()
  const qc = useQueryClient()
  const archiveAudioRef = useRef<HTMLAudioElement | null>(null)
  const [keywordInput, setKeywordInput] = useState('')
  const [keyword, setKeyword] = useState('')
  const [hospitalFilter, setHospitalFilter] = useState<string | undefined>()
  const [focusMode, setFocusMode] = useState('needs_link')
  const [status, setStatus] = useState('all')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [activeId, setActiveId] = useState<string | null>(null)
  const [matchOpen, setMatchOpen] = useState(false)
  const [matchingItem, setMatchingItem] = useState<DingtalkArchiveRecording | null>(null)
  const [ensuredRecording, setEnsuredRecording] = useState<DingtalkArchiveEnsureRecordingResult | null>(null)
  const [recommendMode, setRecommendMode] = useState<'rules' | 'llm'>('rules')
  const [viewingVisitOrderId, setViewingVisitOrderId] = useState<string | null>(null)
  const [visitOrderDetailOpen, setVisitOrderDetailOpen] = useState(false)
  const [dailyVisitOrdersContext, setDailyVisitOrdersContext] = useState<{
    recordingId: string
    fileName: string
    linkedVisitOrderRefs: string[]
  } | null>(null)
  const [dailyVisitOrdersMode, setDailyVisitOrdersMode] = useState<DailyVisitOrdersMode>('self')
  const [dailyVisitOrdersKeyword, setDailyVisitOrdersKeyword] = useState('')
  const [dailyVisitOrdersSearchDraft, setDailyVisitOrdersSearchDraft] = useState('')
  const [splitModalOpen, setSplitModalOpen] = useState(false)
  const [splitAtSeconds, setSplitAtSeconds] = useState<number | null>(null)
  const canFilterByHospital = auth.status === 'authenticated' && isHospitalAdminOrAbove(auth.user.role)
  const hospitalOptionsQuery = useQuery({
    queryKey: ['staff', 'hospital-options'],
    queryFn: () => adminApi.fetchStaffHospitalOptions(),
    enabled: canFilterByHospital,
  })
  const hospitalOptions = (hospitalOptionsQuery.data ?? []).map((item) => ({
    value: item.hospital_code,
    label: item.hospital_name && item.hospital_name !== item.hospital_code
      ? `${item.hospital_name}（${item.hospital_code}）`
      : item.hospital_code,
  }))

  const {
    data,
    isLoading,
    isFetching,
    error,
    refetch,
  } = useQuery({
    queryKey: ['dingtalk-archive-recordings', keyword, hospitalFilter || 'all', focusMode, status, page, pageSize],
    queryFn: () => fetchArchiveRecordings({
      keyword: keyword || undefined,
      hospital_code: hospitalFilter,
      status: status !== 'all' ? status : undefined,
      link_state: focusMode === 'needs_link'
        ? 'needs_link'
        : focusMode === 'linked'
          ? 'linked'
          : undefined,
      exclude_filtered: focusMode === 'needs_link' || focusMode === 'processable',
      problem_only: focusMode === 'filtered',
      include_date_summaries: false,
      page,
      page_size: pageSize,
    }),
    placeholderData: (previousData) => previousData,
    staleTime: 30_000,
  })

  const {
    data: detail,
    isLoading: detailLoading,
    error: detailError,
  } = useQuery({
    queryKey: ['dingtalk-archive-recording-detail', activeId],
    queryFn: () => fetchArchiveRecordingDetail(activeId!),
    enabled: !!activeId,
  })

  const {
    data: audioBlob,
    isFetching: audioLoading,
  } = useQuery({
    queryKey: ['dingtalk-archive-recording-media', activeId],
    queryFn: () => fetchArchiveRecordingMediaBlob(activeId!),
    enabled: !!activeId,
    retry: false,
  })

  const ensureRecordingMut = useMutation({
    mutationFn: ensureArchiveRecording,
    onSuccess: (payload) => {
      setEnsuredRecording(payload)
      if (payload.created_new_recording) {
        message.success('已为这条归档录音接入正式录音记录，可以直接做推荐关联')
      }
    },
    onError: async (error) => {
      message.error(await getApiErrorMessage(error, '推荐关联准备失败'))
    },
  })

  const {
    data: matchData,
    isLoading: matchLoading,
    error: matchError,
    isFetching: matchFetching,
  } = useQuery<RecordingVisitOrderMatch>({
    queryKey: ['archive-recording-visit-order-match', ensuredRecording?.recording_id, recommendMode],
    queryFn: () => fetchRecordingVisitOrderMatch(ensuredRecording!.recording_id, false, recommendMode === 'llm'),
    enabled: matchOpen && Boolean(ensuredRecording?.recording_id),
  })

  const { data: visitOrderDetail, isLoading: isVisitOrderDetailLoading } = useQuery({
    queryKey: ['archive-visit-order-detail', viewingVisitOrderId],
    queryFn: () => adminApi.fetchVisitOrder(viewingVisitOrderId!),
    enabled: visitOrderDetailOpen && Boolean(viewingVisitOrderId),
  })

  const { data: dailyVisitOrdersData, isLoading: isDailyVisitOrdersLoading } = useQuery({
    queryKey: [
      'archive-recording-daily-visit-orders',
      dailyVisitOrdersContext?.recordingId,
      dailyVisitOrdersMode,
      dailyVisitOrdersMode === 'org' ? dailyVisitOrdersKeyword : '',
    ],
    queryFn: () => fetchDailyVisitOrdersForRecording(dailyVisitOrdersContext!.recordingId, {
      scope_mode: dailyVisitOrdersMode,
      keyword: dailyVisitOrdersMode === 'org' ? dailyVisitOrdersKeyword : '',
    }),
    enabled: Boolean(dailyVisitOrdersContext?.recordingId),
  })

  const adoptMatchMut = useMutation({
    mutationFn: ({ recordingId, visitId, linkedVisitIds }: { recordingId: string; visitId: string; linkedVisitIds?: string[] }) =>
      updateRecording(recordingId, { visit_id: visitId, linked_visit_ids: linkedVisitIds ?? [visitId] }),
    onSuccess: async (_result, variables) => {
      message.success('已采用推荐并完成关联')
      await qc.invalidateQueries({ queryKey: ['archive-recording-visit-order-match', variables.recordingId] })
      await qc.invalidateQueries({ queryKey: ['archive-recording-daily-visit-orders', variables.recordingId] })
      await qc.invalidateQueries({ queryKey: ['dingtalk-archive-recordings'] })
      await qc.invalidateQueries({ queryKey: ['dingtalk-archive-recording-detail'] })
      setDailyVisitOrdersContext(null)
      setDailyVisitOrdersMode('self')
      setDailyVisitOrdersKeyword('')
      setDailyVisitOrdersSearchDraft('')
      setVisitOrderDetailOpen(false)
      setViewingVisitOrderId(null)
      setMatchOpen(false)
      setMatchingItem(null)
      setEnsuredRecording(null)
    },
    onError: async (error) => {
      message.error(await getApiErrorMessage(error, '采用推荐失败'))
    },
  })

  const handleRecordingVisitLink = async ({
    recordingId,
    visitId,
    alwaysLinkedVisitIds = [],
    companionVisitIds = [],
    companionVisitOrderRefs = [],
    companionCustomerCodes = [],
    targetLinkedRecordingNames = [],
    targetLinkedRecordingCount,
  }: {
    recordingId: string
    visitId: string
    alwaysLinkedVisitIds?: string[]
    companionVisitIds?: string[]
    companionVisitOrderRefs?: string[]
    companionCustomerCodes?: string[]
    targetLinkedRecordingNames?: string[]
    targetLinkedRecordingCount?: number
  }) => {
    const includeCompanions = await confirmCompanionVisitLinking(
      companionVisitIds,
      companionVisitOrderRefs,
      companionCustomerCodes,
    )
    const linkedVisitIds = buildLinkedVisitIds(visitId, [
      ...(ensuredRecording?.recording_id === recordingId ? ensuredRecording.linked_visit_ids : []),
      ...alwaysLinkedVisitIds,
      ...(includeCompanions ? companionVisitIds : []),
    ])
    const confirmed = await confirmRecordingVisitLinkRisk({
      nextLinkedVisitIds: linkedVisitIds,
      targetLinkedRecordingNames,
      targetLinkedRecordingCount,
    })
    if (!confirmed) return

    adoptMatchMut.mutate({
      recordingId,
      visitId,
      linkedVisitIds,
    })
  }

  const audioUrl = useMemo(() => (audioBlob ? URL.createObjectURL(audioBlob) : null), [audioBlob])
  const matchDisplay = getDisplayCandidates(matchData?.candidates ?? [], matchData?.manual_review_required ?? false)
  const currentDailyVisitOrderRefSet = useMemo(
    () => new Set(dailyVisitOrdersContext?.linkedVisitOrderRefs ?? []),
    [dailyVisitOrdersContext],
  )
  const detailDurationSeconds = detail
    ? detail.duration_seconds ?? (typeof detail.duration_ms === 'number' && detail.duration_ms > 0 ? Math.round(detail.duration_ms / 1000) : null)
    : null
  const currentUser = auth.status === 'authenticated' ? auth.user : null
  const canSplitArchiveRecording = Boolean(
    detail
    && currentUser
    && detail.pipeline_status !== 'filtered'
    && (detailDurationSeconds ?? 0) > 1
    && (
      isHospitalAdminOrAbove(currentUser.role)
      || (detail.staff_id && detail.staff_id === currentUser.staff_id)
    ),
  )

  const openSplitModal = () => {
    const durationSeconds = detailDurationSeconds ?? 0
    const currentSeconds = archiveAudioRef.current?.currentTime ? Math.floor(archiveAudioRef.current.currentTime) : 0
    const defaultSeconds = currentSeconds > 0 && currentSeconds < durationSeconds
      ? currentSeconds
      : Math.max(1, Math.floor(durationSeconds / 2))
    setSplitAtSeconds(defaultSeconds)
    setSplitModalOpen(true)
  }

  const splitArchiveRecordingMut = useMutation({
    mutationFn: async () => {
      if (!detail || splitAtSeconds == null) {
        throw new Error('请先填写裁切时间点')
      }
      const recordingId = detail.recording_id || ensuredRecording?.recording_id || (await ensureArchiveRecording(detail.id)).recording_id
      return splitRecording(recordingId, { split_at_seconds: splitAtSeconds, confirm: true })
    },
    onSuccess: async (result: RecordingSplitResult) => {
      setSplitModalOpen(false)
      setActiveId(null)
      message.success(result.message || '录音裁切完成')
      await qc.invalidateQueries({ queryKey: ['dingtalk-archive-recordings'] })
      await qc.invalidateQueries({ queryKey: ['dingtalk-archive-recording-detail'] })
      await qc.invalidateQueries({ queryKey: ['archive-recording-visit-order-match'] })
    },
    onError: async (error) => {
      message.error(await getApiErrorMessage(error, '录音裁切失败'))
    },
  })

  useEffect(() => {
    if (!audioUrl) return
    return () => {
      URL.revokeObjectURL(audioUrl)
    }
  }, [audioUrl])

  const items = data?.items ?? []
  const currentPageTranscriptCount = items.filter((item) => item.has_transcript).length
  const currentPageAnalysisCount = items.filter((item) => item.has_analysis).length
  const currentPageNeedLinkCount = items.filter((item) => item.needs_visit_link).length
  const currentPageLinkedCount = items.filter((item) => item.has_visit_link).length

  const openRecommendModal = (record: DingtalkArchiveRecording, mode: 'rules' | 'llm') => {
    setMatchingItem(record)
    setEnsuredRecording(null)
    setRecommendMode(mode)
    setMatchOpen(true)
    ensureRecordingMut.mutate(record.id)
  }

  const closeRecommendModal = () => {
    setMatchOpen(false)
    setMatchingItem(null)
    setEnsuredRecording(null)
    setRecommendMode('rules')
    setDailyVisitOrdersContext(null)
    setDailyVisitOrdersMode('self')
    setDailyVisitOrdersKeyword('')
    setDailyVisitOrdersSearchDraft('')
    setVisitOrderDetailOpen(false)
    setViewingVisitOrderId(null)
    ensureRecordingMut.reset()
  }

  const openVisitOrderDetail = (visitOrderId: string) => {
    setViewingVisitOrderId(visitOrderId)
    setVisitOrderDetailOpen(true)
  }

  const openDailyVisitOrders = () => {
    if (!ensuredRecording?.recording_id) {
      message.warning('当前录音还未准备好到诊单数据，请稍后再试')
      return
    }
    const linkedVisitOrderRefs = matchData?.linked_visit_order_refs?.length
      ? matchData.linked_visit_order_refs
      : ensuredRecording.linked_visit_order_refs
    setDailyVisitOrdersMode('self')
    setDailyVisitOrdersKeyword('')
    setDailyVisitOrdersSearchDraft('')
    setDailyVisitOrdersContext({
      recordingId: ensuredRecording.recording_id,
      fileName: formatArchiveRecordingName(
        matchingItem?.display_file_name || ensuredRecording.display_file_name,
        matchingItem?.create_time || undefined,
      ),
      linkedVisitOrderRefs,
    })
  }

  const columns: TableProps<DingtalkArchiveRecording>['columns'] = [
    {
      title: '归档录音',
      dataIndex: 'display_file_name',
      key: 'display_file_name',
      width: 260,
      render: (_, record) => (
        <div className="archive-recordings-page__cell-block">
          <div className="archive-recordings-page__primary">
            {formatArchiveRecordingName(record.display_file_name, record.create_time)}
          </div>
          <div className="archive-recordings-page__meta archive-recordings-page__meta--muted">
            fileId: {record.file_id}
          </div>
        </div>
      ),
    },
    {
      title: '工牌 / 员工',
      key: 'staff',
      width: 176,
      render: (_, record) => {
        const roleLabel = formatStaffRoleLabel(record.staff_role)
        return (
          <div className="archive-recordings-page__cell-block">
            <div className="archive-recordings-page__inline-head">
              <span className="archive-recordings-page__primary">{record.staff_name || '未绑定员工'}</span>
              {roleLabel ? <span className="archive-recordings-page__role">{roleLabel}</span> : null}
            </div>
            <div className="archive-recordings-page__meta">
              工牌：{record.sn || record.device_code || '-'}
            </div>
          </div>
        )
      },
    },
    {
      title: '时间 / 时长',
      key: 'timing',
      width: 152,
      render: (_, record) => (
        <div className="archive-recordings-page__cell-block">
          <div className="archive-recordings-page__primary">{formatDateTime(record.create_time)}</div>
          <div className="archive-recordings-page__meta">
            时长 {formatDurationMs(record.duration_ms, record.duration_seconds)}
          </div>
          <div className="archive-recordings-page__meta archive-recordings-page__meta--muted">
            大小 {formatFileSize(record.file_size)}
          </div>
        </div>
      ),
    },
    {
      title: '处理状态',
      key: 'status',
      width: 190,
      render: (_, record) => {
        return (
          <div className="archive-recordings-page__cell-block">
            <Space wrap size={[4, 4]}>
              {getStatusTags(record)}
            </Space>
            {record.quality_reason ? (
              <div className="archive-recordings-page__status-note archive-recordings-page__status-note--warning">
                {record.quality_reason}
              </div>
            ) : null}
            {record.error_message ? (
              <div className="archive-recordings-page__status-note archive-recordings-page__status-note--error">
                {record.error_message}
              </div>
            ) : null}
          </div>
        )
      },
    },
    {
      title: '关联状态',
      key: 'visit-link',
      width: 108,
      render: (_, record) => {
        const linkStatus = getLinkStatus(record)
        return (
          <div className="archive-recordings-page__cell-block">
            <Tag color={linkStatus.color}>{linkStatus.label}</Tag>
          </div>
        )
      },
    },
    {
      title: '操作',
      key: 'action',
      width: 204,
      render: (_, record) => (
        <Space size={2} wrap>
          <Button
            type="link"
            onClick={(event) => {
              event.stopPropagation()
              setActiveId(record.id)
            }}
          >
            查看详情
          </Button>
          <Button
            type="link"
            disabled={!canRecommendLink(record)}
            title={recommendDisabledReason(record) ?? '使用规则推荐关联到诊单'}
            onClick={(event) => {
              event.stopPropagation()
              openRecommendModal(record, 'rules')
            }}
          >
            规则推荐
          </Button>
          <Button
            type="link"
            disabled={!canRecommendLink(record)}
            title={recommendDisabledReason(record) ?? '使用 LLM 推荐关联到诊单'}
            onClick={(event) => {
              event.stopPropagation()
              openRecommendModal(record, 'llm')
            }}
          >
            LLM推荐
          </Button>
        </Space>
      ),
    },
  ]

  const transcriptRecord = asRecord(detail?.transcript)
  const utterances = asArray(transcriptRecord?.utterances)
  const transcriptFullText = asText(transcriptRecord?.fullText)
  const transcriptProvider = asText(transcriptRecord?.asrProvider)
  const analysisSummary = asRecord(detail?.analysis_summary)
  const analysisResult = asRecord(detail?.analysis_result)
  const evaluation = asRecord(analysisResult?.consultation_evaluation)
  const processEvaluation = asRecord(analysisResult?.consultation_process_evaluation)
  const evaluationDimensions = parseEvaluationDimensions(evaluation?.dimensions)
  const overallSummary = sanitizeEvaluationSummary(
    asText(processEvaluation?.overall_summary)
    ?? asText(evaluation?.overall_summary)
    ?? asText(analysisSummary?.overall_summary),
  )
  const totalScore = asNumber(processEvaluation?.total_score) ?? asNumber(evaluation?.total_score) ?? asNumber(analysisSummary?.total_score)
  const maxTotalScore = asNumber(processEvaluation?.max_total_score) ?? asNumber(evaluation?.max_total_score) ?? asNumber(analysisSummary?.max_total_score) ?? 9
  const overallScore = asNumber(processEvaluation?.overall_score) ?? asNumber(evaluation?.overall_score) ?? asNumber(analysisSummary?.overall_score)

  return (
    <section className="module-page archive-recordings-page">
      <header className="module-page__header">
        <div>
          <p className="eyebrow">录音复盘</p>
          <h1>录音列表</h1>
          <p className="module-page__subtitle">
            支持查看工牌归档录音、音频播放、逐字稿和分析结果。
          </p>
        </div>
      </header>

      <div style={{ display: 'grid', gap: 16 }}>
      <Card className="archive-recordings-page__card" title="录音列表">
        <div className="archive-recordings-page__filters">
          <div className="archive-recordings-page__filter-group archive-recordings-page__filter-group--view">
            <span className="archive-recordings-page__filter-label">业务视图</span>
            <Segmented
              options={FOCUS_OPTIONS}
              value={focusMode}
              onChange={(value) => {
                setPage(1)
                setStatus('all')
                setFocusMode(String(value))
              }}
            />
          </div>

          <div className="archive-recordings-page__filter-group archive-recordings-page__filter-group--actions">
            {canFilterByHospital ? (
              <div className="archive-recordings-page__filter-field">
                <span className="archive-recordings-page__filter-label">机构</span>
                <Select
                  allowClear
                  showSearch
                  className="archive-recordings-page__hospital-select"
                  placeholder="全部机构"
                  value={hospitalFilter}
                  loading={hospitalOptionsQuery.isLoading}
                  options={hospitalOptions}
                  optionFilterProp="label"
                  onChange={(value) => {
                    setPage(1)
                    setHospitalFilter(value || undefined)
                  }}
                />
              </div>
            ) : null}
            <div className="archive-recordings-page__filter-field">
              <span className="archive-recordings-page__filter-label">处理阶段</span>
              <Select
                className="archive-recordings-page__status-select"
                value={status}
                options={STATUS_OPTIONS}
                onChange={(value) => {
                  setPage(1)
                  setStatus(value)
                }}
              />
            </div>
            <Input.Search
              allowClear
              className="archive-recordings-page__search"
              placeholder="录音名 / 工牌 / 员工 / fileId"
              value={keywordInput}
              onChange={(event) => setKeywordInput(event.target.value)}
              onSearch={(value) => {
                setPage(1)
                setKeyword(value.trim())
                setKeywordInput(value)
              }}
            />
            <Button
              icon={<ReloadOutlined />}
              loading={isFetching}
              onClick={() => refetch()}
            >
              刷新
            </Button>
          </div>
        </div>

        <div className="archive-recordings-page__summary-row">
          <span className="archive-recordings-page__summary-pill">共 {data?.total ?? 0} 条</span>
          <span className="archive-recordings-page__summary-pill archive-recordings-page__summary-pill--accent">待关联 {currentPageNeedLinkCount}</span>
          <span className="archive-recordings-page__summary-pill">已关联 {currentPageLinkedCount}</span>
          <span className="archive-recordings-page__summary-pill">已转写 {currentPageTranscriptCount}</span>
          <span className="archive-recordings-page__summary-pill">已分析 {currentPageAnalysisCount}</span>
        </div>

        {error ? (
          <Alert
            type="error"
            showIcon
            message="录音列表加载失败"
            description={String(error)}
            style={{ marginBottom: 16 }}
          />
        ) : null}

        <Table
          className="archive-recordings-page__table"
          rowKey="id"
          size="small"
          loading={isLoading}
          columns={columns}
          dataSource={items}
          scroll={{ x: 1140 }}
          locale={{ emptyText: <Empty description="暂无录音" /> }}
          pagination={{
            current: page,
            pageSize,
            total: data?.total ?? 0,
            showSizeChanger: true,
            pageSizeOptions: ['10', '20', '50', '100'],
            showTotal: (total) => `共 ${total} 条`,
            onChange: (nextPage, nextPageSize) => {
              setPage(nextPage)
              setPageSize(nextPageSize)
            },
          }}
          onRow={(record) => ({
            onClick: () => setActiveId(record.id),
            style: { cursor: 'pointer' },
          })}
        />
      </Card>

      <Drawer
        title={detail ? formatArchiveRecordingName(detail.display_file_name, detail.create_time) : '录音详情'}
        width={960}
        open={!!activeId}
        onClose={() => setActiveId(null)}
        destroyOnClose
      >
        {detailLoading ? (
          <Spin size="large" style={{ display: 'block', margin: '80px auto' }} />
        ) : detailError ? (
          <Alert type="error" showIcon message="详情加载失败" description={String(detailError)} />
        ) : !detail ? (
          <Empty description="暂无详情" />
        ) : (
          <div style={{ display: 'grid', gap: 16 }}>
            {detail.quality_reason ? (
              <Alert type="warning" showIcon message="该录音已被质检过滤" description={detail.quality_reason} />
            ) : null}
            {detail.error_message ? (
              <Alert type="error" showIcon message="处理过程中出现异常" description={detail.error_message} />
            ) : null}

            <Card title="基础信息">
              <Descriptions bordered size="small" column={2}>
                <Descriptions.Item label="录音文件名">{formatArchiveRecordingName(detail.display_file_name, detail.create_time)}</Descriptions.Item>
                <Descriptions.Item label="工牌号">{detail.sn || detail.device_code || '-'}</Descriptions.Item>
                <Descriptions.Item label="员工">{detail.staff_name || '-'}</Descriptions.Item>
                <Descriptions.Item label="录音时间">{formatDateTime(detail.create_time)}</Descriptions.Item>
                <Descriptions.Item label="下载时间">{formatDateTime(detail.downloaded_at)}</Descriptions.Item>
                <Descriptions.Item label="录音时长">{formatDurationMs(detail.duration_ms, detail.duration_seconds)}</Descriptions.Item>
                <Descriptions.Item label="文件大小">{formatFileSize(detail.file_size)}</Descriptions.Item>
                <Descriptions.Item label="处理状态">
                  <Tag color={(STATUS_META[detail.pipeline_status ?? 'archived'] ?? STATUS_META.archived).color}>
                    {(STATUS_META[detail.pipeline_status ?? 'archived'] ?? STATUS_META.archived).label}
                  </Tag>
                </Descriptions.Item>
                <Descriptions.Item label="stageKey">{detail.stage_key || '-'}</Descriptions.Item>
              </Descriptions>

              <div style={{ marginTop: 16 }}>
                <Space align="center" size={8} style={{ marginBottom: 12 }}>
                  <AudioOutlined style={{ color: '#1677ff' }} />
                  <Text strong>音频试听</Text>
                  {audioLoading ? <Text type="secondary">加载中…</Text> : null}
                </Space>
                {audioUrl ? (
                  <audio ref={archiveAudioRef} controls src={audioUrl} style={{ width: '100%' }} />
                ) : (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前录音文件不可播放" />
                )}
                {canSplitArchiveRecording ? (
                  <div className="archive-recording-split-tools">
                    <Button icon={<ScissorOutlined />} onClick={openSplitModal}>
                      按时间点裁切
                    </Button>
                    <Text type="secondary">裁切后会生成两段待重新关联的录音</Text>
                  </div>
                ) : null}
              </div>
            </Card>

            <Card title="ASR 转写">
              <Space wrap size={[8, 8]} style={{ marginBottom: 12 }}>
                {transcriptProvider ? <Tag color="processing">{transcriptProvider}</Tag> : null}
                {detail.utterance_count != null ? <Tag>{detail.utterance_count} 段发言</Tag> : null}
                {detail.full_text_length != null ? <Tag>{detail.full_text_length} 字</Tag> : null}
              </Space>

              {utterances.length > 0 ? (
                <div style={{ display: 'grid', gap: 12 }}>
                  {utterances.map((item, index) => {
                    const utterance = asRecord(item)
                    if (!utterance) return null
                    const speakerKey = asText(utterance.speaker_business_role) || asText(utterance.speaker_role) || asText(utterance.speaker) || 'unknown'
                    const speakerMeta = SPEAKER_MAP[speakerKey] ?? SPEAKER_MAP.unknown
                    const speakerLabel = asText(utterance.speaker_display_label) || speakerMeta.label
                    const identityType = asText(utterance.speaker_identity_type)
                    const boundStaffName = asText(utterance.speaker_staff_name)
                    const similarity = asNumber(utterance.speaker_voiceprint_similarity)
                    const speakerId = asText(utterance.speaker_id)
                    const identityLabel = identityType === 'staff'
                      ? '员工'
                      : identityType === 'visitor'
                        ? '访客'
                        : identityType === 'unknown'
                          ? '未知'
                          : null

                    return (
                      <div
                        key={`${speakerId || speakerKey}-${index}`}
                        style={{
                          padding: 14,
                          border: '1px solid #e5e7eb',
                          borderRadius: 12,
                          background: '#fafafa',
                        }}
                      >
                        <Space wrap size={[8, 8]} style={{ marginBottom: 8 }}>
                          <Tag color={speakerMeta.color}>{speakerLabel}</Tag>
                          {identityLabel ? <Tag>{identityLabel}</Tag> : null}
                          {boundStaffName ? <Tag color="cyan">{boundStaffName}</Tag> : null}
                          {speakerId ? <Tag>speaker: {speakerId}</Tag> : null}
                          {similarity != null ? <Tag>相似度 {similarity.toFixed(3)}</Tag> : null}
                          <Text type="secondary">
                            {formatMs(asNumber(utterance.begin_ms))} - {formatMs(asNumber(utterance.end_ms))}
                          </Text>
                        </Space>
                        <div style={{ whiteSpace: 'pre-wrap', lineHeight: 1.8 }}>
                          {asText(utterance.text) || '（空白发言）'}
                        </div>
                      </div>
                    )
                  })}
                </div>
              ) : transcriptFullText ? (
                <Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>{transcriptFullText}</Paragraph>
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无转写结果" />
              )}
            </Card>

            <Card title="分析结果">
              {analysisResult ? (
                <div style={{ display: 'grid', gap: 16 }}>
                  <Descriptions bordered size="small" column={2}>
                    <Descriptions.Item label={totalScore != null ? '九点评分' : '总评分'}>
                      {totalScore != null
                        ? `${formatScore(totalScore)} / ${formatScore(maxTotalScore)}`
                        : overallScore != null
                          ? overallScore.toFixed(1)
                          : '-'}
                    </Descriptions.Item>
                    <Descriptions.Item label="沟通类型">
                      {asText(analysisSummary?.dialogue_type) || '-'}
                    </Descriptions.Item>
                    <Descriptions.Item label="关注点">
                      {asArray(analysisSummary?.focus_areas).filter((item) => typeof item === 'string').join('、') || '-'}
                    </Descriptions.Item>
                    <Descriptions.Item label="顾虑数">
                      {asNumber(analysisSummary?.concern_count) ?? 0}
                    </Descriptions.Item>
                    <Descriptions.Item label="画像标签数">
                      {asNumber(analysisSummary?.tag_count) ?? 0}
                    </Descriptions.Item>
                    <Descriptions.Item label="推荐项数">
                      {asNumber(analysisSummary?.recommendation_count) ?? 0}
                    </Descriptions.Item>
                  </Descriptions>

                  {overallSummary ? (
                    <div>
                      <Text strong>综合结论</Text>
                      <Paragraph style={{ whiteSpace: 'pre-wrap', marginTop: 8, marginBottom: 0 }}>
                        {overallSummary}
                      </Paragraph>
                    </div>
                  ) : null}

                  {evaluationDimensions.length ? (
                    <div>
                      <Text strong>员工评价</Text>
                      <div style={{ display: 'grid', gap: 12, marginTop: 8 }}>
                        {evaluationDimensions.map((item) => (
                          <div
                            key={item.name}
                            style={{
                              padding: 14,
                              border: '1px solid #e5e7eb',
                              borderRadius: 12,
                              background: '#fafafa',
                            }}
                          >
                            <Space wrap size={[8, 8]} style={{ marginBottom: 8 }}>
                              <Text strong>{item.name}</Text>
                              {item.pointScore != null ? (
                                <Tag color="gold">
                                  {`得分 ${formatScore(item.pointScore)} / ${formatScore(item.maxScore)}`}
                                </Tag>
                              ) : null}
                            </Space>
                            {item.summary ? (
                              <Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: item.issues.length ? 8 : 0 }}>
                                {item.summary}
                              </Paragraph>
                            ) : null}
                            {(() => {
                              const summaryText = (item.summary || '').trim()
                              const visibleIssues = item.issues.filter((issue) => {
                                const desc = (issue.description || '').trim()
                                if (!desc && !issue.evidence) return false
                                if (!issue.evidence && summaryText && desc === summaryText) return false
                                return true
                              }).slice(0, 2)
                              if (visibleIssues.length === 0) return null
                              return (
                                <div style={{ display: 'grid', gap: 6 }}>
                                  {visibleIssues.map((issue, index) => (
                                    <div key={`${item.name}-${index}`} style={{ color: '#475569', fontSize: 13, lineHeight: 1.7 }}>
                                      {issue.description || '待补充说明'}
                                      {issue.evidence ? `：${issue.evidence}` : ''}
                                    </div>
                                  ))}
                                </div>
                              )
                            })()}
                          </div>
                        ))}
                      </div>
                    </div>
                  ) : null}

                  <div>
                    <Text strong>原始分析 JSON</Text>
                    <div style={{ marginTop: 8 }}>
                      {renderPrettyJson(analysisResult)}
                    </div>
                  </div>
                </div>
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无分析结果" />
              )}
            </Card>
          </div>
        )}
      </Drawer>

      <Modal
        title={
          matchingItem
            ? `${recommendMode === 'llm' ? 'LLM推荐关联' : '规则推荐关联'} · ${formatArchiveRecordingName(matchingItem.display_file_name, matchingItem.create_time)}`
            : '推荐关联到诊单'
        }
        open={matchOpen}
        onCancel={closeRecommendModal}
        footer={null}
        destroyOnClose
        width={980}
      >
        {ensureRecordingMut.isPending ? (
          <Spin size="large" style={{ display: 'block', margin: '80px auto' }} />
        ) : ensureRecordingMut.isError ? (
          <Alert
            type="error"
            showIcon
            message="推荐关联准备失败"
            description="这条录音暂时还不能生成推荐关联，请检查转写状态或稍后再试。"
          />
        ) : !ensuredRecording ? (
          <Empty description="正在准备推荐关联..." />
        ) : (
          <div style={{ display: 'grid', gap: 16 }}>
            <Descriptions size="small" bordered column={2}>
              <Descriptions.Item label="当前录音">
                {formatArchiveRecordingName(
                  matchingItem?.display_file_name || ensuredRecording.display_file_name,
                  matchingItem?.create_time || null,
                )}
              </Descriptions.Item>
              <Descriptions.Item label="正式录音ID">{ensuredRecording.recording_id}</Descriptions.Item>
              <Descriptions.Item label="当前关联">
                {renderLinkedVisitOrderSummary(
                  matchData?.linked_visit_order_refs.length
                    ? matchData.linked_visit_order_refs
                    : ensuredRecording.linked_visit_order_refs,
                  matchData?.linked_visit_order_no,
                  matchData?.linked_visit_order_seg,
                )}
              </Descriptions.Item>
              <Descriptions.Item label="最高推荐置信度">
                {matchDisplay.items.length ? getConfidenceTag(matchDisplay.items[0].confidence) : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="分析说明" span={2}>
                {matchLoading || matchFetching ? '正在分析匹配建议...' : matchData?.summary || '暂无推荐分析结果'}
              </Descriptions.Item>
              <Descriptions.Item label="推荐模式" span={2}>
                {recommendMode === 'llm' ? 'LLM 推荐' : '规则推荐'}
              </Descriptions.Item>
            </Descriptions>

            {matchData?.manual_review_required ? renderConflictBlock(matchData.identity_conflicts, matchData.manual_review_reason) : null}

            {matchDisplay.hiddenCount > 0 ? (
              <Tag>已隐藏 {matchDisplay.hiddenCount} 条低置信度或明显落后于 Top1 的候选</Tag>
            ) : null}

            {ensuredRecording ? (
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'center' }}>
                <span style={{ color: '#666' }}>
                  如果推荐候选里没有正确到诊单，可以展开查看当天全部到诊单做人工复核。
                </span>
                <Space wrap>
                  <Button onClick={openDailyVisitOrders}>
                    查看自己当天全部到诊单
                  </Button>
                  <Button
                    onClick={() => {
                      openDailyVisitOrders()
                      setDailyVisitOrdersMode('org')
                    }}
                  >
                    查看所有人当天全部到诊单
                  </Button>
                </Space>
              </div>
            ) : null}

            {matchError ? (
              <Alert
                type="error"
                showIcon
                message="推荐候选加载失败"
                description={String(matchError)}
              />
            ) : (
              <Table<VisitOrderMatchCandidate>
                rowKey="visit_order_id"
                loading={matchLoading || matchFetching}
                dataSource={matchDisplay.items}
                pagination={false}
                size="small"
                scroll={{ x: 780 }}
                locale={{ emptyText: '暂无可展示的候选到诊单' }}
                columns={[
                  {
                    title: '候选到诊单',
                    width: 220,
                    render: (_value, row) => (
                      <div style={{ display: 'grid', gap: 4 }}>
                        <strong>{formatMergedVisitOrderTitle(row.dzdh, row.dzseg, row.merged_line_items?.length ?? 0)}</strong>
                        {row.merged_segments?.length > 1 ? (
                          <Tag color="blue" style={{ width: 'fit-content' }}>
                            已合并 {row.merged_segments.length} 条分诊明细
                          </Tag>
                        ) : null}
                        <span>{row.customer_name || '-'} / {row.customer_code || '-'}</span>
                        {row.customer_type_label ? (
                          <Tag color={row.customer_type_code === 'V' ? 'gold' : 'green'} style={{ width: 'fit-content' }}>
                            {row.customer_type_label}
                          </Tag>
                        ) : null}
                        <span>{row.visit_date || '-'}</span>
                        <span style={{ color: '#666', fontSize: 12 }}>
                          {row.triage_time ? `分诊 ${fmtClock(row.triage_time)}` : ''}
                          {!row.triage_time ? '分诊时间未知' : ''}
                        </span>
                        {row.companion_visit_order_refs.length > 0 ? (
                          <span>同行辅单：{row.companion_visit_order_refs.join(' / ')}</span>
                        ) : null}
                        {row.linked_recording_names?.length > 0 ? (
                          <Tag color="orange" style={{ width: 'fit-content' }}>
                            已关联录音：{row.linked_recording_names.join('、')}
                          </Tag>
                        ) : null}
                        {renderMergedLineItemBlock(row)}
                      </div>
                    ),
                  },
                  {
                    title: '判定',
                    width: 140,
                    render: (_value, row) => (
                      <div style={{ display: 'grid', gap: 6 }}>
                        {getMatchTag(row.decision)}
                        {getConfidenceTag(row.confidence)}
                        <span>{getMethodLabel(row.method)}</span>
                      </div>
                    ),
                  },
                  {
                    title: '关键证据',
                    width: 280,
                    render: (_value, row) => (
                      <div style={{ display: 'grid', gap: 8 }}>
                        {row.manual_review_required ? renderConflictBlock(row.identity_conflicts, row.manual_review_reason) : null}
                        <div>{renderEvidenceBlock(row)}</div>
                      </div>
                    ),
                  },
                  {
                    title: '操作',
                    width: 180,
                    render: (_value, row) => (
                      <Space wrap>
                        <Button size="small" onClick={() => openVisitOrderDetail(row.visit_order_id)}>
                          查看详情
                        </Button>
                        <Button
                          type="primary"
                          size="small"
                          disabled={!row.local_visit_id}
                          loading={adoptMatchMut.isPending}
                          onClick={() => {
                            if (!row.local_visit_id) return
                            void handleRecordingVisitLink({
                              recordingId: ensuredRecording.recording_id,
                              visitId: row.local_visit_id,
                              companionVisitIds: row.associated_local_visit_ids,
                              companionVisitOrderRefs: row.companion_visit_order_refs,
                              companionCustomerCodes: row.companion_customer_codes,
                              targetLinkedRecordingNames: row.linked_recording_names,
                              targetLinkedRecordingCount: row.linked_recording_count,
                            })
                          }}
                        >
                          采用推荐
                        </Button>
                      </Space>
                    ),
                  },
                ]}
              />
            )}
          </div>
        )}
      </Modal>

      <Modal
        title={dailyVisitOrdersContext ? `当天到诊单 · ${dailyVisitOrdersContext.fileName}` : '当天到诊单'}
        open={Boolean(dailyVisitOrdersContext)}
        onCancel={() => {
          setDailyVisitOrdersContext(null)
          setDailyVisitOrdersMode('self')
          setDailyVisitOrdersKeyword('')
          setDailyVisitOrdersSearchDraft('')
        }}
        footer={
          <Button
            onClick={() => {
              setDailyVisitOrdersContext(null)
              setDailyVisitOrdersMode('self')
              setDailyVisitOrdersKeyword('')
              setDailyVisitOrdersSearchDraft('')
            }}
          >
            关闭
          </Button>
        }
        destroyOnClose
        width={1080}
      >
        <Space wrap style={{ marginBottom: 12 }}>
          <Button
            type={dailyVisitOrdersMode === 'self' ? 'primary' : 'default'}
            onClick={() => {
              setDailyVisitOrdersMode('self')
              setDailyVisitOrdersKeyword('')
              setDailyVisitOrdersSearchDraft('')
            }}
          >
            查看自己当天全部到诊单
          </Button>
          <Button
            type={dailyVisitOrdersMode === 'org' ? 'primary' : 'default'}
            onClick={() => setDailyVisitOrdersMode('org')}
          >
            查看所有人当天全部到诊单
          </Button>
          {dailyVisitOrdersMode === 'org' ? (
            <Input.Search
              allowClear
              enterButton="搜索"
              onChange={(event) => setDailyVisitOrdersSearchDraft(event.target.value)}
              onSearch={(value) => setDailyVisitOrdersKeyword(value.trim())}
              placeholder="搜索客户、到诊单号、咨询师、备注"
              style={{ minWidth: 280 }}
              value={dailyVisitOrdersSearchDraft}
            />
          ) : null}
        </Space>
        <Descriptions size="small" bordered column={2} style={{ marginBottom: 16 }}>
          <Descriptions.Item label="录音日期">
            {dailyVisitOrdersData?.recording_date || '-'}
          </Descriptions.Item>
          <Descriptions.Item label="查看范围">
            {dailyVisitOrdersMode === 'org' ? '所有人当天全部到诊单' : '自己当天全部到诊单'}
          </Descriptions.Item>
          <Descriptions.Item label="当天到诊单数">
            {dailyVisitOrdersData?.total ?? 0}
          </Descriptions.Item>
          <Descriptions.Item label="说明" span={2}>
            {dailyVisitOrdersMode === 'org'
              ? '这里展示当前录音所属机构同一天的全部到诊单，便于跨员工做人工复核。'
              : '这里展示当前录音员工同一天的到诊单，便于在推荐列表之外做人工复核。'}
          </Descriptions.Item>
        </Descriptions>

        <Table<DailyVisitOrderItem>
          rowKey="id"
          loading={isDailyVisitOrdersLoading}
          dataSource={dailyVisitOrdersData?.items ?? []}
          pagination={false}
          size="small"
          scroll={{ x: 820, y: 440 }}
          columns={[
            {
              title: '到诊单',
              width: 146,
              render: (_value, row) => {
                const rowRef = `${row.dzdh}${row.dzseg ? `-${row.dzseg}` : ''}`
                const isCurrentLinked = currentDailyVisitOrderRefSet.has(rowRef)
                return (
                  <div style={{ display: 'grid', gap: 6 }}>
                    <strong>{rowRef}</strong>
                    {isCurrentLinked ? <Tag color="success" style={{ width: 'fit-content' }}>当前录音已关联</Tag> : null}
                    <span style={{ color: '#666', fontSize: 12 }}>{row.sjrq || dailyVisitOrdersData?.recording_date || '-'}</span>
                  </div>
                )
              },
            },
            {
              title: '客户',
              width: 164,
              render: (_value, row) => (
                <div style={{ display: 'grid', gap: 4 }}>
                  <strong>{row.ninam || '-'}</strong>
                  <span>{row.kunr || '-'}</span>
                  {row.customer_type_label ? (
                    <Tag color={row.customer_type_code === 'V' ? 'gold' : 'green'} style={{ width: 'fit-content' }}>
                      {row.customer_type_label}
                    </Tag>
                  ) : null}
                  <span>{row.jcsta_txt || '状态未知'}</span>
                </div>
              ),
            },
            {
              title: '时间',
              width: 118,
              render: (_value, row) => (
                <div style={{ display: 'grid', gap: 4 }}>
                  <span>{row.fzsj ? `分诊 ${fmtClock(row.fzsj)}` : '分诊时间未知'}</span>
                  <span>{row.jcsta_txt || '业务状态未知'}</span>
                </div>
              ),
            },
            {
              title: '顾问 / 备注',
              width: 176,
              render: (_value, row) => (
                <div style={{ display: 'grid', gap: 4 }}>
                  <span>分诊顾问：{row.fzuer || '-'}</span>
                  <span>现场顾问：{row.advxc_long || '-'}</span>
                  <span>{row.remark_dz || '无到诊备注'}</span>
                </div>
              ),
            },
            {
              title: '状态 / 关联',
              width: 152,
              render: (_value, row) => (
                <div style={{ display: 'grid', gap: 6 }}>
                  <span>{row.jcsta_txt || '成交状态未知'}</span>
                  {row.companion_visit_order_refs.length > 0 ? (
                    <span style={{ color: '#666', fontSize: 12 }}>
                      同行辅单：{row.companion_visit_order_refs.join(' / ')}
                    </span>
                  ) : null}
                  {row.linked_recording_names.length > 0 ? (
                    <Tag color="orange" style={{ width: 'fit-content', whiteSpace: 'normal' }}>
                      已关联录音：{row.linked_recording_names.join('、')}
                    </Tag>
                  ) : (
                    <Tag color="green" style={{ width: 'fit-content' }}>
                      暂无其他录音关联
                    </Tag>
                  )}
                </div>
              ),
            },
            {
              title: '操作',
              width: 142,
              render: (_value, row) => (
                <Space wrap>
                  <Button size="small" onClick={() => openVisitOrderDetail(row.id)}>
                    查看详情
                  </Button>
                  <Button
                    type="primary"
                    size="small"
                    disabled={!row.local_visit_id || !dailyVisitOrdersContext}
                    loading={adoptMatchMut.isPending}
                    onClick={() => {
                      if (!row.local_visit_id || !dailyVisitOrdersContext) return
                      void handleRecordingVisitLink({
                        recordingId: dailyVisitOrdersContext.recordingId,
                        visitId: row.local_visit_id,
                        alwaysLinkedVisitIds: row.associated_local_visit_ids,
                        companionVisitIds: row.companion_local_visit_ids,
                        companionVisitOrderRefs: row.companion_visit_order_refs,
                        companionCustomerCodes: row.companion_customer_codes,
                        targetLinkedRecordingNames: row.linked_recording_names,
                      })
                    }}
                  >
                    采用这张到诊单
                  </Button>
                </Space>
              ),
            },
          ]}
        />
      </Modal>

      <Modal
        open={splitModalOpen}
        title="确认裁切录音"
        okText="确认裁切"
        cancelText="取消"
        okButtonProps={{ danger: true, loading: splitArchiveRecordingMut.isPending }}
        onOk={() => splitArchiveRecordingMut.mutate()}
        onCancel={() => {
          if (!splitArchiveRecordingMut.isPending) setSplitModalOpen(false)
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
              max={Math.max(1, (detailDurationSeconds ?? 2) - 1)}
              value={splitAtSeconds}
              precision={0}
              addonAfter="秒"
              style={{ width: '100%' }}
              onChange={(value) => setSplitAtSeconds(typeof value === 'number' ? value : null)}
            />
            <div style={{ color: '#999', fontSize: 12, marginTop: 6 }}>
              将在 {splitAtSeconds != null ? formatMs(splitAtSeconds * 1000) : '--:--'} 处分成前后两段。
            </div>
          </div>
        </Space>
      </Modal>

      <Modal
        title={visitOrderDetail ? `到诊单详情 · ${visitOrderDetail.dzdh}${visitOrderDetail.dzseg ? `-${visitOrderDetail.dzseg}` : ''}` : '到诊单详情'}
        open={visitOrderDetailOpen}
        onCancel={() => {
          setVisitOrderDetailOpen(false)
          setViewingVisitOrderId(null)
        }}
        footer={
          <Button
            onClick={() => {
              setVisitOrderDetailOpen(false)
              setViewingVisitOrderId(null)
            }}
          >
            关闭
          </Button>
        }
        destroyOnClose
        width={980}
      >
        {isVisitOrderDetailLoading ? (
          <Spin size="large" style={{ display: 'block', margin: '80px auto' }} />
        ) : (
          <Descriptions size="small" bordered column={2}>
            <Descriptions.Item label="到诊单号">{visitOrderDetail?.dzdh || '-'}</Descriptions.Item>
            <Descriptions.Item label="数据日期">{visitOrderDetail?.sjrq || '-'}</Descriptions.Item>
            <Descriptions.Item label="客户姓名">{visitOrderDetail?.ninam || '-'}</Descriptions.Item>
            <Descriptions.Item label="客户编码">{visitOrderDetail?.kunr || '-'}</Descriptions.Item>
            <Descriptions.Item label="客户属性">{visitOrderDetail?.kutyp_dq_txt || visitOrderDetail?.kut30_dq_txt || visitOrderDetail?.kusta_dq_txt || visitOrderDetail?.kusex_txt || '-'}</Descriptions.Item>
            <Descriptions.Item label="机构编码">{visitOrderDetail?.jgbm || '-'}</Descriptions.Item>
            <Descriptions.Item label="接诊顾问">{visitOrderDetail?.fzuer_long || visitOrderDetail?.fzuer || '-'}</Descriptions.Item>
            <Descriptions.Item label="客服">{visitOrderDetail?.vipkf || visitOrderDetail?.d_vipkf || '-'}</Descriptions.Item>
            <Descriptions.Item label="现场顾问">{visitOrderDetail?.advxc_long || visitOrderDetail?.advxc || '-'}</Descriptions.Item>
            <Descriptions.Item label="分诊时间">{visitOrderDetail?.fzsj || '-'}</Descriptions.Item>
            <Descriptions.Item label="创建时间">{visitOrderDetail?.crttm || '-'}</Descriptions.Item>
            <Descriptions.Item label="到诊状态">{visitOrderDetail?.dzsta_txt || '-'}</Descriptions.Item>
            <Descriptions.Item label="成交状态">{visitOrderDetail?.jcsta_txt || '-'}</Descriptions.Item>
            <Descriptions.Item label="到诊备注">{visitOrderDetail?.remark_dz || '-'}</Descriptions.Item>
            <Descriptions.Item label="机构科室">{visitOrderDetail?.jgks_txt || visitOrderDetail?.jgks || '-'}</Descriptions.Item>
            <Descriptions.Item label="到院目的">{visitOrderDetail?.dymd_txt || '-'}</Descriptions.Item>
            <Descriptions.Item label="到诊来源">{visitOrderDetail?.dzly_txt || '-'}</Descriptions.Item>
            <Descriptions.Item label="到诊需求" span={2}>{visitOrderDetail?.remark_dz || '-'}</Descriptions.Item>
          </Descriptions>
        )}
      </Modal>
      </div>
    </section>
  )
}
