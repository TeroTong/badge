import { useMemo, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Alert,
  Button,
  DatePicker,
  Progress,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import {
  CheckCircleOutlined,
  ClockCircleOutlined,
  CloudServerOutlined,
  HistoryOutlined,
  ReloadOutlined,
  WarningOutlined,
} from '@ant-design/icons'
import type { Dayjs } from 'dayjs'
import dayjs from 'dayjs'

import * as adminApi from '@/api/admin'
import { formatBeijingTime } from '@/utils/time'

const { Text } = Typography

type RequestFilters = {
  source: 'all' | 'local_audit' | 'cloud_audit'
  status?: 'submitted' | 'completed' | 'submit_failed' | 'task_failed' | 'unknown'
  dateRange: [Dayjs | null, Dayjs | null] | null
}

type RequestQueryFilters = {
  source: 'all' | 'local_audit' | 'cloud_audit'
  status?: 'submitted' | 'completed' | 'submit_failed' | 'task_failed' | 'unknown'
  date_from?: string
  date_to?: string
}

const DEFAULT_FILTERS: RequestFilters = {
  source: 'all',
  status: undefined,
  dateRange: null,
}

const DEFAULT_QUERY_FILTERS: RequestQueryFilters = {
  source: 'all',
}

function formatDurationMs(durationMs?: number | null): string {
  if (durationMs == null || durationMs <= 0) return '-'
  const totalSeconds = Math.floor(durationMs / 1000)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  if (hours > 0) {
    return `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`
  }
  return `${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`
}

function toHoursLabel(durationSeconds?: number | null): string {
  if (durationSeconds == null || durationSeconds <= 0) return '0 小时'
  return `${(durationSeconds / 3600).toFixed(durationSeconds % 3600 === 0 ? 0 : 2)} 小时`
}

function formatDays(days?: number | null): string {
  if (days == null || !Number.isFinite(days) || days <= 0) return '0 天'
  if (days >= 30) return `${days.toFixed(0)} 天`
  if (days >= 7) return `${days.toFixed(1)} 天`
  return `${days.toFixed(2)} 天`
}

function formatDateTime(value?: string | null): string {
  return formatBeijingTime(value, 'YYYY-MM-DD HH:mm:ss')
}

function statusTag(status: string) {
  if (status === 'completed') return <Tag color="green">已完成</Tag>
  if (status === 'submitted') return <Tag color="blue">已提交</Tag>
  if (status === 'submit_failed') return <Tag color="red">提交失败</Tag>
  if (status === 'task_failed') return <Tag color="orange">任务失败</Tag>
  return <Tag>未知</Tag>
}

function sourceTag(source: string) {
  if (source === 'local_audit') return <Tag color="geekblue">本地精确审计</Tag>
  return <Tag color="purple">腾讯云历史审计</Tag>
}

function quotaTag(state: 'normal' | 'exhausted' | 'unknown') {
  if (state === 'normal') return <Tag color="green">正常</Tag>
  if (state === 'exhausted') return <Tag color="red">额度不足</Tag>
  return <Tag color="default">未知</Tag>
}

type OverviewMetricCardProps = {
  label: string
  value: string
  hint?: string
  tone?: 'brand' | 'danger' | 'neutral' | 'success'
  icon: ReactNode
}

function OverviewMetricCard({ label, value, hint, tone = 'neutral', icon }: OverviewMetricCardProps) {
  return (
    <div className={`asr-monitoring-page__metric-card asr-monitoring-page__metric-card--${tone}`}>
      <span className="asr-monitoring-page__metric-icon" aria-hidden="true">
        {icon}
      </span>
      <span className="asr-monitoring-page__metric-label">{label}</span>
      <strong className="asr-monitoring-page__metric-value">{value}</strong>
      <span className="asr-monitoring-page__metric-hint">{hint || '—'}</span>
    </div>
  )
}

function SectionHeading({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div className="asr-monitoring-page__section-heading">
      <div>
        <h2>{title}</h2>
        {subtitle ? <p>{subtitle}</p> : null}
      </div>
    </div>
  )
}

export function AsrMonitoringPage() {
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [filters, setFilters] = useState<RequestFilters>(DEFAULT_FILTERS)
  const [queryFilters, setQueryFilters] = useState<RequestQueryFilters>(DEFAULT_QUERY_FILTERS)

  const overviewQuery = useQuery({
    queryKey: ['asr-monitoring-overview'],
    queryFn: () => adminApi.fetchAsrMonitoringOverview(),
  })

  const requestsQuery = useQuery({
    queryKey: ['asr-monitoring-requests', queryFilters, page, pageSize],
    queryFn: () =>
      adminApi.fetchAsrMonitoringRequests({
        ...queryFilters,
        page,
        page_size: pageSize,
      }),
  })

  const overview = overviewQuery.data
  const requestRows = requestsQuery.data?.items ?? []
  const usageRanges = useMemo(() => overview?.usage_ranges ?? [], [overview?.usage_ranges])
  const sevenDayUsage = useMemo(
    () => usageRanges.find((item) => item.label.includes('7')) ?? usageRanges[1],
    [usageRanges],
  )
  const quotaUsedPercent = useMemo(() => {
    if (!overview?.quota_total_seconds) return 0
    return Math.min(100, Math.round((overview.quota_used_seconds / overview.quota_total_seconds) * 100))
  }, [overview])
  const quotaRemainingPercent = useMemo(() => {
    if (!overview?.quota_total_seconds) return 0
    return Math.max(0, 100 - quotaUsedPercent)
  }, [overview, quotaUsedPercent])
  const usageMaxSeconds = useMemo(
    () => usageRanges.reduce((max, item) => Math.max(max, item.duration_seconds || 0), 0),
    [usageRanges],
  )
  const sevenDayAverageSeconds = useMemo(() => {
    if (!sevenDayUsage?.duration_seconds) return 0
    const start = dayjs(sevenDayUsage.start_date)
    const end = dayjs(sevenDayUsage.end_date)
    const days = start.isValid() && end.isValid() ? Math.max(1, end.diff(start, 'day') + 1) : 7
    return sevenDayUsage.duration_seconds / days
  }, [sevenDayUsage])
  const estimatedRemainingDays = useMemo(() => {
    if (!overview?.quota_remaining_seconds || sevenDayAverageSeconds <= 0) return null
    return overview.quota_remaining_seconds / sevenDayAverageSeconds
  }, [overview, sevenDayAverageSeconds])
  const localSuccessRate = useMemo(() => {
    if (!overview?.local_exact_count) return 0
    return Math.round((overview.local_success_count / overview.local_exact_count) * 100)
  }, [overview])

  const runQuery = () => {
    setPage(1)
    setQueryFilters({
      source: filters.source,
      status: filters.status,
      date_from: filters.dateRange?.[0]?.format('YYYY-MM-DD'),
      date_to: filters.dateRange?.[1]?.format('YYYY-MM-DD'),
    })
  }

  const resetFilters = () => {
    setFilters(DEFAULT_FILTERS)
    setQueryFilters(DEFAULT_QUERY_FILTERS)
    setPage(1)
  }

  const refreshAll = async () => {
    await Promise.all([overviewQuery.refetch(), requestsQuery.refetch()])
  }

  const requestSummaryCards = [
    {
      label: '本地精确请求',
      value: String(overview?.local_exact_count ?? 0),
      hint: `成功 ${localSuccessRate}% · 失败 ${overview?.local_failed_count ?? 0}`,
      tone: (overview?.local_failed_count ?? 0) > 0 ? 'danger' : 'success',
      icon: <CheckCircleOutlined />,
    },
    {
      label: '本地累计提交',
      value: formatDurationMs(overview?.local_submitted_duration_ms),
      hint: `识别 ${formatDurationMs(overview?.local_recognized_duration_ms)}`,
      tone: 'brand',
      icon: <CloudServerOutlined />,
    },
    {
      label: '历史云审计请求',
      value: String(overview?.cloud_total_count ?? 0),
      hint: `失败 ${overview?.cloud_failed_count ?? 0}`,
      tone: (overview?.cloud_failed_count ?? 0) > 0 ? 'danger' : 'neutral',
      icon: <HistoryOutlined />,
    },
    {
      label: '最近请求时间',
      value: overview?.latest_event_at ? formatBeijingTime(overview.latest_event_at, 'MM-DD HH:mm') : '-',
      hint: overview?.latest_event_at ? formatDateTime(overview.latest_event_at) : '暂无请求记录',
      tone: 'neutral',
      icon: <ClockCircleOutlined />,
    },
  ] as const

  const warningAlerts = [
    overview?.quota_state === 'exhausted' ? (
      <Alert
        key="quota-exhausted"
        type="error"
        showIcon
        icon={<WarningOutlined />}
        message="腾讯云 ASR 资源包已经耗尽"
        description={overview.quota_message || overview.latest_error_message || '最近一次请求已返回资源包额度不足，请及时补充资源包。'}
      />
    ) : null,
    overview?.usage_error_message ? (
      <Alert
        key="usage-error"
        type="warning"
        showIcon
        message="官方用量查询暂时失败"
        description={overview.usage_error_message}
      />
    ) : null,
    overview?.quota_fetch_error_message ? (
      <Alert
        key="quota-fetch-error"
        type="warning"
        showIcon
        message="资源包查询暂时失败"
        description={overview.quota_fetch_error_message}
      />
    ) : null,
  ].filter(Boolean)

  return (
    <div className="operation-page asr-monitoring-page">
      <div className="operation-page__header">
        <div className="operation-page__title">
          <span className="operation-page__marker" aria-hidden="true" />
          <div>
            <h1>ASR监控</h1>
            <p>先看额度是否健康，再看资源包消耗和每次请求明细，快速定位“还能不能转、为什么失败、消耗到了哪里”。</p>
          </div>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => void refreshAll()} loading={overviewQuery.isFetching || requestsQuery.isFetching}>
            刷新全部
          </Button>
        </Space>
      </div>

      {warningAlerts.length ? <div className="asr-monitoring-page__alert-stack">{warningAlerts}</div> : null}
      <div className="asr-monitoring-page__top-grid">
        <div className="asr-monitoring-page__top-main">
          <section className="operation-card asr-monitoring-page__hero">
            <div className="asr-monitoring-page__hero-main">
              <div className="asr-monitoring-page__hero-eyebrow">
                <span>腾讯云录音文件识别</span>
                {overview ? quotaTag(overview.quota_state) : null}
              </div>
              <div className="asr-monitoring-page__hero-value">
                <strong>{toHoursLabel(overview?.quota_remaining_seconds)}</strong>
                <span>剩余额度</span>
              </div>
              <div className="asr-monitoring-page__hero-summary">
                <span>
                  可支撑 {estimatedRemainingDays != null ? formatDays(estimatedRemainingDays) : '-'}
                </span>
                <span>
                  活跃包 {(overview?.quota_active_package_count ?? 0)} 个
                </span>
                <span>Provider {overview?.provider ?? '-'}</span>
              </div>
              <div className="asr-monitoring-page__hero-progress">
                <div className="asr-monitoring-page__hero-progress-meta">
                  <span>已用 {toHoursLabel(overview?.quota_used_seconds)}</span>
                  <span>总额度 {toHoursLabel(overview?.quota_total_seconds)}</span>
                </div>
                <Progress
                  percent={quotaUsedPercent}
                  success={{ percent: quotaRemainingPercent }}
                  status={overview?.quota_remaining_seconds ? 'active' : 'exception'}
                />
              </div>
              <div className="asr-monitoring-page__hero-pills">
                <Tag color="blue">本地日志 {overview?.request_log_available ? '可用' : '未就绪'}</Tag>
                <Tag color="purple">云审计 {overview?.cloud_audit_log_available ? '可用' : '未导入'}</Tag>
              </div>
            </div>
          </section>

          <section className="operation-card asr-monitoring-page__summary-panel asr-monitoring-page__summary-panel--requests">
            <SectionHeading title="请求审计概览" />
            <div className="asr-monitoring-page__metric-grid asr-monitoring-page__metric-grid--compact asr-monitoring-page__metric-grid--quad">
              {requestSummaryCards.map((item) => (
                <OverviewMetricCard
                  key={item.label}
                  label={item.label}
                  value={item.value}
                  hint={item.hint}
                  tone={item.tone}
                  icon={item.icon}
                />
              ))}
            </div>
          </section>

          <section className="operation-card asr-monitoring-page__usage-board">
            <SectionHeading title="官方用量趋势" />
            <div className="asr-monitoring-page__usage-grid">
              {usageRanges.map((item) => {
                const fillPercent = usageMaxSeconds > 0 ? Math.round((item.duration_seconds / usageMaxSeconds) * 100) : 0
                return (
                  <div key={item.label} className="asr-monitoring-page__usage-card">
                    <div className="asr-monitoring-page__usage-card-head">
                      <span className="asr-monitoring-page__usage-label">{item.label}</span>
                      <Tag color={item.label.includes('今日') ? 'blue' : item.label.includes('30') ? 'gold' : 'geekblue'}>
                        {item.request_count} 次请求
                      </Tag>
                    </div>
                    <strong className="asr-monitoring-page__usage-value">{toHoursLabel(item.duration_seconds)}</strong>
                    <div className="asr-monitoring-page__usage-bar-track" aria-hidden="true">
                      <div
                        className="asr-monitoring-page__usage-bar-fill"
                        style={{ width: `${fillPercent}%` }}
                      />
                    </div>
                    <span className="asr-monitoring-page__usage-meta">占当前可见周期峰值 {fillPercent}%</span>
                    <span className="asr-monitoring-page__usage-range">{item.start_date} 至 {item.end_date}</span>
                  </div>
                )
              })}
            </div>
          </section>
        </div>

        <section className="operation-card asr-monitoring-page__quota-board">
          <div className="asr-monitoring-page__card-head">
            <div>
              <h3>资源包明细</h3>
            </div>
          </div>
          <Table
            className="asr-monitoring-page__quota-table"
            rowKey={(row) => `${row.unit ?? row.name}-${row.available_type}`}
            dataSource={overview?.quota_packages ?? []}
            pagination={false}
            size="small"
            tableLayout="fixed"
            scroll={{ x: 700, y: 458 }}
            columns={[
              {
                title: '资源包名称',
                width: 148,
                render: (_value, row) => (
                  <div className="asr-monitoring-page__table-stack">
                    <div>{row.name}</div>
                    <Text type="secondary">{row.unit || row.sub_product_code || '-'}</Text>
                  </div>
                ),
              },
              {
                title: '状态',
                width: 74,
                render: (_value, row) =>
                  row.remaining_seconds > 0 ? <Tag color="green">可用</Tag> : <Tag color="red">已耗尽</Tag>,
              },
              {
                title: '总额度',
                width: 82,
                render: (_value, row) => toHoursLabel(row.total_seconds),
              },
              {
                title: '用量进度',
                width: 146,
                render: (_value, row) => {
                  const percent =
                    row.total_seconds > 0 ? Math.min(100, Math.round((row.used_seconds / row.total_seconds) * 100)) : 0
                  return (
                    <div className="asr-monitoring-page__table-stack">
                      <Progress
                        percent={percent}
                        size="small"
                        showInfo={false}
                        status={row.remaining_seconds > 0 ? 'active' : 'exception'}
                      />
                      <Text type="secondary" className="asr-monitoring-page__table-progress-meta">
                        已用 {toHoursLabel(row.used_seconds)} · 剩余 {toHoursLabel(row.remaining_seconds)}
                      </Text>
                    </div>
                  )
                },
              },
              {
                title: '到期时间',
                dataIndex: 'expiry_time',
                width: 118,
                render: (value) => value || '-',
              },
              {
                title: '类型',
                width: 76,
                render: (_value, row) =>
                  row.fee_mode ? <Tag color="blue">免费包</Tag> : <Tag color="gold">预付费包</Tag>,
              },
            ]}
          />
        </section>
      </div>

      <div className="operation-card">
        <div className="asr-monitoring-page__card-head">
          <div>
            <h3>请求明细</h3>
          </div>
        </div>

        <div className="asr-monitoring-page__request-filters">
          <div className="operation-filter-grid">
            <label className="operation-filter-item">
              <span>请求来源</span>
              <Select
                value={filters.source}
                onChange={(value) => setFilters((current) => ({ ...current, source: value }))}
                options={[
                  { label: '全部来源', value: 'all' },
                  { label: '本地精确审计', value: 'local_audit' },
                  { label: '腾讯云历史审计', value: 'cloud_audit' },
                ]}
              />
            </label>
            <label className="operation-filter-item">
              <span>请求状态</span>
              <Select
                allowClear
                placeholder="全部状态"
                value={filters.status}
                onChange={(value) => setFilters((current) => ({ ...current, status: value }))}
                options={[
                  { label: '已提交', value: 'submitted' },
                  { label: '已完成', value: 'completed' },
                  { label: '提交失败', value: 'submit_failed' },
                  { label: '任务失败', value: 'task_failed' },
                  { label: '未知', value: 'unknown' },
                ]}
              />
            </label>
            <label className="operation-filter-item">
              <span>请求时间</span>
              <DatePicker.RangePicker
                value={filters.dateRange}
                onChange={(value) => setFilters((current) => ({ ...current, dateRange: value }))}
              />
            </label>
          </div>

          <div className="operation-toolbar">
            <Space>
              <Button type="primary" onClick={runQuery}>
                查询
              </Button>
              <Button onClick={resetFilters}>重置</Button>
            </Space>
          </div>
        </div>

        <Table
          rowKey="id"
          dataSource={requestRows}
          loading={requestsQuery.isLoading}
          scroll={{ x: 980 }}
          pagination={{
            current: page,
            pageSize,
            total: requestsQuery.data?.total ?? 0,
            showSizeChanger: true,
            showTotal: (total) => `共 ${total} 条`,
            onChange: (nextPage, nextPageSize) => {
              setPage(nextPage)
              setPageSize(nextPageSize)
            },
          }}
          columns={[
            {
              title: '请求时间',
              dataIndex: 'occurred_at',
              width: 136,
              render: (value) => formatDateTime(value),
            },
            {
              title: '来源',
              width: 96,
              render: (_value, row) => sourceTag(row.source),
            },
            {
              title: '请求对象',
              width: 176,
              render: (_value, row) => (
                <div className="asr-monitoring-page__table-stack">
                  <div>{row.audio_name || row.source_id || '-'}</div>
                  <Text type="secondary">
                    {row.chunk_index && row.chunk_count
                      ? `分片 ${row.chunk_index}/${row.chunk_count}`
                      : row.source_ip || row.request_id || '-'}
                  </Text>
                </div>
              ),
            },
            {
              title: '提交 / 识别时长',
              width: 132,
              render: (_value, row) => (
                <div className="asr-monitoring-page__table-stack">
                  <div>{formatDurationMs(row.submitted_duration_ms)}</div>
                  <Text type="secondary">识别 {formatDurationMs(row.recognized_duration_ms)}</Text>
                </div>
              ),
            },
            {
              title: '结果',
              width: 90,
              render: (_value, row) => statusTag(row.status),
            },
            {
              title: 'RequestId / TaskId',
              width: 168,
              render: (_value, row) => (
                <div className="asr-monitoring-page__table-stack">
                  <div>{row.request_id || '-'}</div>
                  <Text type="secondary">{row.task_id ? `TaskId ${row.task_id}` : '无 TaskId'}</Text>
                </div>
              ),
            },
            {
              title: '错误信息',
              width: 204,
              render: (_value, row) => row.error_message || row.error_code || '-',
            },
          ]}
        />
      </div>
    </div>
  )
}

export default AsrMonitoringPage
