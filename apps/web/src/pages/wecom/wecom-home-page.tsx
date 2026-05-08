import * as ww from '@wecom/jssdk'
import { useQuery } from '@tanstack/react-query'
import { HTTPError } from 'ky'
import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { isHospitalAdminOrAbove, roleLabel } from '@/app/roles'
import { useAuth } from '@/app/use-auth'
import { isWecomBrowser } from '@/app/wecom'
import {
  fetchDashboard,
  type ConcernTypeItem,
  type DashboardBreakdownItem,
  type DashboardExampleRecordingItem,
  type ProcessEvaluationIssueItem,
  type ProcessEvaluationSectionStats,
  type ProcessEvaluationSummaryStats,
  type StaffStatsItem,
} from '@/api/dashboard'
import { fetchWecomJsSdkConfig } from '@/api/wecom'
import { WecomPageIntro } from '@/components/wecom-page-intro'
import { formatRecordingDisplayName } from '@/utils/recording-display'
import { beijingNow, formatBeijingTime } from '@/utils/time'

type DatePreset = 'today' | '3d' | '7d' | 'all'
type DashboardScopeMode = 'all' | 'mine'

const DATE_PRESETS: Array<{ value: DatePreset; label: string }> = [
  { value: 'today', label: '今天' },
  { value: '3d', label: '近3天' },
  { value: '7d', label: '近7天' },
  { value: 'all', label: '全部时间' },
]

const STAFF_PAGE_SIZE = 20
const WECOM_SHARE_IMAGE_URL = 'https://res.mail.qq.com/node/ww/wwmng/style/images/index_share_logo$13c64306.png'

let wecomShareReadyPromise: Promise<void> | null = null

function resolveStaffScope(staffId: string | null | undefined) {
  return staffId ?? undefined
}

function resolveDateRange(preset: DatePreset) {
  const today = beijingNow()
  switch (preset) {
    case 'today':
      return { date_from: today.format('YYYY-MM-DD'), date_to: today.format('YYYY-MM-DD') }
    case '3d':
      return { date_from: today.subtract(2, 'day').format('YYYY-MM-DD'), date_to: today.format('YYYY-MM-DD') }
    case '7d':
      return { date_from: today.subtract(6, 'day').format('YYYY-MM-DD'), date_to: today.format('YYYY-MM-DD') }
    default:
      return {}
  }
}

function formatInteger(value: number | null | undefined) {
  if (value == null || Number.isNaN(value)) return '--'
  return Math.round(value).toLocaleString('zh-CN')
}

function formatDecimal(value: number | null | undefined) {
  if (value == null || Number.isNaN(value)) return '--'
  return Number.isInteger(value) ? String(value) : value.toFixed(1)
}

function formatPercentValue(value: number | null | undefined) {
  if (value == null || Number.isNaN(value)) return '--'
  return `${Math.round(value)}%`
}

function formatHospitalLabel(hospitalCode: string | null | undefined, hospitalName: string | null | undefined) {
  const normalizedCode = (hospitalCode ?? '').trim()
  const normalizedName = (hospitalName ?? '').trim()
  if (normalizedName) return normalizedName
  if (normalizedCode) return normalizedCode
  return '--'
}

function resolveOverviewErrorMessage(error: unknown) {
  if (error instanceof HTTPError) {
    if (error.response.status === 403 || error.response.status === 404) {
      return '当前账号暂无权限查看'
    }
    if (error.response.status >= 500) {
      return '服务器处理请求时出错，请稍后重试'
    }
  }
  if (error instanceof Error && error.message.trim()) {
    return error.message
  }
  return '请稍后重试'
}

function getBreakdownLabel(item: DashboardBreakdownItem) {
  return item.label || item.detail || item.key || '未命名'
}

function getBreakdownMeta(item: DashboardBreakdownItem) {
  const parts = [`${formatInteger(item.count)}次`]
  if (item.customer_count > 0) parts.push(`${formatInteger(item.customer_count)}位客户`)
  if (item.task_count > 0) parts.push(`${formatInteger(item.task_count)}条录音`)
  return parts.join(' · ')
}

function OverviewMetricCard({
  label,
  value,
  hint,
  tone = 'default',
}: {
  label: string
  value: string
  hint?: string
  tone?: 'default' | 'blue' | 'green' | 'amber'
}) {
  return (
    <div className={`wc-overview-metric wc-overview-metric--${tone}`}>
      <span className="wc-overview-metric__label">{label}</span>
      <strong className="wc-overview-metric__value">{value}</strong>
      <small
        aria-hidden={!hint}
        className={`wc-overview-metric__hint${hint ? '' : ' wc-overview-metric__hint--placeholder'}`}
      >
        {hint ?? '\u00A0'}
      </small>
    </div>
  )
}

function ProcessIssuePanel({
  issues,
  title,
  onClose,
}: {
  issues: ProcessEvaluationIssueItem[]
  title: string
  onClose: () => void
}) {
  return (
    <div className="wc-process-issue-panel">
      <div className="wc-process-issue-panel__head">
        <div>
          <strong>{title}</strong>
          <span>{formatInteger(issues.length)} 个具体问题</span>
        </div>
        <button onClick={onClose} type="button">收起</button>
      </div>
      {issues.length === 0 ? (
        <div className="wc-empty wc-empty--compact">当前范围内没有可展示的问题明细。</div>
      ) : (
        <div className="wc-process-issue-list">
          {issues.map((issue, index) => {
            const displayName = formatRecordingDisplayName(issue.file_name, issue.recorded_at)
            const recordedAt = issue.recorded_at ? formatBeijingTime(issue.recorded_at, 'MM-DD HH:mm') : '未知时间'
            const checkpoint = [issue.checkpoint_code, issue.checkpoint_name].filter(Boolean).join(' ')
            return (
              <article key={`${issue.analysis_task_id}-${issue.section_code}-${issue.checkpoint_code ?? 'section'}-${index}`} className="wc-process-issue-card">
                <div className="wc-process-issue-card__top">
                  <span>{issue.section_name}</span>
                  {checkpoint ? <em>{checkpoint}</em> : null}
                </div>
                <p>{issue.description || '未填写问题描述'}</p>
                <div className="wc-process-issue-card__meta">
                  <span>{issue.staff_name || '未绑定员工'}</span>
                  <span>{recordedAt}</span>
                  <Link to={`/wecom/recordings/${issue.recording_id}`}>{displayName}</Link>
                </div>
                {issue.evidence ? (
                  <details className="wc-process-issue-card__evidence">
                    <summary>查看原文证据</summary>
                    <pre>{issue.evidence}</pre>
                  </details>
                ) : null}
              </article>
            )
          })}
        </div>
      )}
    </div>
  )
}

function ProcessEvaluationOverview({
  summary,
  sections,
  issues,
}: {
  summary?: ProcessEvaluationSummaryStats
  sections: ProcessEvaluationSectionStats[]
  issues: ProcessEvaluationIssueItem[]
}) {
  const [selectedIssueSection, setSelectedIssueSection] = useState<string | null>(null)
  if (!summary || summary.evaluated_count === 0) {
    return (
      <section className="wc-overview-panel wc-overview-panel--process">
        <div className="wc-home-page__section-head">
          <h3 className="wc-home-page__section-title">面诊过程评价</h3>
          <span className="wc-chip wc-chip--default">暂无统计</span>
        </div>
        <div className="wc-empty wc-empty--compact">当前范围内还没有可统计的面诊过程评价。</div>
      </section>
    )
  }
  const selectedIssues = selectedIssueSection === 'all'
    ? issues
    : issues.filter((issue) => issue.section_code === selectedIssueSection)
  const selectedSectionName = selectedIssueSection === 'all'
    ? '全部面诊过程问题'
    : sections.find((section) => section.code === selectedIssueSection)?.name ?? '面诊过程问题'

  return (
    <section className="wc-overview-panel wc-overview-panel--process">
      <div className="wc-home-page__section-head">
        <h3 className="wc-home-page__section-title">面诊过程评价</h3>
        <span className="wc-chip wc-chip--default">{formatInteger(summary.evaluated_count)}条已评价</span>
      </div>
      <div className="wc-process-hero">
        <div className="wc-process-hero__score">
          <span>平均得分</span>
          <strong>{formatDecimal(summary.avg_total_score)}分</strong>
          <small>满分 {formatDecimal(summary.max_total_score)}分 · 平均达标 {formatDecimal(summary.avg_passed_sections)} 项</small>
        </div>
        <div className="wc-process-hero__side">
          <div>
            <span>达标率</span>
            <strong>{formatPercentValue(summary.pass_rate)}</strong>
          </div>
          <button
            className="wc-process-issue-trigger"
            disabled={summary.issue_count <= 0}
            onClick={() => setSelectedIssueSection((current) => current === 'all' ? null : 'all')}
            type="button"
          >
            <span>问题数</span>
            <strong>{formatInteger(summary.issue_count)}</strong>
          </button>
        </div>
      </div>
      {selectedIssueSection ? (
        <ProcessIssuePanel
          issues={selectedIssues}
          onClose={() => setSelectedIssueSection(null)}
          title={selectedSectionName}
        />
      ) : null}
      <div className="wc-process-section-list">
        {sections.map((section) => (
          <div key={section.code || section.name} className="wc-process-section-card">
            <div className="wc-process-section-card__top">
              <div>
                <strong>{section.name}</strong>
                <small>
                  达标 {formatPercentValue(section.pass_rate)}
                  {section.issue_count > 0 ? (
                    <button
                      className="wc-process-section-card__issue-btn"
                      onClick={() => setSelectedIssueSection((current) => current === section.code ? null : section.code)}
                      type="button"
                    >
                      {formatInteger(section.issue_count)}个问题
                    </button>
                  ) : null}
                </small>
              </div>
              <span>{formatDecimal(section.avg_score)}分</span>
            </div>
            <div className="wc-process-section-card__bar">
              <i style={{ width: `${Math.max(0, Math.min(section.pass_rate, 100))}%` }} />
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}

function BreakdownList({
  items,
  emptyText,
  compact = false,
}: {
  items: DashboardBreakdownItem[]
  emptyText: string
  compact?: boolean
}) {
  if (items.length === 0) {
    return <div className="wc-empty wc-empty--compact">{emptyText}</div>
  }

  return (
    <div className={`wc-overview-breakdown${compact ? ' wc-overview-breakdown--compact' : ''}`}>
      {items.map((item, index) => (
        <div key={`${item.key}-${index}`} className="wc-overview-breakdown__item">
          <span className="wc-overview-breakdown__rank">{index + 1}</span>
          <div>
            <strong>{getBreakdownLabel(item)}</strong>
            <small>{getBreakdownMeta(item)}</small>
          </div>
        </div>
      ))}
    </div>
  )
}

function ConcernList({
  items,
}: {
  items: ConcernTypeItem[]
}) {
  if (items.length === 0) {
    return <div className="wc-empty wc-empty--compact">当前范围内还没有提取到客户顾虑。</div>
  }

  return (
    <div className="wc-overview-concerns">
      {items.map((item, index) => (
        <div key={`${item.type}-${index}`} className="wc-overview-concern">
          <span>{item.type || '未分类顾虑'}</span>
          <strong>{formatInteger(item.count)}</strong>
        </div>
      ))}
    </div>
  )
}

function formatDuration(seconds: number | null | undefined) {
  if (seconds == null || Number.isNaN(seconds)) return '--:--'
  const mins = Math.floor(seconds / 60)
  const secs = Math.floor(seconds % 60)
  return `${mins}:${String(secs).padStart(2, '0')}`
}

function buildExampleDetailUrl(recordingId: string) {
  const redirectPath = `/wecom/recordings/${recordingId}`
  return `${window.location.origin}/login?wecom=1&redirect=${encodeURIComponent(redirectPath)}`
}

function buildExampleSharePayload(item: DashboardExampleRecordingItem) {
  const displayName = formatRecordingDisplayName(item.file_name, item.recorded_at)
  const recordedAt = item.recorded_at ? formatBeijingTime(item.recorded_at, 'MM-DD HH:mm') : '未知时间'
  const durationLabel = formatDuration(item.duration_seconds)
  const summaryText = item.summary?.trim() || '暂无摘要'
  return {
    title: `工牌录音示例：${displayName}`,
    desc: `${item.staff_name || '未绑定员工'} · ${formatDecimal(item.total_score)}/${formatDecimal(item.max_score)}分 · ${recordedAt} · ${durationLabel}\n${summaryText}`,
    link: buildExampleDetailUrl(item.recording_id),
    imgUrl: WECOM_SHARE_IMAGE_URL,
  }
}

function buildExampleShareText(item: DashboardExampleRecordingItem) {
  const displayName = formatRecordingDisplayName(item.file_name, item.recorded_at)
  const recordedAt = item.recorded_at ? formatBeijingTime(item.recorded_at, 'MM-DD HH:mm') : '未知时间'
  const detailUrl = buildExampleDetailUrl(item.recording_id)
  return [
    `工牌录音示例：${displayName}`,
    `员工：${item.staff_name || '未绑定员工'}`,
    `面诊过程评分：${formatDecimal(item.total_score)}/${formatDecimal(item.max_score)}分`,
    `录音时间：${recordedAt}`,
    `录音时长：${formatDuration(item.duration_seconds)}`,
    `摘要：${item.summary || '暂无摘要'}`,
    `查看详情：${detailUrl}`,
  ].join('\n')
}

function copyTextToClipboard(text: string) {
  if (navigator.clipboard?.writeText) {
    return navigator.clipboard.writeText(text)
  }
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.style.position = 'fixed'
  textarea.style.opacity = '0'
  document.body.appendChild(textarea)
  textarea.focus()
  textarea.select()
  document.execCommand('copy')
  document.body.removeChild(textarea)
  return Promise.resolve()
}

async function ensureWecomShareSdkReady() {
  if (!isWecomBrowser() || typeof window === 'undefined') {
    return false
  }
  if (wecomShareReadyPromise) {
    await wecomShareReadyPromise
    return true
  }
  wecomShareReadyPromise = (async () => {
    const config = await fetchWecomJsSdkConfig(window.location.href)
    ww.register({
      corpId: config.corp_id,
      jsApiList: ['shareAppMessage', 'showOptionMenu', 'onMenuShareAppMessage', 'onMenuShareWechat'],
      async getConfigSignature(url) {
        const next = await fetchWecomJsSdkConfig(url)
        return {
          timestamp: next.timestamp,
          nonceStr: next.nonceStr,
          signature: next.signature,
        }
      },
    })
    await ww.ensureConfigReady()
    try {
      await ww.showOptionMenu()
    } catch {
      // ignore menu display errors and keep direct share available
    }
  })().catch((error) => {
    wecomShareReadyPromise = null
    throw error
  })
  await wecomShareReadyPromise
  return true
}

function registerWecomShareMenu(item: DashboardExampleRecordingItem) {
  const payload = buildExampleSharePayload(item)
  ww.onMenuShareAppMessage(payload)
  ww.onMenuShareWechat(payload)
}

async function shareExampleRecording(item: DashboardExampleRecordingItem) {
  const payload = buildExampleSharePayload(item)
  const text = buildExampleShareText(item)
  if (isWecomBrowser()) {
    try {
      await ensureWecomShareSdkReady()
      registerWecomShareMenu(item)
      await ww.shareAppMessage(payload)
      return '已打开企业微信会话分享面板'
    } catch {
      // fallback below
    }
  }
  const shareApi = navigator as Navigator & {
    share?: (data: { title?: string; text?: string; url?: string }) => Promise<void>
  }
  if (shareApi.share) {
    await shareApi.share({
      title: payload.title,
      text,
      url: payload.link,
    })
    return '已打开系统分享面板'
  }
  await copyTextToClipboard(text)
  return isWecomBrowser()
    ? '企业微信原生分享暂不可用，已复制分享文案'
    : '已复制分享文案，可粘贴到企业微信转发'
}

function ExampleRecordingList({
  items,
  tone,
  title,
  emptyText,
  onShare,
}: {
  items: DashboardExampleRecordingItem[]
  tone: 'positive' | 'negative'
  title: string
  emptyText: string
  onShare: (item: DashboardExampleRecordingItem) => void
}) {
  if (items.length === 0) {
    return (
      <div className={`wc-example-recordings__group wc-example-recordings__group--${tone}`}>
        <div className="wc-example-recordings__group-head">
          <strong>{title}</strong>
          <span>暂无</span>
        </div>
        <div className="wc-empty wc-empty--compact">{emptyText}</div>
      </div>
    )
  }

  return (
    <div className={`wc-example-recordings__group wc-example-recordings__group--${tone}`}>
      <div className="wc-example-recordings__group-head">
        <strong>{title}</strong>
        <span>Top {items.length}</span>
      </div>
      <div className="wc-example-recordings__list">
        {items.map((item, index) => {
          const displayName = formatRecordingDisplayName(item.file_name, item.recorded_at)
          const recordedAt = item.recorded_at ? formatBeijingTime(item.recorded_at, 'MM-DD HH:mm') : '未知时间'
          const summaryText = item.summary?.trim() || '暂无摘要'
          return (
            <article key={`${tone}-${item.recording_id}-${item.analysis_task_id}`} className="wc-example-recording-card">
              <div className="wc-example-recording-card__rank">{index + 1}</div>
              <div className="wc-example-recording-card__body">
                <div className="wc-example-recording-card__top">
                  <div>
                    <strong title={displayName}>{displayName}</strong>
                    <small>{item.staff_name || '未绑定员工'}</small>
                  </div>
                  <span>{formatDecimal(item.total_score)} / {formatDecimal(item.max_score)}</span>
                </div>
                <div className="wc-example-recording-card__meta">
                  <div className="wc-example-recording-card__meta-item">
                    <span className="wc-example-recording-card__meta-label">时间</span>
                    <strong className="wc-example-recording-card__meta-value">{recordedAt}</strong>
                  </div>
                  <div className="wc-example-recording-card__meta-item">
                    <span className="wc-example-recording-card__meta-label">时长</span>
                    <strong className="wc-example-recording-card__meta-value">{formatDuration(item.duration_seconds)}</strong>
                  </div>
                </div>
                <div className="wc-example-recording-card__summary">
                  <p>{summaryText}</p>
                </div>
                <div className="wc-example-recording-card__actions">
                  <Link to={`/wecom/recordings/${item.recording_id}`}>查看详情</Link>
                  <button onClick={() => onShare(item)} type="button">分享到企微</button>
                </div>
              </div>
            </article>
          )
        })}
      </div>
    </div>
  )
}

function StaffStatsTable({
  items,
}: {
  items: StaffStatsItem[]
}) {
  if (items.length === 0) {
    return <div className="wc-empty wc-empty--compact">当前范围内还没有员工统计数据。</div>
  }

  return (
    <div className="wc-staff-stats-table" role="table" aria-label="员工统计明细">
      <div className="wc-staff-stats-table__row wc-staff-stats-table__row--head" role="row">
        <span role="columnheader">员工</span>
        <span role="columnheader">录音</span>
        <span role="columnheader">关联</span>
        <span role="columnheader">接诊</span>
        <span role="columnheader">成交</span>
        <span role="columnheader">均分</span>
      </div>
      {items.map((item) => (
        <div key={item.staff_id} className="wc-staff-stats-table__row" role="row">
          <div className="wc-staff-stats-table__staff" role="cell">
            <strong>{item.staff_name}</strong>
            {item.job_label ? <small>{item.job_label}</small> : null}
          </div>
          <span role="cell">{formatInteger(item.recording_count)}</span>
          <span role="cell">{formatInteger(item.linked_visit_count)}</span>
          <span role="cell">{formatInteger(item.visit_count)}</span>
          <span role="cell">{formatInteger(item.closed_won_count)}</span>
          <span role="cell">{item.avg_score == null ? '--' : `${formatDecimal(item.avg_score)}`}</span>
        </div>
      ))}
    </div>
  )
}

export function WecomHomePage() {
  const auth = useAuth()
  const userRole = auth.status === 'authenticated' ? auth.user.role : 'staff'
  const canSeeScopedData = isHospitalAdminOrAbove(userRole)
  const rawStaffId = auth.status === 'authenticated' ? resolveStaffScope(auth.user.staff_id) : undefined
  const authHospitalCode = auth.status === 'authenticated' ? auth.user.hospital_code ?? null : null
  const authHospitalName = auth.status === 'authenticated' ? auth.user.hospital_name ?? null : null
  const [datePreset, setDatePreset] = useState<DatePreset>('today')
  const [dashboardScope, setDashboardScope] = useState<DashboardScopeMode>('all')
  const [selectedHospitalCode, setSelectedHospitalCode] = useState('')
  const [selectedStaffId, setSelectedStaffId] = useState('')
  const [staffPage, setStaffPage] = useState(1)
  const [shareFeedback, setShareFeedback] = useState<string | null>(null)
  const dateRange = useMemo(() => resolveDateRange(datePreset), [datePreset])
  const requestedScope: DashboardScopeMode = !canSeeScopedData && rawStaffId
    ? 'mine'
    : dashboardScope === 'mine' && rawStaffId
      ? 'mine'
      : 'all'
  const requestedHospitalCode = requestedScope === 'all' ? selectedHospitalCode || undefined : undefined
  const requestedStaffId = requestedScope === 'all' ? selectedStaffId || undefined : undefined

  const {
    data: dashboard,
    error: dashboardError,
    isError: dashboardIsError,
    isLoading: dashboardLoading,
  } = useQuery({
    queryKey: ['wecom', 'home', 'dashboard', userRole, rawStaffId, datePreset, requestedScope, requestedHospitalCode ?? null, requestedStaffId ?? null],
    queryFn: () =>
      fetchDashboard({
        hospital_code: requestedHospitalCode,
        scope_mode: requestedScope,
        staff_id: requestedStaffId,
        ...dateRange,
      }),
  })

  const canSelectOverviewScope = dashboard?.dashboard_can_select_scope ?? (canSeeScopedData && Boolean(rawStaffId))
  const canSelectHospital = requestedScope === 'all' && (dashboard?.dashboard_can_select_hospital ?? false)
  const activeHospitalCode = dashboard?.dashboard_hospital_code ?? requestedHospitalCode ?? null
  const hospitalSelectValue = requestedHospitalCode ?? activeHospitalCode ?? ''
  const canSelectScopeTarget = canSelectOverviewScope || (dashboard?.dashboard_can_select_staff ?? false)
  const scopeSelectValue = requestedStaffId
    ? `staff:${requestedStaffId}`
    : requestedScope
  const displayHospitalName = dashboard?.dashboard_hospital_name ?? authHospitalName
  const displayHospitalLabel = formatHospitalLabel(activeHospitalCode ?? authHospitalCode, displayHospitalName)
  const greetingName = auth.status === 'authenticated' ? (auth.user.display_name || auth.user.username) : '工作台'
  const introDescription = auth.status === 'authenticated'
    ? `${beijingNow().format('MM月DD日 dddd')} · ${roleLabel(userRole)}`
    : '聚焦录音、客户与接诊进展'

  const totalRecordings = dashboard?.total_recordings ?? 0
  const recordingsWithVisits = dashboard?.recordings_with_visits ?? 0
  const processSummary = dashboard?.process_evaluation_summary
  const analysisSampleCount = processSummary?.evaluated_count
    ?? dashboard?.result_analysis_modules?.[0]?.analyzed_count
    ?? dashboard?.done_count
    ?? 0
  const hotIndications = (dashboard?.indication_breakdown ?? []).slice(0, 5)
  const hotTags = (dashboard?.tag_breakdown ?? []).slice(0, 5)
  const hotConcerns = (dashboard?.concern_types ?? []).slice(0, 5)
  const defaultShareItem = useMemo(
    () => dashboard?.positive_example_recordings?.[0] ?? dashboard?.negative_example_recordings?.[0] ?? null,
    [dashboard],
  )
  const staffStats = useMemo(() => {
    const items = [...(dashboard?.staff_stats ?? [])]
    const scopedItems = requestedStaffId
      ? items.filter((item) => item.staff_id === requestedStaffId)
      : canSeeScopedData || !rawStaffId
        ? items
        : items.filter((item) => item.staff_id === rawStaffId)
    return scopedItems.sort((left, right) => (
      right.recording_count - left.recording_count
      || right.linked_visit_count - left.linked_visit_count
      || right.visit_count - left.visit_count
      || right.closed_won_count - left.closed_won_count
      || (right.avg_score ?? -1) - (left.avg_score ?? -1)
      || left.staff_name.localeCompare(right.staff_name, 'zh-CN')
    ))
  }, [dashboard, canSeeScopedData, rawStaffId, requestedStaffId])
  const staffPageCount = Math.max(1, Math.ceil(staffStats.length / STAFF_PAGE_SIZE))
  const normalizedStaffPage = Math.min(staffPage, staffPageCount)
  const visibleStaffStats = staffStats.slice(
    (normalizedStaffPage - 1) * STAFF_PAGE_SIZE,
    normalizedStaffPage * STAFF_PAGE_SIZE,
  )
  const scopeLabel = dashboard?.dashboard_staff_name ?? (requestedScope === 'mine' ? '我的' : '全部员工')
  const filterColumnClass = 'wc-filter-bar__row wc-filter-bar__row--filters wc-filter-bar__row--filters--3'
  const handleShareExample = async (item: DashboardExampleRecordingItem) => {
    try {
      const message = await shareExampleRecording(item)
      setShareFeedback(message)
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') {
        return
      }
      try {
        await copyTextToClipboard(buildExampleShareText(item))
        setShareFeedback('分享面板未打开，已改为复制分享文案')
      } catch {
        setShareFeedback('分享失败，请进入详情后手动复制链接')
      }
    }
  }

  useEffect(() => {
    if (!defaultShareItem || !isWecomBrowser()) {
      return
    }
    let cancelled = false
    void (async () => {
      try {
        await ensureWecomShareSdkReady()
        if (!cancelled) {
          registerWecomShareMenu(defaultShareItem)
        }
      } catch {
        // keep page usable even when WeCom share registration fails
      }
    })()
    return () => {
      cancelled = true
    }
  }, [defaultShareItem])

  return (
    <div className="wc-page wc-home-page wc-home-page--workflow">
      <WecomPageIntro
        description={introDescription}
        eyebrow="企业微信工作台"
        title={`你好，${greetingName}`}
        tone="sky"
      />

      <section className="wc-card wc-overview-workbench">
        <div className="wc-card__head wc-home-page__head">
          <div>
            <h2 className="wc-card__title">业务总览</h2>
          </div>
          <span className="wc-chip wc-chip--blue">{scopeLabel}</span>
        </div>

        <div className="wc-home-page__toolbar">
          <div className="wc-filter-bar wc-filter-bar--embedded wc-filter-bar--compact">
            <div className={filterColumnClass}>
              {canSelectHospital ? (
                <select
                  className="wc-select wc-select--compact wc-filter-bar__select"
                  onChange={(event) => {
                    setSelectedHospitalCode(event.target.value)
                    setSelectedStaffId('')
                    setStaffPage(1)
                  }}
                  value={hospitalSelectValue}
                >
                  {dashboard?.dashboard_hospital_options.map((item) => (
                    <option key={item.hospital_code} value={item.hospital_code}>
                      {formatHospitalLabel(item.hospital_code, item.hospital_name)}
                    </option>
                  ))}
                </select>
              ) : canSeeScopedData && (activeHospitalCode ?? authHospitalCode ?? displayHospitalName) ? (
                <span className="wc-chip wc-chip--default wc-filter-bar__chip">{displayHospitalLabel}</span>
              ) : null}
              {canSelectScopeTarget ? (
                <select
                  className="wc-select wc-select--compact wc-filter-bar__select"
                  onChange={(event) => {
                    const nextValue = event.target.value
                    if (nextValue === 'all') {
                      setDashboardScope('all')
                      setSelectedStaffId('')
                    } else if (nextValue === 'mine') {
                      setDashboardScope('mine')
                      setSelectedStaffId('')
                    } else if (nextValue.startsWith('staff:')) {
                      setDashboardScope('all')
                      setSelectedStaffId(nextValue.slice('staff:'.length))
                    }
                    setStaffPage(1)
                  }}
                  value={scopeSelectValue}
                >
                  <option value="all">全部员工</option>
                  {canSelectOverviewScope ? <option value="mine">我的</option> : null}
                  {(dashboard?.dashboard_staff_options ?? []).map((item) => (
                    <option key={item.staff_id} value={`staff:${item.staff_id}`}>{item.staff_name}</option>
                  ))}
                </select>
              ) : null}
              <select
                className="wc-select wc-select--compact wc-filter-bar__select"
                onChange={(event) => {
                  setDatePreset(event.target.value as DatePreset)
                  setStaffPage(1)
                }}
                value={datePreset}
              >
                {DATE_PRESETS.map((item) => (
                  <option key={item.value} value={item.value}>{item.label}</option>
                ))}
              </select>
            </div>
          </div>
        </div>

        {dashboardLoading ? (
          <div className="wc-empty">总览加载中…</div>
        ) : dashboardIsError || !dashboard ? (
          <div className="wc-empty">总览加载失败：{resolveOverviewErrorMessage(dashboardError)}</div>
        ) : (
          <>
            <div className="wc-overview-hero wc-overview-hero--3">
              <OverviewMetricCard
                label="录音数量"
                tone="blue"
                value={formatInteger(totalRecordings)}
              />
              <OverviewMetricCard
                hint={formatPercentValue(totalRecordings > 0 ? (recordingsWithVisits / totalRecordings) * 100 : null)}
                label="关联接诊"
                tone="green"
                value={formatInteger(recordingsWithVisits)}
              />
              <OverviewMetricCard
                label="客户数量"
                tone="amber"
                value={formatInteger(dashboard.total_customers)}
              />
            </div>

            <section className="wc-overview-panel wc-overview-panel--insight">
              <div className="wc-home-page__section-head">
                <h3 className="wc-home-page__section-title">面诊结果分析</h3>
                <span className="wc-chip wc-chip--default">{formatInteger(analysisSampleCount)}条已评价</span>
              </div>
              <div className="wc-overview-insight-grid">
                <div>
                  <span>平均适应症数量</span>
                  <strong>{formatDecimal(dashboard.avg_indication_count)}</strong>
                </div>
                <div>
                  <span>平均顾客标签数</span>
                  <strong>{formatDecimal(dashboard.avg_tag_count)}</strong>
                </div>
              </div>
            </section>

            <ProcessEvaluationOverview
              issues={dashboard.process_evaluation_issues ?? []}
              sections={dashboard.process_evaluation_sections ?? []}
              summary={processSummary}
            />

            <section className="wc-overview-panel wc-overview-panel--signals">
              <div className="wc-home-page__section-head">
                <h3 className="wc-home-page__section-title">业务信号</h3>
              </div>
              <div className="wc-overview-signal-grid">
                <div className="wc-overview-signal-card">
                  <div className="wc-overview-signal-card__head">
                    <strong>高频适应症</strong>
                    <span>Top 5</span>
                  </div>
                  <BreakdownList emptyText="当前范围内还没有适应症数据。" items={hotIndications} />
                </div>
                <div className="wc-overview-signal-card">
                  <div className="wc-overview-signal-card__head">
                    <strong>客户顾虑</strong>
                    <span>Top 5</span>
                  </div>
                  <ConcernList items={hotConcerns} />
                </div>
                <div className="wc-overview-signal-card">
                  <div className="wc-overview-signal-card__head">
                    <strong>顾客标签</strong>
                    <span>Top 5</span>
                  </div>
                  <BreakdownList compact emptyText="当前范围内还没有顾客标签数据。" items={hotTags} />
                </div>
              </div>
            </section>

            {canSeeScopedData ? (
              <section className="wc-overview-panel wc-overview-panel--examples">
                <div className="wc-home-page__section-head">
                  <div>
                    <h3 className="wc-home-page__section-title">示例录音</h3>
                  </div>
                  <span className="wc-chip wc-chip--default">管理员可见</span>
                </div>
                {shareFeedback ? <div className="wc-example-recordings__feedback">{shareFeedback}</div> : null}
                <div className="wc-example-recordings">
                  <ExampleRecordingList
                    emptyText="当前范围内还没有可作为优秀示例的录音。"
                    items={dashboard.positive_example_recordings ?? []}
                    onShare={handleShareExample}
                    title="表现较好"
                    tone="positive"
                  />
                  <ExampleRecordingList
                    emptyText="当前范围内还没有可作为待改进示例的录音。"
                    items={dashboard.negative_example_recordings ?? []}
                    onShare={handleShareExample}
                    title="表现较差"
                    tone="negative"
                  />
                </div>
              </section>
            ) : null}

            <section className="wc-overview-panel wc-overview-panel--staff">
              <div className="wc-home-page__section-head">
                <h3 className="wc-home-page__section-title">员工统计明细</h3>
                <span className="wc-chip wc-chip--default">{formatInteger(staffStats.length)}人</span>
              </div>
              <StaffStatsTable items={visibleStaffStats} />
              {staffPageCount > 1 ? (
                <div className="wc-home-page__staff-pagination">
                  <button
                    className="wc-home-page__staff-page-btn"
                    disabled={normalizedStaffPage <= 1}
                    onClick={() => setStaffPage((page) => Math.max(1, page - 1))}
                    type="button"
                  >
                    上一页
                  </button>
                  <span className="wc-home-page__staff-page-indicator">
                    {normalizedStaffPage} / {staffPageCount}
                  </span>
                  <button
                    className="wc-home-page__staff-page-btn"
                    disabled={normalizedStaffPage >= staffPageCount}
                    onClick={() => setStaffPage((page) => Math.min(staffPageCount, page + 1))}
                    type="button"
                  >
                    下一页
                  </button>
                </div>
              ) : null}
            </section>
          </>
        )}
      </section>
    </div>
  )
}

export default WecomHomePage
