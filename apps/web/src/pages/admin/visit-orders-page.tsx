import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, DatePicker, Descriptions, Input, message, Modal, Table, Tag, type TableProps } from 'antd'
import { DownloadOutlined, SyncOutlined } from '@ant-design/icons'

import type { RecordingMatchCandidate, VisitOrder, VisitOrderRecordingMatch } from '@/api/admin'
import * as adminApi from '@/api/admin'
import { getApiErrorMessage } from '@/api/errors'
import * as recordingsApi from '@/api/recordings'
import { getDisplayMatchEvidenceLines } from '@/utils/match-evidence'
import { formatRecordingDisplayName } from '@/utils/recording-display'
import { buildRecordingVisitLinkRiskLines } from '@/utils/recording-visit-link-confirmation'
import { beijingNow, formatBeijingTime } from '@/utils/time'

const PAGE_SIZE = 20

function VisitOrderInfoRow({
  label,
  value,
  emphasis = false,
}: {
  label: string
  value: string
  emphasis?: boolean
}) {
  return (
    <span className={`visit-order-cell__sub${emphasis ? ' is-emphasis' : ''}`}>
      <span className="visit-order-cell__label">{label}</span>
      <span className="visit-order-cell__value">{value}</span>
    </span>
  )
}

function exportVisitOrders(rows: VisitOrder[]) {
  const headers = [
    '到诊单号', '行项目', '数据日期', '机构编码',
    '客户编码', '客户姓名', '预约单号', '预约医生编码', '会员星级',
    '客户性别', '客户类型', '30天类型', '会员状态',
    '分诊顾问编码', '分诊顾问姓名', '现场咨询编码', '现场咨询姓名',
    '客服编码', '当前顾问编码', '当前客服编码',
    '分诊单号', '分诊时间', '分诊状态', '等待时长', '补划扣', '顾问助理',
    '机构科室',
    '到诊类型', '到诊状态', '成交状态',
    '到院目的', '到诊来源', '到诊需求',
    '创建日期', '创建时间',
  ]
  const lines = rows.map((r) => [
    r.dzdh, r.dzseg ?? '', r.sjrq ?? '', r.jgbm ?? '',
    r.kunr ?? '', r.ninam ?? '', r.yydh ?? '', r.yyuer ?? '', r.kulvl_dq ?? '',
    r.kusex_txt ?? '', r.kutyp_dq_txt ?? '', r.kut30_dq_txt ?? '', r.kusta_dq_txt ?? '',
    r.fzuer ?? '', r.fzuer_long ?? '', r.advxc ?? '', r.advxc_long ?? '',
    r.vipkf ?? '', r.d_fzuer ?? '', r.d_vipkf ?? '',
    r.fzdh ?? '', r.fzsj ?? '', r.fzsta_txt ?? '', r.ddsc ?? '', r.bhkx ?? '', r.assxc ?? '',
    r.jgks_txt ?? r.jgks ?? '',
    r.dztyp_txt ?? '', r.dzsta_txt ?? '', r.jcsta_txt ?? '',
    r.dymd_txt ?? '', r.dzly_txt ?? '', r.remark_dz ?? '',
    r.crtdt ?? '', r.crttm ?? '',
  ])

  const csv = [headers, ...lines]
    .map((line) => line.map((item) => `"${String(item).replaceAll('"', '""')}"`).join(','))
    .join('\n')

  const blob = new Blob([`\uFEFF${csv}`], { type: 'text/csv;charset=utf-8;' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `visit-orders-${beijingNow().format('YYYYMMDD')}.csv`
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

function getArrivalStatusTag(value: string | null) {
  if (!value) return <Tag>未知</Tag>
  if (value.includes('分诊')) return <Tag color="blue">{value}</Tag>
  if (value.includes('到诊')) return <Tag color="processing">{value}</Tag>
  return <Tag>{value}</Tag>
}

function getDealStatusTag(value: string | null) {
  if (!value) return <Tag>未知</Tag>
  if (value.includes('已成交')) return <Tag color="success">{value}</Tag>
  if (value.includes('未成交')) return <Tag color="error">{value}</Tag>
  return <Tag>{value}</Tag>
}

function shouldCollapseRecordingCandidates(candidates: RecordingMatchCandidate[]) {
  if (candidates.length < 2) return false

  const top1 = candidates[0]
  const top2 = candidates[1]
  const hasStrongDecision = top1.decision === 'auto' || top1.decision === 'recommend'
  const hasHighConfidence = top1.confidence >= 0.9
  const hasClearGap = top1.confidence - top2.confidence >= 0.2

  return hasStrongDecision && hasHighConfidence && hasClearGap
}

function getVisibleRecordingCandidates(candidates: RecordingMatchCandidate[]) {
  if (!candidates.length) return []
  if (shouldCollapseRecordingCandidates(candidates)) {
    return candidates.slice(0, 1)
  }
  return candidates
}

async function confirmRecordingVisitLinkRisk({
  currentRecordingOtherVisitLabel,
  targetLinkedRecordingCount,
}: {
  currentRecordingOtherVisitLabel?: string | null
  targetLinkedRecordingCount?: number
}) {
  const lines = buildRecordingVisitLinkRiskLines({
    currentRecordingOtherVisitLabel,
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

function renderEvidenceBlock(candidate: RecordingMatchCandidate) {
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

export default function VisitOrdersPage() {
  const queryClient = useQueryClient()
  const [page, setPage] = useState(1)
  const [keyword, setKeyword] = useState('')
  const [fzuer, setFzuer] = useState('')
  const [dateRange, setDateRange] = useState<[string, string] | null>(null)
  const [matchOpen, setMatchOpen] = useState(false)
  const [matchingVisitOrder, setMatchingVisitOrder] = useState<VisitOrder | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['visit-orders', page, keyword, fzuer, dateRange],
    queryFn: () =>
      adminApi.fetchVisitOrders({
        page,
        page_size: PAGE_SIZE,
        keyword: keyword || undefined,
        fzuer: fzuer || undefined,
        sjrq_start: dateRange?.[0] || undefined,
        sjrq_end: dateRange?.[1] || undefined,
      }),
    placeholderData: (previousData) => previousData,
    staleTime: 30_000,
  })

  const syncMutation = useMutation({
    mutationFn: adminApi.syncVisitOrders,
    onSuccess: (result) => {
      message.success(
        `同步完成：${result.date_range}，共 ${result.synced_count} 条，新增 ${result.new_count}，更新 ${result.updated_count}`,
      )
      queryClient.invalidateQueries({ queryKey: ['visit-orders'] })
    },
    onError: async (err) => {
      message.error(await getApiErrorMessage(err, '同步失败'))
    },
  })

  const { data: matchData, isLoading: isMatchLoading } = useQuery<VisitOrderRecordingMatch>({
    queryKey: ['visit-order-recording-match', matchingVisitOrder?.id],
    queryFn: () => adminApi.fetchVisitOrderRecordingMatch(matchingVisitOrder!.id),
    enabled: matchOpen && Boolean(matchingVisitOrder?.id),
  })

  const visibleMatchCandidates = getVisibleRecordingCandidates(matchData?.candidates ?? [])

  const adoptMatchMutation = useMutation({
    mutationFn: ({ recordingId, visitId }: { recordingId: string; visitId: string }) =>
      recordingsApi.updateRecording(recordingId, { visit_id: visitId }),
    onSuccess: async () => {
      message.success('录音已绑定到该到诊单对应的接诊记录')
      await queryClient.invalidateQueries({ queryKey: ['recordings'] })
      await queryClient.invalidateQueries({ queryKey: ['visits'] })
      await queryClient.invalidateQueries({ queryKey: ['visit-orders'] })
      if (matchingVisitOrder) {
        await queryClient.invalidateQueries({ queryKey: ['visit-order-recording-match', matchingVisitOrder.id] })
      }
    },
    onError: async (error) => {
      message.error(await getApiErrorMessage(error, '采用推荐失败'))
    },
  })

  const handleAdoptRecordingMatch = async (row: RecordingMatchCandidate) => {
    if (!matchData?.local_visit_id) return
    const currentVisitLabel = row.current_visit_id && row.current_visit_id !== matchData.local_visit_id
      ? `${row.current_visit_order_no || '其他到诊单'}${row.current_visit_order_seg ? `-${row.current_visit_order_seg}` : ''}`
      : null
    const existingOtherRecordingCount = matchData.linked_recording_ids
      .filter((recordingId) => recordingId !== row.recording_id)
      .length
    const confirmed = await confirmRecordingVisitLinkRisk({
      currentRecordingOtherVisitLabel: currentVisitLabel,
      targetLinkedRecordingCount: existingOtherRecordingCount,
    })
    if (!confirmed) return
    adoptMatchMutation.mutate({ recordingId: row.recording_id, visitId: matchData.local_visit_id })
  }

  const fmtTime = (value: string | null) => {
    const rawValue = value?.trim()
    if (!rawValue || rawValue === '000000' || rawValue === '00:00:00') return '-'

    const colonTimeMatch = rawValue.match(/^(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?/)
    if (colonTimeMatch) {
      const [, hour, minute, second] = colonTimeMatch
      return second
        ? `${hour.padStart(2, '0')}:${minute.padStart(2, '0')}:${second.padStart(2, '0')}`
        : `${hour.padStart(2, '0')}:${minute.padStart(2, '0')}`
    }

    const digits = rawValue.replace(/\D/g, '')
    if (digits.length >= 6) return `${digits.slice(0, 2)}:${digits.slice(2, 4)}:${digits.slice(4, 6)}`
    if (digits.length >= 4) return `${digits.slice(0, 2)}:${digits.slice(2, 4)}`
    return rawValue
  }

  const columns: TableProps<VisitOrder>['columns'] = [
    {
      title: '单号信息',
      width: 170,
      fixed: 'left',
      render: (_value, row) => (
        <div className="visit-order-cell">
          <strong className="visit-order-cell__title" title={`${row.dzdh}${row.dzseg ? `-${row.dzseg}` : ''}`}>
            {row.dzdh}{row.dzseg ? `-${row.dzseg}` : ''}
          </strong>
          <VisitOrderInfoRow label="数据日期" value={row.sjrq || row.crtdt || '-'} emphasis />
          <VisitOrderInfoRow label="机构编码" value={row.jgbm || '-'} />
        </div>
      ),
    },
    {
      title: '客户信息',
      width: 156,
      render: (_value, row) => (
        <div className="visit-order-cell">
          <strong className="visit-order-cell__title" title={row.ninam || '-'}>{row.ninam || '-'}</strong>
          <VisitOrderInfoRow label="客户编码" value={row.kunr || '-'} />
          <VisitOrderInfoRow label="客户分层" value={row.kutyp_dq_txt || row.kut30_dq_txt || row.kusta_dq_txt || '-'} />
        </div>
      ),
    },
    {
      title: '到诊概况',
      width: 196,
      render: (_value, row) => (
        <div className="visit-order-cell">
          <div className="visit-order-cell__tags">
            {getArrivalStatusTag(row.dzsta_txt)}
            {getDealStatusTag(row.jcsta_txt)}
          </div>
          <VisitOrderInfoRow label="到诊来源" value={row.dzly_txt || '-'} />
          <VisitOrderInfoRow label="到院目的" value={row.dymd_txt || '-'} />
          <span className="visit-order-cell__desc" title={row.remark_dz || '暂无到诊需求描述'}>
            {row.remark_dz || '暂无到诊需求描述'}
          </span>
        </div>
      ),
    },
    {
      title: '顾问信息',
      width: 168,
      render: (_value, row) => (
        <div className="visit-order-cell">
          <VisitOrderInfoRow label="美学顾问" value={row.fzuer_long || row.fzuer || '-'} emphasis />
          <VisitOrderInfoRow label="现场咨询" value={row.advxc_long || row.advxc || '-'} />
          <VisitOrderInfoRow label="客服" value={row.vipkf || row.d_vipkf || '-'} />
        </div>
      ),
    },
    {
      title: '时间轴',
      width: 138,
      render: (_value, row) => (
        <div className="visit-order-cell">
          <VisitOrderInfoRow label="分诊" value={fmtTime(row.fzsj)} emphasis />
          <VisitOrderInfoRow label="创建" value={`${row.crtdt || '-'} ${fmtTime(row.crttm)}`} />
        </div>
      ),
    },
    {
      title: '补充信息',
      width: 166,
      render: (_value, row) => (
        <div className="visit-order-cell">
          <VisitOrderInfoRow label="机构科室" value={row.jgks_txt || row.jgks || '-'} />
          <VisitOrderInfoRow label="星级 / 会员" value={`${row.kulvl_dq || '-'} / ${row.kusta_dq_txt || '-'}`} />
        </div>
      ),
    },
    {
      title: '匹配录音',
      width: 100,
      fixed: 'right',
      render: (_value: unknown, row: VisitOrder) => (
        <Button className="visit-orders-table__action" size="small" onClick={() => { setMatchingVisitOrder(row); setMatchOpen(true) }}>
          查看建议
        </Button>
      ),
    },
  ]

  return (
    <div className="operation-page visit-orders-page">
      <div className="operation-page__header">
        <div className="operation-page__title">
          <span className="operation-page__marker" aria-hidden="true" />
          <div>
            <h1>到诊单据</h1>
            <p>把高频核对信息收敛到一屏内，优先看单号、客户、顾问、机构和状态，再决定是否进入录音匹配。</p>
          </div>
        </div>
      </div>

      <div className="operation-card visit-orders-toolbar">
        <div className="visit-orders-toolbar__filters">
          <Input.Search
            placeholder="搜索单号 / 客户 / 顾问"
            allowClear
            className="visit-orders-toolbar__search"
            onSearch={(v) => { setKeyword(v); setPage(1) }}
          />
          <Input
            placeholder="顾问编号"
            allowClear
            className="visit-orders-toolbar__advisor"
            value={fzuer}
            onChange={(e) => { setFzuer(e.target.value); setPage(1) }}
          />
          <DatePicker.RangePicker
            className="visit-orders-toolbar__daterange"
            onChange={(_, dateStrings) => {
              if (dateStrings[0] && dateStrings[1]) {
                setDateRange(dateStrings as [string, string])
              } else {
                setDateRange(null)
              }
              setPage(1)
            }}
          />
        </div>

        <div className="visit-orders-toolbar__actions">
          <Button
            type="primary"
            icon={<SyncOutlined />}
            loading={syncMutation.isPending}
            onClick={() => syncMutation.mutate()}
          >
            同步到诊单
          </Button>
          <Button
            icon={<DownloadOutlined />}
            disabled={!data?.items?.length}
            onClick={() => data?.items && exportVisitOrders(data.items)}
          >
            导出 CSV
          </Button>
        </div>
      </div>

      <div className="operation-card visit-orders-table-card">
        <Table<VisitOrder>
          rowKey="id"
          columns={columns}
          dataSource={data?.items ?? []}
          loading={isLoading}
          className="visit-orders-table"
          tableLayout="fixed"
          rowClassName={(_record, index) => (index % 2 === 0 ? 'visit-orders-table__row' : 'visit-orders-table__row is-alt')}
          scroll={{ x: 1080 }}
          pagination={{
            current: page,
            pageSize: PAGE_SIZE,
            total: data?.total ?? 0,
            showTotal: (total) => `共 ${total} 条`,
            onChange: (p) => setPage(p),
          }}
          size="small"
        />
      </div>

      <Modal
        title={matchingVisitOrder ? `到诊单匹配录音 · ${matchingVisitOrder.dzdh}${matchingVisitOrder.dzseg ? `-${matchingVisitOrder.dzseg}` : ''}` : '到诊单匹配录音'}
        open={matchOpen}
        onCancel={() => {
          setMatchOpen(false)
          setMatchingVisitOrder(null)
        }}
        footer={null}
        destroyOnClose
        width={980}
      >
        <Descriptions size="small" bordered column={2} style={{ marginBottom: 16 }}>
          <Descriptions.Item label="分析说明" span={2}>
            {isMatchLoading ? '正在分析匹配建议...' : matchData?.summary || '暂无分析结果'}
          </Descriptions.Item>
          <Descriptions.Item label="客户">{matchData?.customer_name || '-'}</Descriptions.Item>
          <Descriptions.Item label="客户编码">{matchData?.customer_code || '-'}</Descriptions.Item>
          <Descriptions.Item label="机构编码">{matchingVisitOrder?.jgbm || '-'}</Descriptions.Item>
          <Descriptions.Item label="机构科室">{matchingVisitOrder?.jgks_txt || matchingVisitOrder?.jgks || '-'}</Descriptions.Item>
          <Descriptions.Item label="顾问编号">{matchData?.advisor_code || '-'}</Descriptions.Item>
          <Descriptions.Item label="当前已绑定录音">
            {matchData?.linked_recording_ids.length ? `${matchData.linked_recording_ids.length} 条` : '暂无'}
          </Descriptions.Item>
          <Descriptions.Item label="最高推荐置信度" span={2}>
            {matchData?.candidates?.length ? getConfidenceTag(matchData.candidates[0].confidence) : '-'}
          </Descriptions.Item>
        </Descriptions>

        <Table<RecordingMatchCandidate>
          rowKey="recording_id"
          dataSource={visibleMatchCandidates}
          loading={isMatchLoading}
          pagination={false}
          size="small"
          scroll={{ x: 780 }}
          columns={[
            {
              title: '候选录音',
              width: 210,
              render: (_value, row) => (
                <div style={{ display: 'grid', gap: 4 }}>
                  <strong>{formatRecordingDisplayName(row.file_name, row.created_at)}</strong>
                  <span>{formatBeijingTime(row.created_at, 'YYYY-MM-DD HH:mm:ss')}</span>
                  <span>{row.staff_name || row.advisor_code || '-'}</span>
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
              render: (_value, row) => renderEvidenceBlock(row),
            },
            {
              title: '当前绑定',
              width: 124,
              render: (_value, row) =>
                row.current_visit_order_no
                  ? `${row.current_visit_order_no}${row.current_visit_order_seg ? `-${row.current_visit_order_seg}` : ''}`
                  : '未绑定',
            },
            {
              title: '操作',
              width: 108,
              render: (_value, row) => (
                <Button
                  type="primary"
                  size="small"
                  disabled={!matchData?.local_visit_id}
                  loading={adoptMatchMutation.isPending}
                  onClick={() => {
                    if (!matchData?.local_visit_id) return
                    void handleAdoptRecordingMatch(row)
                  }}
                >
                  采用推荐
                </Button>
              ),
            },
          ]}
          locale={{ emptyText: '没有可展示的候选录音' }}
        />
      </Modal>
    </div>
  )
}
