import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CalendarOutlined, ClockCircleOutlined, DownOutlined, PauseCircleFilled, PlayCircleFilled, RightOutlined, ScissorOutlined, SearchOutlined, UserOutlined } from '@ant-design/icons'
import { Modal } from 'antd'
import { HTTPError } from 'ky'
import { Link, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'

import { fetchAnalysisDetail, type AnalysisDetail } from '@/api/analysis'
import {
  ensureArchiveRecording,
  fetchArchiveRecordingDetail,
  fetchArchiveRecordingMediaSource,
  type ArchiveRecordingDetail,
} from '@/api/archive-recordings'
import {
  confirmRecordingMultiCustomerReview,
  ensureRecordingVisitOrderLocalVisit,
  fetchDailyVisitOrdersForRecording,
  fetchRecording,
  fetchRecordingAnalysis,
  fetchRecordingMediaSource,
  fetchRecordingMultiCustomerReview,
  fetchRecordingVisitOrderMatch,
  resetRecordingMultiCustomerReview,
  splitRecording,
  updateRecording,
  type RecordingSplitResult,
  type VisitOrderMatchCandidate,
} from '@/api/recordings'
import { AnalysisDetailContent } from '@/components/analysis-detail-content'
import { SPEAKER_MAP } from '@/api/segments'
import { fetchTranscripts, type Transcript, type TranscriptUtterance } from '@/api/transcripts'
import { buildArchiveAnalysisDetail } from '@/pages/admin/dingtalk-audio-analysis-utils'
import {
  buildCompanionVisitPromptMessage,
  buildLinkedVisitIds,
  hasCompanionVisitOptions,
} from '@/utils/companion-visit-linking'
import { getDisplayMatchEvidenceLines } from '@/utils/match-evidence'
import { buildRecordingVisitLinkRiskText } from '@/utils/recording-visit-link-confirmation'
import { getQuickRecommendSelection } from '@/utils/visit-order-recommendations'
import { keepElementInScrollContainerView } from '@/utils/scroll'
import {
  buildVisitOrderLineItemMeta,
  formatMergedVisitOrderTitle,
  formatVisitOrderLineItemRef,
} from '@/utils/visit-order-line-items'
import { formatRecordingDisplayName } from '@/utils/recording-display'
import { isHospitalAdminOrAbove } from '@/app/roles'
import { useAuth } from '@/app/use-auth'
import { formatBeijingTime } from '@/utils/time'

function formatDateTime(value: string | null) {
  if (!value) return '-'
  return formatBeijingTime(value, 'YYYY-MM-DD HH:mm')
}

function formatDuration(seconds: number | null) {
  if (seconds == null) return '-'
  const mins = Math.floor(seconds / 60)
  const secs = Math.floor(seconds % 60)
  return `${mins}:${String(secs).padStart(2, '0')}`
}

function resolveArchiveDurationSeconds(recording: ArchiveRecordingDetail | null | undefined) {
  if (!recording) return null
  if (recording.duration_seconds != null) return recording.duration_seconds
  if (recording.duration_ms != null) return Math.max(1, Math.round(recording.duration_ms / 1000))
  return null
}

function resolveArchiveCreatedAt(recording: ArchiveRecordingDetail | null | undefined) {
  if (!recording) return null
  return recording.create_time || recording.downloaded_at || recording.updated_at || null
}

function resolveArchiveUtterances(recording: ArchiveRecordingDetail | null | undefined): TranscriptUtterance[] {
  const utterances = recording?.transcript && typeof recording.transcript === 'object'
    ? (recording.transcript as { utterances?: unknown }).utterances
    : null
  return Array.isArray(utterances) ? (utterances as TranscriptUtterance[]) : []
}

function formatMs(ms: number) {
  const totalSeconds = Math.floor(ms / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${minutes}:${seconds.toString().padStart(2, '0')}`
}

function formatAudioTime(seconds: number | null) {
  if (seconds == null || Number.isNaN(seconds)) return '0:00'
  const totalSeconds = Math.max(0, Math.floor(seconds))
  const minutes = Math.floor(totalSeconds / 60)
  const remainSeconds = totalSeconds % 60
  return `${minutes}:${String(remainSeconds).padStart(2, '0')}`
}

function createDefaultAudioPlayerState(sourceKey: string, durationSeconds: number | null) {
  return {
    sourceKey,
    playbackMs: null as number | null,
    durationSeconds,
    currentTimeSeconds: 0,
    ready: false,
    playing: false,
    error: false,
  }
}

function fmtClock(value: string | null | undefined) {
  if (!value || value.length < 4) return value || ''
  const normalized = value.replace(/[^0-9]/g, '')
  if (normalized.length < 4) return value
  const padded = normalized.padStart(6, '0')
  return `${padded.slice(0, 2)}:${padded.slice(2, 4)}:${padded.slice(4, 6)}`
}

function formatVisitRef(orderNo: string | null | undefined, orderSeg: string | null | undefined) {
  const normalizedOrderNo = String(orderNo || '').trim()
  const normalizedOrderSeg = String(orderSeg || '').trim()
  if (!normalizedOrderNo) return '已关联接诊'
  return normalizedOrderSeg ? `${normalizedOrderNo}-${normalizedOrderSeg}` : normalizedOrderNo
}

function getVisitAnalysisStatusLabel(status: string) {
  if (status === 'done') return '分析完成'
  if (status === 'running') return '分析中'
  if (status === 'pending') return '待分析'
  if (status === 'failed') return '分析失败'
  return '待确认'
}

function normalizeRecordingSummaryPart(value: string | null | undefined) {
  return String(value ?? '')
    .replace(/\s+/g, ' ')
    .replace(/；+/g, '；')
    .trim()
}

function buildRecordingRecallSummary(detail: AnalysisDetail | null | undefined) {
  if (!detail) return null

  const candidateLines = [
    {
      label: '主诉',
      value: normalizeRecordingSummaryPart(
        detail.consultation_result?.chief_complaint_and_indications?.summary
        || detail.primary_demand_summary,
      ),
    },
    {
      label: '顾虑',
      value: normalizeRecordingSummaryPart(
        detail.customer_concerns?.summary
        || detail.consultation_result?.deal_factors?.summary,
      ),
    },
    {
      label: '方案',
      value: normalizeRecordingSummaryPart(detail.consultation_result?.recommended_plan?.summary),
    },
    {
      label: '结果',
      value: normalizeRecordingSummaryPart(detail.consultation_result?.deal_outcome?.summary),
    },
  ]

  const usedValues = new Set<string>()
  const lines = candidateLines.flatMap((item) => {
    if (!item.value || usedValues.has(item.value)) return []
    usedValues.add(item.value)
    return [`${item.label}：${item.value}`]
  })

  if (lines.length > 0) {
    return lines.join('\n')
  }

  return normalizeRecordingSummaryPart(
    detail.consultation_process_evaluation?.overall_summary
    || detail.consultation_evaluation?.overall_summary
    || detail.overall_summary,
  ) || null
}

function getMatchMethodLabel(method: string) {
  if (method === 'direct_dzdh') return 'DZDH 直连'
  if (method === 'strict_customer_day_advisor') return '客户编码+日期+顾问'
  return null
}

function MatchLineItems({ candidate }: { candidate: VisitOrderMatchCandidate }) {
  if ((candidate.merged_line_items?.length ?? 0) <= 1) return null

  return (
    <div className="wc-match-card__line-items">
      {candidate.merged_line_items.map((item, index) => {
        const metaLines = buildVisitOrderLineItemMeta(item)
        return (
          <div key={`${item.fzdh ?? item.dzseg ?? 'line-item'}-${index}`} className="wc-match-card__line-item">
            <strong>{formatVisitOrderLineItemRef(item)}</strong>
            {metaLines.map((line) => <span key={line}>{line}</span>)}
            {item.note_summary ? <span className="wc-match-card__line-item-note">备注：{item.note_summary}</span> : null}
          </div>
        )
      })}
    </div>
  )
}

type PendingLinkChoice = {
  linkKey: string
  visitId: string
  visitOrderRef: string
  alwaysLinkedVisitIds: string[]
  companionVisitIds: string[]
  companionVisitOrderRefs: string[]
  companionCustomerCodes: string[]
  targetLinkedRecordingNames: string[]
}

type LinkMutationVariables = {
  recId: string
  visitId: string | null
  linkedVisitIds: string[]
  successMessage: string
  successFeedbackMode: 'inline' | 'modal'
}

function resolveRecordingDetailErrorMessage(error: unknown) {
  if (error instanceof HTTPError) {
    if (error.response.status === 403 || error.response.status === 404) {
      return '当前账号暂无权限查看该录音'
    }
    if (error.response.status >= 500) {
      return '服务器处理录音详情时出错，请稍后重试'
    }
  }
  if (error instanceof Error && error.message.trim()) {
    return error.message
  }
  return '请稍后重试'
}

function CollapseToggle({
  expanded,
  label,
  onClick,
}: {
  expanded: boolean
  label: string
  onClick: () => void
}) {
  return (
    <button className="wc-collapse-btn" onClick={onClick} type="button">
      {expanded ? <DownOutlined /> : <RightOutlined />}
      <span>{label}</span>
    </button>
  )
}

export function WecomRecordingDetailPage() {
  const { recordingId } = useParams<{ recordingId: string }>()
  const location = useLocation()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const qc = useQueryClient()
  const auth = useAuth()
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const transcriptListRef = useRef<HTMLDivElement | null>(null)
  const transcriptItemRefs = useRef(new Map<number, HTMLDivElement>())
  const [analysisExpanded, setAnalysisExpanded] = useState(false)
  const [matchExpanded, setMatchExpanded] = useState(true)
  const [recommendExpanded, setRecommendExpanded] = useState(true)
  const [transcriptCardExpanded, setTranscriptCardExpanded] = useState(false)
  const [linkingId, setLinkingId] = useState<string | null>(null)
  const [ensuringDetailId, setEnsuringDetailId] = useState<string | null>(null)
  const [linkSuccessMessage, setLinkSuccessMessage] = useState<string | null>(null)
  const [unlinkConfirmOpen, setUnlinkConfirmOpen] = useState(false)
  const [pendingLinkChoice, setPendingLinkChoice] = useState<PendingLinkChoice | null>(null)
  const [multiCustomerMappingDraft, setMultiCustomerMappingDraft] = useState<Record<string, string>>({})
  const [audioState, setAudioState] = useState(() => createDefaultAudioPlayerState('', null))
  const [splitModalOpen, setSplitModalOpen] = useState(false)
  const [splitAtSeconds, setSplitAtSeconds] = useState<number | null>(null)
  const [ensuredArchiveRecording, setEnsuredArchiveRecording] = useState<{ itemId: string; recordingId: string } | null>(null)
  const fromVisitId = searchParams.get('from_visit_id')
  const archiveItemId = searchParams.get('archive_item_id')
  const dailyVisitOrdersMode = searchParams.get('daily_orders_mode') === 'org'
    ? 'org'
    : searchParams.get('daily_orders_mode') === 'self'
      ? 'self'
      : null
  const dailyVisitOrdersKeyword = searchParams.get('daily_orders_keyword') || ''
  const currentBackTo = `${location.pathname}${location.search}`
  const [orgDailyVisitOrderSearchDraft, setOrgDailyVisitOrderSearchDraft] = useState(dailyVisitOrdersKeyword)

  useEffect(() => {
    setOrgDailyVisitOrderSearchDraft(dailyVisitOrdersKeyword)
  }, [dailyVisitOrdersKeyword])

  async function updateDailyVisitOrdersMode(nextMode: 'self' | 'org' | null) {
    if (nextMode) {
      const preparedRecordingId = await ensureEffectiveRecordingId()
      if (!preparedRecordingId) return
    }
    const nextSearchParams = new URLSearchParams(searchParams)
    if (nextMode) {
      nextSearchParams.set('daily_orders_mode', nextMode)
      if (nextMode !== 'org') {
        nextSearchParams.delete('daily_orders_keyword')
      }
    } else {
      nextSearchParams.delete('daily_orders_mode')
      nextSearchParams.delete('daily_orders_keyword')
    }
    setSearchParams(nextSearchParams, { replace: true })
  }

  const updateDailyVisitOrdersKeyword = (nextKeyword: string) => {
    const nextSearchParams = new URLSearchParams(searchParams)
    if (dailyVisitOrdersMode) {
      nextSearchParams.set('daily_orders_mode', dailyVisitOrdersMode)
    }
    const normalizedKeyword = nextKeyword.trim()
    if (normalizedKeyword) {
      nextSearchParams.set('daily_orders_keyword', normalizedKeyword)
    } else {
      nextSearchParams.delete('daily_orders_keyword')
    }
    setSearchParams(nextSearchParams, { replace: true })
  }

  const {
    data: archiveRecording,
    isLoading: archiveRecordingLoading,
    isError: archiveRecordingIsError,
    error: archiveRecordingError,
  } = useQuery({
    queryKey: ['wecom', 'archive-recording', archiveItemId],
    queryFn: () => fetchArchiveRecordingDetail(archiveItemId!),
    enabled: !!archiveItemId,
    retry: false,
  })

  const ensureArchiveRecordingMutation = useMutation({
    mutationFn: (itemId: string) => ensureArchiveRecording(itemId),
    onSuccess: async (result, itemId) => {
      setEnsuredArchiveRecording({ itemId, recordingId: result.recording_id })
      await qc.invalidateQueries({ queryKey: ['wecom', 'archive-recording', itemId] })
      await qc.invalidateQueries({ queryKey: ['wecom', 'recording', result.recording_id] })
      await qc.invalidateQueries({ queryKey: ['wecom', 'transcripts', result.recording_id] })
    },
  })

  const ensuredArchiveRecordingId = ensuredArchiveRecording?.itemId === archiveItemId
    ? ensuredArchiveRecording.recordingId
    : null
  const effectiveRecordingId = archiveItemId
    ? (archiveRecording?.recording_id || ensuredArchiveRecordingId || null)
    : (recordingId ?? null)

  async function ensureEffectiveRecordingId() {
    const currentRecordingId = effectiveRecordingId || ensuredArchiveRecordingId
    if (currentRecordingId) return currentRecordingId
    if (!archiveItemId) return null
    if (!archiveRecording?.has_transcript) {
      Modal.error({
        title: '暂时无法关联',
        content: '当前归档录音还没有可用的 ASR 转写结果，暂不能生成推荐或关联到诊单。',
        okText: '我知道了',
        centered: true,
        wrapClassName: 'wc-badge-action-modal',
      })
      return null
    }
    try {
      const ensured = await ensureArchiveRecordingMutation.mutateAsync(archiveItemId)
      setEnsuredArchiveRecording({ itemId: archiveItemId, recordingId: ensured.recording_id })
      return ensured.recording_id
    } catch (error) {
      Modal.error({
        title: '暂时无法准备关联',
        content: resolveRecordingDetailErrorMessage(error),
        okText: '我知道了',
        centered: true,
        wrapClassName: 'wc-badge-action-modal',
      })
      return null
    }
  }

  const {
    data: recording,
    isLoading: recordingLoading,
    isError: recordingIsError,
    error: recordingError,
  } = useQuery({
    queryKey: ['wecom', 'recording', effectiveRecordingId],
    queryFn: () => fetchRecording(effectiveRecordingId!),
    enabled: !!effectiveRecordingId,
    retry: false,
  })

  const {
    data: multiCustomerReview,
    isLoading: multiCustomerReviewLoading,
  } = useQuery({
    queryKey: ['wecom', 'recording-multi-customer-review', effectiveRecordingId],
    queryFn: () => fetchRecordingMultiCustomerReview(effectiveRecordingId!),
    enabled: !!effectiveRecordingId && (recording?.linked_visit_ids?.length ?? 0) > 1,
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status === 'analyzing' ? 5000 : false
    },
  })

  const { data: matchData, isLoading: matchLoading } = useQuery({
    queryKey: ['wecom', 'recording-match', effectiveRecordingId],
    queryFn: () => fetchRecordingVisitOrderMatch(effectiveRecordingId!, false, false),
    enabled: !!effectiveRecordingId && matchExpanded && recommendExpanded,
  })

  useEffect(() => {
    if (!multiCustomerReview?.required) return
    setMultiCustomerMappingDraft((current) => {
      const next = { ...current }
      const segmentIds = multiCustomerReview.segments.map((segment) => segment.id)
      for (const visitAnalysis of multiCustomerReview.visit_analyses) {
        if (visitAnalysis.customer_segment_id && segmentIds.includes(visitAnalysis.customer_segment_id)) {
          next[visitAnalysis.visit_id] = visitAnalysis.customer_segment_id
        } else if (!next[visitAnalysis.visit_id]) {
          const usedSegmentIds = new Set(Object.values(next).filter(Boolean))
          next[visitAnalysis.visit_id] = segmentIds.find((segmentId) => !usedSegmentIds.has(segmentId)) || ''
        }
      }
      return next
    })
  }, [multiCustomerReview])

  const { data: dailyVisitOrdersData, isLoading: dailyVisitOrdersLoading } = useQuery({
    queryKey: ['wecom', 'recording-daily-visit-orders', effectiveRecordingId, dailyVisitOrdersMode, dailyVisitOrdersKeyword],
    queryFn: () => fetchDailyVisitOrdersForRecording(effectiveRecordingId!, {
      scope_mode: dailyVisitOrdersMode ?? 'self',
      keyword: dailyVisitOrdersMode === 'org' ? dailyVisitOrdersKeyword : '',
    }),
    enabled: !!effectiveRecordingId && !!dailyVisitOrdersMode,
  })

  const linkMutation = useMutation({
    mutationFn: (variables: LinkMutationVariables) =>
      updateRecording(variables.recId, { visit_id: variables.visitId, linked_visit_ids: variables.linkedVisitIds }),
    onSuccess: async (_result, variables) => {
      setLinkingId(null)
      setPendingLinkChoice(null)
      setUnlinkConfirmOpen(false)
      if (variables.successFeedbackMode === 'modal') {
        setLinkSuccessMessage(null)
        Modal.success({
          title: '关联已成功',
          content: variables.successMessage,
          okText: '我知道了',
          centered: true,
          wrapClassName: 'wc-badge-action-modal',
        })
      } else {
      setLinkSuccessMessage(variables.successMessage)
      }
      setRecommendExpanded(false)
      void updateDailyVisitOrdersMode(null)
      await qc.invalidateQueries({ queryKey: ['wecom', 'archive-recording', archiveItemId] })
      await qc.invalidateQueries({ queryKey: ['wecom', 'recording', variables.recId] })
      await qc.invalidateQueries({ queryKey: ['wecom', 'recording-multi-customer-review', variables.recId] })
      await qc.invalidateQueries({ queryKey: ['wecom', 'recording-match', variables.recId] })
      await qc.invalidateQueries({ queryKey: ['wecom', 'recording-daily-visit-orders', variables.recId] })
      await qc.invalidateQueries({ queryKey: ['wecom', 'recordings'] })
      await qc.invalidateQueries({ queryKey: ['wecom', 'home'] })
      await qc.invalidateQueries({ queryKey: ['wecom', 'customer-detail'] })
      await qc.invalidateQueries({ queryKey: ['wecom', 'visit-detail'] })
      if (fromVisitId) {
        await qc.invalidateQueries({ queryKey: ['wecom', 'visit-detail', fromVisitId] })
      }
    },
    onError: () => {
      setLinkingId(null)
    },
  })

  const ensureLocalVisitMutation = useMutation({
    mutationFn: (variables: { recId: string; visitOrderId: string }) =>
      ensureRecordingVisitOrderLocalVisit(variables.recId, variables.visitOrderId),
  })

  const multiCustomerConfirmMutation = useMutation({
    mutationFn: () => {
      if (!effectiveRecordingId || !multiCustomerReview) {
        throw new Error('当前录音还没有多客户确认数据')
      }
      return confirmRecordingMultiCustomerReview(
        effectiveRecordingId,
        multiCustomerReview.visit_analyses.map((item) => ({
          visit_id: item.visit_id,
          customer_segment_id: multiCustomerMappingDraft[item.visit_id] || '',
        })),
      )
    },
    onSuccess: async () => {
      Modal.success({
        title: '客户对应关系已确认',
        content: '系统已开始按到诊单分别生成客户分析结果，完成后会进入 SAP 自动回传等待期。',
        okText: '我知道了',
        centered: true,
        wrapClassName: 'wc-badge-action-modal',
      })
      await qc.invalidateQueries({ queryKey: ['wecom', 'recording-multi-customer-review', effectiveRecordingId] })
      await qc.invalidateQueries({ queryKey: ['wecom', 'recording-analysis', effectiveRecordingId] })
    },
    onError: (error) => {
      Modal.error({
        title: '确认失败',
        content: error instanceof Error ? error.message : '请检查客户段是否重复选择，稍后再试。',
        okText: '我知道了',
        centered: true,
        wrapClassName: 'wc-badge-action-modal',
      })
    },
  })

  const multiCustomerResetMutation = useMutation({
    mutationFn: () => {
      if (!effectiveRecordingId) {
        throw new Error('当前录音还没有多客户确认数据')
      }
      return resetRecordingMultiCustomerReview(effectiveRecordingId)
    },
    onSuccess: async () => {
      setMultiCustomerMappingDraft({})
      Modal.success({
        title: '已解除客户对应确认',
        content: '旧的客户段映射和到诊单级分析结果已清空，重新确认后系统会重新分析并重新进入 SAP 自动回传等待期。',
        okText: '我知道了',
        centered: true,
        wrapClassName: 'wc-badge-action-modal',
      })
      await qc.invalidateQueries({ queryKey: ['wecom', 'recording-multi-customer-review', effectiveRecordingId] })
    },
    onError: (error) => {
      Modal.error({
        title: '解除失败',
        content: error instanceof Error ? error.message : '请稍后再试。',
        okText: '我知道了',
        centered: true,
        wrapClassName: 'wc-badge-action-modal',
      })
    },
  })

  const splitMutation = useMutation({
    mutationFn: () => {
      if (!effectiveRecordingId || splitAtSeconds == null) {
        throw new Error('请先填写裁切时间点')
      }
      return splitRecording(effectiveRecordingId, { split_at_seconds: splitAtSeconds, confirm: true })
    },
    onSuccess: async (result: RecordingSplitResult) => {
      setSplitModalOpen(false)
      Modal.success({
        title: '裁切完成',
        content: `已生成 ${result.parts.length} 段新录音，原录音已隐藏。`,
        okText: '我知道了',
        centered: true,
        wrapClassName: 'wc-badge-action-modal',
      })
      await qc.invalidateQueries({ queryKey: ['wecom', 'recording', effectiveRecordingId] })
      await qc.invalidateQueries({ queryKey: ['wecom', 'recording-media-source', archiveItemId || effectiveRecordingId] })
      await qc.invalidateQueries({ queryKey: ['wecom', 'transcripts', effectiveRecordingId] })
      await qc.invalidateQueries({ queryKey: ['wecom', 'recordings'] })
      await qc.invalidateQueries({ queryKey: ['wecom', 'home'] })
    },
    onError: (error) => {
      Modal.error({
        title: '裁切失败',
        content: error instanceof Error ? error.message : '请稍后再试。',
        okText: '我知道了',
        centered: true,
        wrapClassName: 'wc-badge-action-modal',
      })
    },
  })

  const { data: analysisTask } = useQuery({
    queryKey: ['wecom', 'recording-analysis', effectiveRecordingId],
    queryFn: () => fetchRecordingAnalysis(effectiveRecordingId!),
    enabled: !!effectiveRecordingId,
  })

  const {
    data: audioSource,
    isLoading: audioSourceLoading,
    isError: audioSourceIsError,
  } = useQuery({
    queryKey: ['wecom', 'recording-media-source', archiveItemId || effectiveRecordingId],
    queryFn: () => (
      archiveItemId
        ? fetchArchiveRecordingMediaSource(archiveItemId)
        : fetchRecordingMediaSource(effectiveRecordingId!)
    ),
    enabled: !!(archiveItemId || effectiveRecordingId),
    retry: false,
  })

  const displayDuration = archiveRecording ? resolveArchiveDurationSeconds(archiveRecording) : recording?.duration_seconds ?? null
  const audioStateKey = `${archiveItemId ?? effectiveRecordingId ?? 'none'}:${audioSource?.url ?? 'none'}`
  const resolvedAudioState = audioState.sourceKey === audioStateKey
    ? audioState
    : createDefaultAudioPlayerState(audioStateKey, displayDuration)
  const playbackMs = resolvedAudioState.playbackMs
  const audioDurationSeconds = resolvedAudioState.durationSeconds
  const currentTimeSeconds = resolvedAudioState.currentTimeSeconds
  const audioReady = resolvedAudioState.ready
  const audioPlaying = resolvedAudioState.playing
  const audioElementError = resolvedAudioState.error
  const resolvedAudioDurationSeconds = audioDurationSeconds ?? displayDuration
  const playbackProgress = resolvedAudioDurationSeconds && resolvedAudioDurationSeconds > 0
    ? Math.min((currentTimeSeconds / resolvedAudioDurationSeconds) * 100, 100)
    : 0

  const analysisFileId = analysisTask?.file_name
    ? analysisTask.file_name.replace(/\.json$/i, '')
    : (analysisTask?.status === 'done' && effectiveRecordingId ? `recording_${effectiveRecordingId}` : undefined)

  const {
    data: analysisDetail,
    error: analysisDetailError,
    isLoading: analysisDetailLoading,
  } = useQuery({
    queryKey: ['wecom', 'recording-analysis-detail', analysisFileId],
    queryFn: () => fetchAnalysisDetail(analysisFileId!),
    enabled: !!analysisFileId && analysisTask?.status === 'done',
  })
  const archiveAnalysisDetail = useMemo(() => buildArchiveAnalysisDetail(archiveRecording), [archiveRecording])
  const resolvedAnalysisDetail = analysisDetail ?? archiveAnalysisDetail
  const recordingRecallSummary = useMemo(
    () => buildRecordingRecallSummary(resolvedAnalysisDetail),
    [resolvedAnalysisDetail],
  )

  const { data: transcriptsData } = useQuery({
    queryKey: ['wecom', 'transcripts', effectiveRecordingId],
    queryFn: () => fetchTranscripts({ recording_id: effectiveRecordingId!, page_size: 100 }),
    enabled: !!effectiveRecordingId,
  })

  const transcript = transcriptsData?.items?.[0] as Transcript | undefined
  const utterances = transcript?.utterances ?? resolveArchiveUtterances(archiveRecording)
  const activeUtteranceIndex = utterances.findIndex(
    (utterance) => playbackMs != null && playbackMs >= utterance.begin_ms && playbackMs < utterance.end_ms,
  )
  const activeUtteranceKey = activeUtteranceIndex >= 0 ? utterances[activeUtteranceIndex]?.begin_ms ?? null : null
  const ensureTranscriptVisibleForPlayback = () => {
    setTranscriptCardExpanded(true)
  }

  useEffect(() => {
    if (!linkSuccessMessage) return
    const timer = window.setTimeout(() => setLinkSuccessMessage(null), 2200)
    return () => window.clearTimeout(timer)
  }, [linkSuccessMessage])

  useEffect(() => {
    if (activeUtteranceKey == null || !transcriptCardExpanded) {
      return
    }
    const element = transcriptItemRefs.current.get(activeUtteranceKey)
    keepElementInScrollContainerView(transcriptListRef.current, element, {
      topPadding: 64,
      bottomPadding: 96,
    })
  }, [activeUtteranceKey, transcriptCardExpanded])

  useEffect(() => {
    if (!audioRef.current || !audioSource?.url) {
      return
    }
    audioRef.current.load()
  }, [audioStateKey, audioSource?.url, displayDuration])

  const toggleRecommendExpanded = () => {
    setRecommendExpanded((prev) => {
      const next = !prev
      if (!next) {
        void updateDailyVisitOrdersMode(null)
      }
      return next
    })
  }
  const updatePlaybackPosition = (nextPlaybackMs: number) => {
    setAudioState((prev) => {
      const base = prev.sourceKey === audioStateKey ? prev : createDefaultAudioPlayerState(audioStateKey, displayDuration)
      return {
        ...base,
        playbackMs: nextPlaybackMs,
        currentTimeSeconds: nextPlaybackMs / 1000,
      }
    })
  }
  const seekAudio = (targetSeconds: number, shouldPlay = false) => {
    if (!audioRef.current) {
      return
    }
    const safeSeconds = Math.max(0, targetSeconds)
    audioRef.current.currentTime = safeSeconds
    setAudioState((prev) => {
      const base = prev.sourceKey === audioStateKey ? prev : createDefaultAudioPlayerState(audioStateKey, displayDuration)
      return {
        ...base,
        currentTimeSeconds: safeSeconds,
        playbackMs: Math.round(safeSeconds * 1000),
      }
    })
    if (shouldPlay) {
      void audioRef.current.play().catch(() => undefined)
    }
  }
  const toggleAudioPlayback = () => {
    if (!audioRef.current || !audioSource?.url || audioElementError) {
      return
    }
    if (audioRef.current.paused) {
      if (audioRef.current.readyState < 2) {
        audioRef.current.load()
      }
      void audioRef.current.play().catch(() => {
        setAudioState((prev) => {
          const base = prev.sourceKey === audioStateKey ? prev : createDefaultAudioPlayerState(audioStateKey, displayDuration)
          return {
            ...base,
            error: true,
            ready: false,
            playing: false,
          }
        })
      })
      return
    }
    audioRef.current.pause()
  }
  const jumpToUtterance = (utterance: TranscriptUtterance) => {
    if (!audioRef.current) {
      return
    }
    ensureTranscriptVisibleForPlayback()
    seekAudio(Math.max(0, utterance.begin_ms) / 1000, true)
  }
  const getUtteranceProgress = (utterance: TranscriptUtterance) => {
    if (playbackMs == null || activeUtteranceKey !== utterance.begin_ms) {
      return 0
    }
    const duration = utterance.end_ms - utterance.begin_ms
    if (duration <= 0) {
      return 0
    }
    return Math.max(0, Math.min(((playbackMs - utterance.begin_ms) / duration) * 100, 100))
  }

  const linkedVisits = recording?.linked_visits ?? []
  const primaryLinkedVisit = linkedVisits.find((visit) => visit.is_primary) ?? linkedVisits[0] ?? null
  const companionLinkedVisits = primaryLinkedVisit
    ? linkedVisits.filter((visit) => visit.id !== primaryLinkedVisit.id)
    : []
  const linkedVisitIds = linkedVisits.map((visit) => visit.id)
  const displayTitle =
    (archiveRecording ? formatRecordingDisplayName(archiveRecording.display_file_name, resolveArchiveCreatedAt(archiveRecording)) : null)
    || (recording ? formatRecordingDisplayName(recording.file_name, recording.created_at) : null)
    || '录音详情'
  const displayStaffName = recording?.staff_name
    || archiveRecording?.staff_name
    || archiveRecording?.sn
    || archiveRecording?.device_code
    || '未识别员工'
  const displayCreatedAt = archiveRecording ? resolveArchiveCreatedAt(archiveRecording) : (recording?.created_at ?? null)
  const displayCreatedLabel = formatDateTime(displayCreatedAt)
  const currentUser = auth.status === 'authenticated' ? auth.user : null
  const canSplitRecording = Boolean(
    effectiveRecordingId
    && recording
    && currentUser
    && recording.status !== 'filtered'
    && (displayDuration ?? 0) > 1
    && (
      isHospitalAdminOrAbove(currentUser.role)
      || (recording.staff_id && recording.staff_id === currentUser.staff_id)
    ),
  )
  const openSplitModal = () => {
    const durationSeconds = displayDuration ?? resolvedAudioDurationSeconds ?? 0
    const defaultSeconds = currentTimeSeconds > 0 && currentTimeSeconds < durationSeconds
      ? Math.floor(currentTimeSeconds)
      : Math.max(1, Math.floor(durationSeconds / 2))
    setSplitAtSeconds(defaultSeconds)
    setSplitModalOpen(true)
  }

  if (archiveItemId && archiveRecordingIsError) {
    return <div className="wc-empty">录音详情加载失败：{resolveRecordingDetailErrorMessage(archiveRecordingError)}</div>
  }

  if (!archiveItemId && recordingIsError) {
    return <div className="wc-empty">录音详情加载失败：{resolveRecordingDetailErrorMessage(recordingError)}</div>
  }

  if ((archiveItemId && (archiveRecordingLoading || !archiveRecording)) || (!archiveItemId && (recordingLoading || !recording))) {
    return <div className="wc-empty">加载中…</div>
  }

  const quickRecommendCandidates = getQuickRecommendSelection(matchData?.candidates ?? []).items
  const buildVisitDetailLink = (visitId: string) => {
    const params = new URLSearchParams()
    params.set('from_recording_match', '1')
    params.set('back_to', currentBackTo)
    if (archiveItemId) {
      params.set('archive_item_id', archiveItemId)
    }
    if (effectiveRecordingId) {
      params.set('from_recording_id', effectiveRecordingId)
    }
    if (fromVisitId) {
      params.set('from_visit_id', fromVisitId)
    }
    return `/wecom/visits/${visitId}?${params.toString()}`
  }
  const executeRecordingLink = async ({
    linkKey,
    recIdOverride,
    visitId,
    linkedVisitIds: nextLinkedVisitIds,
    successMessage,
    successFeedbackMode,
  }: {
    linkKey: string
    recIdOverride?: string
    visitId: string | null
    linkedVisitIds: string[]
    successMessage: string
    successFeedbackMode: 'inline' | 'modal'
  }) => {
    const preparedRecordingId = recIdOverride ?? await ensureEffectiveRecordingId()
    if (!preparedRecordingId) return
    setLinkingId(linkKey)
    linkMutation.mutate({
      recId: preparedRecordingId,
      visitId,
      linkedVisitIds: nextLinkedVisitIds,
      successMessage,
      successFeedbackMode,
    })
  }
  const buildCandidateLinkedVisitIds = ({
    visitId,
    alwaysLinkedVisitIds = [],
    companionVisitIds = [],
    companionVisitOrderRefs = [],
    companionCustomerCodes = [],
  }: {
    visitId: string
    alwaysLinkedVisitIds?: string[]
    companionVisitIds?: string[]
    companionVisitOrderRefs?: string[]
    companionCustomerCodes?: string[]
  }) => {
    const includeCompanions = hasCompanionVisitOptions(companionVisitIds)
      ? window.confirm(buildCompanionVisitPromptMessage(companionVisitOrderRefs, companionCustomerCodes))
      : false
    return buildLinkedVisitIds(visitId, [
      ...alwaysLinkedVisitIds,
      ...(includeCompanions ? companionVisitIds : []),
    ])
  }
  const confirmRecordingVisitLinkRisk = ({
    nextLinkedVisitIds,
    targetLinkedRecordingNames = [],
  }: {
    nextLinkedVisitIds: string[]
    targetLinkedRecordingNames?: string[]
  }) => {
    const message = buildRecordingVisitLinkRiskText({
      nextLinkedVisitIds,
      targetLinkedRecordingNames,
    })
    return !message || window.confirm(message)
  }
  const handleReplaceOrAppendLinkedVisit = (mode: 'replace' | 'append') => {
    if (!pendingLinkChoice) return
    const nextCandidateLinkedVisitIds = buildCandidateLinkedVisitIds({
      visitId: pendingLinkChoice.visitId,
      alwaysLinkedVisitIds: pendingLinkChoice.alwaysLinkedVisitIds,
      companionVisitIds: pendingLinkChoice.companionVisitIds,
      companionVisitOrderRefs: pendingLinkChoice.companionVisitOrderRefs,
      companionCustomerCodes: pendingLinkChoice.companionCustomerCodes,
    })
    if (mode === 'replace') {
      if (!confirmRecordingVisitLinkRisk({
        nextLinkedVisitIds: nextCandidateLinkedVisitIds,
        targetLinkedRecordingNames: pendingLinkChoice.targetLinkedRecordingNames,
      })) return
      void executeRecordingLink({
        linkKey: pendingLinkChoice.linkKey,
        visitId: pendingLinkChoice.visitId,
        linkedVisitIds: nextCandidateLinkedVisitIds,
        successMessage: '已换绑到新的到诊单。',
        successFeedbackMode: 'modal',
      })
      return
    }
    const currentPrimaryVisitId = primaryLinkedVisit?.id || linkedVisits[0]?.id || null
    if (!currentPrimaryVisitId) {
      if (!confirmRecordingVisitLinkRisk({
        nextLinkedVisitIds: nextCandidateLinkedVisitIds,
        targetLinkedRecordingNames: pendingLinkChoice.targetLinkedRecordingNames,
      })) return
      void executeRecordingLink({
        linkKey: pendingLinkChoice.linkKey,
        visitId: pendingLinkChoice.visitId,
        linkedVisitIds: nextCandidateLinkedVisitIds,
        successMessage: '已绑定到该到诊单。',
        successFeedbackMode: 'modal',
      })
      return
    }
    const nextLinkedVisitIds = buildLinkedVisitIds(currentPrimaryVisitId, [
      ...linkedVisitIds.filter((visitId) => visitId !== currentPrimaryVisitId),
      ...nextCandidateLinkedVisitIds.filter((visitId) => visitId !== currentPrimaryVisitId),
    ])
    if (!confirmRecordingVisitLinkRisk({
      nextLinkedVisitIds,
      targetLinkedRecordingNames: pendingLinkChoice.targetLinkedRecordingNames,
    })) return
    void executeRecordingLink({
      linkKey: pendingLinkChoice.linkKey,
      visitId: currentPrimaryVisitId,
      linkedVisitIds: nextLinkedVisitIds,
      successMessage: '已作为辅到诊单继续绑定。',
      successFeedbackMode: 'modal',
    })
  }
  const requestLinkRecordingToVisit = async ({
    linkKey,
    recIdOverride,
    visitId,
    visitOrderRef,
    alwaysLinkedVisitIds = [],
    companionVisitIds = [],
    companionVisitOrderRefs = [],
    companionCustomerCodes = [],
    targetLinkedRecordingNames = [],
  }: {
    linkKey: string
    recIdOverride?: string
    visitId: string
    visitOrderRef: string
    alwaysLinkedVisitIds?: string[]
    companionVisitIds?: string[]
    companionVisitOrderRefs?: string[]
    companionCustomerCodes?: string[]
    targetLinkedRecordingNames?: string[]
  }) => {
    const preparedRecordingId = recIdOverride ?? await ensureEffectiveRecordingId()
    if (!preparedRecordingId) return
    if (linkedVisitIds.includes(visitId)) {
      setLinkSuccessMessage('当前录音已关联这张到诊单。')
      return
    }
    if (linkedVisits.length > 0) {
      setPendingLinkChoice({
        linkKey,
        visitId,
        visitOrderRef,
        alwaysLinkedVisitIds,
        companionVisitIds,
        companionVisitOrderRefs,
        companionCustomerCodes,
        targetLinkedRecordingNames,
      })
      return
    }
    const nextLinkedVisitIds = buildCandidateLinkedVisitIds({
      visitId,
      alwaysLinkedVisitIds,
      companionVisitIds,
      companionVisitOrderRefs,
      companionCustomerCodes,
    })
    if (!confirmRecordingVisitLinkRisk({
      nextLinkedVisitIds,
      targetLinkedRecordingNames,
    })) return
    void executeRecordingLink({
      linkKey,
      recIdOverride: preparedRecordingId,
      visitId,
      linkedVisitIds: nextLinkedVisitIds,
      successMessage: '关联成功！录音已绑定到诊单，推荐列表已自动收起。',
      successFeedbackMode: 'modal',
    })
  }
  const requestLinkRecordingToVisitOrder = async ({
    linkKey,
    visitId,
    visitOrderId,
    visitOrderRef,
    alwaysLinkedVisitIds = [],
    companionVisitIds = [],
    companionVisitOrderRefs = [],
    companionCustomerCodes = [],
    targetLinkedRecordingNames = [],
  }: {
    linkKey: string
    visitId: string | null
    visitOrderId: string
    visitOrderRef: string
    alwaysLinkedVisitIds?: string[]
    companionVisitIds?: string[]
    companionVisitOrderRefs?: string[]
    companionCustomerCodes?: string[]
    targetLinkedRecordingNames?: string[]
  }) => {
    const preparedRecordingId = await ensureEffectiveRecordingId()
    if (!preparedRecordingId) return
    if (visitId) {
      await requestLinkRecordingToVisit({
        linkKey,
        recIdOverride: preparedRecordingId,
        visitId,
        visitOrderRef,
        alwaysLinkedVisitIds,
        companionVisitIds,
        companionVisitOrderRefs,
        companionCustomerCodes,
        targetLinkedRecordingNames,
      })
      return
    }
    setLinkingId(linkKey)
    ensureLocalVisitMutation.mutate(
      {
        recId: preparedRecordingId,
        visitOrderId,
      },
      {
        onSuccess: async (localVisit) => {
          setLinkingId(null)
          await qc.invalidateQueries({ queryKey: ['wecom', 'recording-match', preparedRecordingId] })
          await qc.invalidateQueries({ queryKey: ['wecom', 'recording-daily-visit-orders', preparedRecordingId] })
          await requestLinkRecordingToVisit({
            linkKey,
            recIdOverride: preparedRecordingId,
            visitId: localVisit.visit_id,
            visitOrderRef,
            alwaysLinkedVisitIds,
            companionVisitIds,
            companionVisitOrderRefs,
            companionCustomerCodes,
            targetLinkedRecordingNames,
          })
        },
        onError: () => {
          setLinkingId(null)
          Modal.error({
            title: '暂时无法关联',
            content: '系统未能为这张到诊单生成本地接诊，请刷新后重试。',
            okText: '我知道了',
            centered: true,
            wrapClassName: 'wc-badge-action-modal',
          })
        },
      },
    )
  }
  const requestOpenVisitOrderDetail = async ({
    visitId,
    visitOrderId,
  }: {
    visitId: string | null
    visitOrderId: string
  }) => {
    if (visitId) {
      navigate(buildVisitDetailLink(visitId))
      return
    }
    const preparedRecordingId = await ensureEffectiveRecordingId()
    if (!preparedRecordingId) return
    setEnsuringDetailId(visitOrderId)
    ensureLocalVisitMutation.mutate(
      {
        recId: preparedRecordingId,
        visitOrderId,
      },
      {
        onSuccess: async (localVisit) => {
          setEnsuringDetailId(null)
          await qc.invalidateQueries({ queryKey: ['wecom', 'recording-match', preparedRecordingId] })
          await qc.invalidateQueries({ queryKey: ['wecom', 'recording-daily-visit-orders', preparedRecordingId] })
          navigate(buildVisitDetailLink(localVisit.visit_id))
        },
        onError: () => {
          setEnsuringDetailId(null)
          Modal.error({
            title: '暂时无法查看',
            content: '系统未能为这张到诊单生成本地接诊，请刷新后重试。',
            okText: '我知道了',
            centered: true,
            wrapClassName: 'wc-badge-action-modal',
          })
        },
      },
    )
  }
  const handleConfirmUnlink = () => {
    void executeRecordingLink({
      linkKey: '__unlink__',
      visitId: null,
      linkedVisitIds: [],
      successMessage: '已解除当前录音与到诊单的关联。',
      successFeedbackMode: 'inline',
    })
  }
  const multiCustomerSelectedSegmentIds = multiCustomerReview
    ? multiCustomerReview.visit_analyses.map((item) => multiCustomerMappingDraft[item.visit_id]).filter(Boolean)
    : []
  const hasDuplicateMultiCustomerMapping = new Set(multiCustomerSelectedSegmentIds).size !== multiCustomerSelectedSegmentIds.length
  const multiCustomerMappingComplete = Boolean(
    multiCustomerReview?.required
    && multiCustomerReview.visit_analyses.length > 0
    && multiCustomerReview.visit_analyses.every((item) => Boolean(multiCustomerMappingDraft[item.visit_id]))
    && !hasDuplicateMultiCustomerMapping,
  )
  const matchPanel = (
    <div className="wc-card wc-card--amber wc-match-panel wc-recording-detail-page__match-card">
      <div className="wc-card__head">
        <h2 className="wc-card__title">录音关联</h2>
        <div className="wc-card__head-actions">
          <CollapseToggle
            expanded={matchExpanded}
            label={matchExpanded ? '收起' : '展开'}
            onClick={() => setMatchExpanded((prev) => !prev)}
          />
        </div>
      </div>

      {!matchExpanded ? null : (
        <>
          <div className="wc-match-panel__section">
            <div className="wc-match-panel__section-head">
              <div>
                <strong>当前关联</strong>
              </div>
              {linkedVisits.length > 0 ? (
                <button
                  className="wc-btn wc-btn--ghost wc-btn--compact wc-match-panel__unlink-btn"
                  disabled={Boolean(linkingId)}
                  onClick={() => setUnlinkConfirmOpen(true)}
                  type="button"
                >
                  解除关联
                </button>
              ) : null}
            </div>
            {linkSuccessMessage && (
              <div className="wc-match-success">{linkSuccessMessage}</div>
            )}
            {linkedVisits.length > 0 ? (
              <div className="wc-linked-visit-list">
                {primaryLinkedVisit ? (
                    <div className="wc-linked-visit-group">
                      <div className="wc-linked-visit-group__label">主到诊单</div>
                    <Link className="wc-linked-visit-card wc-linked-visit-card--primary" to={buildVisitDetailLink(primaryLinkedVisit.id)}>
                      <div>
                        <strong>{formatVisitRef(primaryLinkedVisit.external_visit_order_no, primaryLinkedVisit.external_visit_order_seg)}</strong>
                        <span className="wc-linked-visit-card__meta">{primaryLinkedVisit.customer_name || '未识别客户'}</span>
                      </div>
                      <span className="wc-chip wc-chip--blue">查看</span>
                    </Link>
                  </div>
                ) : null}
                {companionLinkedVisits.length > 0 ? (
                  <div className="wc-linked-visit-group">
                    <div className="wc-linked-visit-group__label">同行辅单</div>
                    <div className="wc-linked-visit-group__stack">
                      {companionLinkedVisits.map((visit) => (
                        <Link key={visit.id} className="wc-linked-visit-card wc-linked-visit-card--secondary" to={buildVisitDetailLink(visit.id)}>
                          <div>
                            <strong>{formatVisitRef(visit.external_visit_order_no, visit.external_visit_order_seg)}</strong>
                            <span className="wc-linked-visit-card__meta">{visit.customer_name || '未识别客户'}</span>
                          </div>
                          <span className="wc-chip">查看</span>
                        </Link>
                      ))}
                    </div>
                  </div>
                ) : null}
              </div>
            ) : (
              <div className="wc-empty wc-empty--compact">当前尚未关联接诊</div>
            )}
          </div>

          {recording?.linked_visit_ids && recording.linked_visit_ids.length > 1 ? (
            <div className="wc-match-panel__section wc-multi-customer-review">
              <div className="wc-match-panel__section-head">
                <div>
                  <strong>多客户对应确认</strong>
                  <span className="wc-multi-customer-review__hint">
                    一条录音关联了 {recording.linked_visit_ids.length} 张到诊单，需要确认客户段分别对应哪位客户。
                  </span>
                </div>
              </div>
              {multiCustomerReviewLoading || !multiCustomerReview ? (
                <div className="wc-empty wc-empty--compact">正在生成客户段候选…</div>
              ) : (
                <div className="wc-multi-customer-review__body">
                  <div className={`wc-multi-customer-review__status wc-multi-customer-review__status--${multiCustomerReview.status}`}>
                    {multiCustomerReview.message}
                  </div>
                  <div className="wc-multi-customer-review__segments">
                    {multiCustomerReview.segments.map((segment) => (
                      <div key={segment.id} className="wc-multi-customer-review__segment">
                        <div className="wc-multi-customer-review__segment-head">
                          <strong>{segment.label}</strong>
                          <span>{formatMs(segment.begin_ms)}-{formatMs(segment.end_ms)} · {segment.utterance_count}句</span>
                        </div>
                        <p>{segment.summary}</p>
                      </div>
                    ))}
                  </div>
                  <div className="wc-multi-customer-review__mappings">
                    {multiCustomerReview.visit_analyses.map((visitAnalysis) => (
                      <label key={visitAnalysis.visit_id} className="wc-multi-customer-review__mapping">
                        <span>
                          {formatVisitRef(visitAnalysis.visit_order_no, visitAnalysis.visit_order_seg)}
                          {visitAnalysis.customer_name ? ` · ${visitAnalysis.customer_name}` : ''}
                        </span>
                        <select
                          value={multiCustomerMappingDraft[visitAnalysis.visit_id] || ''}
                          onChange={(event) => {
                            const selectedSegmentId = event.target.value
                            setMultiCustomerMappingDraft((current) => ({
                              ...current,
                              [visitAnalysis.visit_id]: selectedSegmentId,
                            }))
                          }}
                        >
                          <option value="">选择录音中的客户段</option>
                          {multiCustomerReview.segments.map((segment) => (
                            <option key={segment.id} value={segment.id}>
                              {segment.label}（{formatMs(segment.begin_ms)}-{formatMs(segment.end_ms)}）
                            </option>
                          ))}
                        </select>
                        <em>{getVisitAnalysisStatusLabel(visitAnalysis.analysis_status)}</em>
                      </label>
                    ))}
                  </div>
                  {hasDuplicateMultiCustomerMapping ? (
                    <div className="wc-multi-customer-review__warning">每个客户段只能对应一张到诊单，请调整重复选择。</div>
                  ) : null}
                  <button
                    className="wc-btn wc-btn--primary wc-multi-customer-review__submit"
                    disabled={!multiCustomerMappingComplete || multiCustomerConfirmMutation.isPending}
                    onClick={() => multiCustomerConfirmMutation.mutate()}
                    type="button"
                  >
                    {multiCustomerConfirmMutation.isPending ? '确认中…' : '确认对应关系并重新分析'}
                  </button>
                  {multiCustomerReview.status !== 'pending_mapping' ? (
                    <button
                      className="wc-btn wc-btn--ghost wc-multi-customer-review__reset"
                      disabled={multiCustomerResetMutation.isPending}
                      onClick={() => {
                        Modal.confirm({
                          title: '解除多客户对应确认？',
                          content: '解除后会清空当前客户段映射和到诊单级分析结果，并停止这批结果进入后续自动回传；如果已有 SAP 回传日志，则不会自动撤回 SAP 中已生成的咨询单。',
                          okText: '确认解除',
                          cancelText: '取消',
                          centered: true,
                          wrapClassName: 'wc-badge-action-modal',
                          onOk: () => multiCustomerResetMutation.mutate(),
                        })
                      }}
                      type="button"
                    >
                      {multiCustomerResetMutation.isPending ? '解除中…' : '解除确认，重新匹配'}
                    </button>
                  ) : null}
                </div>
              )}
            </div>
          ) : null}

          <div className="wc-match-panel__section wc-match-panel__section--summary">
            <div className="wc-match-panel__section-head">
              <div className="wc-match-panel__summary-head">
                <strong>录音摘要</strong>
              </div>
            </div>
            {analysisDetailLoading ? (
              <div className="wc-match-panel__summary-empty">正在整理录音摘要…</div>
            ) : recordingRecallSummary ? (
              <p className="wc-match-panel__summary-text">{recordingRecallSummary}</p>
            ) : (
              <div className="wc-match-panel__summary-empty">当前录音还没有可用摘要，可先查看转写原文后再关联。</div>
            )}
          </div>

          <div className="wc-match-panel__section wc-match-panel__section--recommend">
            <div className="wc-match-panel__section-head">
              <strong>快速推荐</strong>
              <div className="wc-match-panel__section-actions">
                <CollapseToggle
                  expanded={recommendExpanded}
                  label={recommendExpanded ? '收起推荐' : '展开推荐'}
                  onClick={toggleRecommendExpanded}
                />
              </div>
            </div>

            {!recommendExpanded ? null : (
              <>
                {!effectiveRecordingId ? (
                  <div className="wc-empty">
                    <p>当前归档录音尚未生成可关联的正式记录。</p>
                    <button
                      className="wc-btn wc-btn--primary wc-btn--compact"
                      disabled={ensureArchiveRecordingMutation.isPending}
                      onClick={() => void ensureEffectiveRecordingId()}
                      type="button"
                    >
                      {ensureArchiveRecordingMutation.isPending ? '准备中…' : '生成快速推荐'}
                    </button>
                  </div>
                ) : matchLoading ? (
                  <div className="wc-empty">正在生成快速推荐…</div>
                ) : !quickRecommendCandidates.length ? (
                  <div className="wc-empty">{matchData?.summary || '暂无推荐到诊单'}</div>
                ) : (
                  <div className="wc-match-list">
                    {quickRecommendCandidates.map((c) => (
                      <MatchCard
                        key={c.visit_order_id}
                        candidate={c}
                        linking={linkingId === c.visit_order_id}
                        linked={Boolean(
                          (c.local_visit_id && linkedVisitIds.includes(c.local_visit_id))
                          || c.associated_local_visit_ids.some((visitId) => linkedVisitIds.includes(visitId)),
                        )}
                        visitDetailLink={c.local_visit_id ? buildVisitDetailLink(c.local_visit_id) : null}
                        viewingDetail={ensuringDetailId === c.visit_order_id}
                        onOpenDetail={() => void requestOpenVisitOrderDetail({
                          visitId: c.local_visit_id,
                          visitOrderId: c.visit_order_id,
                        })}
                        onLink={() => {
                          void requestLinkRecordingToVisitOrder({
                            linkKey: c.visit_order_id,
                            visitId: c.local_visit_id,
                            visitOrderId: c.visit_order_id,
                            visitOrderRef: formatMergedVisitOrderTitle(c.dzdh, c.dzseg, c.merged_line_items?.length ?? 0),
                            companionVisitIds: c.associated_local_visit_ids,
                            companionVisitOrderRefs: c.companion_visit_order_refs,
                            companionCustomerCodes: c.companion_customer_codes,
                            targetLinkedRecordingNames: c.linked_recording_names,
                          })
                        }}
                      />
                    ))}
                  </div>
                )}

                <div className="wc-match-panel__all-orders-actions">
                  <button
                    className={`wc-match-panel__all-orders-btn${dailyVisitOrdersMode === 'self' ? ' wc-match-panel__all-orders-btn--active' : ''}`}
                    disabled={ensureArchiveRecordingMutation.isPending}
                    onClick={() => void updateDailyVisitOrdersMode(dailyVisitOrdersMode === 'self' ? null : 'self')}
                    type="button"
                  >
                    {dailyVisitOrdersMode === 'self' ? '收起自己当天到诊单' : '查看自己当天全部到诊单'}
                  </button>
                  <button
                    className={`wc-match-panel__all-orders-btn${dailyVisitOrdersMode === 'org' ? ' wc-match-panel__all-orders-btn--active' : ''}`}
                    disabled={ensureArchiveRecordingMutation.isPending}
                    onClick={() => void updateDailyVisitOrdersMode(dailyVisitOrdersMode === 'org' ? null : 'org')}
                    type="button"
                  >
                    {dailyVisitOrdersMode === 'org' ? '收起所有人当天到诊单' : '查看所有人当天全部到诊单'}
                  </button>
                </div>
              </>
            )}
          </div>

          {recommendExpanded && dailyVisitOrdersMode && (
            <div className="wc-daily-orders">
              <div className="wc-daily-orders__head">
                <strong>{dailyVisitOrdersMode === 'org' ? '机构内当天全部到诊单' : '自己当天全部到诊单'}</strong>
                <span>
                  {dailyVisitOrdersData?.recording_date || '-'} · {dailyVisitOrdersData?.total ?? 0}条
                </span>
              </div>
              {dailyVisitOrdersMode === 'org' ? (
                <form
                  className="wc-daily-orders__search"
                  onSubmit={(event) => {
                    event.preventDefault()
                    updateDailyVisitOrdersKeyword(orgDailyVisitOrderSearchDraft)
                  }}
                >
                  <label className="wc-search wc-search--compact" htmlFor="wecom-daily-orders-search">
                    <SearchOutlined />
                    <input
                      id="wecom-daily-orders-search"
                      onChange={(event) => setOrgDailyVisitOrderSearchDraft(event.target.value)}
                      placeholder="搜索客户、到诊单号、咨询师、备注"
                      value={orgDailyVisitOrderSearchDraft}
                    />
                  </label>
                  <button className="wc-btn wc-btn--ghost wc-btn--compact" type="submit">搜索</button>
                  {dailyVisitOrdersKeyword ? (
                    <button
                      className="wc-btn wc-btn--ghost wc-btn--compact"
                      onClick={() => {
                        setOrgDailyVisitOrderSearchDraft('')
                        updateDailyVisitOrdersKeyword('')
                      }}
                      type="button"
                    >
                      清空
                    </button>
                  ) : null}
                </form>
              ) : null}
              {dailyVisitOrdersLoading ? (
                <div className="wc-empty">正在加载当天到诊单…</div>
              ) : !(dailyVisitOrdersData?.items?.length) ? (
                <div className="wc-empty">当天没有可展示的到诊单</div>
              ) : (
                <div className="wc-daily-orders__list">
                  {dailyVisitOrdersData.items.map((item) => {
                    const isCurrentLinked = linkedVisits.some(
                      (visit) =>
                        visit.external_visit_order_no === item.dzdh
                        && (visit.external_visit_order_seg ?? null) === (item.dzseg ?? null),
                    )
                    return (
                      <div key={item.id} className="wc-daily-order-card">
                        <div className="wc-daily-order-card__head">
                          <div>
                            <strong>{item.dzdh}{item.dzseg ? `-${item.dzseg}` : ''}</strong>
                            <span>{item.ninam || '-'}{item.kunr ? ` / ${item.kunr}` : ''}</span>
                          </div>
                          <div className="wc-tag-wrap">
                            {item.customer_type_label ? (
                              <span className={`wc-chip ${item.customer_type_code === 'V' ? 'wc-chip--green' : 'wc-chip--blue'}`}>
                                {item.customer_type_label}
                              </span>
                            ) : null}
                            {isCurrentLinked ? (
                              <span className="wc-chip wc-chip--green">当前已关联</span>
                            ) : (
                              <span className="wc-chip">{item.jcsta_txt || '状态未知'}</span>
                            )}
                          </div>
                        </div>
	                        <div className="wc-daily-order-card__meta">
	                          <span>{item.fzsj ? `分诊 ${fmtClock(item.fzsj)}` : '分诊时间未知'}</span>
	                          <span>{item.remark_dz || '线索未填写'}</span>
	                        </div>
                        {item.linked_recording_names.length > 0 && (
                          <div className="wc-match-card__linked">已关联录音：{item.linked_recording_names.join('、')}</div>
                        )}
                        {item.companion_visit_order_refs.length > 0 && (
                          <div className="wc-match-card__linked">同行辅单：{item.companion_visit_order_refs.join(' / ')}</div>
                        )}
                        <div className="wc-daily-order-card__actions">
                          {item.detail_local_visit_id ? (
                            <Link className="wc-btn wc-btn--ghost wc-btn--compact wc-daily-order-card__detail-link" to={buildVisitDetailLink(item.detail_local_visit_id)}>
                              查看到诊单
                            </Link>
                          ) : null}
                          <button
                            className="wc-btn wc-btn--ghost"
                            disabled={linkingId === item.id || !effectiveRecordingId || isCurrentLinked}
                            onClick={() => {
                              void requestLinkRecordingToVisitOrder({
                                linkKey: item.id,
                                visitId: item.local_visit_id,
                                visitOrderId: item.id,
                                visitOrderRef: formatVisitRef(item.dzdh, item.dzseg),
                                alwaysLinkedVisitIds: item.associated_local_visit_ids,
                                companionVisitIds: item.companion_local_visit_ids,
                                companionVisitOrderRefs: item.companion_visit_order_refs,
                                companionCustomerCodes: item.companion_customer_codes,
                                targetLinkedRecordingNames: item.linked_recording_names,
                              })
                            }}
                            type="button"
                          >
                            {isCurrentLinked ? '当前已关联' : linkingId === item.id ? '关联中…' : '绑定这张到诊单'}
                          </button>
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  )

  return (
    <div className="wc-page wc-recording-detail-page">
      <div className="wc-card wc-card--sky wc-recording-overview wc-recording-detail-page__hero">
        <div className="wc-detail-header wc-detail-header--recording wc-recording-detail-page__header">
          <div className="wc-recording-detail-page__header-top">
            <div className="wc-recording-detail-page__header-main">
              <h1 className="wc-detail-header__title">{displayTitle}</h1>
              <div className="wc-recording-detail-page__meta-strip">
                <span className="wc-recording-detail-page__meta-item wc-recording-detail-page__meta-item--staff">
                  <UserOutlined />
                  <span>{displayStaffName}</span>
                </span>
                <span className="wc-recording-detail-page__meta-item wc-recording-detail-page__meta-item--time">
                  <CalendarOutlined />
                  <span>{displayCreatedLabel}</span>
                </span>
              </div>
            </div>
            <div className="wc-recording-detail-page__header-side">
              <div className="wc-recording-detail-page__duration-card" title={`时长 ${formatDuration(displayDuration)}`}>
                <ClockCircleOutlined className="wc-recording-detail-page__duration-icon" />
                <strong className="wc-recording-detail-page__duration-value">{formatDuration(displayDuration)}</strong>
              </div>
            </div>
          </div>
          {fromVisitId && (
            <div className="wc-detail-header__context">
              <span className="wc-chip wc-chip--green">来自某次接诊</span>
              <Link className="wc-more-link" to={buildVisitDetailLink(fromVisitId)}>返回本次接诊</Link>
            </div>
          )}
        </div>

        <div className="wc-recording-overview__audio">
          <div className="wc-recording-overview__player-shell">
            {audioSource?.url ? (
              <>
                <audio
                  ref={audioRef}
                  preload="auto"
                  playsInline
                  src={audioSource.url}
                  className="wc-recording-overview__native-audio"
                  onLoadedMetadata={(event) => {
                    const nextDuration = Number.isFinite(event.currentTarget.duration)
                      ? event.currentTarget.duration
                      : null
                    setAudioState((prev) => {
                      const base = prev.sourceKey === audioStateKey ? prev : createDefaultAudioPlayerState(audioStateKey, displayDuration)
                      return {
                        ...base,
                        durationSeconds: nextDuration,
                      }
                    })
                  }}
                  onLoadedData={() => {
                    setAudioState((prev) => {
                      const base = prev.sourceKey === audioStateKey ? prev : createDefaultAudioPlayerState(audioStateKey, displayDuration)
                      return {
                        ...base,
                        ready: true,
                        error: false,
                      }
                    })
                  }}
                  onCanPlay={() => {
                    setAudioState((prev) => {
                      const base = prev.sourceKey === audioStateKey ? prev : createDefaultAudioPlayerState(audioStateKey, displayDuration)
                      return {
                        ...base,
                        ready: true,
                        error: false,
                      }
                    })
                  }}
                  onPlay={() => {
                    ensureTranscriptVisibleForPlayback()
                    setAudioState((prev) => {
                      const base = prev.sourceKey === audioStateKey ? prev : createDefaultAudioPlayerState(audioStateKey, displayDuration)
                      return {
                        ...base,
                        playing: true,
                      }
                    })
                  }}
                  onPause={() => {
                    setAudioState((prev) => {
                      const base = prev.sourceKey === audioStateKey ? prev : createDefaultAudioPlayerState(audioStateKey, displayDuration)
                      return {
                        ...base,
                        playing: false,
                      }
                    })
                  }}
                  onTimeUpdate={(event) => {
                    updatePlaybackPosition(Math.round(event.currentTarget.currentTime * 1000))
                  }}
                  onSeeked={(event) => {
                    ensureTranscriptVisibleForPlayback()
                    updatePlaybackPosition(Math.round(event.currentTarget.currentTime * 1000))
                  }}
                  onEnded={() => {
                    setAudioState((prev) => {
                      const base = prev.sourceKey === audioStateKey ? prev : createDefaultAudioPlayerState(audioStateKey, displayDuration)
                      return {
                        ...base,
                        playing: false,
                        playbackMs: null,
                        currentTimeSeconds: 0,
                      }
                    })
                  }}
                  onError={() => {
                    setAudioState((prev) => {
                      const base = prev.sourceKey === audioStateKey ? prev : createDefaultAudioPlayerState(audioStateKey, displayDuration)
                      return {
                        ...base,
                        error: true,
                        ready: false,
                      }
                    })
                  }}
                >
                  您的浏览器暂不支持音频播放。
                </audio>

                <div className="wc-recording-overview__player-control">
                  <button
                    className="wc-recording-overview__play-btn"
                    disabled={!audioSource?.url || audioElementError}
                    onClick={toggleAudioPlayback}
                    type="button"
                  >
                    {audioPlaying ? <PauseCircleFilled /> : <PlayCircleFilled />}
                  </button>

                  <div className="wc-recording-overview__timeline">
                    <div className="wc-recording-overview__timeline-row">
                      <span>{formatAudioTime(currentTimeSeconds)}</span>
                      <div className="wc-recording-overview__timeline-slider">
                        <span
                          className="wc-recording-overview__timeline-fill"
                          style={{ width: `${playbackProgress}%` }}
                          aria-hidden="true"
                        />
                        <input
                          type="range"
                          min={0}
                          max={resolvedAudioDurationSeconds && resolvedAudioDurationSeconds > 0 ? resolvedAudioDurationSeconds : 0}
                          step={0.1}
                          value={Math.min(currentTimeSeconds, resolvedAudioDurationSeconds || currentTimeSeconds)}
                          disabled={!audioReady || !resolvedAudioDurationSeconds || resolvedAudioDurationSeconds <= 0}
                          onChange={(event) => seekAudio(Number(event.currentTarget.value))}
                        />
                      </div>
                      <span>{formatAudioTime(resolvedAudioDurationSeconds)}</span>
                    </div>
                  </div>
                </div>
                {canSplitRecording ? (
                  <div className="wc-recording-overview__split-row">
                    <button className="wc-btn wc-btn--ghost wc-btn--compact" onClick={openSplitModal} type="button">
                      <ScissorOutlined />
                      <span>裁切录音</span>
                    </button>
                    <span>当前定位 {formatAudioTime(currentTimeSeconds)}</span>
                  </div>
                ) : null}
              </>
            ) : audioSourceLoading ? (
              <div className="wc-recording-overview__player-loading" aria-live="polite">
                <div className="wc-recording-overview__player-loading-bar" />
                <div className="wc-recording-overview__player-loading-copy">
                  正在准备音频，稍后即可边加载边播放…
                </div>
              </div>
            ) : audioSourceIsError || audioElementError ? (
              <div className="wc-empty">音频加载失败，请稍后重试</div>
            ) : (
              <div className="wc-empty">暂无可播放音频</div>
            )}
          </div>

          <div className="wc-inline-transcript wc-inline-transcript--panel">
            <div className="wc-card__head">
              <h2 className="wc-card__title">对话全文</h2>
              <div className="wc-card__head-actions">
                <CollapseToggle
                  expanded={transcriptCardExpanded}
                  label={transcriptCardExpanded ? '收起' : '展开'}
                  onClick={() => setTranscriptCardExpanded((prev) => !prev)}
                />
              </div>
            </div>

            {!transcriptCardExpanded ? (
              utterances.length === 0 ? (
                <div className="wc-empty">暂无对话全文</div>
              ) : null
            ) : utterances.length === 0 ? (
              <div className="wc-empty">暂无对话全文</div>
            ) : (
              <div ref={transcriptListRef} className="wc-transcript">
                {utterances.map((item, index) => {
                  const rawSpeaker = item.speaker?.trim?.() || ''
                  const speakerMeta = rawSpeaker ? SPEAKER_MAP[rawSpeaker] : undefined
                  const speaker = (speakerMeta?.label ?? rawSpeaker) || SPEAKER_MAP.unknown.label
                  const speakerTone = speakerMeta?.color ?? SPEAKER_MAP.unknown.color
                  const isCustomer = ['customer', '客户', 'patient', '患者', 'client'].includes(rawSpeaker)
                  const isActive = activeUtteranceKey === item.begin_ms
                  return (
                    <div
                      key={`${item.begin_ms}-${index}`}
                      ref={(element) => {
                        if (element) {
                          transcriptItemRefs.current.set(item.begin_ms, element)
                        } else {
                          transcriptItemRefs.current.delete(item.begin_ms)
                        }
                      }}
                      className={`wc-bubble wc-bubble--${speakerTone}${isCustomer ? ' wc-bubble--right' : ''}${audioSource?.url ? ' wc-bubble--interactive' : ''}${isActive ? ' wc-bubble--active' : ''}`}
                      onClick={() => {
                        if (audioSource?.url) {
                          jumpToUtterance(item)
                        }
                      }}
                    >
                      {isActive && (
                        <div className="wc-bubble__progress" aria-hidden="true">
                          <span style={{ width: `${getUtteranceProgress(item)}%` }} />
                        </div>
                      )}
                      <div className="wc-bubble__head">
                        <span className={`wc-bubble__speaker wc-bubble__speaker--${speakerTone}`}>{speaker}</span>
                        <small className="wc-bubble__time">{formatMs(item.begin_ms)}</small>
                      </div>
                      <p>{item.text}</p>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        </div>
      </div>

      {matchPanel}

      <Modal
        open={splitModalOpen}
        title="确认裁切录音"
        okText="确认裁切"
        cancelText="取消"
        okButtonProps={{
          danger: true,
          loading: splitMutation.isPending,
        }}
        onOk={() => splitMutation.mutate()}
        onCancel={() => {
          if (splitMutation.isPending) return
          setSplitModalOpen(false)
        }}
        centered
        wrapClassName="wc-badge-action-modal"
      >
        <p>裁切后原录音会被隐藏，新生成的两段录音需要按实际客户重新关联到诊单。</p>
        <label className="wc-split-field">
          <span>裁切时间点（秒）</span>
          <input
            min={1}
            max={Math.max(1, (displayDuration ?? 2) - 1)}
            step={1}
            type="number"
            value={splitAtSeconds ?? ''}
            onChange={(event) => setSplitAtSeconds(event.currentTarget.value ? Number(event.currentTarget.value) : null)}
          />
        </label>
        <p className="wc-modal-copy">将在 {formatAudioTime(splitAtSeconds ?? 0)} 处分成前后两段。</p>
      </Modal>

      <Modal
        open={unlinkConfirmOpen}
        title="确认解除关联"
        okText="确认解除"
        cancelText="取消"
        okButtonProps={{
          danger: true,
          loading: linkingId === '__unlink__',
        }}
        onOk={handleConfirmUnlink}
        onCancel={() => {
          if (linkingId === '__unlink__') return
          setUnlinkConfirmOpen(false)
        }}
      >
        <p>确认解除这条录音与当前已关联到诊单的绑定关系吗？</p>
        <p className="wc-modal-copy">解除后，这条录音会重新回到待关联状态。</p>
      </Modal>

      <Modal
        open={Boolean(pendingLinkChoice)}
        title="这条录音已有关联"
        footer={[
          <button
            key="cancel"
            className="wc-btn wc-btn--ghost"
            disabled={Boolean(linkingId)}
            onClick={() => setPendingLinkChoice(null)}
            type="button"
          >
            取消
          </button>,
          <button
            key="append"
            className="wc-btn wc-btn--ghost"
            disabled={Boolean(linkingId)}
            onClick={() => handleReplaceOrAppendLinkedVisit('append')}
            type="button"
          >
            作为辅到诊单绑定
          </button>,
          <button
            key="replace"
            className="wc-btn wc-btn--primary"
            disabled={Boolean(linkingId)}
            onClick={() => handleReplaceOrAppendLinkedVisit('replace')}
            type="button"
          >
            换绑新到诊单
          </button>,
        ]}
        onCancel={() => {
          if (linkingId) return
          setPendingLinkChoice(null)
        }}
      >
        <p>当前录音已经关联到诊单。</p>
        <p className="wc-modal-copy">
          {pendingLinkChoice?.visitOrderRef
            ? `请选择将 ${pendingLinkChoice.visitOrderRef} 换绑为新的主到诊单，还是作为辅到诊单继续绑定。`
            : '请选择要换绑新的主到诊单，还是作为辅到诊单继续绑定。'}
        </p>
      </Modal>

      {/* Analysis */}
      <div className="wc-card wc-card--mint wc-card--compact wc-recording-detail-page__analysis-card">
        <div className="wc-card__head">
          <h2 className="wc-card__title">分析结果</h2>
          <div className="wc-card__head-actions">
            <CollapseToggle
              expanded={analysisExpanded}
              label={analysisExpanded ? '收起' : '展开'}
              onClick={() => setAnalysisExpanded((prev) => !prev)}
            />
          </div>
        </div>

        {!analysisExpanded ? null : !analysisTask && !resolvedAnalysisDetail ? (
          <div className="wc-empty">暂无分析结果</div>
        ) : (analysisTask?.status === 'pending' || analysisTask?.status === 'running') && !resolvedAnalysisDetail ? (
          <div className="wc-empty">分析结果整理中…</div>
        ) : analysisTask?.status === 'failed' && !resolvedAnalysisDetail ? (
          <div className="wc-empty">当前暂无可用分析结果</div>
        ) : analysisDetailLoading && !resolvedAnalysisDetail ? (
          <div className="wc-empty">分析详情加载中…</div>
        ) : analysisDetailError && !resolvedAnalysisDetail ? (
          <div className="wc-empty">分析详情加载失败，请稍后重试</div>
        ) : resolvedAnalysisDetail ? (
          <div className="wc-analysis-detail-shell">
            <AnalysisDetailContent
              data={resolvedAnalysisDetail}
              embedded
              embeddedSectionDefaultOpen={false}
              embeddedSimplified
              customerTagDisplayMode="extracted"
              recordingId={effectiveRecordingId}
              recordingLinkBase={null}
            />
          </div>
        ) : (
          <div className="wc-empty">暂无分析详情</div>
        )}
      </div>

    </div>
  )
}

function MatchCard({
  candidate: c,
  linking,
  linked,
  onLink,
  onOpenDetail,
  visitDetailLink,
  viewingDetail,
}: {
  candidate: VisitOrderMatchCandidate
  linking: boolean
  linked: boolean
  onLink: () => void
  onOpenDetail: () => void
  visitDetailLink?: string | null
  viewingDetail: boolean
}) {
  const pct = Math.round(c.confidence * 100)
  const isHigh = c.confidence >= 0.9
  const isMedium = c.confidence >= 0.6 && c.confidence < 0.9
  const decisionLabel = c.decision === 'auto' ? '自动关联' : c.decision === 'recommend' ? '推荐关联' : '待确认'
  const decisionClass = c.decision === 'auto' ? 'wc-chip--green' : c.decision === 'recommend' ? 'wc-chip--blue' : ''
  const matchMethodLabel = getMatchMethodLabel(c.method)
  const displayEvidenceLines = getDisplayMatchEvidenceLines(c)
  const detailPath = visitDetailLink || null

  return (
    <div className={`wc-match-card${isHigh ? ' wc-match-card--high' : ''}`}>
      <div className="wc-match-card__head">
        <div>
          <strong className="wc-match-card__dzdh">
            {formatMergedVisitOrderTitle(c.dzdh, c.dzseg, c.merged_line_items?.length ?? 0)}
          </strong>
          {c.merged_segments?.length > 1 && <span className="wc-match-card__merged">已合并 {c.merged_segments.length} 条分诊明细</span>}
        </div>
        <div className="wc-tag-wrap">
          {c.customer_type_label ? (
            <span className={`wc-chip ${c.customer_type_code === 'V' ? 'wc-chip--green' : 'wc-chip--blue'}`}>
              {c.customer_type_label}
            </span>
          ) : null}
          <span className={`wc-chip ${decisionClass}`}>{decisionLabel}</span>
        </div>
      </div>

      <div className="wc-match-card__info">
        <span>{c.customer_name || '-'}{c.customer_code ? ` / ${c.customer_code}` : ''}</span>
        <span>{c.visit_date || '-'}</span>
        {matchMethodLabel ? <span>{matchMethodLabel}</span> : null}
        <span className="wc-match-card__conf">
          置信度 <strong className={isHigh ? 'wc-conf--high' : isMedium ? 'wc-conf--mid' : 'wc-conf--low'}>{pct}%</strong>
        </span>
      </div>

      {displayEvidenceLines.length > 0 && (
        <div className="wc-match-card__reasons">
          {displayEvidenceLines.map((line) => <span key={line}>{line}</span>)}
        </div>
      )}

      <MatchLineItems candidate={c} />

      {c.linked_recording_names?.length > 0 && (
        <div className="wc-match-card__linked">已关联录音：{c.linked_recording_names.join('、')}</div>
      )}
      {c.companion_visit_order_refs.length > 0 && (
        <div className="wc-match-card__linked">同行辅单：{c.companion_visit_order_refs.join(' / ')}</div>
      )}

      {c.manual_review_required && (
        <div className="wc-match-card__warn">{c.manual_review_reason || '需人工确认'}</div>
      )}

      <div className="wc-match-card__actions">
        {detailPath ? (
          <Link className="wc-btn wc-btn--ghost wc-btn--compact wc-match-card__detail-link" to={detailPath}>
            查看到诊单
          </Link>
        ) : (
          <button
            className="wc-btn wc-btn--ghost wc-btn--compact wc-match-card__detail-link"
            disabled={viewingDetail || linking}
            onClick={onOpenDetail}
            type="button"
          >
            {viewingDetail ? '打开中…' : '查看到诊单'}
          </button>
        )}
        <button
          className="wc-match-card__btn"
          disabled={linking || linked}
          onClick={onLink}
          type="button"
        >
          {linked ? '当前已关联' : linking ? '关联中…' : '关联此到诊单'}
        </button>
      </div>
    </div>
  )
}
export default WecomRecordingDetailPage
