import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Button, DatePicker, Input, Space, Table, Tag } from 'antd'
import type { Dayjs } from 'dayjs'

import * as adminApi from '@/api/admin'
import { splitBeijingDateTime } from '@/utils/time'

type LogFilters = {
  dateRange: [Dayjs | null, Dayjs | null] | null
  ip_address: string
  module_name: string
  content: string
  operator_name: string
}

type LogQueryFilters = {
  date_from?: string
  date_to?: string
  ip_address: string
  module_name: string
  content: string
  operator_name: string
}

const DEFAULT_FILTERS: LogFilters = {
  dateRange: null,
  ip_address: '',
  module_name: '',
  content: '',
  operator_name: '',
}

const DEFAULT_QUERY_FILTERS: LogQueryFilters = {
  ip_address: '',
  module_name: '',
  content: '',
  operator_name: '',
}

function splitDateTime(value?: string | null) {
  return splitBeijingDateTime(value)
}

export function AuditLogsPage() {
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [filters, setFilters] = useState<LogFilters>(DEFAULT_FILTERS)
  const [queryFilters, setQueryFilters] = useState<LogQueryFilters>(DEFAULT_QUERY_FILTERS)

  const { data, isLoading } = useQuery({
    queryKey: ['audit-logs', queryFilters, page, pageSize],
    queryFn: () =>
      adminApi.fetchAuditLogs({
        ...queryFilters,
        date_from: queryFilters.date_from || undefined,
        date_to: queryFilters.date_to || undefined,
        ip_address: queryFilters.ip_address || undefined,
        module_name: queryFilters.module_name || undefined,
        content: queryFilters.content || undefined,
        operator_name: queryFilters.operator_name || undefined,
        page,
        page_size: pageSize,
      }),
  })

  const rows = data?.items ?? []

  const runQuery = () => {
    setPage(1)
    setQueryFilters({
      date_from: filters.dateRange?.[0]?.format('YYYY-MM-DD'),
      date_to: filters.dateRange?.[1]?.format('YYYY-MM-DD'),
      ip_address: filters.ip_address,
      module_name: filters.module_name,
      content: filters.content,
      operator_name: filters.operator_name,
    })
  }

  const resetFilters = () => {
    setFilters(DEFAULT_FILTERS)
    setQueryFilters(DEFAULT_QUERY_FILTERS)
    setPage(1)
  }

  return (
    <div className="operation-page audit-logs-page">
      <div className="operation-page__header">
        <div className="operation-page__title">
          <span className="operation-page__marker" aria-hidden="true" />
          <div>
            <h1>操作日志</h1>
            <p>查看后台登录、人员变更和配置操作记录，支持按时间、IP、模块和内容检索。</p>
          </div>
        </div>
      </div>

      <div className="operation-card">
        <div className="operation-filter-grid">
          <label className="operation-filter-item">
            <span>操作时间</span>
            <DatePicker.RangePicker
              value={filters.dateRange}
              onChange={(value) => setFilters((current) => ({ ...current, dateRange: value }))}
            />
          </label>
          <label className="operation-filter-item">
            <span>IP</span>
            <Input
              placeholder="请输入 IP"
              value={filters.ip_address}
              onChange={(event) => setFilters((current) => ({ ...current, ip_address: event.target.value }))}
            />
          </label>
          <label className="operation-filter-item">
            <span>操作模块</span>
            <Input
              placeholder="请输入模块"
              value={filters.module_name}
              onChange={(event) => setFilters((current) => ({ ...current, module_name: event.target.value }))}
            />
          </label>
          <label className="operation-filter-item">
            <span>操作内容</span>
            <Input
              placeholder="请输入内容"
              value={filters.content}
              onChange={(event) => setFilters((current) => ({ ...current, content: event.target.value }))}
            />
          </label>
          <label className="operation-filter-item">
            <span>操作人</span>
            <Input
              placeholder="请输入操作人"
              value={filters.operator_name}
              onChange={(event) => setFilters((current) => ({ ...current, operator_name: event.target.value }))}
            />
          </label>
        </div>

        <div className="operation-toolbar">
          <span className="operation-table__muted">记录所有关键后台操作，便于排查和追踪。</span>
          <Space>
            <Button type="primary" onClick={runQuery}>
              查询
            </Button>
            <Button onClick={resetFilters}>重置</Button>
          </Space>
        </div>

        <Table
          className="audit-logs-page__table"
          rowKey="id"
          dataSource={rows}
          loading={isLoading}
          tableLayout="fixed"
          scroll={{ x: 820 }}
          pagination={{
            current: page,
            pageSize,
            total: data?.total ?? 0,
            showSizeChanger: true,
            showTotal: (total) => `共 ${total} 条`,
            onChange: (nextPage, nextPageSize) => {
              setPage(nextPage)
              setPageSize(nextPageSize)
            },
          }}
          columns={[
            {
              title: '操作时间',
              dataIndex: 'created_at',
              width: 126,
              render: (value) => {
                const display = splitDateTime(value)
                return (
                  <div className="audit-logs-page__cell audit-logs-page__cell--time">
                    <div className="audit-logs-page__cell-primary">{display.date}</div>
                    <div className="audit-logs-page__cell-secondary">{display.time || '-'}</div>
                  </div>
                )
              },
            },
            {
              title: '操作人',
              dataIndex: 'operator_name',
              width: 96,
              render: (value) => <div className="audit-logs-page__cell-primary">{value || '-'}</div>,
            },
            {
              title: 'IP',
              dataIndex: 'ip_address',
              width: 126,
              render: (value) => <div className="audit-logs-page__cell-secondary">{value || '-'}</div>,
            },
            {
              title: '操作模块',
              width: 124,
              render: (_value, row) => <Tag color="purple">{row.module_name || row.action_name || '-'}</Tag>,
            },
            {
              title: '操作内容',
              dataIndex: 'content',
              width: 348,
              render: (value) => (
                <div className="audit-logs-page__content" title={value || '-'}>
                  {value || '-'}
                </div>
              ),
            },
          ]}
        />
      </div>
    </div>
  )
}

export default AuditLogsPage
