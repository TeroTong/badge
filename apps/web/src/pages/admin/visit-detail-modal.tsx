import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  AudioOutlined,
  FileTextOutlined,
  FundOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { Avatar, Button, Card, Empty, Modal, Progress, Space, Spin, Tag } from 'antd'
import dayjs from 'dayjs'
import { useNavigate } from 'react-router-dom'

import { fetchRecordingMediaBlob } from '@/api/recordings'
import { fetchTranscript, type TranscriptUtterance } from '@/api/transcripts'
import { fetchVisitDetail, type VisitDetail } from '@/api/visits'
import { sanitizeEvaluationDimensionSummary, sanitizeEvaluationSummary } from '@/utils/evaluation-summary'
import { keepElementInScrollContainerView } from '@/utils/scroll'
import {
  buildVisitOrderLineItemMeta,
  formatVisitOrderLineItemRef,
} from '@/utils/visit-order-line-items'
import { formatRecordingDisplayName } from '@/utils/recording-display'
import { formatBeijingTime } from '@/utils/time'

type EvaluationIssue = { description: string; evidence: string }
type AnalysisDimension = {
  name: string
  score?: number | string | null
  point_score?: number | string | null
  max_score?: number | string | null
  comment?: string | null
  summary?: string | null
  status?: string | null
  issues?: EvaluationIssue[]
}
type AnalysisFocusArea = { area: string; surface_need?: string; deep_need?: string }
type AnalysisConcern = { type?: string; content?: string; evidence?: string }
type AnalysisTag = { category?: string; value?: string }
type AnalysisResult = {
  source?: string
  _original?: Record<string, unknown>
  consultation_evaluation?: {
    overall_score?: number
    total_score?: number
    max_total_score?: number
    overall_summary?: string
    dimensions?: AnalysisDimension[]
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
    expectation?: { dialogue_type?: string }
  }
  customer_concerns?: {
    summary?: string
    items?: AnalysisConcern[]
  }
  customer_profile?: {
    tags?: AnalysisTag[]
  }
}

function formatDuration(seconds: number | null) {
  if (seconds == null) return '时长未知'
  const minutes = Math.floor(seconds / 60)
  const remainder = Math.round(seconds % 60)
  return `${minutes}:${remainder.toString().padStart(2, '0')}`
}

function formatMs(ms: number) {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000))
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${minutes}:${seconds.toString().padStart(2, '0')}`
}

function resolveGender(gender: string | null) {
  if (gender === 'male') return '男'
  if (gender === 'female') return '女'
  return '未标注'
}

function resolveSpeakerLabel(speaker: string) {
  if (speaker === 'consultant') return '顾问'
  if (speaker === 'doctor') return '医生'
  if (speaker === 'customer') return '客户'
  return '未知'
}

function resolveSpeakerTone(speaker: string) {
  if (speaker === 'consultant') return 'visit-transcript-item__speaker--consultant'
  if (speaker === 'doctor') return 'visit-transcript-item__speaker--doctor'
  if (speaker === 'customer') return 'visit-transcript-item__speaker--customer'
  return 'visit-transcript-item__speaker--unknown'
}

function scoreTone(score: number | null) {
  if (score == null) {
    return { label: '暂无评分', tone: 'muted', color: '#cbd5e1' }
  }
  if (score >= 8.5) {
    return { label: '优秀', tone: 'excellent', color: '#7c3aed' }
  }
  if (score >= 7) {
    return { label: '良好', tone: 'good', color: '#8b5cf6' }
  }
  if (score >= 5.5) {
    return { label: '一般', tone: 'normal', color: '#f59e0b' }
  }
  return { label: '待提升', tone: 'warning', color: '#fb7185' }
}

function toFiniteNumber(value: unknown): number | null {
  if (typeof value === 'number') {
    return Number.isFinite(value) ? value : null
  }
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

function normalizeDimensions(items: AnalysisDimension[] | undefined) {
  return (items ?? [])
    .map((item) => {
      const pointScore = toFiniteNumber(item?.point_score)
      const maxScore = toFiniteNumber(item?.max_score) ?? 1
      const score = toFiniteNumber(item?.score) ?? (pointScore != null ? (pointScore / maxScore) * 10 : null)
      return {
        ...item,
        name: item?.name ?? '未命名维度',
        pointScore,
        maxScore,
        summary: item?.summary ?? item?.comment ?? '',
        issues: item?.issues ?? [],
        score,
      }
    })
    .filter((item) => item.score != null)
}

function formatPointScore(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return '--'
  return value.toFixed(2).replace(/\.?0+$/, '')
}

function buildSummary(analysis: AnalysisResult | null, visit: VisitDetail) {
  const snippets: string[] = []
  const firstDemand = analysis?.customer_demands?.focus_areas?.[0]
  const concernSummary = analysis?.customer_concerns?.summary
  const firstConcern = analysis?.customer_concerns?.items?.[0]?.content

  if (firstDemand?.surface_need) {
    snippets.push(`客户当前更关注${firstDemand.area}，表层诉求集中在${firstDemand.surface_need}。`)
  }
  if (firstDemand?.deep_need) {
    snippets.push(`进一步追问后，深层诉求偏向${firstDemand.deep_need}。`)
  }
  if (concernSummary) {
    snippets.push(concernSummary)
  } else if (firstConcern) {
    snippets.push(`本次接诊的主要顾虑是${firstConcern}。`)
  }
  if (!snippets.length && visit.notes) {
    snippets.push(visit.notes)
  }
  if (!snippets.length && visit.latest_transcript_excerpt) {
    snippets.push(visit.latest_transcript_excerpt)
  }
  return snippets
}

function VisitOrderLineItemsBlock({ items }: { items: NonNullable<VisitDetail['visit_order_context']>['line_items'] }) {
  if (!items.length) return null

  return (
    <div className="visit-detail-basic__line-items">
      <span>分诊明细{items.length > 1 ? `（合并 ${items.length} 条）` : ''}</span>
      <div className="visit-detail-basic__line-item-grid">
        {items.map((item, index) => {
          const metaLines = buildVisitOrderLineItemMeta(item)
          return (
            <div key={`${item.fzdh ?? item.dzseg ?? 'line-item'}-${index}`} className="visit-detail-basic__line-item">
              <strong>{formatVisitOrderLineItemRef(item)}</strong>
              {metaLines.map((line) => (
                <p key={line}>{line}</p>
              ))}
              {item.note_summary ? <p className="visit-detail-basic__line-item-note">备注：{item.note_summary}</p> : null}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function RecordingItem({ recording }: { recording: VisitDetail['recordings'][number] }) {
  const navigate = useNavigate()

  return (
    <article className="visit-detail-recording">
      <div className="visit-detail-recording__header">
        <div>
          <strong>{formatRecordingDisplayName(recording.file_name, recording.created_at)}</strong>
          <p>
            {formatBeijingTime(recording.created_at, 'MM/DD HH:mm')} · {formatDuration(recording.duration_seconds)}
            {recording.staff_name ? ` · ${recording.staff_name}` : ''}
          </p>
        </div>
        <Space wrap>
          {recording.transcript_provider && <Tag>{recording.transcript_provider}</Tag>}
          {recording.analysis_overall_score != null && (
            <Tag color="purple">评分 {recording.analysis_overall_score.toFixed(1)}</Tag>
          )}
        </Space>
      </div>

      <p className="visit-detail-recording__excerpt">
        {recording.transcript_excerpt || '暂无对话摘要。'}
      </p>

      <div className="visit-detail-recording__actions">
        <Button
          size="small"
          icon={<AudioOutlined />}
          onClick={() => navigate(`/admin/recordings/${recording.id}`)}
        >
          录音详情
        </Button>
        <Button
          size="small"
          icon={<FileTextOutlined />}
          onClick={() => recording.transcript_id && navigate(`/admin/transcripts/${recording.transcript_id}`)}
          disabled={!recording.transcript_id}
        >
          转写详情
        </Button>
      </div>
    </article>
  )
}

export function VisitDetailModal({
  open,
  visitId,
  onClose,
}: {
  open: boolean
  visitId: string | null
  onClose: () => void
}) {
  const navigate = useNavigate()
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const transcriptItemRefs = useRef(new Map<number, HTMLElement>())
  const transcriptListRef = useRef<HTMLDivElement | null>(null)
  const [manualRecordingId, setManualRecordingId] = useState<string | null>(null)
  const [playbackMs, setPlaybackMs] = useState<number | null>(null)
  const { data, error, isLoading } = useQuery({
    queryKey: ['visit-detail', visitId],
    queryFn: () => fetchVisitDetail(visitId!),
    enabled: open && !!visitId,
  })
  const selectedRecordingId =
    (manualRecordingId && data?.recordings.some((recording) => recording.id === manualRecordingId)
      ? manualRecordingId
      : data?.latest_recording_id) ??
    data?.recordings[0]?.id ??
    null
  const { data: audioUrl, isLoading: loadingAudio } = useQuery({
    queryKey: ['visit-recording-audio', selectedRecordingId],
    queryFn: async () => {
      const blob = await fetchRecordingMediaBlob(selectedRecordingId!)
      return URL.createObjectURL(blob)
    },
    enabled: open && !!selectedRecordingId,
  })

  const analysis = (data?.latest_analysis_result ?? null) as AnalysisResult | null
  const evaluation = analysis?.consultation_evaluation
  const processEvaluation = analysis?.consultation_process_evaluation
  const processSections = (processEvaluation?.sections ?? []).map((section) => ({
    name: section.name ?? '未命名大项',
    status: section.status ?? '未涉及',
    summary: sanitizeEvaluationDimensionSummary(section.summary ?? ''),
    issues: (section.checkpoints ?? []).flatMap((checkpoint) => checkpoint.issues ?? []),
    pointScore: toFiniteNumber(section.point_score),
    maxScore: toFiniteNumber(section.max_score) ?? 1,
    score: null,
  }))
  const dimensions = processSections.length ? processSections : normalizeDimensions(evaluation?.dimensions)
  const totalScore = toFiniteNumber(processEvaluation?.total_score) ?? toFiniteNumber(evaluation?.total_score)
  const maxTotalScore = toFiniteNumber(processEvaluation?.max_total_score) ?? toFiniteNumber(evaluation?.max_total_score) ?? (processSections.length || 9)
  const demands = analysis?.customer_demands?.focus_areas ?? []
  const concerns = analysis?.customer_concerns?.items ?? []
  const profileTags = analysis?.customer_profile?.tags ?? []
  const summaryLines = data ? buildSummary(analysis, data) : []
  const visitOrderLineItems = data?.visit_order_context?.line_items ?? []
  const overallScore = totalScore != null
    ? (maxTotalScore > 0 ? (totalScore / maxTotalScore) * 10 : null)
    : toFiniteNumber(data?.latest_analysis_overall_score ?? processEvaluation?.overall_score ?? evaluation?.overall_score ?? null)
  const displayOverallScore = totalScore != null
    ? `${formatPointScore(totalScore)}/${formatPointScore(maxTotalScore)}`
    : overallScore != null
      ? overallScore.toFixed(1)
      : '--'
  const scoreMeta = scoreTone(overallScore)
  const lowScoreDimensions = dimensions.filter((item) => {
    if (item.pointScore != null) {
      return item.pointScore < (item.maxScore ?? 1)
    }
    return (item.score ?? 0) < 10
  })
  const selectedRecording =
    data?.recordings.find((recording) => recording.id === selectedRecordingId) ??
    data?.recordings[0] ??
    null
  const selectedTranscriptId = selectedRecording?.transcript_id ?? null
  const { data: transcript, isLoading: loadingTranscript } = useQuery({
    queryKey: ['visit-recording-transcript', selectedTranscriptId],
    queryFn: () => fetchTranscript(selectedTranscriptId!),
    enabled: open && !!selectedTranscriptId,
  })
  const activeUtteranceKey =
    transcript?.utterances?.find(
      (utterance) =>
        playbackMs != null && playbackMs >= utterance.begin_ms && playbackMs <= utterance.end_ms,
    )?.begin_ms ?? null
  const recordingDurationMs =
    transcript?.duration_ms ??
    (selectedRecording?.duration_seconds != null ? selectedRecording.duration_seconds * 1000 : null)
  const playbackPercent =
    recordingDurationMs && playbackMs != null
      ? Math.max(0, Math.min((playbackMs / recordingDurationMs) * 100, 100))
      : 0

  useEffect(() => {
    return () => {
      if (audioUrl) {
        URL.revokeObjectURL(audioUrl)
      }
    }
  }, [audioUrl])

  useEffect(() => {
    if (activeUtteranceKey == null) {
      return
    }
    const element = transcriptItemRefs.current.get(activeUtteranceKey)
    keepElementInScrollContainerView(transcriptListRef.current, element, {
      topPadding: 64,
      bottomPadding: 88,
    })
  }, [activeUtteranceKey])

  const jumpToUtterance = (utterance: TranscriptUtterance) => {
    const nextMs = Math.max(0, utterance.begin_ms)
    setPlaybackMs(nextMs)
    if (!audioRef.current) {
      return
    }
    audioRef.current.currentTime = nextMs / 1000
    void audioRef.current.play().catch(() => undefined)
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

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width={1580}
      destroyOnClose
      className="visit-detail-modal-shell"
    >
      {!open ? null : isLoading ? (
        <div className="visit-detail-modal__loading">
          <Spin size="large" />
        </div>
      ) : error || !data ? (
        <div className="visit-detail-modal__loading">
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="接诊详情暂时加载失败，请稍后重试。"
          />
        </div>
      ) : (
        <div className="visit-detail-modal">
          <div className="visit-detail-modal__header">
            <div>
              <p className="visit-page__eyebrow">客户中心 / 接诊记录 / 详情</p>
              <h2>接诊详情</h2>
            </div>
            <Space wrap>
              <Button onClick={() => navigate(`/admin/customers/${data.customer_id}`)} icon={<UserOutlined />}>
                客户档案
              </Button>
              <Button
                type="primary"
                icon={<FundOutlined />}
                onClick={() => data.latest_recording_id && navigate(`/admin/recordings/${data.latest_recording_id}`)}
                disabled={!data.latest_recording_id}
              >
                查看最新录音
              </Button>
            </Space>
          </div>

          <div className="visit-detail-modal__hero">
            <div className="visit-stat-chip">
              <span>到诊日期</span>
              <strong>{data.visit_date ? dayjs(data.visit_date).format('MM/DD') : '未登记'}</strong>
            </div>
            <div className="visit-stat-chip">
              <span>关联录音</span>
              <strong>{data.recording_count}</strong>
            </div>
            <div className="visit-stat-chip">
              <span>已完成转写</span>
              <strong>{data.transcript_count}</strong>
            </div>
            <div className="visit-stat-chip">
              <span>整段分析</span>
              <strong>{displayOverallScore}</strong>
            </div>
          </div>

          <div className="visit-detail-modal__grid">
            <Card bordered={false} className="visit-detail-card visit-detail-card--basic">
              <div className="visit-detail-card__title-row">
                <strong>基本信息</strong>
                <span>接诊时间：{data.visit_date ? dayjs(data.visit_date).format('YYYY-MM-DD') : '未登记'}</span>
              </div>

              <div className="visit-detail-basic__identity">
                <Avatar size={64} className="visit-card__avatar">
                  {data.customer_name.slice(0, 1) || '客'}
                </Avatar>
                <div>
                  <div className="visit-detail-basic__name-row">
                    <strong>{data.customer_name}</strong>
                    <span>{data.customer_code ? `客户编码：${data.customer_code}` : '客户编码未登记'}</span>
                  </div>
                  <div className="visit-detail-basic__tags">
                    <Tag color="blue">{data.consultant_name || '待分配顾问'}</Tag>
                    <Tag>{resolveGender(data.customer_gender)}</Tag>
                    {data.customer_age != null && <Tag>到诊年龄: {data.customer_age} 岁</Tag>}
                  </div>
                </div>
              </div>

              <div className="visit-detail-basic__facts">
                <div>
                  <span>主诊医生</span>
                  <strong>{data.doctor_name || '待分配'}</strong>
                </div>
                <div>
                  <span>接诊状态</span>
                  <strong>{data.status || '未登记'}</strong>
                </div>
                <div>
                  <span>创建时间</span>
                  <strong>{formatBeijingTime(data.created_at, 'MM/DD HH:mm')}</strong>
                </div>
                <div>
                  <span>企微ID</span>
                  <strong>{data.customer_wechat_external_uid || '未绑定'}</strong>
                </div>
              </div>

              <div className="visit-detail-basic__notes">
                <span>到院信息</span>
                <p>{data.notes || '当前还没有补充到院目的或接待备注。'}</p>
              </div>

              <VisitOrderLineItemsBlock items={visitOrderLineItems} />

              <div className="visit-detail-basic__audio">
                <div className="visit-detail-card__title-row">
                  <strong>关联录音试听</strong>
                  <span>{selectedRecording ? formatDuration(selectedRecording.duration_seconds) : '暂无录音'}</span>
                </div>

                {data.recordings.length ? (
                  <>
                    <div className="visit-detail-audio__switcher">
                      {data.recordings.map((recording) => (
                        <button
                          key={recording.id}
                          type="button"
                          className={`visit-detail-audio__pill${selectedRecordingId === recording.id ? ' visit-detail-audio__pill--active' : ''}`}
                          onClick={() => {
                            setManualRecordingId(recording.id)
                            setPlaybackMs(null)
                          }}
                        >
                          {formatRecordingDisplayName(recording.file_name, recording.created_at)}
                        </button>
                      ))}
                    </div>

                    <div className="visit-detail-audio__player">
                      {loadingAudio ? (
                        <Spin />
                      ) : audioUrl ? (
                        <audio
                          ref={audioRef}
                          controls
                          preload="metadata"
                          src={audioUrl}
                          onTimeUpdate={(event) =>
                            setPlaybackMs(Math.round(event.currentTarget.currentTime * 1000))
                          }
                          onSeeked={(event) =>
                            setPlaybackMs(Math.round(event.currentTarget.currentTime * 1000))
                          }
                          onEnded={() => setPlaybackMs(null)}
                        >
                          您的浏览器暂不支持音频播放。
                        </audio>
                      ) : (
                        <p>当前录音暂时无法播放。</p>
                      )}
                    </div>

                    <div className="visit-detail-audio__progress">
                      <div className="visit-detail-audio__progress-meta">
                        <span>播放进度</span>
                        <strong>
                          {formatMs(playbackMs ?? 0)} / {formatMs(recordingDurationMs ?? 0)}
                        </strong>
                      </div>
                      <div className="visit-detail-audio__progress-track">
                        <span style={{ width: `${playbackPercent}%` }} />
                      </div>
                    </div>

                    {selectedRecording && (
                      <div className="visit-detail-audio__meta">
                        <span>{formatRecordingDisplayName(selectedRecording.file_name, selectedRecording.created_at)}</span>
                        <Button
                          size="small"
                          type="link"
                          icon={<AudioOutlined />}
                          onClick={() => navigate(`/admin/recordings/${selectedRecording.id}`)}
                        >
                          去录音详情
                        </Button>
                      </div>
                    )}
                  </>
                ) : (
                  <p className="visit-detail-card__empty-text">这次接诊还没有关联录音。</p>
                )}
              </div>
            </Card>

            <Card bordered={false} className="visit-detail-card visit-detail-card--summary">
              <div className="visit-detail-card__title-row">
                <strong>AI总结</strong>
                {analysis?.customer_demands?.expectation?.dialogue_type && (
                  <Tag color="purple">{analysis.customer_demands.expectation.dialogue_type}</Tag>
                )}
              </div>

              {summaryLines.length ? (
                <div className="visit-detail-summary">
                  {summaryLines.map((line) => (
                    <p key={line}>{line}</p>
                  ))}
                </div>
              ) : (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description="还没有整段分析结果，可以先在录音详情页发起分析。"
                />
              )}

              <div className="visit-detail-summary__section">
                <strong>客户诉求</strong>
                {demands.length ? (
                  <div className="visit-detail-bullet-list">
                    {demands.slice(0, 5).map((item, index) => (
                      <div key={`${item.area}-${index}`} className="visit-detail-bullet">
                        <span className="visit-detail-bullet__index">{index + 1}</span>
                        <div>
                          <strong>{item.area}</strong>
                          <p>{item.surface_need || item.deep_need || '暂无更细描述'}</p>
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="visit-detail-card__empty-text">暂无客户诉求提取。</p>
                )}
              </div>

              <div className="visit-detail-summary__section">
                <strong>主要顾虑</strong>
                {concerns.length ? (
                  <div className="visit-detail-chip-list">
                    {concerns.slice(0, 6).map((item, index) => (
                      <Tag key={`${item.type ?? 'concern'}-${index}`} color="gold">
                        {item.type ? `${item.type}：` : ''}
                        {item.content || item.evidence || '未提取内容'}
                      </Tag>
                    ))}
                  </div>
                ) : (
                  <p className="visit-detail-card__empty-text">暂无顾虑提取。</p>
                )}
              </div>
            </Card>

            <Card bordered={false} className="visit-detail-card visit-detail-card--score">
              <div className="visit-detail-card__title-row">
                <strong>接诊评价</strong>
              </div>

              <div className="visit-detail-score">
                <Progress
                  type="dashboard"
                  percent={totalScore != null
                    ? Math.round(Math.min((totalScore / maxTotalScore) * 100, 100))
                    : overallScore != null
                      ? Math.round(Math.min(overallScore * 10, 100))
                      : 0}
                  width={186}
                  strokeColor={scoreMeta.color}
                  trailColor="#e5e7eb"
                  format={() => displayOverallScore}
                />
                <span className={`visit-card__score-tag visit-card__score-tag--${scoreMeta.tone}`}>
                  {scoreMeta.label}
                </span>
              </div>

              <div className="visit-detail-score__summary">
                {evaluation?.overall_summary ? <p>{sanitizeEvaluationSummary(evaluation.overall_summary)}</p> : null}
                {dimensions.length ? (
                  <p>
                    共提取 {dimensions.length} 个质检维度，
                    {lowScoreDimensions.length
                      ? `当前需要优先补强 ${lowScoreDimensions
                          .slice(0, 3)
                          .map((item) => item.name)
                          .join('、')}。`
                      : '当前整体表现比较稳定。'}
                  </p>
                ) : (
                  <p>当前还没有可展示的质检维度。</p>
                )}
              </div>

              <div className="visit-detail-score__insights">
                <div>
                  <span>客户洞察</span>
                  <strong>{demands.length}</strong>
                </div>
                <div>
                  <span>画像标签</span>
                  <strong>{profileTags.length}</strong>
                </div>
                <div>
                  <span>风险提醒</span>
                  <strong>{lowScoreDimensions.length}</strong>
                </div>
              </div>
            </Card>

            <Card bordered={false} className="visit-detail-card visit-detail-card--tags">
              <div className="visit-detail-card__title-row">
                <strong>需求标签</strong>
                {analysis?.source && <Tag>{analysis.source}</Tag>}
              </div>

              <div className="visit-detail-tag-section">
                <span>客户画像</span>
                {profileTags.length ? (
                  <div className="visit-detail-chip-list">
                    {profileTags.map((item, index) => (
                      <Tag key={`${item.category ?? 'profile'}-${item.value ?? index}-${index}`} color="blue">
                        {item.category ? `${item.category}：` : ''}
                        {item.value || '未标记'}
                      </Tag>
                    ))}
                  </div>
                ) : (
                  <p className="visit-detail-card__empty-text">暂无画像标签。</p>
                )}
              </div>

              <div className="visit-detail-tag-section">
                <span>转写摘要</span>
                <p>{data.latest_transcript_excerpt || '这次接诊还没有可用的转写摘要。'}</p>
              </div>
            </Card>

            <Card bordered={false} className="visit-detail-card visit-detail-card--execution">
              <div className="visit-detail-card__title-row">
                <strong>流程执行</strong>
                <span>{dimensions.length ? `${dimensions.length} 个维度` : '暂无数据'}</span>
              </div>

              {dimensions.length ? (
                <div className="visit-detail-execution-list">
                  {dimensions.map((item) => {
                    const score = item.score ?? 0
                    const percent = item.pointScore != null
                      ? Math.max(0, Math.min(Math.round((item.pointScore / (item.maxScore ?? 1)) * 100), 100))
                      : Math.max(0, Math.min(Math.round(score * 10), 100))
                    return (
                      <div key={item.name} className="visit-detail-execution-row">
                        <div className="visit-detail-execution-row__label">
                          <span>{item.name}</span>
                          <strong>
                            {item.pointScore != null
                              ? `${formatPointScore(item.pointScore)}/${formatPointScore(item.maxScore ?? 1)}`
                              : `${percent}%`}
                          </strong>
                        </div>
                        <Progress
                          percent={percent}
                          showInfo={false}
                          strokeColor={score >= 7 ? '#8b5cf6' : score >= 5 ? '#f59e0b' : '#fb7185'}
                          trailColor="#ede9fe"
                          size="small"
                        />
                        {item.summary && <p>{sanitizeEvaluationDimensionSummary(item.summary)}</p>}
                        {!item.summary && ('comment' in item) && item.comment && <p>{sanitizeEvaluationDimensionSummary(item.comment)}</p>}
                        {item.issues?.length ? (
                          <p>{item.issues[0]?.description}{item.issues[0]?.evidence ? `：${item.issues[0].evidence}` : ''}</p>
                        ) : null}
                      </div>
                    )
                  })}
                </div>
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无流程执行数据" />
              )}
            </Card>

            <Card bordered={false} className="visit-detail-card visit-detail-card--transcript">
              <div className="visit-detail-card__title-row">
                <strong>逐句转写时间轴</strong>
                <span>
                  {selectedRecording
                    ? `${formatRecordingDisplayName(selectedRecording.file_name, selectedRecording.created_at)}${transcript?.utterances?.length ? ` · ${transcript.utterances.length} 句` : ''}`
                    : '暂无转写'}
                </span>
              </div>

              {loadingTranscript ? (
                <div className="visit-detail-transcript__loading">
                  <Spin />
                </div>
              ) : transcript?.utterances?.length ? (
                <>
                  <div className="visit-detail-transcript__summary">
                    {transcript.asr_provider && <Tag>{transcript.asr_provider}</Tag>}
                    {transcript.duration_ms != null && <Tag>{formatMs(transcript.duration_ms)}</Tag>}
                  </div>

                  <div ref={transcriptListRef} className="visit-detail-transcript-list">
                    {transcript.utterances.map((utterance: TranscriptUtterance, index: number) => {
                      const isRight = utterance.speaker === 'customer' || utterance.speaker === '客户'
                      return (
                      <article
                        key={`${utterance.begin_ms}-${utterance.end_ms}-${index}`}
                        ref={(element) => {
                          if (element) {
                            transcriptItemRefs.current.set(utterance.begin_ms, element)
                          } else {
                            transcriptItemRefs.current.delete(utterance.begin_ms)
                          }
                        }}
                        className={`visit-transcript-item${isRight ? ' visit-transcript-item--right' : ''}${audioUrl ? ' visit-transcript-item--interactive' : ''}${activeUtteranceKey === utterance.begin_ms ? ' visit-transcript-item--active' : ''}`}
                        onClick={() => {
                          if (audioUrl) {
                            jumpToUtterance(utterance)
                          }
                        }}
                      >
                        {activeUtteranceKey === utterance.begin_ms && (
                          <div className="visit-transcript-item__progress">
                            <span style={{ width: `${getUtteranceProgress(utterance)}%` }} />
                          </div>
                        )}
                        <div className="visit-transcript-item__time">
                          <strong>{formatMs(utterance.begin_ms)}</strong>
                          <span>{formatMs(utterance.end_ms)}</span>
                        </div>
                        <div className="visit-transcript-item__body">
                          <span
                            className={`visit-transcript-item__speaker ${resolveSpeakerTone(utterance.speaker)}`}
                          >
                            {resolveSpeakerLabel(utterance.speaker)}
                          </span>
                          <p>{utterance.text}</p>
                        </div>
                      </article>
                      )
                    })}
                  </div>
                </>
              ) : (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description="当前录音还没有可展示的逐句转写。"
                />
              )}
            </Card>

            <Card bordered={false} className="visit-detail-card visit-detail-card--recordings">
              <div className="visit-detail-card__title-row">
                <strong>关联录音</strong>
                <span>{data.recordings.length} 条</span>
              </div>

              {data.recordings.length ? (
                <div className="visit-detail-recording-list">
                  {data.recordings.map((recording) => (
                    <RecordingItem key={recording.id} recording={recording} />
                  ))}
                </div>
              ) : (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description="这次接诊还没有关联录音，可以先去录音管理页关联。"
                />
              )}
            </Card>
          </div>
        </div>
      )}
    </Modal>
  )
}

export default VisitDetailModal
