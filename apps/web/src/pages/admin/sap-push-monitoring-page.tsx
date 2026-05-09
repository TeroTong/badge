import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Button, DatePicker, Input, Select, Space, Table, Tag } from 'antd'
import type { Dayjs } from 'dayjs'

import * as adminApi from '@/api/admin'
import { useHospitalScopeFilter } from '@/hooks/use-hospital-scope-filter'
import { formatRecordingDisplayName } from '@/utils/recording-display'
import { formatBeijingTime, splitBeijingDateTime } from '@/utils/time'

type MonitoringFilters = {
  dateRange: [Dayjs | null, Dayjs | null] | null
  status: string
  triggerMode: string
  keyword: string
}

type MonitoringQueryFilters = {
  date_from?: string
  date_to?: string
  status: string
  trigger_mode: string
  keyword: string
}

const DEFAULT_FILTERS: MonitoringFilters = {
  dateRange: null,
  status: 'all',
  triggerMode: 'all',
  keyword: '',
}

const DEFAULT_QUERY_FILTERS: MonitoringQueryFilters = {
  status: 'all',
  trigger_mode: 'all',
  keyword: '',
}

function formatDateTime(value?: string | null) {
  return formatBeijingTime(value, 'YYYY-MM-DD HH:mm:ss')
}

function splitDateTime(value?: string | null) {
  return splitBeijingDateTime(value)
}

function resultTag(status: string) {
  if (status === 'succeeded') return <Tag color="green">成功</Tag>
  if (status === 'failed') return <Tag color="red">失败</Tag>
  if (status === 'queued') return <Tag color="blue">排队中</Tag>
  if (status === 'sending') return <Tag color="processing">发送中</Tag>
  if (status === 'skipped') return <Tag color="default">已跳过</Tag>
  return <Tag>{status || '待处理'}</Tag>
}

function triggerTag(mode?: string | null) {
  if (mode === 'manual') return <Tag color="gold">手动</Tag>
  if (mode === 'auto_bind') return <Tag color="geekblue">自动</Tag>
  return <Tag>{mode || '未知'}</Tag>
}

function OverviewCard({
  label,
  value,
  hint,
}: {
  label: string
  value: string
  hint?: string
}) {
  return (
    <div className="sap-monitoring-overview-card">
      <div className="sap-monitoring-overview-card__label">{label}</div>
      <div className="sap-monitoring-overview-card__value">{value}</div>
      <div className="sap-monitoring-overview-card__hint">{hint || '—'}</div>
    </div>
  )
}

export function SapPushMonitoringPage() {
  const hospitalScope = useHospitalScopeFilter()
  const activeHospitalCode = hospitalScope.hospitalCode
  const queryEnabled = hospitalScope.isReady && Boolean(activeHospitalCode)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [filters, setFilters] = useState<MonitoringFilters>(DEFAULT_FILTERS)
  const [queryFilters, setQueryFilters] = useState<MonitoringQueryFilters>(DEFAULT_QUERY_FILTERS)

  const overviewQuery = useQuery({
    queryKey: ['sap-push-monitoring-overview', activeHospitalCode ?? ''],
    queryFn: () => adminApi.fetchSapPushMonitoringOverview({ hospital_code: activeHospitalCode }),
    enabled: queryEnabled,
  })

  const logsQuery = useQuery({
    queryKey: ['sap-push-monitoring-logs', activeHospitalCode ?? '', queryFilters, page, pageSize],
    queryFn: () =>
      adminApi.fetchSapPushMonitoringLogs({
        hospital_code: activeHospitalCode,
        ...queryFilters,
        status: queryFilters.status || 'all',
        trigger_mode: queryFilters.trigger_mode || 'all',
        keyword: queryFilters.keyword || undefined,
        date_from: queryFilters.date_from || undefined,
        date_to: queryFilters.date_to || undefined,
        page,
        page_size: pageSize,
      }),
    enabled: queryEnabled,
  })

  const rows = logsQuery.data?.items ?? []
  const overview = overviewQuery.data

  const runQuery = () => {
    setPage(1)
    setQueryFilters({
      date_from: filters.dateRange?.[0]?.format('YYYY-MM-DD'),
      date_to: filters.dateRange?.[1]?.format('YYYY-MM-DD'),
      status: filters.status,
      trigger_mode: filters.triggerMode,
      keyword: filters.keyword.trim(),
    })
  }

  const resetFilters = () => {
    setFilters(DEFAULT_FILTERS)
    setQueryFilters(DEFAULT_QUERY_FILTERS)
    setPage(1)
  }

  const refreshAll = async () => {
    await Promise.all([overviewQuery.refetch(), logsQuery.refetch()])
  }

  return (
    <div className="operation-page sap-monitoring-page">
      <div className="operation-page__header">
        <div className="operation-page__title">
          <span className="operation-page__marker" aria-hidden="true" />
          <div>
            <h1>SAP回传监控</h1>
            <p>查看自动/手动咨询单回传结果，快速定位成功、失败和业务侧返回原因。</p>
          </div>
        </div>
        <Space>
          {hospitalScope.canSelectHospital ? (
            <Select
              showSearch
              placeholder="机构范围"
              value={activeHospitalCode}
              loading={hospitalScope.isLoading}
              options={hospitalScope.selectOptions}
              optionFilterProp="label"
              style={{ minWidth: 180 }}
              onChange={(value) => {
                hospitalScope.setHospitalCode(value)
                setPage(1)
              }}
            />
          ) : hospitalScope.hospitalName ? (
            <Tag bordered={false} color="blue">
              {hospitalScope.hospitalName}
            </Tag>
          ) : null}
          <Button onClick={refreshAll}>刷新</Button>
        </Space>
      </div>

      <div className="sap-monitoring-overview-grid">
        <OverviewCard label="总回传到诊单" value={String(overview?.total_count ?? 0)} hint="同一到诊单多次回传只计一次" />
        <OverviewCard label="成功" value={String(overview?.succeeded_count ?? 0)} hint="只要成功过一次即计成功" />
        <OverviewCard label="失败" value={String(overview?.failed_count ?? 0)} hint="从未成功且存在失败" />
        <OverviewCard label="待处理" value={String(overview?.pending_count ?? 0)} hint="从未成功且尚无失败" />
        <OverviewCard label="自动触发" value={String(overview?.auto_count ?? 0)} hint="唯一到诊单口径" />
        <OverviewCard label="最近一次发送" value={overview?.latest_sent_at ? formatBeijingTime(overview.latest_sent_at, 'MM-DD HH:mm') : '-'} hint={formatDateTime(overview?.latest_sent_at)} />
      </div>

      <div className="operation-card">
        <div className="operation-filter-grid">
          <label className="operation-filter-item">
            <span>发送时间</span>
            <DatePicker.RangePicker
              value={filters.dateRange}
              onChange={(value) => setFilters((current) => ({ ...current, dateRange: value }))}
            />
          </label>
          <label className="operation-filter-item">
            <span>结果状态</span>
            <Select
              value={filters.status}
              onChange={(value) => setFilters((current) => ({ ...current, status: value }))}
              options={[
                { label: '全部状态', value: 'all' },
                { label: '成功', value: 'succeeded' },
                { label: '失败', value: 'failed' },
                { label: '待处理', value: 'prepared' },
                { label: '排队中', value: 'queued' },
                { label: '发送中', value: 'sending' },
                { label: '已跳过', value: 'skipped' },
              ]}
            />
          </label>
          <label className="operation-filter-item">
            <span>触发方式</span>
            <Select
              value={filters.triggerMode}
              onChange={(value) => setFilters((current) => ({ ...current, triggerMode: value }))}
              options={[
                { label: '全部方式', value: 'all' },
                { label: '自动', value: 'auto_bind' },
                { label: '手动', value: 'manual' },
              ]}
            />
          </label>
          <label className="operation-filter-item">
            <span>关键词</span>
            <Input
              placeholder="录音名 / 到诊单号 / 客户名 / 顾问"
              value={filters.keyword}
              onChange={(event) => setFilters((current) => ({ ...current, keyword: event.target.value }))}
            />
          </label>
        </div>

        <div className="operation-toolbar">
          <span className="operation-table__muted">已按目标到诊单拆分显示；业务失败会直接展示 SAP 返回原因，便于判断是否需要人工重发。</span>
          <Space>
            <Button type="primary" onClick={runQuery}>
              查询
            </Button>
            <Button onClick={resetFilters}>重置</Button>
          </Space>
        </div>

        <Table
          className="sap-monitoring-page__table"
          rowKey="id"
          dataSource={rows}
          loading={overviewQuery.isLoading || logsQuery.isLoading}
          tableLayout="fixed"
          scroll={{ x: 940 }}
          pagination={{
            current: page,
            pageSize,
            total: logsQuery.data?.total ?? 0,
            showSizeChanger: true,
            showTotal: (total) => `共 ${total} 条`,
            onChange: (nextPage, nextPageSize) => {
              setPage(nextPage)
              setPageSize(nextPageSize)
            },
          }}
          columns={[
            {
              title: '发送时间',
              dataIndex: 'sent_at',
              width: 124,
              render: (value, row) => {
                const display = splitDateTime(value || row.created_at)
                return (
                  <div className="sap-monitoring-page__cell sap-monitoring-page__cell--time">
                    <div className="sap-monitoring-page__cell-primary">{display.date}</div>
                    <div className="sap-monitoring-page__cell-secondary">{display.time || '-'}</div>
                  </div>
                )
              },
            },
            {
              title: '录音',
              width: 176,
              render: (_value, row) => (
                <div className="sap-monitoring-page__cell">
                  <div
                    className="sap-monitoring-page__cell-primary sap-monitoring-page__cell-primary--file"
                    title={
                      row.recording_file_name
                        ? formatRecordingDisplayName(row.recording_file_name, row.recording_created_at)
                        : row.recording_id
                    }
                  >
                    {row.recording_file_name
                      ? formatRecordingDisplayName(row.recording_file_name, row.recording_created_at)
                      : row.recording_id}
                  </div>
                  <div className="sap-monitoring-page__cell-secondary" title={row.recording_id}>
                    {row.recording_id}
                  </div>
                </div>
              ),
            },
            {
              title: '到诊单',
              width: 126,
              render: (_value, row) => (
                <div className="sap-monitoring-page__cell">
                  <div className="sap-monitoring-page__cell-primary">{row.visit_order_no || '-'}</div>
                  <div className="sap-monitoring-page__cell-secondary">
                    {row.visit_order_seg || '-'} · {row.is_primary_target ? '主关联' : '辅关联'}
                  </div>
                </div>
              ),
            },
            {
              title: '客户 / 顾问',
              width: 146,
              render: (_value, row) => (
                <div className="sap-monitoring-page__cell">
                  <div className="sap-monitoring-page__cell-primary" title={row.customer_name || row.customer_code || '-'}>
                    {row.customer_name || row.customer_code || '-'}
                  </div>
                  <div className="sap-monitoring-page__cell-secondary" title={row.advisor_name || '-'}>
                    {row.advisor_name || '-'}
                  </div>
                </div>
              ),
            },
            {
              title: '触发',
              width: 74,
              render: (_value, row) => triggerTag(row.trigger_mode),
            },
            {
              title: '结果',
              width: 74,
              render: (_value, row) => resultTag(row.result_status),
            },
            {
              title: 'SAP状态',
              width: 82,
              render: (_value, row) => row.effective_business_status || row.business_status || '-',
            },
            {
              title: '原因',
              dataIndex: 'result_reason',
              width: 150,
              render: (value, row) => {
                const reason = value || row.effective_reason || row.business_message || row.error_message || '-'
                return (
                  <div className="sap-monitoring-page__reason" title={reason}>
                    {reason}
                  </div>
                )
              },
            },
          ]}
        />
      </div>
    </div>
  )
}

export default SapPushMonitoringPage
