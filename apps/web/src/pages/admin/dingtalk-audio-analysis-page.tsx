import { type CSSProperties, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import dayjs from 'dayjs'
import { Button, Empty, Input, Pagination, Select, Spin } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'

import * as adminApi from '@/api/admin'
import {
  type ArchiveRecording as DingtalkArchiveRecording,
  fetchArchiveRecordings,
} from '@/api/archive-recordings'
import { isHospitalAdminOrAbove } from '@/app/roles'
import { useAuth } from '@/app/use-auth'
import { formatRecordingDisplayName } from '@/utils/recording-display'
import { beijingNow, formatBeijingTime } from '@/utils/time'

type DatePreset = 'today' | '3d' | '7d' | 'all'

const DATE_PRESETS: Array<{ value: DatePreset; label: string }> = [
  { value: 'all', label: '全部时间' },
  { value: 'today', label: '今日' },
  { value: '3d', label: '近3天' },
  { value: '7d', label: '近7天' },
]
const DEFAULT_DATE_PRESET: DatePreset = 'all'

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

function firstNonEmpty(...values: Array<string | null | undefined>) {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return null
}

function formatScore(value?: number | null): string | null {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return null
  return value.toFixed(2).replace(/\.?0+$/, '')
}

function getStaffAvatarText(name?: string | null): string {
  const normalized = (name ?? '').trim()
  if (!normalized) return '录'
  const match = normalized.match(/[\u4e00-\u9fa5A-Za-z0-9]/)
  return (match?.[0] ?? normalized[0] ?? '录').toUpperCase()
}

function getScoreBand(value?: number | null): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return '待评估'
  if (value >= 7.5) return '优秀'
  if (value >= 6) return '良好'
  if (value >= 4.5) return '合格'
  return '待提升'
}

function getScoreBandTone(value?: number | null): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return 'muted'
  if (value >= 7.5) return 'excellent'
  if (value >= 6) return 'good'
  if (value >= 4.5) return 'stable'
  return 'warn'
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function asText(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function asTextArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map((item) => asText(item)).filter((item): item is string => Boolean(item))
    : []
}

function buildArchiveAnalysisCardSummary(item: DingtalkArchiveRecording) {
  const summary = asRecord(item.analysis_summary)
  if (!summary) return null
  return {
    duration_display: asText(summary.duration_display),
    dialogue_type: asText(summary.dialogue_type),
    focus_areas: asTextArray(summary.focus_areas),
    overall_score: asNumber(summary.overall_score),
    tag_count: asNumber(summary.tag_count),
  }
}

function ArchiveAnalysisCard({
  item,
  detailReady,
}: {
  item: DingtalkArchiveRecording
  detailReady: ReturnType<typeof buildArchiveAnalysisCardSummary>
}) {
  const displayName = formatRecordingDisplayName(item.display_file_name, item.create_time)
  const durationLabel = detailReady?.duration_display
    || (item.duration_ms ? `${Math.floor(item.duration_ms / 60000)}:${Math.floor(item.duration_ms / 1000 % 60).toString().padStart(2, '0')}` : '--:--')
  const focusAreas = detailReady?.focus_areas ?? []
  const compactDemandSummary = firstNonEmpty(
    focusAreas.slice(0, 3).join('；'),
  ) || '分析摘要加载中…'
  const tagCount = detailReady?.tag_count ?? 0
  const indicationCount = focusAreas.length
  const scoreValue = detailReady?.overall_score
  const scoreLabel = formatScore(scoreValue)
  const scoreBand = getScoreBand(scoreValue)
  const scoreBandTone = getScoreBandTone(scoreValue)
  const scoreRatio = typeof scoreValue === 'number' && Number.isFinite(scoreValue) && scoreValue > 0
    ? Math.max(0, Math.min(scoreValue / 9, 1))
    : 0
  const scoreStyle = {
    '--analysis-score-progress': `${scoreRatio * 180}deg`,
  } as CSSProperties
  const staffName = item.staff_name || '未绑定员工'
  const staffAvatar = getStaffAvatarText(staffName)
  const footerTime = item.create_time ? formatBeijingTime(item.create_time, 'MM/DD HH:mm') : '未知时间'
  const statItems = [
    { key: 'indications', label: '适应症', value: indicationCount },
    { key: 'tags', label: '标签', value: tagCount },
  ]

  return (
    <Link to={`/admin/llm-results/${item.id}`} className="analysis-card">
      <div className="analysis-card__header">
        <div className="analysis-card__identity">
          <span className="analysis-card__avatar" aria-hidden="true">{staffAvatar}</span>
          <div className="analysis-card__identity-text">
            <p className="analysis-card__title" title={displayName}>{displayName}</p>
            <div className="analysis-card__meta-row">
              <span className="analysis-card__meta-item">{staffName}</span>
              <span className="analysis-card__meta-sep">·</span>
              <span className="analysis-card__meta-item">{footerTime}</span>
              <span className="analysis-card__meta-sep">·</span>
              <span className="analysis-card__meta-item">{durationLabel}</span>
            </div>
          </div>
        </div>
        {detailReady?.dialogue_type ? (
          <span className="analysis-card__type-pill">{detailReady.dialogue_type}</span>
        ) : null}
      </div>

      <div className="analysis-card__body">
        <div className="analysis-card__summary">
          <div className="analysis-card__metric-head">
            <span className="analysis-card__label">主诉摘要</span>
          </div>
          <p className="analysis-card__summary-text" title={compactDemandSummary}>
            {compactDemandSummary}
          </p>
        </div>

        <div className="analysis-card__score-side">
          <div className={`analysis-card__score-box analysis-card__score-box--${scoreBandTone}`} style={scoreStyle}>
            <span className="analysis-card__score-box-label">面诊评分</span>
            <div className="analysis-card__score-gauge">
              <div className="analysis-card__score-gauge-ring" />
              <div className="analysis-card__score-gauge-cutout" />
              <div className="analysis-card__score-gauge-center">
                <strong className="analysis-card__score-box-value">{scoreLabel ?? '--'}</strong>
              </div>
            </div>
            <span className={`analysis-card__score-band analysis-card__score-band--${scoreBandTone}`}>
              {scoreBand}
            </span>
          </div>
        </div>

        <div className="analysis-card__stats">
          {statItems.map((item) => (
            <div key={item.key} className="analysis-card__stat-item">
              <span className="analysis-card__stat-value">{item.value}</span>
              <span className="analysis-card__stat-label">{item.label}</span>
            </div>
          ))}
        </div>
      </div>

      <div className="analysis-card__footer">
        <span className="analysis-card__footer-stats">已生成面诊结果分析与过程评价</span>
        <span className="analysis-card__cta">查看详情</span>
      </div>
    </Link>
  )
}

export default function DingtalkAudioAnalysisPage() {
  const auth = useAuth()
  const [keywordInput, setKeywordInput] = useState('')
  const [keyword, setKeyword] = useState('')
  const [hospitalFilter, setHospitalFilter] = useState<string | undefined>()
  const [datePreset, setDatePreset] = useState<DatePreset>(DEFAULT_DATE_PRESET)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(12)
  const dateRange = useMemo(() => resolveDateRange(datePreset), [datePreset])
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
    queryKey: ['dingtalk-audio-analysis-list', keyword, hospitalFilter || 'all', datePreset, page, pageSize],
    queryFn: () => fetchArchiveRecordings({
      keyword: keyword || undefined,
      hospital_code: hospitalFilter,
      status: 'analyzed',
      exclude_quality_filtered: true,
      include_date_summaries: false,
      include_analysis_summary: true,
      fast_page: true,
      ...dateRange,
      page,
      page_size: pageSize,
    }),
    placeholderData: (previousData) => previousData,
    staleTime: 30_000,
  })

  const items = useMemo(() => data?.items ?? [], [data])

  const groupedItems = useMemo(() => {
    const groups: Array<{ label: string; items: DingtalkArchiveRecording[] }> = []
    for (const item of items) {
      const dateStr = item.create_time ? formatBeijingTime(item.create_time, 'YYYY-MM-DD') : '未知日期'
      const last = groups[groups.length - 1]
      if (last && last.label === dateStr) {
        last.items.push(item)
      } else {
        groups.push({ label: dateStr, items: [item] })
      }
    }
    return groups
  }, [items])

  const handlePageChange = (nextPage: number, nextPageSize: number) => {
    if (nextPageSize !== pageSize) {
      setPageSize(nextPageSize)
      setPage(1)
      return
    }
    setPage(nextPage)
  }

  return (
    <section className="module-page analysis-results-page">
      <header className="module-page__header">
        <div>
          <p className="eyebrow">录音复盘</p>
          <h1>分析结果</h1>
          <p className="module-page__subtitle">
            按日期查看已分析录音，卡片展示核心结论，点击可进入完整详情。
          </p>
        </div>
        <div className="module-page__actions">
          <Select<DatePreset>
            className="analysis-results-page__hospital-select"
            value={datePreset}
            options={DATE_PRESETS}
            onChange={(value: DatePreset) => {
              setDatePreset(value)
              setPage(1)
            }}
          />
          {canFilterByHospital ? (
            <Select
              allowClear
              showSearch
              className="analysis-results-page__hospital-select"
              placeholder="全部机构"
              value={hospitalFilter}
              loading={hospitalOptionsQuery.isLoading}
              options={hospitalOptions}
              optionFilterProp="label"
              onChange={(value) => {
                setHospitalFilter(value || undefined)
                setPage(1)
              }}
            />
          ) : null}
          <Input.Search
            allowClear
            placeholder="搜文件名、工牌号、员工、fileId"
            style={{ width: 320 }}
            value={keywordInput}
            onChange={(event) => setKeywordInput(event.target.value)}
            onSearch={(value) => {
              setKeyword(value.trim())
              setKeywordInput(value)
              setPage(1)
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
      </header>

      {isLoading ? <Spin size="large" style={{ display: 'block', margin: '64px auto' }} /> : null}
      {error ? <p className="page-feedback page-feedback--error">加载失败：{String(error)}</p> : null}

      {!isLoading && !error ? (
        <>
          <div className="analysis-results-page__summary">
            当前第 {page} 页，已加载 {items.length} 条分析结果
          </div>
          <div className="analysis-results-page__summary" hidden>
            共 {data?.total ?? 0} 条已分析工牌录音，当前页 {items.length} 条。
          </div>

          {items.length === 0 ? (
            <Empty description="当前没有可展示的录音分析结果" />
          ) : (
            <div>
              {groupedItems.map((group) => (
                <div key={group.label} className="date-group">
                  <div className="date-group__header">
                    <span className="date-group__line" />
                    <span className="date-group__label">
                      {group.label === '未知日期' ? '未知日期' : dayjs(group.label).format('YYYY年MM月DD日')}
                    </span>
                    <span className="date-group__count">{group.items.length} 条</span>
                    <span className="date-group__line" />
                  </div>
                  <div className="analysis-card-grid">
                    {group.items.map((item) => (
                      <ArchiveAnalysisCard
                        key={item.id}
                        item={item}
                        detailReady={buildArchiveAnalysisCardSummary(item)}
                      />
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}

          <div className="analysis-results-page__footer">
            <span className="analysis-results-page__footer-total">第 {page} 页</span>
            <span className="analysis-results-page__footer-total">共 {data?.total ?? 0} 条记录</span>
            <Pagination
              current={page}
              pageSize={pageSize}
              total={data?.total ?? 0}
              showSizeChanger
              pageSizeOptions={[12, 24, 48]}
              onChange={handlePageChange}
              size="small"
            />
          </div>
        </>
      ) : null}
    </section>
  )
}
