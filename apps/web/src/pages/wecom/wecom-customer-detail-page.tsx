import { CalendarOutlined, DownOutlined, IdcardOutlined, ProfileOutlined, RightOutlined } from '@ant-design/icons'
import { useQueries, useQuery } from '@tanstack/react-query'
import dayjs from 'dayjs'
import { HTTPError } from 'ky'
import { useState, type ReactNode } from 'react'
import { Link, useLocation, useParams } from 'react-router-dom'

import {
  fetchCustomerDetail,
  fetchCustomerTagCompletion,
  type CustomerDetail,
  type TagExtractionItem,
} from '@/api/customers'
import { fetchAnalysisDetail, type AnalysisDetail } from '@/api/analysis'
import { AnalysisDetailContent } from '@/components/analysis-detail-content'
import { VISIT_STATUS_MAP } from '@/api/visits'
import { TAG_CATALOG_GROUPS } from '@/constants/tag-catalog'
import { formatRecordingDisplayName } from '@/utils/recording-display'
import { formatBeijingTime } from '@/utils/time'

const GENDER_LABELS: Record<string, string> = {
  male: '男',
  female: '女',
  unknown: '未知',
  男: '男',
  女: '女',
  未知: '未知',
}

const WEIGHT_LABELS: Record<number, { text: string; tagClass: string; sectionClass: string }> = {
  1: { text: '必问', tagClass: 'wc-tag--red', sectionClass: 'wc-tag-section--required' },
  2: { text: '重要', tagClass: 'wc-tag--orange', sectionClass: 'wc-tag-section--important' },
  3: { text: '一般', tagClass: 'wc-tag--blue', sectionClass: 'wc-tag-section--common' },
  4: { text: '次要', tagClass: 'wc-tag--gray', sectionClass: 'wc-tag-section--minor' },
}

type TagTableRow = {
  key: string
  weight: number | null
  weightLabel: string
  sectionClass: string
  groupName: string
  itemName: string
  values: string[]
  status: TagExtractionItem['status']
  showWeight: boolean
  weightRowSpan: number
  showGroup: boolean
  groupRowSpan: number
}

type TagDisplayGroup = {
  key: string
  title: string
  weightLabel: string
  badgeLabel: string
  sectionClass: string
  items: Array<{
    key: string
    label: string
    values: string[]
  }>
}

const RECORDING_STATUS_LABELS: Record<string, string> = {
  uploaded: '已上传',
  transcribing: '转写中',
  transcribed: '已转写',
  analyzing: '分析中',
  analyzed: '已分析',
  filtered: '已过滤',
  failed: '失败',
}

function formatDateTime(value: string | null | undefined) {
  if (!value) return '暂无'
  return formatBeijingTime(value, 'MM/DD HH:mm')
}

function buildCustomerMetaTags(customer: CustomerDetail) {
  const tags: string[] = []
  if (customer.customer_type_label) tags.push(customer.customer_type_label)
  if (customer.gender) tags.push(`性别：${GENDER_LABELS[customer.gender] ?? customer.gender}`)
  tags.push(customer.age != null && customer.age > 0 ? `年龄：${customer.age}岁` : '年龄：-岁')
  return tags
}

function isExtractedTagItem(item: TagExtractionItem) {
  return item.status === 'extracted' && item.extracted_values.some((value) => value.trim().length > 0)
}

function buildTagTableRows(categories: TagExtractionItem[]): TagTableRow[] {
  const extractedCategories = categories.filter(isExtractedTagItem)
  if (extractedCategories.length === 0) return []

  const rows: TagTableRow[] = []
  const categoryMap = new Map(extractedCategories.map((item) => [item.category_name, item] as const))
  const consumedCategories = new Set<string>()

  for (const weightGroup of TAG_CATALOG_GROUPS) {
    const groupedItems = new Map<string, Array<{ catalog: (typeof weightGroup.items)[number]; item: TagExtractionItem }>>()

    for (const catalogItem of weightGroup.items) {
      const matched = categoryMap.get(catalogItem.name)
      if (!matched) continue
      consumedCategories.add(catalogItem.name)
      const currentGroup = groupedItems.get(catalogItem.group) ?? []
      currentGroup.push({ catalog: catalogItem, item: matched })
      groupedItems.set(catalogItem.group, currentGroup)
    }

    const weightRowSpan = Array.from(groupedItems.values()).reduce((sum, currentGroup) => sum + currentGroup.length, 0)
    let showWeight = true

    for (const [groupName, groupItems] of groupedItems) {
      let showGroup = true
      for (const { catalog, item } of groupItems) {
        const weightMeta =
          item.weight_level != null ? WEIGHT_LABELS[item.weight_level] : WEIGHT_LABELS[catalog.weight]
        rows.push({
          key: item.category_id,
          weight: item.weight_level ?? catalog.weight,
          weightLabel: weightMeta?.text ?? '未分级',
          sectionClass: weightMeta?.sectionClass ?? 'wc-tag-section--unknown',
          groupName,
          itemName: catalog.group === catalog.name ? '—' : catalog.name,
          values: item.extracted_values.filter((value) => value.trim().length > 0),
          status: item.status,
          showWeight,
          weightRowSpan: showWeight ? weightRowSpan : 0,
          showGroup,
          groupRowSpan: showGroup ? groupItems.length : 0,
        })
        showWeight = false
        showGroup = false
      }
    }
  }

  const fallbackItems = extractedCategories.filter((item) => !consumedCategories.has(item.category_name))
  for (const item of fallbackItems) {
    const weightMeta = item.weight_level != null ? WEIGHT_LABELS[item.weight_level] : null
    rows.push({
      key: item.category_id,
      weight: item.weight_level,
      weightLabel: weightMeta?.text ?? '未分级',
      sectionClass: weightMeta?.sectionClass ?? 'wc-tag-section--unknown',
      groupName: item.category_name,
      itemName: '—',
      values: item.extracted_values.filter((value) => value.trim().length > 0),
      status: item.status,
      showWeight: true,
      weightRowSpan: 1,
      showGroup: true,
      groupRowSpan: 1,
    })
  }

  return rows
}

function resolveTagDisplayLabel(row: TagTableRow) {
  return row.itemName === '—' ? row.groupName : row.itemName
}

function buildTagDisplayGroups(rows: TagTableRow[]): TagDisplayGroup[] {
  const groups: TagDisplayGroup[] = []
  const groupIndexMap = new Map<string, number>()

  for (const row of rows) {
    const isStandalone = row.itemName === '—'
    const title = isStandalone ? `${row.weightLabel}标签` : row.groupName
    const groupKey = `${row.sectionClass}-${row.weightLabel}-${title}`
    let groupIndex = groupIndexMap.get(groupKey)
    if (groupIndex == null) {
      groupIndex = groups.length
      groupIndexMap.set(groupKey, groupIndex)
      groups.push({
        key: groupKey,
        title,
        weightLabel: row.weightLabel,
        badgeLabel: '',
        sectionClass: row.sectionClass,
        items: [],
      })
    }

    groups[groupIndex].items.push({
      key: row.key,
      label: resolveTagDisplayLabel(row),
      values: row.values,
    })
  }

  return groups.map((group) => ({
    ...group,
    badgeLabel: `${group.weightLabel} · ${group.items.length}项`,
  }))
}

function sortVisits(visits: CustomerDetail['visits']) {
  return [...visits].sort((a, b) => {
    const aTime = a.visit_date ? dayjs(`${a.visit_date} ${a.visit_time || '00:00'}`).valueOf() : dayjs(a.created_at).valueOf()
    const bTime = b.visit_date ? dayjs(`${b.visit_date} ${b.visit_time || '00:00'}`).valueOf() : dayjs(b.created_at).valueOf()
    return bTime - aTime
  })
}

function formatVisitTimelineTime(visit: CustomerDetail['visits'][number]) {
  if (visit.visit_date) {
    return `${dayjs(visit.visit_date).format('MM/DD')}${visit.visit_time ? ` ${visit.visit_time.slice(0, 5)}` : ''}`
  }
  return formatDateTime(visit.created_at)
}

function formatRecordingTitle(fileName: string, createdAt?: string | null) {
  return formatRecordingDisplayName(fileName, createdAt)
}

function formatDuration(seconds: number | null | undefined) {
  if (seconds == null || seconds < 0) return ''
  const totalSeconds = Math.round(seconds)
  const minutes = Math.floor(totalSeconds / 60)
  const remainSeconds = totalSeconds % 60
  return `${minutes}:${String(remainSeconds).padStart(2, '0')}`
}

function formatRecordingStatus(status: string | null | undefined) {
  if (!status) return '状态未知'
  return RECORDING_STATUS_LABELS[status] ?? status
}

function buildFallbackAnalysisDetail(
  recording: CustomerDetail['visits'][number]['recordings'][number],
): AnalysisDetail | null {
  if (recording.analysis_status !== 'done') return null

  const hasStructuredAnalysis =
    Boolean(recording.analysis_summary) ||
    recording.analysis_primary_demands.length > 0 ||
    recording.analysis_concerns.length > 0 ||
    recording.analysis_recommendations.length > 0 ||
    recording.analysis_evaluation_dimensions.length > 0

  if (!hasStructuredAnalysis) return null

  const durationSeconds = recording.duration_seconds ?? 0
  const evaluationDimensions = recording.analysis_evaluation_dimensions.map((dimension) => ({
    name: dimension.name,
    point_score: dimension.point_score ?? undefined,
    max_score: dimension.max_score,
    summary: dimension.summary ?? undefined,
    issues: [],
  }))
  const totalScore = evaluationDimensions.reduce((sum, dimension) => sum + (dimension.point_score ?? 0), 0)

  return {
    file_id: `recording_${recording.id}`,
    recorded_at: recording.created_at,
    audio_start_time: recording.created_at,
    audio_end_time: null,
    duration_ms: durationSeconds * 1000,
    duration_display: formatDuration(recording.duration_seconds) || '-',
    segment_count: 0,
    overall_score: recording.analysis_overall_score ?? 0,
    eval_issue_count: 0,
    overall_summary: recording.analysis_summary ?? '',
    dialogue_type: '',
    primary_demand_summary: recording.analysis_primary_demands[0] ?? null,
    focus_areas: [],
    recommendation_count: recording.analysis_recommendations.length,
    standardized_indication_count: 0,
    indication_names: [],
    concern_count: recording.analysis_concerns.length,
    tag_count: recording.analysis_profile_tags.length,
    weight_1_tag_count: 0,
    consumption_intent_present: false,
    inference_note: null,
    analysis_version: 'new',
    recording_file_name: formatRecordingTitle(recording.file_name, recording.created_at),
    customer_primary_demands: {
      inference_note: null,
      summary: recording.analysis_primary_demands.join('；'),
      items: recording.analysis_primary_demands.map((demand, index) => ({
        priority: index + 1,
        demand,
        body_part: null,
        evidence: '',
      })),
    },
    staff_recommendations: {
      summary: recording.analysis_recommendations.join('；'),
      items: recording.analysis_recommendations.map((recommendation) => ({
        recommendation,
        product_or_solution: null,
        body_part: null,
        evidence: '',
        customer_response: '未提及',
        demand_priority: [],
      })),
    },
    standardized_indications: {
      inference_note: null,
      summary: '',
      items: [],
    },
    customer_demands: {
      inference_note: null,
      focus_areas: [],
      expectation: {
        dialogue_type: '',
        entry_state: '',
        exit_state: '',
        turning_points: [],
        specific_standards: null,
      },
      product_preference: {
        preferred_products: [],
        information_sources: [],
        comparison_factors: [],
        consultant_influence: '',
      },
    },
    customer_concerns: {
      inference_note: null,
      summary: recording.analysis_concerns.join('；'),
      items: recording.analysis_concerns.map((content) => ({
        type: '其他',
        content,
        evidence: '',
      })),
    },
    customer_profile: {
      inference_note: null,
      tags: recording.analysis_profile_tags.map((tag) => {
        const [category, ...rest] = tag.split('：')
        return {
          category: rest.length > 0 ? category : '标签',
          value: rest.length > 0 ? rest.join('：') : category,
        }
      }),
    },
    consumption_intent: null,
    consultation_evaluation: {
      total_score: totalScore,
      max_total_score: 6,
      overall_score: recording.analysis_overall_score ?? undefined,
      overall_summary: recording.analysis_summary ?? undefined,
      dimensions: evaluationDimensions,
    },
    consultation_result: {
      chief_complaint_and_indications: {
        summary: recording.analysis_primary_demands.join('；'),
        primary_demands: recording.analysis_primary_demands,
        standardized_indications: [],
      },
      customer_profile_summary: {
        summary: recording.analysis_profile_tags.length > 0 ? `本次共提取 ${recording.analysis_profile_tags.length} 个画像标签。` : '',
        extracted_tag_count: recording.analysis_profile_tags.length,
        tags: recording.analysis_profile_tags.map((tag) => {
          const [category, ...rest] = tag.split('：')
          return {
            category: rest.length > 0 ? category : '标签',
            value: rest.length > 0 ? rest.join('：') : category,
          }
        }),
      },
      deal_factors: {
        summary: recording.analysis_concerns.join('；'),
        budget: null,
        concerns: recording.analysis_concerns,
        decision_factors: [],
      },
      recommended_plan: {
        summary: recording.analysis_recommendations.join('；'),
        items: recording.analysis_recommendations.map((recommendation) => ({
          plan: recommendation,
          acceptance: '未明确回应',
          evidence: null,
        })),
      },
      deal_outcome: {
        status: '未明确',
        summary: recording.analysis_summary ?? '',
        deal_items: [],
        amount: null,
        loss_reasons: [],
      },
    },
    consultation_process_evaluation: {
      total_score: undefined,
      max_total_score: undefined,
      overall_score: recording.analysis_overall_score ?? undefined,
      overall_summary: recording.analysis_summary ?? '',
      sections: [],
    },
  }
}

function resolveRecordingAnalysisFileId(recording: CustomerDetail['visits'][number]['recordings'][number]) {
  if (recording.analysis_status !== 'done') return null
  return `recording_${recording.id}`
}

function resolveCustomerDetailErrorMessage(error: unknown) {
  if (error instanceof HTTPError) {
    if (error.response.status === 403 || error.response.status === 404) {
      return '当前账号暂无权限查看该客户档案'
    }
    if (error.response.status >= 500) {
      return '服务器处理客户档案时出错，请稍后重试'
    }
  }
  if (error instanceof Error && error.message.trim()) {
    return error.message
  }
  return '请稍后重试'
}

function resolveCustomerInsightErrorMessage(error: unknown) {
  if (error instanceof HTTPError) {
    if (error.response.status === 403 || error.response.status === 404) {
      return '当前账号暂无权限查看这部分客户信息'
    }
    if (error.response.status >= 500) {
      return '服务器处理客户补充信息时出错，请稍后重试'
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

export function WecomCustomerDetailPage() {
  const { customerId } = useParams<{ customerId: string }>()
  const location = useLocation()
  const backTo = `${location.pathname}${location.search}`
  const [tagsExpanded, setTagsExpanded] = useState(true)

  const { data: customer, isLoading: customerLoading, isError: customerIsError, error: customerError } = useQuery({
    queryKey: ['wecom', 'customer-detail', customerId],
    queryFn: () => fetchCustomerDetail(customerId!),
    enabled: !!customerId,
  })

  const { data: tagCompletion, error: tagCompletionError, isError: tagCompletionIsError } = useQuery({
    queryKey: ['wecom', 'customer-tag-completion', customerId],
    queryFn: () => fetchCustomerTagCompletion(customerId!),
    enabled: !!customerId,
  })

  const timelineVisits = customer ? sortVisits(customer.visits) : []
  const visitRecordings = timelineVisits.flatMap((visit) => visit.recordings)
  const visitRecordingAnalysisQueries = useQueries({
    queries: visitRecordings.map((recording) => {
      const analysisFileId = resolveRecordingAnalysisFileId(recording)
      return {
        queryKey: ['wecom', 'customer-detail-recording-analysis', recording.id, analysisFileId],
        queryFn: () => fetchAnalysisDetail(analysisFileId!),
        enabled: !!analysisFileId,
        retry: false,
        staleTime: 60_000,
      }
    }),
  })

  if (customerIsError) {
    return <div className="wc-empty">客户档案加载失败：{resolveCustomerDetailErrorMessage(customerError)}</div>
  }

  if (customerLoading || !customer) {
    return <div className="wc-empty">加载中…</div>
  }

  const metaTags = buildCustomerMetaTags(customer)
  const tagTableRows = tagCompletionIsError ? [] : buildTagTableRows(tagCompletion?.categories ?? [])
  const tagDisplayGroups = buildTagDisplayGroups(tagTableRows)
  const extractedTagCount = tagTableRows.length
  const hasRecordings = customer.recording_count > 0
  const buildRecordingLink = (recordingId: string, visitId: string | null) => {
    const params = new URLSearchParams()
    params.set('from_customer_id', customer.id)
    params.set('back_to', backTo)
    if (visitId) params.set('from_visit_id', visitId)
    return `/wecom/recordings/${recordingId}?${params.toString()}`
  }
  const recordingAnalysisQueryMap = new Map(
    visitRecordings.map((recording, index) => [recording.id, visitRecordingAnalysisQueries[index]]),
  )
  return (
    <div className="wc-page wc-customer-detail-page">
      <div
        className={`wc-row wc-row--stacked wc-row--card wc-customer-row wc-customer-detail-page__hero ${
          hasRecordings ? 'wc-customer-row--linked' : 'wc-customer-row--unlinked'
        }`}
      >
        <div className="wc-row__main">
          <div className="wc-customer-row__identity">
            <div className="wc-customer-row__top">
              <strong>{customer.name}</strong>
              <span className="wc-customer-row__meta-item wc-customer-row__meta-item--code">
                <IdcardOutlined />
                <span>{customer.external_customer_code || '无客户号'}</span>
              </span>
              {metaTags.map((tag) => (
                <span key={`detail-${customer.id}-${tag}`} className="wc-customer-row__meta-item wc-customer-row__meta-item--tag">
                  <span>{tag}</span>
                </span>
              ))}
            </div>
            <div className="wc-customer-row__meta wc-customer-row__meta--secondary">
              <span className="wc-customer-row__meta-item wc-customer-row__meta-item--visit">
                <CalendarOutlined />
                <span>最近到诊 {formatDateTime(customer.last_visit_at)}</span>
              </span>
              <span className="wc-customer-row__meta-item wc-customer-row__meta-item--visit">
                <ProfileOutlined />
                <span>到诊 {customer.visit_count} 次</span>
              </span>
            </div>
          </div>
        </div>
      </div>

      <div className="wc-card wc-card--mint wc-customer-detail-page__tags-shell">
        <div className="wc-card__head">
          <h2 className="wc-card__title">客户标签</h2>
          <div className="wc-card__head-actions">
            <span className={`wc-chip ${tagCompletionIsError ? 'wc-chip--amber' : 'wc-chip--default'}`}>
              {tagCompletionIsError ? '异常' : `已提取 ${extractedTagCount}`}
            </span>
            <CollapseToggle
              expanded={tagsExpanded}
              label={tagsExpanded ? '收起' : '展开'}
              onClick={() => setTagsExpanded((prev) => !prev)}
            />
          </div>
        </div>

        {!tagsExpanded ? null : tagCompletionIsError ? (
          <div className="wc-empty">标签信息暂时加载失败：{resolveCustomerInsightErrorMessage(tagCompletionError)}</div>
        ) : tagDisplayGroups.length > 0 ? (
          <div className="wc-customer-tag-panel">
            <div className="wc-customer-tag-board">
              {tagDisplayGroups.map((group) => (
                <section key={group.key} className={`wc-customer-tag-group ${group.sectionClass}`}>
                  <div className="wc-customer-tag-group__head">
                    <strong>{group.title}</strong>
                    <span className="wc-customer-tag-group__count">{group.badgeLabel}</span>
                  </div>
                  <div className="wc-customer-tag-group__list">
                    {group.items.map((item) => (
                      <div key={item.key} className="wc-customer-tag-pill">
                        <span className="wc-customer-tag-pill__name">{item.label}</span>
                        <span className="wc-customer-tag-pill__value">{item.values.join('、')}</span>
                      </div>
                    ))}
                  </div>
                </section>
              ))}
            </div>
          </div>
        ) : (
          <div className="wc-empty">当前还没有可展示的标签提取结果</div>
        )}
      </div>

      <div className="wc-card wc-card--slate wc-customer-detail-page__timeline-shell">
        <div className="wc-card__head">
          <h2 className="wc-card__title">来访记录</h2>
          <span className="wc-chip wc-chip--blue">共 {timelineVisits.length} 次</span>
        </div>
        {timelineVisits.length === 0 ? (
          <div className="wc-empty">当前客户还没有来访记录</div>
        ) : (
          <div className="wc-customer-timeline wc-customer-detail-page__visit-list">
            {timelineVisits.map((visit) => {
              const visitStatus = VISIT_STATUS_MAP[visit.status]
              const summaryConsultantName = visit.consultant_name ?? null
              const summaryCreatedAt = formatVisitTimelineTime(visit)
              return (
                <div key={visit.id} className="wc-customer-timeline__item">
                  <div className="wc-customer-timeline__rail">
                    <span className="wc-customer-timeline__dot" />
                  </div>
                  <div className="wc-customer-timeline__content">
                    <div className="wc-customer-timeline-card wc-customer-detail-page__latest-visit-card">
                      <div className="wc-customer-timeline-card__head">
                        <div className="wc-customer-timeline-card__title-wrap">
                          <strong>{summaryCreatedAt}</strong>
                          <span>{summaryConsultantName ? `接待咨询师：${summaryConsultantName}` : '接待咨询师：未记录'}</span>
                        </div>
                        <span className="wc-chip wc-chip--blue">
                          {visitStatus?.label ?? visit.status}
                        </span>
                      </div>

                      <div className="wc-customer-timeline-card__recordings">
                        <div className="wc-customer-timeline-card__recordings-head">
                          <strong>关联录音</strong>
                          <span>{visit.recordings.length > 0 ? `${visit.recordings.length} 条` : '无'}</span>
                        </div>
                        {visit.recordings.length > 0 ? (
                          <div className="wc-customer-timeline-recordings">
                            {visit.recordings.map((recording) => {
                              const recordingMeta = [
                                formatDateTime(recording.created_at),
                                recording.duration_seconds != null ? formatDuration(recording.duration_seconds) : '',
                                recording.staff_name || '',
                                formatRecordingStatus(recording.status),
                              ]
                                .filter(Boolean)
                                .join(' · ')
                              return (
                                <Link
                                  key={recording.id}
                                  className="wc-customer-timeline-recording wc-customer-detail-page__recording-link"
                                  to={buildRecordingLink(recording.id, visit.id)}
                                >
                                  <div>
                                    <strong>{formatRecordingTitle(recording.file_name, recording.created_at)}</strong>
                                    <span>{recordingMeta}</span>
                                  </div>
                                  <span className="wc-chip wc-chip--blue">查看录音</span>
                                </Link>
                              )
                            })}
                          </div>
                        ) : (
                          <div className="wc-empty wc-empty--compact">无</div>
                        )}
                      </div>

                      {visit.recordings.length > 0 ? (
                        <div className="wc-customer-detail-page__latest-visit-analysis">
                          <div className="wc-customer-timeline-card__recordings-head">
                            <strong>本次来访分析</strong>
                          </div>
                          <div className="wc-customer-detail-page__latest-visit-analysis-list">
                            {visit.recordings.map((recording) => {
                              const analysisQuery = recordingAnalysisQueryMap.get(recording.id)
                              const analysisData = analysisQuery?.data ?? buildFallbackAnalysisDetail(recording)
                              const isAnalysisLoading = Boolean(analysisQuery?.isLoading) && !analysisData
                              const hasAnalysisError = Boolean(analysisQuery?.error) && !analysisData
                              let analysisContent: ReactNode

                              if (recording.analysis_status === 'done') {
                                if (isAnalysisLoading) {
                                  analysisContent = <div className="wc-empty wc-empty--compact">分析详情加载中…</div>
                                } else if (hasAnalysisError) {
                                  analysisContent = <div className="wc-empty wc-empty--compact">分析详情加载失败，请稍后重试</div>
                                } else if (analysisData) {
                                  analysisContent = (
                                    <div className="wc-analysis-detail-shell">
                                      <AnalysisDetailContent
                                        data={analysisData}
                                        embedded
                                        embeddedSectionDefaultOpen={false}
                                        embeddedSimplified
                                        recordingId={recording.id}
                                        recordingLinkBase={null}
                                        showCustomerTags={false}
                                      />
                                    </div>
                                  )
                                } else {
                                  analysisContent = <div className="wc-empty wc-empty--compact">暂无分析详情</div>
                                }
                              } else if (recording.analysis_status === 'pending' || recording.analysis_status === 'running') {
                                analysisContent = <div className="wc-empty wc-empty--compact">分析结果整理中…</div>
                              } else {
                                analysisContent = <div className="wc-empty wc-empty--compact">当前暂无可用分析结果</div>
                              }

                              return (
                                <div key={recording.id} className="wc-customer-detail-page__analysis-item">
                                  {analysisContent}
                                </div>
                              )
                            })}
                          </div>
                        </div>
                      ) : null}
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}

export default WecomCustomerDetailPage
