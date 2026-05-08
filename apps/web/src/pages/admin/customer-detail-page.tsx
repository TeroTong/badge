import { useQuery } from '@tanstack/react-query'
import {
  ArrowLeftOutlined,
  AudioOutlined,
  CheckCircleOutlined,
  FileTextOutlined,
  MinusCircleOutlined,
  TagsOutlined,
} from '@ant-design/icons'
import { Button, Card, Empty, Progress, Space, Spin, Tag, Tooltip } from 'antd'
import dayjs from 'dayjs'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { CustomerInsightBoard } from '@/components/customer-insight-board'
import {
  fetchCustomerDetail,
  fetchCustomerMergedAnalysis,
  fetchCustomerTagCompletion,
  fetchCustomerVisitOrders,
  type CustomerDetail,
  type CustomerDetailRecording,
  type CustomerMergedAnalysis,
  type CustomerMergedTheme,
  type TagCompletion,
  type TagExtractionItem,
  type CustomerVisitOrders,
} from '@/api/customers'
import { ANALYSIS_TAG_CATALOG_GROUPS } from '@/constants/tag-catalog'
import { fetchVisitDetail } from '@/api/visits'
import { VISIT_STATUS_MAP } from '@/api/visits'
import { formatRecordingDisplayName } from '@/utils/recording-display'
import { formatBeijingTime, toBeijingTime } from '@/utils/time'

const GENDER_LABELS: Record<string, string> = {
  male: '男',
  female: '女',
  unknown: '未知',
  男: '男',
  女: '女',
  未知: '未知',
}

function formatDuration(seconds: number | null) {
  if (seconds == null) return '时长未知'
  const minutes = Math.floor(seconds / 60)
  const remainder = Math.round(seconds % 60)
  return `${minutes}:${remainder.toString().padStart(2, '0')}`
}

function truncateText(value: string | null | undefined, maxLength = 96) {
  const text = value?.trim()
  if (!text) return ''
  if (text.length <= maxLength) return text
  return `${text.slice(0, maxLength).trimEnd()}...`
}

function formatVisitDateTime(visitDate: string | null, visitTime: string | null, createdAt?: string | null) {
  if (visitDate && visitTime) {
    return `${dayjs(visitDate).format('YYYY-MM-DD')} ${visitTime.slice(0, 5)}`
  }
  if (createdAt) {
    return formatBeijingTime(createdAt, 'YYYY-MM-DD HH:mm')
  }
  if (visitDate) {
    return dayjs(visitDate).format('YYYY-MM-DD')
  }
  return '暂无'
}

function formatArchiveMeta(createdAt: string | null | undefined) {
  if (!createdAt) {
    return { label: '建档日期', value: '未登记' }
  }

  const raw = createdAt.trim()
  const midnightLike = raw.match(/^(\d{4}-\d{2}-\d{2})(?:[T\s]00:00(?::00(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$/)
  if (midnightLike) {
    return {
      label: '建档日期',
      value: midnightLike[1],
    }
  }

  const parsed = toBeijingTime(createdAt)
  if (!parsed.isValid()) {
    return { label: '建档日期', value: '未登记' }
  }

  const hasOnlyDatePrecision = parsed.hour() === 0 && parsed.minute() === 0 && parsed.second() === 0
  if (hasOnlyDatePrecision) {
    return {
      label: '建档日期',
      value: parsed.format('YYYY-MM-DD'),
    }
  }

  return {
    label: '档案创建时间',
    value: parsed.format('YYYY-MM-DD HH:mm'),
  }
}

function visitDealStatusColor(value: string | null) {
  if (value === '已成交') return 'success'
  if (value === '未成交') return 'error'
  return value ? 'processing' : 'default'
}

function customerMetaTags(customer: CustomerDetail) {
  const tags: Array<{ key: string; label: string; color?: string }> = []
  if (customer.gender) {
    tags.push({
      key: 'gender',
      label: `性别：${GENDER_LABELS[customer.gender] ?? customer.gender}`,
    })
  }
  if (customer.age != null) tags.push({ key: 'age', label: `年龄：${customer.age}岁` })
  if (customer.customer_type_label) {
    tags.push({
      key: 'customer_type',
      label: customer.customer_type_label,
      color: customer.customer_type_code === 'V' ? 'gold' : 'green',
    })
  }
  if (customer.closed_won_count > 0) {
    tags.push({ key: 'won', label: `成交次数：${customer.closed_won_count}`, color: 'gold' })
  }
  return tags
}

function scoreTrendMeta(merged: CustomerMergedAnalysis) {
  if (merged.score_trend === 'improving') {
    return { label: '评分回升', className: 'customer-merged__trend customer-merged__trend--up' }
  }
  if (merged.score_trend === 'declining') {
    return { label: '评分下滑', className: 'customer-merged__trend customer-merged__trend--down' }
  }
  return { label: '评分平稳', className: 'customer-merged__trend customer-merged__trend--stable' }
}

function RecordingSummary({
  recording,
  onOpenRecording,
  onOpenTranscript,
}: {
  recording: CustomerDetailRecording
  onOpenRecording: (recordingId: string) => void
  onOpenTranscript: (transcriptId: string) => void
}) {
  return (
    <article className="customer-detail-recording">
      <div className="customer-detail-recording__header">
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

      <p className="customer-detail-recording__excerpt">
        {recording.transcript_excerpt || '暂无对话摘要。'}
      </p>

      <div className="customer-detail-recording__actions">
        <Button size="small" icon={<AudioOutlined />} onClick={() => onOpenRecording(recording.id)}>
          录音详情
        </Button>
        <Button
          size="small"
          icon={<FileTextOutlined />}
          onClick={() => recording.transcript_id && onOpenTranscript(recording.transcript_id)}
          disabled={!recording.transcript_id}
        >
          转写详情
        </Button>
      </div>
    </article>
  )
}

function ThemeChips({ items, emptyText }: { items: CustomerMergedTheme[]; emptyText: string }) {
  if (!items.length) {
    return <div className="customer-merged__empty">{emptyText}</div>
  }

  return (
    <div className="customer-merged__chip-list">
      {items.map((item) => (
        <div key={item.label} className="customer-merged__chip-card">
          <strong>{item.label}</strong>
          <span>出现 {item.count} 次</span>
          {item.detail ? <p>{item.detail}</p> : null}
        </div>
      ))}
    </div>
  )
}

function MergedAnalysisPanel({
  merged,
  isLoading,
}: {
  merged: CustomerMergedAnalysis | undefined
  isLoading: boolean
}) {
  if (isLoading) {
    return (
      <Card bordered={false} className="customer-detail-card customer-detail-card--merged">
        <Spin style={{ display: 'block', margin: '28px auto' }} />
      </Card>
    )
  }

  if (!merged || merged.analyzed_recordings === 0) {
    return (
      <Card bordered={false} className="customer-detail-card customer-detail-card--merged">
        <div className="customer-detail-card__title-row">
          <div>
            <p className="visit-page__eyebrow">长期复盘</p>
            <h2>多次录音合并分析</h2>
          </div>
        </div>
        <Empty description="这个客户还没有可用的分析结果" />
      </Card>
    )
  }

  const trendMeta = scoreTrendMeta(merged)

  return (
    <Card bordered={false} className="customer-detail-card customer-detail-card--merged">
        <div className="customer-detail-card__title-row">
          <div>
            <p className="visit-page__eyebrow">长期复盘</p>
            <h2>多次录音合并分析</h2>
          </div>
        <Space wrap>
          {merged.last_analyzed_at ? (
            <Tag color="purple">最近分析 {formatBeijingTime(merged.last_analyzed_at, 'MM/DD HH:mm')}</Tag>
          ) : null}
        </Space>
      </div>

      <div className="customer-merged__stats">
        <div className="customer-merged__stat-card">
          <span>平均评分</span>
          <strong>{merged.average_score != null ? merged.average_score.toFixed(1) : '-'}</strong>
        </div>
        <div className="customer-merged__stat-card">
          <span>最近评分</span>
          <strong>{merged.latest_score != null ? merged.latest_score.toFixed(1) : '-'}</strong>
        </div>
        <div className="customer-merged__stat-card">
          <span>趋势判断</span>
          <strong className={trendMeta.className}>
            {trendMeta.label}
            {merged.score_delta != null ? ` ${merged.score_delta > 0 ? '+' : ''}${merged.score_delta.toFixed(1)}` : ''}
          </strong>
        </div>
      </div>

      <p className="customer-merged__summary">{merged.merged_summary}</p>

      <div className="customer-merged__grid">
        <section className="customer-merged__section">
          <h3>画像标签</h3>
          {(() => {
            // 将已提取标签按 label 索引
            const hitMap = new Map<string, string[]>()
            const appendHit = (label: string, detail: string) => {
              const normalizedLabel = label.trim()
              const normalizedDetail = detail.trim()
              if (!normalizedLabel || !normalizedDetail) return
              const list = hitMap.get(normalizedLabel) ?? []
              if (!list.includes(normalizedDetail)) {
                list.push(normalizedDetail)
                hitMap.set(normalizedLabel, list)
              }
            }
            for (const t of merged.profile_tags) {
              const rawLabel = t.label || ''
              const separatorIndex = rawLabel.indexOf('：')
              if (separatorIndex > 0) {
                appendHit(
                  rawLabel.slice(0, separatorIndex),
                  rawLabel.slice(separatorIndex + 1) || t.detail || `出现 ${t.count} 次`,
                )
                continue
              }
              appendHit(rawLabel, t.detail ?? `出现 ${t.count} 次`)
            }
            const hitCount = hitMap.size
            const totalCount = ANALYSIS_TAG_CATALOG_GROUPS.reduce((s, g) => s + g.items.length, 0)

            return (
              <>
                <p className="ad-tag-stats">
                  已命中 <strong>{hitCount}</strong> / {totalCount} 项
                </p>
                {ANALYSIS_TAG_CATALOG_GROUPS.map((group) => (
                  <div key={group.weight} className="ad-tag-group">
                    <h4 className="ad-tag-group__title">
                      <Tag color={group.color}>{group.label}</Tag>
                      {group.items.filter((it) => hitMap.has(it.name)).length}/{group.items.length}
                    </h4>
                    {(() => {
                      const subGroups: { label: string; items: typeof group.items }[] = []
                      let currentGroup = ''
                      for (const item of group.items) {
                        if (item.group !== currentGroup) {
                          subGroups.push({ label: item.group, items: [] })
                          currentGroup = item.group
                        }
                        subGroups[subGroups.length - 1].items.push(item)
                      }
                      return subGroups.map((sg) => {
                        const isStandalone = sg.items.length === 1 && sg.items[0].name === sg.label
                        return (
                          <div key={sg.label} className="ad-tag-subgroup">
                            {!isStandalone && (
                              <h5 className="ad-tag-subgroup__title">{sg.label}</h5>
                            )}
                            <div className="ad-tags-grid">
                              {sg.items.map((item) => {
                                const detail = hitMap.get(item.name)
                                const hit = detail != null
                                return (
                                  <div key={item.name} className={`ad-tag-item ${hit ? 'ad-tag-item--hit' : 'ad-tag-item--miss'}`}>
                                    <span className="ad-tag-item__category">{item.name}</span>
                                    <span className="ad-tag-item__value">{hit ? detail.join('；') : '—'}</span>
                                  </div>
                                )
                              })}
                            </div>
                          </div>
                        )
                      })
                    })()}
                  </div>
                ))}
              </>
            )
          })()}
        </section>

        <section className="customer-merged__section">
          <h3>重复诉求</h3>
          <ThemeChips items={merged.recurring_focus_areas} emptyText="暂未提取到稳定诉求主题" />
        </section>

        <section className="customer-merged__section">
          <h3>重复顾虑</h3>
          <ThemeChips items={merged.recurring_concerns} emptyText="暂未提取到重复顾虑" />
        </section>
      </div>

      <section className="customer-merged__section">
        <h3>维度均分</h3>
        {merged.dimension_averages.length ? (
          <details className="customer-detail-collapsible">
            <summary className="customer-detail-collapsible__summary">
              <div>
                <span>深入查看</span>
                <strong>维度均分明细</strong>
              </div>
              <small>{merged.dimension_averages.length} 个维度</small>
            </summary>
            <div className="customer-detail-collapsible__body">
              <div className="customer-merged__dimension-list">
                {merged.dimension_averages.map((item) => (
                  <div key={item.name} className="customer-merged__dimension-card">
                    <div className="customer-merged__dimension-header">
                      <strong>{item.name}</strong>
                      <span>{item.average_score.toFixed(1)}</span>
                    </div>
                    <Progress percent={Math.round(item.average_score * 10)} size="small" showInfo={false} />
                    <p>
                      最近一次 {item.latest_score != null ? item.latest_score.toFixed(1) : '-'} · 出现 {item.mention_count} 次
                    </p>
                    {item.latest_comment ? <small>{item.latest_comment}</small> : null}
                  </div>
                ))}
              </div>
            </div>
          </details>
        ) : (
          <div className="customer-merged__empty">暂无维度评分数据</div>
        )}
      </section>

      <section className="customer-merged__section">
        <h3>最近分析时间线</h3>
        <details className="customer-detail-collapsible">
          <summary className="customer-detail-collapsible__summary">
            <div>
              <span>深入查看</span>
              <strong>分析时间线</strong>
            </div>
            <small>{merged.timeline.length} 次分析</small>
          </summary>
          <div className="customer-detail-collapsible__body">
            <div className="customer-merged__timeline">
              {merged.timeline.map((item) => (
                <div key={item.task_id} className="customer-merged__timeline-item">
                  <div className="customer-merged__timeline-main">
                    <strong>{item.recording_name || item.task_id}</strong>
                    <p className="customer-merged__timeline-subtitle">
                      {item.completed_at ? formatBeijingTime(item.completed_at, 'YYYY-MM-DD HH:mm') : '时间未知'}
                      {item.visit_id ? ` · 接诊单 ${item.visit_id}` : ''}
                    </p>
                    {item.visit_status ? (
                      <div className="customer-merged__timeline-meta">
                        {item.visit_status ? (
                          <Tag color={VISIT_STATUS_MAP[item.visit_status]?.color ?? 'default'}>
                            {VISIT_STATUS_MAP[item.visit_status]?.label ?? item.visit_status}
                          </Tag>
                        ) : null}
                      </div>
                    ) : null}
                  </div>
                  <Space wrap className="customer-merged__timeline-actions">
                    <Tag color={item.overall_score != null && item.overall_score < 6 ? 'error' : 'processing'}>
                      {item.overall_score != null ? item.overall_score.toFixed(1) : item.quality_label}
                    </Tag>
                    {item.recording_id ? <Link to={`/admin/recordings/${item.recording_id}`}>录音详情</Link> : null}
                  </Space>
                </div>
              ))}
            </div>
          </div>
        </details>
      </section>
    </Card>
  )
}

const WEIGHT_LABELS: Record<number, { text: string; color: string }> = {
  1: { text: '必问', color: '#f5222d' },
  2: { text: '重要', color: '#fa8c16' },
  3: { text: '一般', color: '#1890ff' },
  4: { text: '次要', color: '#8c8c8c' },
}

function TagCompletionPanel({
  tagCompletion,
  isLoading,
}: {
  tagCompletion: TagCompletion | undefined
  isLoading: boolean
}) {
  if (isLoading) {
    return (
      <Card bordered={false} className="customer-detail-card">
        <Spin style={{ display: 'block', margin: '28px auto' }} />
      </Card>
    )
  }

  if (!tagCompletion) {
    return (
      <Card bordered={false} className="customer-detail-card">
        <Empty description="无法加载标签完成度数据" />
      </Card>
    )
  }

  const { total_categories, extracted_categories, completion_rate, categories } = tagCompletion

  // 按权重分组
  const weightGroups = new Map<string, TagExtractionItem[]>()
  for (const cat of categories) {
    const key = cat.weight_level != null ? `W${cat.weight_level}` : '未分级'
    const group = weightGroups.get(key)
    if (group) {
      group.push(cat)
    } else {
      weightGroups.set(key, [cat])
    }
  }

  return (
    <Card bordered={false} className="customer-detail-card">
      <div className="customer-detail-card__title-row">
        <div>
          <p className="visit-page__eyebrow">客户画像</p>
          <h2>
            <TagsOutlined style={{ marginRight: 8 }} />
            标签提取完成度
          </h2>
        </div>
        <Space>
          <Tag color="green">已提取 {extracted_categories}</Tag>
          <Tag>未提取 {total_categories - extracted_categories}</Tag>
          <Progress
            type="circle"
            percent={Math.round(completion_rate * 100)}
            size={48}
            strokeColor={completion_rate > 0.5 ? '#52c41a' : completion_rate > 0.2 ? '#faad14' : '#ff4d4f'}
          />
        </Space>
      </div>

      <p className="customer-merged__summary">
        在所有 {total_categories} 个标签类别中，已从录音分析中成功提取 {extracted_categories} 个。
        {total_categories - extracted_categories > 0 &&
          `咨询师在下次接待该客户时，可重点关注未提取的标签，特别是权重为"必问"和"重要"的类别。`}
      </p>

      {Array.from(weightGroups.entries()).map(([groupLabel, items]) => {
        const weightNum = items[0]?.weight_level
        const meta = weightNum != null ? WEIGHT_LABELS[weightNum] : null
        const extracted = items.filter((i) => i.status === 'extracted')

        return (
          <section key={groupLabel} className="customer-merged__section tag-completion-section">
            <h3>
              {meta ? (
                <Tag color={meta.color} style={{ marginRight: 6 }}>
                  {meta.text}
                </Tag>
              ) : null}
              {groupLabel}（{extracted.length}/{items.length}）
            </h3>
            <div className="tag-completion-grid">
              {items.map((item) => (
                <div
                  key={item.category_id}
                  className={`tag-completion-card ${item.status === 'extracted' ? 'tag-completion-card--extracted' : 'tag-completion-card--missing'}`}
                >
                  <div className="tag-completion-card__header">
                    {item.status === 'extracted' ? (
                      <CheckCircleOutlined className="tag-icon--extracted" />
                    ) : (
                      <MinusCircleOutlined className="tag-icon--missing" />
                    )}
                    <strong>{item.category_name}</strong>
                  </div>
                  {item.status === 'extracted' ? (
                    <div className="tag-completion-card__values">
                      {item.extracted_values.map((v) => (
                        <Tag key={v} color="blue" className="tag-completion-value">
                          {v}
                        </Tag>
                      ))}
                      {item.evidence && (
                        <Tooltip title={item.evidence}>
                          <small className="tag-completion-card__evidence">📝 有证据</small>
                        </Tooltip>
                      )}
                    </div>
                  ) : (
                    <div className="tag-completion-card__placeholder">
                      {item.available_tags.length > 0
                        ? `可选: ${item.available_tags.slice(0, 3).join(', ')}${item.available_tags.length > 3 ? '...' : ''}`
                        : '待提取'}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </section>
        )
      })}
    </Card>
  )
}

function VisitOrdersPanel({
  visitOrders,
  isLoading,
}: {
  visitOrders: CustomerVisitOrders | undefined
  isLoading: boolean
}) {
  if (isLoading) {
    return (
      <Card bordered={false} className="customer-detail-card">
        <Spin style={{ display: 'block', margin: '28px auto' }} />
      </Card>
    )
  }

  if (!visitOrders || visitOrders.visit_groups.length === 0) {
    return (
      <Card bordered={false} className="customer-detail-card">
        <div className="customer-detail-card__title-row">
          <div>
            <p className="visit-page__eyebrow">原始业务单据</p>
            <h2>到诊单原始明细</h2>
          </div>
        </div>
        <Empty description={visitOrders?.customer_code ? '该客户暂无到诊单记录' : '客户未关联客户编码，无法查询到诊单'} />
      </Card>
    )
  }

  return (
    <Card bordered={false} className="customer-detail-card">
      <div className="customer-detail-card__title-row">
        <div>
          <p className="visit-page__eyebrow">原始业务单据</p>
          <h2>到诊单原始明细</h2>
          <p className="visit-orders-subtitle">
            客户编码 {visitOrders.customer_code} · 共 {visitOrders.total_visits} 次到诊
            {visitOrders.visit_groups.some((g) => g.items.length > 0) &&
              '（用于核对原始到诊单与行项目，不重复展示接诊链路）'}
          </p>
        </div>
      </div>

      <div className="customer-visit-stack">
        {visitOrders.visit_groups.map((group) => {
          const statusColor = group.status_text?.includes('成交')
            ? 'success'
            : group.status_text?.includes('分诊')
              ? 'processing'
              : 'default'

          return (
            <article key={group.dzdh} className="customer-visit-card">
              <header className="customer-visit-card__header">
                <div>
                  <div className="customer-visit-card__title-row">
                    <strong>到诊单 {group.dzdh}</strong>
                    {group.status_text && <Tag color={statusColor}>{group.status_text}</Tag>}
                    {group.customer_type && <Tag>{group.customer_type}</Tag>}
                    {group.customer_type_t30 && <Tag color="orange">T30: {group.customer_type_t30}</Tag>}
                    {group.member_level && <Tag color="gold">{group.member_level}</Tag>}
                  </div>
                  <p>
                    原始到诊日期 {group.visit_date || '未登记'} · 行项目 {group.items.length} 个
                  </p>
                </div>
              </header>

              {group.remark && <p className="customer-visit-card__notes">{group.remark}</p>}

              {group.items.length > 0 && (
                <details className="visit-order-sub-items">
                  <summary>
                    {group.items.length} 个行项目（DZSEG 合并前）
                  </summary>
                  <div className="visit-order-sub-items__grid">
                    {group.items.map((item, idx) => (
                      <div key={idx} className="visit-order-sub-item">
                        <span className="visit-order-sub-item__seg">行项目 {item.dzseg}</span>
                        {item.remark_dz && <span>线索: {item.remark_dz}</span>}
                        {item.jcsta_txt && <Tag>{item.jcsta_txt}</Tag>}
                      </div>
                    ))}
                  </div>
                </details>
              )}
            </article>
          )
        })}
      </div>
    </Card>
  )
}

export function CustomerDetailPage() {
  const { customerId } = useParams<{ customerId: string }>()
  const navigate = useNavigate()

  const detailQuery = useQuery({
    queryKey: ['customer-detail', customerId],
    queryFn: () => fetchCustomerDetail(customerId!),
    enabled: !!customerId,
  })

  const mergedQuery = useQuery({
    queryKey: ['customer-merged-analysis', customerId],
    queryFn: () => fetchCustomerMergedAnalysis(customerId!),
    enabled: !!customerId,
  })

  const tagCompletionQuery = useQuery({
    queryKey: ['customer-tag-completion', customerId],
    queryFn: () => fetchCustomerTagCompletion(customerId!),
    enabled: !!customerId,
  })

  const visitOrdersQuery = useQuery({
    queryKey: ['customer-visit-orders', customerId],
    queryFn: () => fetchCustomerVisitOrders(customerId!),
    enabled: !!customerId,
  })

  const data = detailQuery.data
  const latestInsightVisitId = data?.visits.find((visit) => visit.recordings.some((recording) => Boolean(recording.analysis_task_id)))?.id
    ?? data?.visits[0]?.id

  const latestInsightVisitQuery = useQuery({
    queryKey: ['customer-latest-visit-detail', latestInsightVisitId],
    queryFn: () => fetchVisitDetail(latestInsightVisitId!),
    enabled: Boolean(latestInsightVisitId),
  })

  if (detailQuery.isLoading || !data) {
    return <Spin style={{ display: 'block', margin: '80px auto' }} size="large" />
  }

  const latestVisit = data.visits[0] ?? null
  const latestVisitLabel = data.visits[0]
    ? formatVisitDateTime(data.visits[0].visit_date, data.visits[0].visit_time, data.visits[0].created_at)
    : data.last_visit_at
      ? formatBeijingTime(data.last_visit_at, 'YYYY-MM-DD HH:mm')
      : '暂无'
  const notePreview = truncateText(data.notes, 136)
  const archiveMeta = formatArchiveMeta(data.created_at)
  const heroSummary = latestVisit
    ? `当前档案已累计沉淀 ${data.visit_count} 次接诊、${data.recording_count} 条录音，可结合下方接待简报和来访时间线快速完成接手。`
    : '当前档案还没有沉淀接诊记录，可以先从备注和后续录音中补齐业务上下文。'

  return (
    <div className="customer-detail-page">
      <div className="customer-detail-page__topbar">
        <Link to="/admin/customers" className="back-link">
          <ArrowLeftOutlined /> 返回客户档案
        </Link>
      </div>

      <div className="customer-detail-page__hero">
        <Card bordered={false} className="customer-detail-card customer-detail-page__hero-main">
          <div className="customer-detail-page__hero-main-layout">
            <div className="customer-detail-page__hero-top">
              <div>
                <p className="visit-page__eyebrow">客户中心 / 客户档案 / 详情</p>
                <h1>{data.name}</h1>
                <p className="customer-detail-page__lead">{heroSummary}</p>
                <div className="customer-detail-page__tags">
                  {customerMetaTags(data).map((tag) => (
                    <Tag key={tag.key} color={tag.color}>
                      {tag.label}
                    </Tag>
                  ))}
                  {data.wechat_external_uid ? <Tag color="cyan">企微：已绑定</Tag> : null}
                </div>
              </div>

              <div className="customer-detail-page__hero-identity-meta">
                <span>客户编码</span>
                <strong>{data.external_customer_code || `ID ${data.id.slice(0, 8)}`}</strong>
                <small>{archiveMeta.label} {archiveMeta.value}</small>
              </div>
            </div>

            <div className="customer-detail-page__hero-facts">
              <div className="customer-detail-page__hero-fact">
                <span>最近来访</span>
                <strong>{latestVisitLabel}</strong>
              </div>
              {notePreview ? (
                <div className="customer-detail-page__hero-note" title={data.notes || ''}>
                  <span>跟进提醒</span>
                  <p>{notePreview}</p>
                </div>
              ) : null}
            </div>
          </div>
        </Card>

        <Card bordered={false} className="customer-detail-card customer-detail-page__hero-side">
          <div className="customer-detail-page__hero-side-layout">
            <div className="customer-detail-card__title-row">
              <div>
                <p className="visit-page__eyebrow">经营快照</p>
                <h2>客户摘要</h2>
              </div>
            </div>

            <div className="customer-detail-page__snapshot-grid">
              <div className="customer-detail-page__snapshot-stat">
                <span>接诊次数</span>
                <strong>{data.visit_count}</strong>
              </div>
              <div className="customer-detail-page__snapshot-stat">
                <span>录音数</span>
                <strong>{data.recording_count}</strong>
              </div>
              <div className="customer-detail-page__snapshot-stat">
                <span>成交次数</span>
                <strong>{data.closed_won_count}</strong>
              </div>
              <div className="customer-detail-page__snapshot-stat">
                <span>最近到诊</span>
                <strong>{latestVisitLabel}</strong>
              </div>
            </div>
          </div>
        </Card>
      </div>

      <div className="customer-detail-page__stack">
        <nav className="customer-detail-page__quicknav" aria-label="客户档案快速导航">
          <a href="#customer-insight">接待简报</a>
          <a href="#customer-merged-analysis">长期复盘</a>
          <a href="#customer-tags">客户标签</a>
          <a href="#customer-visit-chain">接诊链路</a>
        </nav>

        <div className="customer-detail-page__workspace">
          <main className="customer-detail-page__main-column">
            <section id="customer-insight" className="customer-detail-section">
              {latestInsightVisitQuery.isLoading ? (
                <Card bordered={false} className="customer-detail-card">
                  <Spin style={{ display: 'block', margin: '28px auto' }} />
                </Card>
              ) : latestInsightVisitQuery.data ? (
                <CustomerInsightBoard
                  visit={latestInsightVisitQuery.data}
                  onOpenVisitDetail={(visitId) => navigate(`/admin/visits/${visitId}`)}
                  onOpenRecording={(recordingId) => navigate(`/admin/recordings/${recordingId}`)}
                />
              ) : (
                <Card bordered={false} className="customer-detail-card">
                  <Empty description="当前客户还没有可生成业务洞察的接诊记录" />
                </Card>
              )}
            </section>

            <section id="customer-merged-analysis" className="customer-detail-section">
              <MergedAnalysisPanel merged={mergedQuery.data} isLoading={mergedQuery.isLoading} />
            </section>
          </main>

          <aside id="customer-tags" className="customer-detail-page__side-column">
            <TagCompletionPanel tagCompletion={tagCompletionQuery.data} isLoading={tagCompletionQuery.isLoading} />
          </aside>
        </div>

        <section id="customer-visit-chain" className="customer-detail-section">
          <div className="customer-detail-section__head">
            <div>
              <p className="visit-page__eyebrow">来访时间线</p>
              <h2>接诊与录音链路</h2>
            </div>
            <Space wrap>
              <Button onClick={() => navigate('/admin/visits')}>查看全部接诊</Button>
              <Button type="primary" icon={<AudioOutlined />} onClick={() => navigate('/admin/recordings')}>
                录音管理
              </Button>
            </Space>
          </div>

          {data.visits.length === 0 ? (
            <Card bordered={false}>
              <Empty description="这个客户还没有接诊记录" />
            </Card>
          ) : (
            <div className="customer-visit-stack">
              {data.visits.map((visit) => {
                const status = VISIT_STATUS_MAP[visit.status]
                const visitDateLabel = formatVisitDateTime(visit.visit_date, visit.visit_time, visit.created_at)
                const primaryRecording = visit.recordings[0] ?? null
                const moreRecordings = visit.recordings.slice(1)
                const hasArrivalPurpose = Boolean(
                  visit.arrival_purpose && visit.arrival_purpose.trim() && visit.arrival_purpose !== visit.project_needs,
                )

                return (
                  <article key={visit.id} className="customer-visit-card">
                    <header className="customer-visit-card__header">
                      <div>
                        <div className="customer-visit-card__title-row">
                          <strong>接诊单 {visit.id}</strong>
                          <Tag color={status?.color ?? 'default'}>{status?.label ?? visit.status}</Tag>
                          {visit.deal_status ? (
                            <Tag color={visitDealStatusColor(visit.deal_status)}>{visit.deal_status}</Tag>
                          ) : null}
                          {visit.recordings.length > 0 ? <Tag color="blue">录音 {visit.recordings.length} 条</Tag> : null}
                        </div>
                        <p>
                          到诊时间 {visitDateLabel} · 顾问 {visit.consultant_name || '待分配'} · 医生 {visit.doctor_name || '待分配'}
                        </p>
                      </div>
                      <div className="customer-visit-card__header-actions">
                        <Button size="small" type="primary" ghost onClick={() => navigate(`/admin/visits/${visit.id}`)}>
                          接诊详情
                        </Button>
                      </div>
                    </header>

                    {hasArrivalPurpose ? (
                      <div className="customer-visit-card__facts">
                        {hasArrivalPurpose ? (
                          <div>
                            <span>到诊目的</span>
                            <strong>{visit.arrival_purpose}</strong>
                          </div>
                        ) : null}
                      </div>
                    ) : null}

                    {visit.notes && <p className="customer-visit-card__notes">{visit.notes}</p>}

                    {visit.recordings.length === 0 ? (
                      <div className="customer-visit-card__empty">
                        这次接诊还没有关联录音，可以去录音管理页补充关联。
                      </div>
                    ) : (
                      <div className="customer-visit-card__recordings">
                        <div className="customer-visit-card__recordings-header">
                          <strong>关联录音</strong>
                          <span>{visit.recordings.length} 条</span>
                        </div>
                        <div className="customer-detail-recording-list">
                          {primaryRecording ? (
                            <RecordingSummary
                              key={primaryRecording.id}
                              recording={primaryRecording}
                              onOpenRecording={(recordingId) => navigate(`/admin/recordings/${recordingId}`)}
                              onOpenTranscript={(transcriptId) => navigate(`/admin/transcripts/${transcriptId}`)}
                            />
                          ) : null}
                          {moreRecordings.length > 0 ? (
                            <details className="customer-visit-card__recordings-more">
                              <summary>另外 {moreRecordings.length} 条录音</summary>
                              <div className="customer-detail-recording-list">
                                {moreRecordings.map((recording) => (
                                  <RecordingSummary
                                    key={recording.id}
                                    recording={recording}
                                    onOpenRecording={(recordingId) => navigate(`/admin/recordings/${recordingId}`)}
                                    onOpenTranscript={(transcriptId) => navigate(`/admin/transcripts/${transcriptId}`)}
                                  />
                                ))}
                              </div>
                            </details>
                          ) : null}
                        </div>
                      </div>
                    )}
                  </article>
                )
              })}
            </div>
          )}
        </section>

        <details className="customer-detail-collapsible">
          <summary className="customer-detail-collapsible__summary">
            <div>
              <span>次级信息</span>
              <strong>到诊单原始明细</strong>
            </div>
            <small>
              {visitOrdersQuery.data?.customer_code
                ? `${visitOrdersQuery.data.total_visits} 次到诊`
                : '按需展开查看'}
            </small>
          </summary>
          <div className="customer-detail-collapsible__body">
            <VisitOrdersPanel visitOrders={visitOrdersQuery.data} isLoading={visitOrdersQuery.isLoading} />
          </div>
        </details>
      </div>
    </div>
  )
}

export default CustomerDetailPage
