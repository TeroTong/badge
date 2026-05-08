import { useQuery } from '@tanstack/react-query'
import { ArrowLeftOutlined } from '@ant-design/icons'
import { Button, Card, Descriptions, Empty, Spin, Tag } from 'antd'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { fetchTranscript } from '@/api/transcripts'
import { formatRecordingDisplayName } from '@/utils/recording-display'
import { formatBeijingTime } from '@/utils/time'

const SPEAKER_MAP: Record<string, { label: string; color: string }> = {
  consultant: { label: '咨询师', color: 'blue' },
  doctor: { label: '医生', color: 'purple' },
  customer: { label: '客户', color: 'green' },
  unknown: { label: '未知', color: 'default' },
}

function formatMs(ms: number): string {
  const totalSeconds = Math.floor(ms / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${minutes}:${seconds.toString().padStart(2, '0')}`
}

export function TranscriptDetailPage() {
  const { transcriptId } = useParams<{ transcriptId: string }>()
  const navigate = useNavigate()

  const { data, isLoading } = useQuery({
    queryKey: ['transcript', transcriptId],
    queryFn: () => fetchTranscript(transcriptId!),
    enabled: !!transcriptId,
  })

  if (isLoading || !data) {
    return <Spin style={{ display: 'block', margin: '80px auto' }} size="large" />
  }

  return (
    <div style={{ padding: 24 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <div>
          <Link to="/admin/transcripts" style={{ display: 'inline-flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
            <ArrowLeftOutlined />
            返回转写列表
          </Link>
          <h2 style={{ margin: 0 }}>转写详情</h2>
        </div>
        <Button onClick={() => navigate(`/admin/recordings/${data.recording_id}`)}>查看录音详情</Button>
      </div>

      <Card size="small" style={{ marginBottom: 16 }}>
        <Descriptions column={{ xs: 1, sm: 2, md: 3 }} size="small">
          <Descriptions.Item label="录音文件">
            {data.recording_file_name ? formatRecordingDisplayName(data.recording_file_name, data.created_at) : data.recording_id}
          </Descriptions.Item>
          <Descriptions.Item label="来源">
            <Tag color={data.asr_provider === 'manual' ? 'gold' : 'blue'}>{data.asr_provider}</Tag>
          </Descriptions.Item>
          <Descriptions.Item label="创建时间">{formatBeijingTime(data.created_at, 'YYYY-MM-DD HH:mm:ss')}</Descriptions.Item>
          <Descriptions.Item label="完成时间">
            {data.completed_at ? formatBeijingTime(data.completed_at, 'YYYY-MM-DD HH:mm:ss') : '-'}
          </Descriptions.Item>
          <Descriptions.Item label="时长">
            {data.duration_ms != null ? `${(data.duration_ms / 1000).toFixed(1)}s` : '-'}
          </Descriptions.Item>
        </Descriptions>
      </Card>

      <Card size="small" title={`逐句对话 (${data.utterances?.length ?? 0})`}>
        {data.utterances?.length ? (
          <div className="chat-bubble-list" style={{ maxHeight: 420, overflow: 'auto', padding: '8px 0' }}>
            {data.utterances.map((utterance, index) => {
              const isRight = utterance.speaker === 'customer' || utterance.speaker === '客户'
              const speakerLabel = SPEAKER_MAP[utterance.speaker]?.label ?? utterance.speaker
              const speakerColor = SPEAKER_MAP[utterance.speaker]?.color ?? 'default'
              return (
                <div
                  key={index}
                  className={`chat-bubble-row${isRight ? ' chat-bubble-row--right' : ' chat-bubble-row--left'}`}
                >
                  <div className="chat-bubble">
                    <div className="chat-bubble__header">
                      <Tag color={speakerColor} style={{ fontSize: 11 }}>{speakerLabel}</Tag>
                      <span className="chat-bubble__time">{formatMs(utterance.begin_ms)}</span>
                    </div>
                    <div className="chat-bubble__text">{utterance.text}</div>
                  </div>
                </div>
              )
            })}
          </div>
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无逐句数据" />
        )}
      </Card>
    </div>
  )
}

export default TranscriptDetailPage
