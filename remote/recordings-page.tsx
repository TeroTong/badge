import { useState, type Key } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AudioOutlined,
  DeleteOutlined,
  ExportOutlined,
  EyeOutlined,
  SearchOutlined,
  UploadOutlined,
} from '@ant-design/icons'
import {
  Avatar,
  Button,
  Descriptions,
  DatePicker,
  Form,
  Input,
  message,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Upload,
} from 'antd'
import type { Dayjs } from 'dayjs'
import { Link, useNavigate } from 'react-router-dom'

import * as adminApi from '@/api/admin'
import { getApiErrorMessage } from '@/api/errors'
import type { Recording, RecordingUpdatePayload, RecordingVisitOrderMatch, VisitOrderMatchCandidate } from '@/api/recordings'
import * as recordingsApi from '@/api/recordings'
import { STAFF_ROLE_MAP } from '@/api/recordings'
import {
  buildCompanionVisitPromptMessage,
  buildLinkedVisitIds,
  hasCompanionVisitOptions,
} from '@/utils/companion-visit-linking'
import { formatStaffDisplayLabel, getRecordingDeviceBadge } from '@/utils/staff-display'
import { getDisplayMatchEvidenceLines } from '@/utils/match-evidence'
import { getQuickRecommendSelection } from '@/utils/visit-order-recommendations'
import { buildRecordingVisitLinkRiskLines } from '@/utils/recording-visit-link-confirmation'
import {
  buildVisitOrderLineItemMeta,
  formatMergedVisitOrderTitle,
  formatVisitOrderLineItemRef,
} from '@/utils/visit-order-line-items'
import * as visitsApi from '@/api/visits'
import { formatRecordingDisplayName } from '@/utils/recording-display'
import { beijingNow, formatBeijingTime } from '@/utils/time'

const { RangePicker } = DatePicker

type QuickRange = 'all' | 'today' | 'yesterday' | '7d' | '30d' | '90d' | 'custom'
type LinkFilter = 'all' | 'linked' | 'unlinked'
type DailyVisitOrdersMode = 'self' | 'org'

type RecordingFilters = {
  quickRange: QuickRange
  dateRange: [Dayjs | null, Dayjs | null] | null
  linkState: LinkFilter
  staff_id?: string
  role?: string
  customer_keyword: string
  badge_id: string
  visit_id: string
  keyword: string
}

/** Format HHMMSS string like "182016" to "18:20:16" */
function fmtClock(s: string | null | undefined): string {
  if (!s || s.length < 4) return s || ''
  const normalized = s.replace(/[^0-9]/g, '')
  if (normalized.length < 4) return s
  const p = normalized.padStart(6, '0')
  return `${p.slice(0,2)}:${p.slice(2,4)}:${p.slice(4,6)}`
}

const QUICK_RANGE_OPTIONS: Array<{ key: QuickRange; label: string }> = [
  { key: 'all', label: '全部' },
  { key: 'today', label: '今日' },
  { key: 'yesterday', label: '昨日' },
  { key: '7d', label: '近7日' },
  { key: '30d', label: '近30日' },
  { key: '90d', label: '近90日' },
]

const LINK_OPTIONS = [
  { value: 'all', label: '全部录音' },
  { value: 'linked', label: '已关联来访' },
  { value: 'unlinked', label: '未关联来访' },
]

const ROLE_OPTIONS = [
  { value: 'consultant', label: '咨询师' },
  { value: 'doctor', label: '医生' },
]

function createDefaultFilters(): RecordingFilters {
  return {
    quickRange: 'all',
    dateRange: null,
    linkState: 'all',
    staff_id: undefined,
    role: undefined,
    customer_keyword: '',
    badge_id: '',
    visit_id: '',
    keyword: '',
  }
}

function resolveQuickRange(range: QuickRange): [Dayjs | null, Dayjs | null] | null {
  const today = beijingNow()
  if (range === 'all' || range === 'custom') return null
  if (range === 'today') return [today.startOf('day'), today.endOf('day')]
  if (range === 'yesterday') {
    const yesterday = today.subtract(1, 'day')
    return [yesterday.startOf('day'), yesterday.endOf('day')]
  }
  if (range === '7d') return [today.subtract(6, 'day').startOf('day'), today.endOf('day')]
  if (range === '30d') return [today.subtract(29, 'day').startOf('day'), today.endOf('day')]
  return [today.subtract(89, 'day').startOf('day'), today.endOf('day')]
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(2)} KB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`
}

function formatDuration(seconds: number | null): string {
  if (seconds == null) return '--:--'
  const total = Math.max(0, seconds)
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const secs = Math.floor(total % 60)
  if (hours > 0) return `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`
  return `${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`
}

function formatVisitRef(visit: {
  id: string
  external_visit_order_no?: string | null
  external_visit_order_seg?: string | null
  customer_name?: string | null
}) {
  const orderRef = visit.external_visit_order_no
    ? `${visit.external_visit_order_no}${visit.external_visit_order_seg ? `-${visit.external_visit_order_seg}` : ''}`
    : visit.id
  return visit.customer_name ? `${orderRef} · ${visit.customer_name}` : orderRef
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

function getRecordingLinkedVisitSummary(recording: Recording) {
  if (!recording.linked_visits.length) {
    return {
      label: '未关联来访',
      detail: '',
      count: 0,
      primaryVisit: null as Recording['linked_visits'][number] | null,
      secondaryVisits: [] as Recording['linked_visits'],
    }
  }
  const primary = recording.linked_visits.find((item) => item.is_primary) ?? recording.linked_visits[0]
  const secondaryVisits = recording.linked_visits.filter((item) => item.id !== primary.id)
  const extraCount = secondaryVisits.length
  return {
    label: `主接诊单 ${formatVisitRef(primary)}`,
    detail: extraCount > 0 ? `另有关联 ${extraCount} 张辅接诊单` : '',
    count: recording.linked_visits.length,
    primaryVisit: primary,
    secondaryVisits,
  }
}

function buildRecordingLinkPayload(values: { visit_id?: string; linked_visit_ids?: string[] }): RecordingUpdatePayload {
  const primaryVisitId = values.visit_id?.trim() || null
  const linkedVisitIds = Array.from(new Set([primaryVisitId, ...(values.linked_visit_ids ?? [])].filter(Boolean) as string[]))
  return {
    visit_id: primaryVisitId,
    linked_visit_ids: linkedVisitIds,
  }
}

function extractNativeFile(input: unknown): File | null {
  const candidate =
    (input as { file?: { originFileObj?: unknown } })?.file?.originFileObj ??
    (input as { originFileObj?: unknown })?.originFileObj ??
    (input as { fileList?: Array<{ originFileObj?: unknown }> })?.fileList?.[0]?.originFileObj ??
    input

  return candidate instanceof File ? candidate : null
}

function exportCurrentRows(rows: Recording[]) {
  const headers = ['录音名称', '设备工牌号', '录音归属', '创建时间', '接诊单ID']
  const lines = rows.map((row) => [
    formatRecordingDisplayName(row.file_name, row.created_at),
    getRecordingDeviceBadge(row) ?? '',
    row.staff_name ?? '',
    row.created_at ? formatBeijingTime(row.created_at, 'YYYY-MM-DD HH:mm:ss') : '',
    row.visit_id ?? '',
  ])
  const csv = [headers, ...lines]
    .map((line) => line.map((item) => `"${String(item).replaceAll('"', '""')}"`).join(','))
    .join('\n')
  const blob = new Blob([`\uFEFF${csv}`], { type: 'text/csv;charset=utf-8;' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `recordings-${beijingNow().format('YYYYMMDD-HHmmss')}.csv`
  anchor.click()
  URL.revokeObjectURL(url)
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

function getManualReviewTag(reason?: string | null) {
  return <Tag color="warning">需人工确认{reason ? `：${reason}` : ''}</Tag>
}

const VERY_LOW_TOP1_CONFIDENCE = 0.45
const MIN_VISIBLE_CANDIDATE_CONFIDENCE = 0.35
const MAX_VISIBLE_CONFIDENCE_GAP = 0.2

function getDisplayCandidates(candidates: VisitOrderMatchCandidate[], preserveAll = false) {
  if (!candidates.length) {
    return {
      items: [] as VisitOrderMatchCandidate[],
      hiddenCount: 0,
      top1Weak: false,
    }
  }

  if (preserveAll) {
    return {
      items: candidates,
      hiddenCount: 0,
      top1Weak: false,
    }
  }

  const top1 = candidates[0]
  const top1Weak = top1.confidence < VERY_LOW_TOP1_CONFIDENCE
  if (top1Weak) {
    return {
      items: candidates,
      hiddenCount: 0,
      top1Weak,
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
    top1Weak,
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
      {manualReviewReason && <span style={{ color: '#8c6d1f' }}>{manualReviewReason}</span>}
      {visibleConflicts.length > 0 && (
        <div style={{ display: 'grid', gap: 4, color: '#8c6d1f' }}>
          {visibleConflicts.map((conflict) => (
            <span key={conflict}>{conflict}</span>
          ))}
        </div>
      )}
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

type RecordingMatchPreviewProps = {
  recording: Recording
  expanded: boolean
  onOpenVisitOrderDetail: (visitOrderId: string) => void
  onOpenFullMatchModal: (recording: Recording) => void
  onOpenDailyVisitOrders: (recording: Recording, mode: DailyVisitOrdersMode) => void
  onAdoptRecommendation: (payload: {
    recordingId: string
    visitId: string
    alwaysLinkedVisitIds?: string[]
    companionVisitIds?: string[]
    companionVisitOrderRefs?: string[]
    companionCustomerCodes?: string[]
    targetLinkedRecordingNames?: string[]
    targetLinkedRecordingCount?: number
  }) => void
  adoptPending: boolean
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

function RecordingMatchPreview({
  recording,
  expanded,
  onOpenVisitOrderDetail,
  onOpenFullMatchModal,
  onOpenDailyVisitOrders,
  onAdoptRecommendation,
  adoptPending,
}: RecordingMatchPreviewProps) {
  const { data, error, isError, isLoading, refetch, isFetching } = useQuery<RecordingVisitOrderMatch>({
    queryKey: ['recording-visit-order-preview', recording.id],
    queryFn: () => recordingsApi.fetchRecordingVisitOrderMatch(recording.id, false, false),
    enabled: expanded,
  })

  if (!expanded) return null
  if (isLoading) {
    return <div style={{ padding: '8px 4px', color: '#666' }}>正在加载推荐到诊单...</div>
  }
  if (isError) {
    return (
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 12,
          padding: '8px 4px',
          color: '#999',
        }}
      >
        <span>{error instanceof Error ? `推荐加载失败：${error.message}` : '推荐加载失败，请稍后重试'}</span>
        <Button size="small" loading={isFetching} onClick={() => void refetch()}>
          重试
        </Button>
      </div>
    )
  }
  if (!data?.candidates?.length) {
    return <div style={{ padding: '8px 4px', color: '#999' }}>暂无可展示的推荐到诊单</div>
  }

  const displayCandidates = getQuickRecommendSelection(data.candidates)
  const topCandidates = displayCandidates.items

  if (!topCandidates.length) {
    return <div style={{ padding: '8px 4px', color: '#999' }}>暂无可展示的推荐到诊单</div>
  }

  return (
    <div style={{ display: 'grid', gap: 12, padding: '4px 0' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
        <div style={{ display: 'grid', gap: 4 }}>
          <strong>推荐到诊单</strong>
          <span style={{ color: '#666' }}>{data.summary || '系统已给出推荐候选'}</span>
          {data.manual_review_required && renderConflictBlock(data.identity_conflicts, data.manual_review_reason)}
          {displayCandidates.hiddenCount > 0 && (
            <span style={{ color: '#999' }}>已隐藏 {displayCandidates.hiddenCount} 条低置信度或明显落后于 Top1 的候选</span>
          )}
        </div>
        <Space>
          {data.auto_applied && <Tag color="success">系统已自动关联</Tag>}
          <Button size="small" onClick={() => onOpenDailyVisitOrders(recording, 'self')}>
            查看自己当天全部到诊单
          </Button>
          <Button size="small" onClick={() => onOpenDailyVisitOrders(recording, 'org')}>
            查看所有人当天全部到诊单
          </Button>
          <Button size="small" onClick={() => onOpenFullMatchModal(recording)}>
            查看完整建议
          </Button>
        </Space>
      </div>

      {topCandidates.map((candidate) => (
        <div
          key={candidate.visit_order_id}
          style={{
            display: 'grid',
            gap: 8,
            padding: 12,
            border: '1px solid #f0f0f0',
            borderRadius: 10,
            background: candidate.decision === 'recommend' || candidate.decision === 'auto' ? '#fafcff' : '#fff',
          }}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'flex-start' }}>
            <div style={{ display: 'grid', gap: 4 }}>
              <strong>{formatMergedVisitOrderTitle(candidate.dzdh, candidate.dzseg, candidate.merged_line_items?.length ?? 0)}</strong>
              {candidate.merged_segments?.length > 1 && (
                <Tag color="blue" style={{ marginLeft: 0, width: 'fit-content' }}>
                  已合并 {candidate.merged_segments.length} 条分诊明细
                </Tag>
              )}
              <span>{candidate.customer_name || '-'} / {candidate.customer_code || '-'}</span>
              <span>{candidate.visit_date || '-'}</span>
              <span style={{ color: '#666', fontSize: 12 }}>
                {candidate.triage_time ? `分诊 ${fmtClock(candidate.triage_time)}` : ''}
                {!candidate.triage_time ? '分诊时间未知' : ''}
              </span>
              {candidate.companion_visit_order_refs.length > 0 && (
                <span>同行辅单：{candidate.companion_visit_order_refs.join(' / ')}</span>
              )}
                {candidate.linked_recording_names?.length > 0 && (
                  <Tag color="orange" style={{ marginLeft: 0, width: 'fit-content' }}>已关联录音：{candidate.linked_recording_names.join('、')}</Tag>
                )}
            </div>
            <div style={{ display: 'grid', gap: 6, justifyItems: 'end' }}>
              {getMatchTag(candidate.decision)}
              {getConfidenceTag(candidate.confidence)}
              {candidate.manual_review_required && getManualReviewTag(candidate.manual_review_reason)}
            </div>
          </div>

          {candidate.manual_review_required && renderConflictBlock(candidate.identity_conflicts, candidate.manual_review_reason)}
          {renderMergedLineItemBlock(candidate)}
          <div style={{ color: '#666' }}>{renderEvidenceBlock(candidate)}</div>

          <Space wrap>
            <Button size="small" onClick={() => onOpenVisitOrderDetail(candidate.visit_order_id)}>
              查看到诊单详情
            </Button>
            <Button
              type="primary"
              size="small"
              disabled={!candidate.local_visit_id}
              loading={adoptPending}
              onClick={() => {
                if (!candidate.local_visit_id) return
                onAdoptRecommendation({
                  recordingId: recording.id,
                  visitId: candidate.local_visit_id,
                  companionVisitIds: candidate.associated_local_visit_ids,
                  companionVisitOrderRefs: candidate.companion_visit_order_refs,
                  companionCustomerCodes: candidate.companion_customer_codes,
                  targetLinkedRecordingNames: candidate.linked_recording_names,
                  targetLinkedRecordingCount: candidate.linked_recording_count,
                })
              }}
            >
              直接关联
            </Button>
          </Space>
        </div>
      ))}
    </div>
  )
}


export function RecordingsPage() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [selectedRowKeys, setSelectedRowKeys] = useState<Key[]>([])
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [draftFilters, setDraftFilters] = useState<RecordingFilters>(createDefaultFilters)
  const [queryFilters, setQueryFilters] = useState<RecordingFilters>(createDefaultFilters)

  const [uploadOpen, setUploadOpen] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [uploadForm] = Form.useForm()

  const [editOpen, setEditOpen] = useState(false)
  const [editing, setEditing] = useState<Recording | null>(null)
  const [editMode, setEditMode] = useState<'edit' | 'bind'>('edit')
  const [editForm] = Form.useForm()
  const [matchOpen, setMatchOpen] = useState(false)
  const [matchingRecording, setMatchingRecording] = useState<Recording | null>(null)
  const [expandedRowKeys, setExpandedRowKeys] = useState<Key[]>([])
  const [recentlyLinkedRecordingId, setRecentlyLinkedRecordingId] = useState<string | null>(null)
  const [visitOrderDetailOpen, setVisitOrderDetailOpen] = useState(false)
  const [viewingVisitOrderId, setViewingVisitOrderId] = useState<string | null>(null)
  const [visitDetailOpen, setVisitDetailOpen] = useState(false)
  const [viewingVisitId, setViewingVisitId] = useState<string | null>(null)
  const [dailyVisitOrdersOpen, setDailyVisitOrdersOpen] = useState(false)
  const [dailyVisitOrdersRecording, setDailyVisitOrdersRecording] = useState<Recording | null>(null)
  const [dailyVisitOrdersMode, setDailyVisitOrdersMode] = useState<DailyVisitOrdersMode>('self')
  const [dailyVisitOrdersKeyword, setDailyVisitOrdersKeyword] = useState('')
  const [dailyVisitOrdersSearchDraft, setDailyVisitOrdersSearchDraft] = useState('')

  const { data, isLoading } = useQuery({
    queryKey: ['recordings', queryFilters, page, pageSize],
    queryFn: () =>
      recordingsApi.fetchRecordings({
        visit_id: queryFilters.visit_id || undefined,
        staff_id: queryFilters.staff_id || undefined,
        keyword: queryFilters.keyword || undefined,
        customer_keyword: queryFilters.customer_keyword || undefined,
        badge_id: queryFilters.badge_id || undefined,
        role: queryFilters.role || undefined,
        has_visit:
          queryFilters.linkState === 'linked'
            ? true
            : queryFilters.linkState === 'unlinked'
              ? false
              : undefined,
        date_from: queryFilters.dateRange?.[0]?.format('YYYY-MM-DD'),
        date_to: queryFilters.dateRange?.[1]?.format('YYYY-MM-DD'),
        page,
        page_size: pageSize,
      }),
    placeholderData: (previousData) => previousData,
    staleTime: 30_000,
  })

  const { data: visitsData } = useQuery({
    queryKey: ['visits', 'all'],
    queryFn: () => visitsApi.fetchVisits({ page_size: 100 }),
  })
  const visits = visitsData?.items ?? []

  const { data: staffData } = useQuery({
    queryKey: ['staff', 'all'],
    queryFn: () => adminApi.fetchStaff({ page_size: 100 }),
  })
  const staff = staffData?.items ?? []

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['recordings'] })
    qc.invalidateQueries({ queryKey: ['visits'] })
  }

  const markRecordingLinkSuccess = (recordingId: string) => {
    setRecentlyLinkedRecordingId(recordingId)
    window.setTimeout(() => {
      setRecentlyLinkedRecordingId((current) => (current === recordingId ? null : current))
    }, 2200)
  }

  const closeLinkingSurfaces = (recordingId: string) => {
    setExpandedRowKeys((current) => current.filter((key) => key !== recordingId))
    if (matchingRecording?.id === recordingId) {
      setMatchOpen(false)
      setMatchingRecording(null)
    }
    if (dailyVisitOrdersRecording?.id === recordingId) {
      setDailyVisitOrdersOpen(false)
      setDailyVisitOrdersRecording(null)
      setDailyVisitOrdersKeyword('')
      setDailyVisitOrdersSearchDraft('')
    }
    markRecordingLinkSuccess(recordingId)
  }

  const getExistingLinkedVisitIds = (recordingId: string) => {
    const candidates = [
      matchingRecording,
      dailyVisitOrdersRecording,
      editing,
      ...(data?.items ?? []),
    ].filter(Boolean) as Recording[]
    return candidates.find((recording) => recording.id === recordingId)?.linked_visit_ids ?? []
  }

  const { data: matchData, isLoading: isMatchLoading } = useQuery<RecordingVisitOrderMatch>({
    queryKey: ['recording-visit-order-match', matchingRecording?.id],
    queryFn: () => recordingsApi.fetchRecordingVisitOrderMatch(matchingRecording!.id, true, true),
    enabled: matchOpen && Boolean(matchingRecording?.id),
  })
  const matchDisplay = getDisplayCandidates(matchData?.candidates ?? [], matchData?.manual_review_required ?? false)

  const { data: visitOrderDetail, isLoading: isVisitOrderDetailLoading } = useQuery({
    queryKey: ['visit-order', viewingVisitOrderId],
    queryFn: () => adminApi.fetchVisitOrder(viewingVisitOrderId!),
    enabled: visitOrderDetailOpen && Boolean(viewingVisitOrderId),
  })

  const { data: visitDetail, isLoading: isVisitDetailLoading } = useQuery({
    queryKey: ['visit-detail', viewingVisitId],
    queryFn: () => visitsApi.fetchVisitDetail(viewingVisitId!),
    enabled: visitDetailOpen && Boolean(viewingVisitId),
  })

  const { data: dailyVisitOrdersData, isLoading: isDailyVisitOrdersLoading } = useQuery({
    queryKey: [
      'recording-daily-visit-orders',
      dailyVisitOrdersRecording?.id,
      dailyVisitOrdersMode,
      dailyVisitOrdersMode === 'org' ? dailyVisitOrdersKeyword : '',
    ],
    queryFn: () => recordingsApi.fetchDailyVisitOrdersForRecording(dailyVisitOrdersRecording!.id, {
      scope_mode: dailyVisitOrdersMode,
      keyword: dailyVisitOrdersMode === 'org' ? dailyVisitOrdersKeyword : '',
    }),
    enabled: dailyVisitOrdersOpen && Boolean(dailyVisitOrdersRecording?.id),
  })

  const updateMut = useMutation({
    mutationFn: ({ id, data: payload }: { id: string; data: RecordingUpdatePayload }) =>
      recordingsApi.updateRecording(id, payload),
    onSuccess: async (_result, variables) => {
      if (editMode === 'bind') {
        message.success('关联完成，当前录音的推荐列表已自动收起')
        closeLinkingSurfaces(variables.id)
        await qc.invalidateQueries({ queryKey: ['recording-visit-order-preview', variables.id] })
        await qc.invalidateQueries({ queryKey: ['recording-visit-order-match', variables.id] })
        await qc.invalidateQueries({ queryKey: ['recording-daily-visit-orders', variables.id] })
      } else {
        message.success('录音信息已更新')
      }
      setEditOpen(false)
      setEditing(null)
      invalidate()
    },
    onError: async (error) => {
      message.error(await getApiErrorMessage(error, '保存失败'))
    },
  })

  const deleteMut = useMutation({
    mutationFn: recordingsApi.deleteRecording,
    onSuccess: () => {
      message.success('录音已删除')
      invalidate()
    },
    onError: async (error) => {
      message.error(await getApiErrorMessage(error, '删除失败'))
    },
  })

  const adoptMatchMut = useMutation({
    mutationFn: ({ recordingId, visitId, linkedVisitIds }: { recordingId: string; visitId: string; linkedVisitIds?: string[] }) =>
      recordingsApi.updateRecording(recordingId, { visit_id: visitId, linked_visit_ids: linkedVisitIds ?? [visitId] }),
    onSuccess: async (_result, variables) => {
      message.success('关联完成，推荐列表已自动收起')
      closeLinkingSurfaces(variables.recordingId)
      await qc.invalidateQueries({ queryKey: ['recordings'] })
      await qc.invalidateQueries({ queryKey: ['visits'] })
      await qc.invalidateQueries({ queryKey: ['recording-visit-order-preview', variables.recordingId] })
      await qc.invalidateQueries({ queryKey: ['recording-visit-order-match', variables.recordingId] })
      await qc.invalidateQueries({ queryKey: ['recording-daily-visit-orders', variables.recordingId] })
    },
    onError: async (error) => {
      message.error(await getApiErrorMessage(error, '采用推荐失败'))
    },
  })

  const handleUpload = async () => {
    const values = await uploadForm.validateFields()
    const file = extractNativeFile(values.file)
    if (!file) {
      message.error('请选择有效的音频文件')
      return
    }

    setUploading(true)
    try {
      await recordingsApi.uploadRecording(file, {
        visit_id: values.visit_id || undefined,
        staff_id: values.staff_id || undefined,
        device_id: values.device_id || undefined,
      })
      message.success('录音已上传，请到详情页手动触发转写')
      setUploadOpen(false)
      uploadForm.resetFields()
      invalidate()
    } catch (error) {
      message.error(await getApiErrorMessage(error, '上传失败'))
    } finally {
      setUploading(false)
    }
  }

  const openEdit = (recording: Recording, mode: 'edit' | 'bind') => {
    setEditMode(mode)
    setEditing(recording)
    editForm.setFieldsValue({
      visit_id: recording.visit_id,
      linked_visit_ids: recording.linked_visit_ids,
      staff_id: recording.staff_id,
      device_id: recording.device_id,
    })
    setEditOpen(true)
  }

  const openMatchModal = (recording: Recording) => {
    setMatchingRecording(recording)
    setMatchOpen(true)
  }

  const openVisitOrderDetail = (visitOrderId: string) => {
    setViewingVisitOrderId(visitOrderId)
    setVisitOrderDetailOpen(true)
  }

  const openVisitDetail = (visitId: string) => {
    setViewingVisitId(visitId)
    setVisitDetailOpen(true)
  }

  const openDailyVisitOrders = (recording: Recording, mode: DailyVisitOrdersMode = 'self') => {
    setDailyVisitOrdersRecording(recording)
    setDailyVisitOrdersMode(mode)
    setDailyVisitOrdersKeyword('')
    setDailyVisitOrdersSearchDraft('')
    setDailyVisitOrdersOpen(true)
  }

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
      ...getExistingLinkedVisitIds(recordingId),
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

  const togglePreview = (recordingId: string) => {
    setExpandedRowKeys((current) => (
      current.includes(recordingId)
        ? current.filter((key) => key !== recordingId)
        : [...current, recordingId]
    ))
  }

  const handleEdit = async () => {
    if (!editing) return
    const values = await editForm.validateFields()
    const payload: RecordingUpdatePayload = editMode === 'bind'
      ? buildRecordingLinkPayload(values)
      : {
          ...buildRecordingLinkPayload(values),
          staff_id: values.staff_id || undefined,
          device_id: values.device_id || undefined,
        }
    const linkedVisitIds = payload.linked_visit_ids ?? []
    const existingLinkedVisitIds = buildLinkedVisitIds(editing.visit_id || '', editing.linked_visit_ids)
    const nextLinkedVisitIds = buildLinkedVisitIds(payload.visit_id || '', linkedVisitIds)
    const linkageChanged = (payload.visit_id ?? null) !== (editing.visit_id ?? null)
      || existingLinkedVisitIds.join('|') !== nextLinkedVisitIds.join('|')
    if (linkageChanged && linkedVisitIds.length > 1) {
      const confirmed = await confirmRecordingVisitLinkRisk({ nextLinkedVisitIds: linkedVisitIds })
      if (!confirmed) return
    }
    updateMut.mutate({ id: editing.id, data: payload })
  }

  const applyFilters = () => {
    setPage(1)
    setQueryFilters({ ...draftFilters })
  }

  const resetFilters = () => {
    const next = createDefaultFilters()
    setDraftFilters(next)
    setQueryFilters(next)
    setPage(1)
  }

  const handleQuickRange = (range: QuickRange) => {
    const next: RecordingFilters = {
      ...draftFilters,
      quickRange: range,
      dateRange: resolveQuickRange(range),
    }
    setDraftFilters(next)
    setQueryFilters(next)
    setPage(1)
  }

  const handleExport = () => {
    const rows = data?.items ?? []
    if (!rows.length) {
      message.warning('当前页没有可导出的录音')
      return
    }
    exportCurrentRows(rows)
    message.success('当前页录音已导出')
  }

  const handleBatchDelete = async () => {
    if (!selectedRowKeys.length) {
      message.warning('请先选择要删除的录音')
      return
    }

    Modal.confirm({
        title: '批量删除录音',
        content: `确定删除已选中的 ${selectedRowKeys.length} 条录音吗？`,
        okText: '删除',
        okButtonProps: { danger: true },
        cancelText: '取消',
        onOk: async () => {
          const results = await Promise.allSettled(
            selectedRowKeys.map((id) => deleteMut.mutateAsync(String(id))),
          )
        const successCount = results.filter((item) => item.status === 'fulfilled').length
        const failedCount = results.length - successCount
        setSelectedRowKeys([])
        invalidate()
        if (failedCount === 0) {
          message.success(`已删除 ${successCount} 条录音`)
        } else {
          message.warning(`删除完成，成功 ${successCount} 条，失败 ${failedCount} 条`)
        }
      },
    })
  }

  const currentDailyVisitOrderRefSet = new Set(
    (dailyVisitOrdersRecording?.linked_visits ?? []).map((visit) =>
      `${visit.external_visit_order_no ?? ''}::${visit.external_visit_order_seg ?? ''}`,
    ),
  )
  const dailyVisitOrdersScopeLabel = dailyVisitOrdersMode === 'org' ? '所有人当天全部到诊单' : '自己当天全部到诊单'

  return (
    <div className="operation-page recordings-page">
      <div className="operation-page__header">
        <div className="operation-page__title">
          <span className="operation-page__marker" aria-hidden="true" />
          <div>
            <h1>录音列表</h1>
            <p>先筛选录音，再进入音频、逐字稿和到诊单匹配；这里是所有录音内容的总入口。</p>
          </div>
        </div>
      </div>
      <div className="recordings-page__hub-links">
        <Link className="recordings-page__hub-link" to="/admin/transcripts">
          <strong>对话逐字稿</strong>
          <span>按录音查看全文转写，适合先校对文本再进入匹配判断。</span>
        </Link>
      </div>

      <div className="recordings-page__quick-range">
        {QUICK_RANGE_OPTIONS.map((item) => (
          <button
            key={item.key}
            type="button"
            className={draftFilters.quickRange === item.key ? 'is-active' : ''}
            onClick={() => handleQuickRange(item.key)}
          >
            {item.label}
          </button>
        ))}

        <RangePicker
          value={draftFilters.dateRange}
          onChange={(value) =>
            setDraftFilters((current) => ({
              ...current,
              quickRange: 'custom',
              dateRange: value,
            }))
          }
        />
      </div>

      <div className="operation-card recordings-filters">
        <div className="recordings-filters__topbar">
          <Input
            allowClear
            className="recordings-filters__search"
            placeholder="搜索录音名称 / 录音ID / 设备ID"
            prefix={<SearchOutlined />}
            value={draftFilters.keyword}
            onChange={(event) =>
              setDraftFilters((current) => ({
                ...current,
                keyword: event.target.value,
              }))
            }
            onPressEnter={applyFilters}
          />

          <div className="recordings-filters__actions">
            <Button icon={<UploadOutlined />} onClick={() => setUploadOpen(true)}>
              上传录音
            </Button>
            <Button danger icon={<DeleteOutlined />} onClick={() => void handleBatchDelete()}>
              批量删除
            </Button>
            <Button icon={<ExportOutlined />} onClick={handleExport}>
              导出
            </Button>
          </div>
        </div>

        <div className="recordings-filters__grid">
          <Select
            value={draftFilters.linkState}
            options={LINK_OPTIONS}
            onChange={(value) => setDraftFilters((current) => ({ ...current, linkState: value }))}
          />
          <Select
            allowClear
            showSearch
            optionFilterProp="label"
            placeholder="所属人员"
            options={staff.map((person) => ({
              label: formatStaffDisplayLabel(person),
              value: person.id,
            }))}
            value={draftFilters.staff_id}
            onChange={(value) => setDraftFilters((current) => ({ ...current, staff_id: value }))}
          />
          <Input
            allowClear
            placeholder="客户姓名"
            value={draftFilters.customer_keyword}
            onChange={(event) =>
              setDraftFilters((current) => ({
                ...current,
                customer_keyword: event.target.value,
              }))
            }
            onPressEnter={applyFilters}
          />
        </div>

        {showAdvanced && (
          <div className="recordings-filters__grid">
            <Select
              allowClear
              placeholder="接待角色"
              options={ROLE_OPTIONS}
              value={draftFilters.role}
              onChange={(value) => setDraftFilters((current) => ({ ...current, role: value }))}
            />
            <Input
              allowClear
              placeholder="设备工牌号"
              value={draftFilters.badge_id}
              onChange={(event) =>
                setDraftFilters((current) => ({
                  ...current,
                  badge_id: event.target.value,
                }))
              }
              onPressEnter={applyFilters}
            />
            <Input
              allowClear
              placeholder="接诊单ID"
              value={draftFilters.visit_id}
              onChange={(event) =>
                setDraftFilters((current) => ({
                  ...current,
                  visit_id: event.target.value,
                }))
              }
              onPressEnter={applyFilters}
            />
          </div>
        )}

        <div className="recordings-filters__footer">
          <Button onClick={() => setShowAdvanced((current) => !current)}>
            {showAdvanced ? '收起' : '更多'}
          </Button>
          <Space>
            <Button type="primary" onClick={applyFilters}>
              查询
            </Button>
            <Button onClick={resetFilters}>重置</Button>
          </Space>
        </div>
      </div>

      <div className="operation-card recordings-table-card">
        <Table
          rowKey="id"
          loading={isLoading}
          dataSource={data?.items ?? []}
          className="recordings-table"
          tableLayout="fixed"
          expandable={{
            expandedRowKeys,
            showExpandColumn: false,
            onExpandedRowsChange: (keys) => setExpandedRowKeys([...keys]),
            expandedRowRender: (recording: Recording) => (
              <RecordingMatchPreview
                recording={recording}
                expanded={expandedRowKeys.includes(recording.id)}
                onOpenVisitOrderDetail={openVisitOrderDetail}
                onOpenFullMatchModal={openMatchModal}
                onOpenDailyVisitOrders={openDailyVisitOrders}
                onAdoptRecommendation={(payload) =>
                  void handleRecordingVisitLink(payload)
                }
                adoptPending={adoptMatchMut.isPending}
              />
            ),
          }}
          rowClassName={(record, index) => {
            const classes = [index % 2 === 0 ? 'recordings-table__row' : 'recordings-table__row is-alt']
            if (record.id === recentlyLinkedRecordingId) {
              classes.push('is-link-success')
            }
            return classes.join(' ')
          }}
          rowSelection={{
            selectedRowKeys,
            onChange: setSelectedRowKeys,
          }}
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
          scroll={{ x: 1020 }}
          size="small"
          columns={[
            {
              title: '录音名称',
              dataIndex: 'file_name',
              width: 292,
              render: (_value: string, row: Recording) => (
                <div className="recording-file-cell">
                  <div className="recording-file-cell__cover">
                    <AudioOutlined />
                  </div>
                  <div className="recording-file-cell__body">
                    {(() => {
                      const linkedSummary = getRecordingLinkedVisitSummary(row)
                      return (
                        <>
                    <button
                      type="button"
                      className="recording-file-cell__name"
                      title={formatRecordingDisplayName(row.file_name, row.created_at)}
                      onClick={() => navigate(`/admin/recordings/${row.id}`)}
                    >
                      {formatRecordingDisplayName(row.file_name, row.created_at)}
                    </button>
                    <div className="recording-file-cell__meta">
                      <span>{formatDuration(row.duration_seconds)}</span>
                      <span>{formatFileSize(row.file_size)}</span>
                      {linkedSummary.primaryVisit ? (
                        <Button
                          type="link"
                          size="small"
                          className={`recording-file-cell__visit-link ${row.linked_visits.length ? 'is-linked' : 'is-unlinked'}`}
                          style={{ padding: 0, height: 'auto' }}
                          onClick={() => openVisitDetail(linkedSummary.primaryVisit!.id)}
                        >
                          {linkedSummary.label}
                        </Button>
                      ) : (
                        <span className={row.linked_visits.length ? 'is-linked' : 'is-unlinked'}>
                          {linkedSummary.label}
                        </span>
                      )}
                      {linkedSummary.detail && <span>{linkedSummary.detail}</span>}
                      {!row.linked_visits.length && <Tag color="gold">可智能匹配</Tag>}
                    </div>
                    {linkedSummary.secondaryVisits.length > 0 && (
                      <div className="recording-file-cell__secondary-visits">
                        {linkedSummary.secondaryVisits.map((visit) => (
                          <Button
                            key={`${row.id}-secondary-visit-${visit.id}`}
                            type="link"
                            size="small"
                            className="recording-file-cell__secondary-link"
                            style={{ justifyContent: 'flex-start', padding: 0, height: 'auto', fontSize: 12 }}
                            onClick={() => openVisitDetail(visit.id)}
                          >
                            辅接诊单 {formatVisitRef(visit)}
                          </Button>
                        ))}
                      </div>
                    )}
                        </>
                      )
                    })()}
                  </div>
                </div>
              ),
            },
            {
              title: '设备工牌号',
              width: 118,
              render: (_value: unknown, row: Recording) => {
                const badge = getRecordingDeviceBadge(row)
                return (
                  <div className={`recording-device-cell${badge ? '' : ' is-empty'}`}>
                    <strong className="recording-device-cell__code">{badge || '未绑定'}</strong>
                    <span className="recording-device-cell__sub">{row.device_id ? `设备 ID ${row.device_id}` : '暂无设备工牌信息'}</span>
                  </div>
                )
              },
            },
            {
              title: '录音归属',
              width: 154,
              render: (_value: unknown, row: Recording) => (
                <div className="recording-owner-cell">
                  <Avatar size={28}>{(row.staff_name || '录').slice(0, 1)}</Avatar>
                  <div>
                    <strong>{row.staff_name || '未分配'}</strong>
                    <span>{row.staff_role ? STAFF_ROLE_MAP[row.staff_role] || row.staff_role : '待绑定人员'}</span>
                  </div>
                </div>
              ),
            },
            {
              title: '创建时间',
              dataIndex: 'created_at',
              width: 136,
              render: (value: string) => (
                value ? (
                  <div className="recording-time-cell">
                    <strong className="recording-time-cell__date">{formatBeijingTime(value, 'YYYY-MM-DD')}</strong>
                    <span className="recording-time-cell__time">{formatBeijingTime(value, 'HH:mm:ss')}</span>
                  </div>
                ) : (
                  <span className="recording-time-cell__empty">-</span>
                )
              ),
            },
            {
              title: '操作',
              width: 214,
              fixed: 'right',
              render: (_value: unknown, row: Recording) => (
                <Space wrap className="recordings-actions">
                  <Button type="primary" size="small" onClick={() => openEdit(row, 'bind')}>
                    {row.linked_visits.length ? '调整绑定' : '绑定接诊单'}
                  </Button>
                  <Button size="small" onClick={() => togglePreview(row.id)}>
                    {expandedRowKeys.includes(row.id) ? '收起推荐' : '展开推荐'}
                  </Button>
                  <Button size="small" onClick={() => openMatchModal(row)}>
                    完整建议
                  </Button>
                  <Button
                    size="small"
                    icon={<EyeOutlined />}
                    onClick={() => navigate(`/admin/recordings/${row.id}`)}
                  >
                    详情
                  </Button>
                </Space>
              ),
            },
          ]}
        />
      </div>

      <Modal
        title="上传录音"
        open={uploadOpen}
        onOk={() => void handleUpload()}
        onCancel={() => setUploadOpen(false)}
        confirmLoading={uploading}
        destroyOnClose
        width={520}
      >
        <Form form={uploadForm} layout="vertical" preserve={false}>
          <Form.Item
            name="file"
            label="音频文件"
            rules={[{ required: true, message: '请选择音频文件' }]}
            getValueFromEvent={(event) => event}
          >
            <Upload beforeUpload={() => false} maxCount={1} accept=".wav,.mp3,.m4a,.ogg,.flac,.webm,.amr">
              <Button icon={<UploadOutlined />}>选择文件</Button>
            </Upload>
          </Form.Item>

          <Form.Item name="visit_id" label="关联接诊单">
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="可选"
              options={visits.map((visit) => ({
                label: `${visit.customer_name} · ${formatBeijingTime(visit.created_at, 'MM/DD HH:mm')}`,
                value: visit.id,
              }))}
            />
          </Form.Item>

          <Form.Item name="staff_id" label="所属人员">
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="可选"
              options={staff.map((person) => ({
                label: formatStaffDisplayLabel(person),
                value: person.id,
              }))}
            />
          </Form.Item>

          <Form.Item name="device_id" label="设备ID">
            <Input placeholder="可选" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={editMode === 'bind' ? '绑定接诊单' : '编辑录音'}
        open={editOpen}
        onOk={() => void handleEdit()}
        onCancel={() => {
          setEditOpen(false)
          setEditing(null)
        }}
        confirmLoading={updateMut.isPending}
        destroyOnClose
        width={520}
      >
        <Form form={editForm} layout="vertical" preserve={false}>
          <Form.Item name="visit_id" label="主接诊单">
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="请选择主接诊单"
              options={visits.map((visit) => ({
                label: `${visit.customer_name} · ${visit.id} · ${formatBeijingTime(visit.created_at, 'MM/DD HH:mm')}`,
                value: visit.id,
              }))}
            />
          </Form.Item>

          <Form.Item name="linked_visit_ids" label="关联接诊单集合">
            <Select
              mode="multiple"
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="可添加辅接诊单；系统会自动保留主接诊单"
              options={visits.map((visit) => ({
                label: `${visit.customer_name} · ${visit.id} · ${formatBeijingTime(visit.created_at, 'MM/DD HH:mm')}`,
                value: visit.id,
              }))}
            />
          </Form.Item>

          {editMode === 'edit' && (
            <>
              <Form.Item name="staff_id" label="所属人员">
                <Select
                  allowClear
                  showSearch
                  optionFilterProp="label"
                  placeholder="可选"
                  options={staff.map((person) => ({
                    label: formatStaffDisplayLabel(person),
                    value: person.id,
                  }))}
                />
              </Form.Item>

              <Form.Item name="device_id" label="设备ID">
                <Input />
              </Form.Item>
            </>
          )}
        </Form>
      </Modal>

      <Modal
        title={
          matchingRecording
            ? `录音匹配建议 · ${formatRecordingDisplayName(matchingRecording.file_name, matchingRecording.created_at)}`
            : '录音匹配建议'
        }
        open={matchOpen}
        onCancel={() => {
          setMatchOpen(false)
          setMatchingRecording(null)
        }}
        footer={null}
        destroyOnClose
        width={980}
      >
        <Descriptions size="small" bordered column={2} style={{ marginBottom: 16 }}>
          <Descriptions.Item label="分析说明" span={2}>
            {isMatchLoading ? '正在分析匹配建议...' : matchData?.summary || '暂无分析结果'}
          </Descriptions.Item>
          <Descriptions.Item label="录音日期">{matchData?.record_date || '-'}</Descriptions.Item>
          <Descriptions.Item label="顾问编号">{matchData?.advisor_code || '-'}</Descriptions.Item>
          <Descriptions.Item label="客户编码">{matchData?.customer_code || '-'}</Descriptions.Item>
          <Descriptions.Item label="当前关联">
            {renderLinkedVisitOrderSummary(
              matchData?.linked_visit_order_refs ?? [],
              matchData?.linked_visit_order_no,
              matchData?.linked_visit_order_seg,
            )}
          </Descriptions.Item>
          <Descriptions.Item label="最高推荐置信度" span={2}>
            {matchDisplay.items.length ? getConfidenceTag(matchDisplay.items[0].confidence) : '-'}
          </Descriptions.Item>
        </Descriptions>

        {matchData?.manual_review_required && (
          <div style={{ marginBottom: 12 }}>
            {renderConflictBlock(matchData.identity_conflicts, matchData.manual_review_reason)}
          </div>
        )}

        {matchData?.auto_applied && (
          <div style={{ marginBottom: 12 }}>
            <Tag color="success">系统已按高置信度结果自动关联</Tag>
          </div>
        )}

        {matchDisplay.hiddenCount > 0 && (
          <div style={{ marginBottom: 12 }}>
            <Tag>已隐藏 {matchDisplay.hiddenCount} 条低置信度或明显落后于 Top1 的候选</Tag>
          </div>
        )}

        {matchingRecording && (
          <div style={{ marginBottom: 12, display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'center' }}>
            <span style={{ color: '#666' }}>
              如果真实匹配单没有进入推荐前列，可以切到当天全量到诊单继续核对。
            </span>
            <Space wrap>
              <Button onClick={() => openDailyVisitOrders(matchingRecording, 'self')}>
                查看自己当天全部到诊单
              </Button>
              <Button onClick={() => openDailyVisitOrders(matchingRecording, 'org')}>
                查看所有人当天全部到诊单
              </Button>
            </Space>
          </div>
        )}

        <Table<VisitOrderMatchCandidate>
          rowKey="visit_order_id"
          loading={isMatchLoading}
          dataSource={matchDisplay.items}
          pagination={false}
          size="small"
          scroll={{ x: 780 }}
          columns={[
            {
              title: '候选到诊单',
              width: 210,
              render: (_value, row) => (
                <div style={{ display: 'grid', gap: 4 }}>
                  <strong>{formatMergedVisitOrderTitle(row.dzdh, row.dzseg, row.merged_line_items?.length ?? 0)}</strong>
                  {row.merged_segments?.length > 1 && (
                    <Tag color="blue" style={{ width: 'fit-content' }}>已合并 {row.merged_segments.length} 条分诊明细</Tag>
                  )}
                  <span>{row.customer_name || '-'} / {row.customer_code || '-'}</span>
                  <span>{row.visit_date || '-'}</span>
                  <span style={{ color: '#666', fontSize: 12 }}>
                    {row.triage_time ? `分诊 ${fmtClock(row.triage_time)}` : ''}
                    {!row.triage_time ? '分诊时间未知' : ''}
                  </span>
                  {row.linked_recording_names?.length > 0 && (
                    <Tag color="orange" style={{ width: 'fit-content' }}>已关联录音：{row.linked_recording_names.join('、')}</Tag>
                  )}
                  {row.companion_visit_order_refs.length > 0 && (
                    <span>同行辅单：{row.companion_visit_order_refs.join(' / ')}</span>
                  )}
                  {renderMergedLineItemBlock(row)}
                </div>
              ),
            },
            {
              title: '判定',
              width: 148,
              render: (_value, row) => (
                <div style={{ display: 'grid', gap: 6 }}>
                  {getMatchTag(row.decision)}
                  {getConfidenceTag(row.confidence)}
                  {row.manual_review_required && getManualReviewTag(row.manual_review_reason)}
                  <span>{getMethodLabel(row.method)}</span>
                </div>
              ),
            },
            {
              title: '推荐置信度',
              dataIndex: 'confidence',
              width: 92,
              render: (value: number) => `${Math.round(value * 100)}%`,
            },
            {
              title: '关键证据',
              width: 280,
              render: (_value, row) => (
                <div style={{ display: 'grid', gap: 8 }}>
                  {row.manual_review_required && renderConflictBlock(row.identity_conflicts, row.manual_review_reason)}
                  <div>{renderEvidenceBlock(row)}</div>
                </div>
              ),
            },
            {
              title: '关联情况',
              width: 136,
              render: (_value, row) => (
                <div style={{ display: 'grid', gap: 4 }}>
                  {row.linked_recording_count
                    ? <span>已有 {row.linked_recording_count} 条录音</span>
                    : <span style={{ color: '#999' }}>暂无录音</span>}
                  {row.linked_recording_names?.length > 0 && (
                    <span style={{ color: '#fa8c16', fontSize: 12 }}>{row.linked_recording_names.join('、')}</span>
                  )}
                </div>
              ),
            },
            {
              title: '操作',
              width: 176,
              render: (_value, row) => (
                <Space wrap>
                  <Button size="small" onClick={() => openVisitOrderDetail(row.visit_order_id)}>
                    查看详情
                  </Button>
                  <Button
                    type="primary"
                    size="small"
                    disabled={!matchingRecording || !row.local_visit_id}
                    loading={adoptMatchMut.isPending}
                    onClick={() => {
                      if (!matchingRecording || !row.local_visit_id) return
                      void handleRecordingVisitLink({
                        recordingId: matchingRecording.id,
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
          locale={{ emptyText: '没有可展示的候选到诊单' }}
        />
      </Modal>

      <Modal
        title={
          dailyVisitOrdersRecording
            ? `${dailyVisitOrdersScopeLabel} · ${formatRecordingDisplayName(dailyVisitOrdersRecording.file_name, dailyVisitOrdersRecording.created_at)}`
            : '当天到诊单'
        }
        open={dailyVisitOrdersOpen}
        onCancel={() => {
          setDailyVisitOrdersOpen(false)
          setDailyVisitOrdersRecording(null)
          setDailyVisitOrdersKeyword('')
          setDailyVisitOrdersSearchDraft('')
        }}
        footer={
          <Space>
            {dailyVisitOrdersRecording && (
              <Button
                type="primary"
                onClick={() => {
                  const recording = dailyVisitOrdersRecording
                  setDailyVisitOrdersOpen(false)
                  setDailyVisitOrdersRecording(null)
                  setDailyVisitOrdersKeyword('')
                  setDailyVisitOrdersSearchDraft('')
                  openEdit(recording, 'bind')
                }}
              >
                去手动绑定当前录音
              </Button>
            )}
            <Button
              onClick={() => {
                setDailyVisitOrdersOpen(false)
                setDailyVisitOrdersRecording(null)
                setDailyVisitOrdersKeyword('')
                setDailyVisitOrdersSearchDraft('')
              }}
            >
              关闭
            </Button>
          </Space>
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
          {dailyVisitOrdersMode === 'org' && (
            <Input.Search
              allowClear
              enterButton="搜索"
              onChange={(event) => setDailyVisitOrdersSearchDraft(event.target.value)}
              onSearch={(value) => setDailyVisitOrdersKeyword(value.trim())}
              placeholder="搜索客户、到诊单号、咨询师、备注"
              style={{ minWidth: 280 }}
              value={dailyVisitOrdersSearchDraft}
            />
          )}
        </Space>
        <Descriptions size="small" bordered column={2} style={{ marginBottom: 16 }}>
          <Descriptions.Item label="录音日期">
            {dailyVisitOrdersData?.recording_date || '-'}
          </Descriptions.Item>
          <Descriptions.Item label="查看范围">
            {dailyVisitOrdersScopeLabel}
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

        <Table<recordingsApi.DailyVisitOrderItem>
          rowKey="id"
          loading={isDailyVisitOrdersLoading}
          dataSource={dailyVisitOrdersData?.items ?? []}
          pagination={false}
          size="small"
          scroll={{ x: 960, y: 440 }}
          columns={[
            {
              title: '到诊单',
              width: 168,
              render: (_value, row) => {
                const isCurrentLinked = currentDailyVisitOrderRefSet.has(`${row.dzdh}::${row.dzseg ?? ''}`)
                return (
                  <div style={{ display: 'grid', gap: 6 }}>
                    <strong>{row.dzdh}{row.dzseg ? `-${row.dzseg}` : ''}</strong>
                    {isCurrentLinked && <Tag color="success" style={{ width: 'fit-content' }}>当前录音已关联</Tag>}
                    <span style={{ color: '#666', fontSize: 12 }}>{row.sjrq || dailyVisitOrdersData?.recording_date || '-'}</span>
                  </div>
                )
              },
            },
            {
              title: '客户',
              width: 188,
              render: (_value, row) => (
                <div style={{ display: 'grid', gap: 4 }}>
                  <strong>{row.ninam || '-'}</strong>
                  <span>{row.kunr || '-'}</span>
                  <span>{row.jcsta_txt || '状态未知'}</span>
                </div>
              ),
            },
            {
              title: '时间',
              width: 150,
              render: (_value, row) => (
                <div style={{ display: 'grid', gap: 4 }}>
                  <span>{row.fzsj ? `分诊 ${fmtClock(row.fzsj)}` : '分诊时间未知'}</span>
                  <span>{row.jcsta_txt || '业务状态未知'}</span>
                </div>
              ),
            },
            {
              title: '顾问 / 备注',
              width: 216,
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
              width: 196,
              render: (_value, row) => (
                <div style={{ display: 'grid', gap: 6 }}>
                  <span>{row.jcsta_txt || '成交状态未知'}</span>
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
              width: 114,
              render: (_value, row) => (
                <Button size="small" onClick={() => openVisitOrderDetail(row.id)}>
                  查看详情
                </Button>
              ),
            },
          ]}
          locale={{ emptyText: '当天没有可展示的到诊单' }}
        />
      </Modal>

      <Modal
        title={visitOrderDetail ? `到诊单详情 · ${visitOrderDetail.dzdh}${visitOrderDetail.dzseg ? `-${visitOrderDetail.dzseg}` : ''}` : '到诊单详情'}
        open={visitOrderDetailOpen}
        onCancel={() => {
          setVisitOrderDetailOpen(false)
          setViewingVisitOrderId(null)
        }}
        footer={null}
        destroyOnClose
        width={960}
      >
        {isVisitOrderDetailLoading ? (
          <div style={{ padding: '24px 0', color: '#666' }}>正在加载到诊单详情...</div>
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

      <Modal
        title={visitDetail ? `接诊单详情 · ${visitDetail.customer_name || visitDetail.id}` : '接诊单详情'}
        open={visitDetailOpen}
        onCancel={() => {
          setVisitDetailOpen(false)
          setViewingVisitId(null)
        }}
        footer={null}
        destroyOnClose
        width={960}
      >
        {isVisitDetailLoading ? (
          <div style={{ padding: '24px 0', color: '#666' }}>正在加载接诊单详情...</div>
        ) : (
          <Descriptions size="small" bordered column={2}>
            <Descriptions.Item label="接诊单ID">{visitDetail?.id || '-'}</Descriptions.Item>
            <Descriptions.Item label="客户姓名">{visitDetail?.customer_name || '-'}</Descriptions.Item>
            <Descriptions.Item label="客户编码">{visitDetail?.customer_code || '-'}</Descriptions.Item>
            <Descriptions.Item label="咨询师">{visitDetail?.consultant_name || '-'}</Descriptions.Item>
            <Descriptions.Item label="医生">{visitDetail?.doctor_name || '-'}</Descriptions.Item>
            <Descriptions.Item label="状态">{visitDetail?.status || '-'}</Descriptions.Item>
            <Descriptions.Item label="到诊日期">{visitDetail?.visit_date || '-'}</Descriptions.Item>
            <Descriptions.Item label="关联录音数">{visitDetail?.recording_count ?? '-'}</Descriptions.Item>
            <Descriptions.Item label="最新录音ID">{visitDetail?.latest_recording_id || '-'}</Descriptions.Item>
            <Descriptions.Item label="创建时间">{visitDetail?.created_at ? formatBeijingTime(visitDetail.created_at, 'YYYY-MM-DD HH:mm:ss') : '-'}</Descriptions.Item>
            <Descriptions.Item label="客户性别/年龄">
              {visitDetail?.customer_gender || '-'}{visitDetail?.customer_age != null ? ` / ${visitDetail.customer_age}岁` : ''}
            </Descriptions.Item>
            <Descriptions.Item label="客户企业微信ID">{visitDetail?.customer_wechat_external_uid || '-'}</Descriptions.Item>
            <Descriptions.Item label="备注" span={2}>{visitDetail?.notes || '-'}</Descriptions.Item>
          </Descriptions>
        )}
      </Modal>
    </div>
  )
}

export default RecordingsPage
