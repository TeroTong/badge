import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, Navigate, useParams } from 'react-router-dom'
import {
  AudioOutlined,
  ArrowLeftOutlined,
  ReloadOutlined,
  UserOutlined,
  IdcardOutlined,
  ClockCircleOutlined,
  FileTextOutlined,
  DownOutlined,
  UpOutlined,
} from '@ant-design/icons'
import {
  Alert,
  Button,
  Empty,
  Spin,
  Tag,
  Tooltip,
  Typography,
} from 'antd'

import {
  fetchArchiveRecordingDetail,
  fetchArchiveRecordingMediaBlob,
} from '@/api/archive-recordings'
import { extractRecordingIdFromAnalysisFileId } from '@/api/analysis'
import { AnalysisDetailContent } from '@/components/analysis-detail-content'
import {
  TranscriptPlaybackPanel,
  type TranscriptUtteranceLite,
} from '@/components/transcript-playback-panel'
import { buildArchiveAnalysisDetail } from '@/pages/admin/dingtalk-audio-analysis-utils'
import { formatRecordingDisplayName } from '@/utils/recording-display'
import { formatBeijingTime } from '@/utils/time'

const { Text } = Typography

function formatDateTime(value?: string | null): string {
  return formatBeijingTime(value, 'YYYY-MM-DD HH:mm:ss')
}

function statusTagMeta(status?: string | null) {
  switch (status) {
    case 'analyzed':
      return { color: 'success', label: '已分析' }
    case 'analyzing':
      return { color: 'processing', label: '分析中' }
    case 'transcribed':
      return { color: 'blue', label: '已转写' }
    case 'failed':
      return { color: 'error', label: '失败' }
    default:
      return { color: 'default', label: status || '未知状态' }
  }
}

function extractUtterances(transcript: unknown): TranscriptUtteranceLite[] {
  if (!transcript || typeof transcript !== 'object') return []
  const obj = transcript as Record<string, unknown>
  const list = obj.utterances ?? obj.segments ?? obj.items
  if (!Array.isArray(list)) return []
  return list
    .map((raw): TranscriptUtteranceLite | null => {
      if (!raw || typeof raw !== 'object') return null
      const r = raw as Record<string, unknown>
      const beginCandidates = [r.begin_ms, r.beginMs, r.start_ms, r.startMs, r.begin_time, r.beginTime]
      const endCandidates = [r.end_ms, r.endMs, r.stop_ms, r.stopMs, r.end_time, r.endTime]
      const beginMs = beginCandidates.find((v) => typeof v === 'number')
      const endMs = endCandidates.find((v) => typeof v === 'number')
      return {
        speaker: typeof r.speaker === 'string' ? r.speaker : (r.speaker_id != null ? String(r.speaker_id) : null),
        speaker_role: typeof r.speaker_role === 'string' ? r.speaker_role : null,
        speaker_business_role: typeof r.speaker_business_role === 'string' ? r.speaker_business_role : null,
        speaker_display_label: typeof r.speaker_display_label === 'string' ? r.speaker_display_label : null,
        speaker_staff_name: typeof r.speaker_staff_name === 'string' ? r.speaker_staff_name : null,
        speaker_identity_type: typeof r.speaker_identity_type === 'string' ? r.speaker_identity_type : null,
        speaker_id: r.speaker_id != null ? String(r.speaker_id) : null,
        text: typeof r.text === 'string' ? r.text : (typeof r.content === 'string' ? r.content : ''),
        begin_ms: typeof beginMs === 'number' ? beginMs : null,
        end_ms: typeof endMs === 'number' ? endMs : null,
      }
    })
    .filter((item): item is TranscriptUtteranceLite => Boolean(item))
}

interface MetaItemProps {
  icon: React.ReactNode
  label: string
  value: React.ReactNode
  copyable?: boolean
  ellipsis?: boolean
}

function MetaItem({ icon, label, value, copyable, ellipsis }: MetaItemProps) {
  const displayValue = value === null || value === undefined || value === '' ? '-' : value
  const valueNode =
    ellipsis && typeof displayValue === 'string' ? (
      <Tooltip title={displayValue} placement="topLeft">
        <Text
          style={{ maxWidth: '100%' }}
          ellipsis
          copyable={copyable ? { tooltips: ['复制', '已复制'] } : false}
        >
          {displayValue}
        </Text>
      </Tooltip>
    ) : copyable && typeof displayValue === 'string' ? (
      <Text copyable={{ tooltips: ['复制', '已复制'] }}>{displayValue}</Text>
    ) : (
      <span>{displayValue}</span>
    )
  return (
    <div className="ad-meta-item">
      <span className="ad-meta-item__icon">{icon}</span>
      <div className="ad-meta-item__body">
        <span className="ad-meta-item__label">{label}</span>
        <span className="ad-meta-item__value">{valueNode}</span>
      </div>
    </div>
  )
}

export default function DingtalkAudioAnalysisDetailPage() {
  const { itemId, fileId } = useParams<{ itemId?: string; fileId?: string }>()
  const resolvedId = itemId || fileId || null
  const legacyRecordingId = resolvedId ? extractRecordingIdFromAnalysisFileId(resolvedId) : null
  const shouldRedirectToRecording = Boolean(legacyRecordingId)
  const queryEnabled = Boolean(resolvedId) && !shouldRedirectToRecording

  const audioRef = useRef<HTMLAudioElement | null>(null)
  const [playbackMs, setPlaybackMs] = useState<number | null>(null)
  const [transcriptOpen, setTranscriptOpen] = useState<boolean>(false)

  const {
    data,
    isLoading,
    isFetching,
    error,
    refetch,
  } = useQuery({
    queryKey: ['dingtalk-audio-analysis-detail-page', resolvedId],
    queryFn: () => fetchArchiveRecordingDetail(resolvedId!),
    enabled: queryEnabled,
  })

  const {
    data: audioBlob,
    isFetching: audioLoading,
  } = useQuery({
    queryKey: ['dingtalk-audio-analysis-detail-media', resolvedId],
    queryFn: () => fetchArchiveRecordingMediaBlob(resolvedId!),
    enabled: queryEnabled,
    retry: false,
  })

  const audioUrl = useMemo(() => (audioBlob ? URL.createObjectURL(audioBlob) : null), [audioBlob])
  const analysisDetail = useMemo(() => buildArchiveAnalysisDetail(data), [data])
  const tagMeta = statusTagMeta(data?.pipeline_status)
  const utterances = useMemo(() => extractUtterances(data?.transcript), [data?.transcript])
  const displayName = useMemo(
    () => formatRecordingDisplayName(data?.display_file_name, data?.create_time),
    [data?.display_file_name, data?.create_time],
  )

  useEffect(() => {
    if (!audioUrl) return
    return () => URL.revokeObjectURL(audioUrl)
  }, [audioUrl])

  const handleSeek = (ms: number) => {
    const audio = audioRef.current
    if (!audio) return
    audio.currentTime = Math.max(0, ms / 1000)
    void audio.play().catch(() => {})
  }

  if (legacyRecordingId) {
    return <Navigate replace to={`/admin/recordings/${legacyRecordingId}?from=llm`} />
  }

  return (
    <section className="module-page ad-detail-page">
      <header className="module-page__header">
        <div>
          <p className="eyebrow">录音复盘</p>
          <h1>分析结果详情</h1>
          <p className="module-page__subtitle">
            查看单条录音的 LLM 结构化分析输出。
          </p>
        </div>
        <div className="module-page__actions">
          <Link to="/admin/llm-results" className="sort-btn">
            <ArrowLeftOutlined /> 返回列表
          </Link>
          <Button
            icon={<ReloadOutlined />}
            loading={isFetching}
            onClick={() => refetch()}
          >
            刷新
          </Button>
        </div>
      </header>

      {isLoading ? <Spin size="large" style={{ display: 'block', margin: '64px auto' }} /> : null}
      {error ? <Alert type="error" showIcon message="详情加载失败" description={String(error)} /> : null}

      {!isLoading && !error && data ? (
        <div className="ad-detail-layout">
          <div className="ad-detail-summary-card">
            <div className="ad-detail-summary-card__head">
              <div className="ad-detail-summary-card__title">
                <Text strong ellipsis style={{ fontSize: 16 }}>
                  {displayName}
                </Text>
                <Tag color={tagMeta.color} style={{ marginLeft: 8 }}>
                  {tagMeta.label}
                </Tag>
              </div>
            </div>
            <div className="ad-detail-summary-card__body">
              <div className="ad-meta-grid">
                <MetaItem icon={<UserOutlined />} label="员工" value={data.staff_name || '未绑定员工'} />
                <MetaItem icon={<IdcardOutlined />} label="工牌" value={data.sn || data.device_code} />
                <MetaItem icon={<ClockCircleOutlined />} label="录音时间" value={formatDateTime(data.create_time)} />
                
                
                <MetaItem icon={<FileTextOutlined />} label="文件 ID" value={data.file_id} copyable ellipsis />
              </div>
              <div className="ad-detail-audio-bar">
                <div className="ad-detail-audio-bar__icon">
                  <AudioOutlined style={{ color: '#1677ff' }} />
                </div>
                <div className="ad-detail-audio-bar__player">
                  {audioUrl ? (
                    <audio
                      ref={audioRef}
                      controls
                      src={audioUrl}
                      className="ad-detail-audio__player"
                      onTimeUpdate={(e) => {
                        const t = (e.currentTarget as HTMLAudioElement).currentTime
                        setPlaybackMs(Math.round(t * 1000))
                      }}
                    />
                  ) : audioLoading ? (
                    <Spin size="small" />
                  ) : (
                    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前没有可播放音频" style={{ margin: 0 }} />
                  )}
                </div>
                <Button
                  type={transcriptOpen ? 'primary' : 'default'}
                  size="small"
                  icon={transcriptOpen ? <UpOutlined /> : <DownOutlined />}
                  onClick={() => setTranscriptOpen((v) => !v)}
                  disabled={utterances.length === 0}
                >
                  {transcriptOpen ? '收起原文' : `查看转写原文${utterances.length ? ` (${utterances.length})` : ''}`}
                </Button>
              </div>
              <TranscriptPlaybackPanel
                utterances={utterances}
                playbackMs={playbackMs}
                onSeek={handleSeek}
                open={transcriptOpen}
              />
            </div>
          </div>

          {analysisDetail ? (
            <AnalysisDetailContent
              data={analysisDetail}
              recordingId={null}
              recordingLinkBase={null}
              showHeader={false}
            />
          ) : (
            <Empty description="当前录音还没有可展示的分析结果" />
          )}
        </div>
      ) : null}
    </section>
  )
}
