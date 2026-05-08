import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Form, Input, message, Modal, Popconfirm, Select, Space, Switch, Table, Tag, Typography } from 'antd'
import { PlusOutlined, TeamOutlined } from '@ant-design/icons'

import type {
  DepartmentAssistantDepartmentConfig,
  DepartmentAssistantMatchConfig,
  Staff,
  WecomTenant,
  WecomTenantPayload,
} from '@/api/admin'
import * as adminApi from '@/api/admin'
import { getApiErrorMessage } from '@/api/errors'
import { isSystemAdminOrAbove } from '@/app/roles'
import { useAuth } from '@/app/use-auth'
import { formatBeijingTime } from '@/utils/time'

const { Text } = Typography

type ActiveFilter = 'all' | 'active' | 'inactive'

type TenantFormValues = {
  name: string
  host: string
  corp_id: string
  agent_id: string
  agent_secret?: string
  frontend_url: string
  default_hospital_code?: string
  sap_summary_template_name?: string
  sap_summary_template_version?: string
  sap_summary_template?: string
  sap_summary_prompt?: string
  is_default: boolean
  is_active: boolean
}

const DEPARTMENT_OPTIONS: Array<Pick<DepartmentAssistantDepartmentConfig, 'department_code' | 'department_name'>> = [
  { department_code: 'JGKS01', department_name: '口腔科' },
  { department_code: 'JGKS02', department_name: '皮肤科' },
  { department_code: 'JGKS03', department_name: '外科' },
  { department_code: 'JGKS04', department_name: '微整科' },
  { department_code: 'JGKS05', department_name: '中医' },
  { department_code: 'JGKS06', department_name: '纹绣' },
  { department_code: 'JGKS07', department_name: '会籍' },
  { department_code: 'JGKS08', department_name: '毛发移植科' },
  { department_code: 'JGKS09', department_name: '非手术' },
  { department_code: 'JGKS10', department_name: '私密中心' },
  { department_code: 'JGKS11', department_name: '纤体中心' },
  { department_code: 'JGKS12', department_name: '植发中心' },
  { department_code: 'JGKS13', department_name: '形体私密中心' },
  { department_code: 'JGKS14', department_name: 'SPA中心' },
]

function isChangshaYameiTenant(row: WecomTenant | null | undefined) {
  if (!row) return false
  const text = [row.name, row.host, row.frontend_url, row.default_hospital_code].filter(Boolean).join(' ')
  return row.default_hospital_code === '6501' || /长沙雅美|雅美|csyamei/i.test(text)
}

function normalizeDepartmentAssistantConfig(
  config: DepartmentAssistantMatchConfig | null | undefined,
): DepartmentAssistantMatchConfig {
  const byCode = new Map(
    (config?.departments ?? []).map((item) => [
      item.department_code,
      Array.from(new Set((item.assistant_staff_ids ?? []).filter(Boolean))),
    ]),
  )
  return {
    enabled: config?.enabled ?? true,
    departments: DEPARTMENT_OPTIONS.map((item) => ({
      ...item,
      assistant_staff_ids: byCode.get(item.department_code) ?? [],
    })),
  }
}

function compactDepartmentAssistantConfig(config: DepartmentAssistantMatchConfig): DepartmentAssistantMatchConfig {
  return {
    enabled: config.enabled,
    departments: config.departments
      .map((item) => ({
        department_code: item.department_code,
        department_name: item.department_name,
        assistant_staff_ids: Array.from(new Set((item.assistant_staff_ids ?? []).filter(Boolean))),
      }))
      .filter((item) => item.assistant_staff_ids.length > 0),
  }
}

function departmentAssistantSummary(config: DepartmentAssistantMatchConfig | null | undefined) {
  const departments = config?.departments ?? []
  const configuredDepartments = departments.filter((item) => (item.assistant_staff_ids ?? []).length > 0)
  const assistants = new Set(configuredDepartments.flatMap((item) => item.assistant_staff_ids ?? []))
  return {
    configuredDepartmentCount: configuredDepartments.length,
    assistantCount: assistants.size,
  }
}

function formatStaffOption(staff: Staff) {
  const bits = [staff.name]
  if (staff.external_account) bits.push(staff.external_account)
  if (staff.position_name) bits.push(staff.position_name)
  return bits.join(' / ')
}

function compactPayload(values: TenantFormValues, editing: boolean): WecomTenantPayload {
  const payload: WecomTenantPayload = {
    name: values.name?.trim(),
    host: values.host?.trim() || null,
    corp_id: values.corp_id?.trim() || null,
    agent_id: values.agent_id?.trim() || null,
    frontend_url: values.frontend_url?.trim() || null,
    default_hospital_code: values.default_hospital_code?.trim() || null,
    default_hospital_name: null,
    sap_summary_template_name: values.sap_summary_template_name?.trim() || null,
    sap_summary_template_version: values.sap_summary_template_version?.trim() || null,
    sap_summary_template: values.sap_summary_template?.trim() || null,
    sap_summary_prompt: values.sap_summary_prompt?.trim() || null,
    is_default: values.is_default,
    is_active: values.is_active,
  }
  const secret = values.agent_secret?.trim()
  if (secret || !editing) {
    payload.agent_secret = secret || null
  }
  return payload
}

function isWecomConfigured(row: WecomTenant) {
  return Boolean(row.host && row.corp_id && row.agent_id && row.frontend_url && row.agent_secret_configured)
}

function formatTime(value: string | null | undefined) {
  return formatBeijingTime(value, 'YYYY-MM-DD HH:mm:ss')
}

export function InstitutionsPage() {
  const qc = useQueryClient()
  const auth = useAuth()
  const currentRole = auth.status === 'authenticated' ? auth.user.role : 'staff'
  const canManageInstitutionGlobals = isSystemAdminOrAbove(currentRole)
  const [modalOpen, setModalOpen] = useState(false)
  const [editingTenant, setEditingTenant] = useState<WecomTenant | null>(null)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [filters, setFilters] = useState({
    keyword: '',
    is_active: 'all' as ActiveFilter,
  })
  const [queryFilters, setQueryFilters] = useState(filters)
  const [form] = Form.useForm<TenantFormValues>()
  const [assistantModalOpen, setAssistantModalOpen] = useState(false)
  const [assistantTenant, setAssistantTenant] = useState<WecomTenant | null>(null)
  const [assistantConfig, setAssistantConfig] = useState<DepartmentAssistantMatchConfig>(
    normalizeDepartmentAssistantConfig(null),
  )

  const { data, isLoading } = useQuery({
    queryKey: ['wecom-tenants', queryFilters, page, pageSize],
    queryFn: () =>
      adminApi.fetchWecomTenants({
        keyword: queryFilters.keyword || undefined,
        is_active:
          queryFilters.is_active === 'all'
            ? undefined
            : queryFilters.is_active === 'active',
        page,
        page_size: pageSize,
      }),
  })

  const { data: assistantStaffData, isFetching: assistantStaffLoading } = useQuery({
    queryKey: ['department-assistant-staff', assistantTenant?.default_hospital_code],
    queryFn: () =>
      adminApi.fetchStaff({
        hospital_code: assistantTenant?.default_hospital_code || undefined,
        page: 1,
        page_size: 100,
      }),
    enabled: assistantModalOpen && Boolean(assistantTenant?.default_hospital_code),
  })

  const staffOptions = useMemo(() => {
    const staff = assistantStaffData?.items ?? []
    const selectedIds = new Set(assistantConfig.departments.flatMap((item) => item.assistant_staff_ids ?? []))
    const options = staff.map((item) => ({
      value: item.id,
      label: formatStaffOption(item),
    }))
    const knownIds = new Set(options.map((item) => item.value))
    for (const staffId of selectedIds) {
      if (!knownIds.has(staffId)) {
        options.push({ value: staffId, label: `未知人员 / ${staffId}` })
      }
    }
    return options
  }, [assistantConfig.departments, assistantStaffData?.items])

  const invalidate = async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ['wecom-tenants'] }),
      qc.invalidateQueries({ queryKey: ['audit-logs'] }),
    ])
  }

  const createMutation = useMutation({
    mutationFn: adminApi.createWecomTenant,
    onSuccess: () => void invalidate(),
  })
  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: WecomTenantPayload }) => adminApi.updateWecomTenant(id, data),
    onSuccess: () => void invalidate(),
  })
  const deleteMutation = useMutation({
    mutationFn: adminApi.deleteWecomTenant,
    onSuccess: () => void invalidate(),
  })

  const resetFilters = () => {
    const next = { keyword: '', is_active: 'all' as ActiveFilter }
    setFilters(next)
    setQueryFilters(next)
    setPage(1)
  }

  const openModal = (tenant?: WecomTenant) => {
    setEditingTenant(tenant ?? null)
    form.setFieldsValue({
      name: tenant?.name ?? '',
      host: tenant?.host ?? '',
      corp_id: tenant?.corp_id ?? '',
      agent_id: tenant?.agent_id ?? '',
      agent_secret: '',
      frontend_url: tenant?.frontend_url ?? '',
      default_hospital_code: tenant?.default_hospital_code ?? '',
      sap_summary_template_name: tenant?.sap_summary_template_name ?? '',
      sap_summary_template_version: tenant?.sap_summary_template_version ?? '',
      sap_summary_template: tenant?.sap_summary_template ?? '',
      sap_summary_prompt: tenant?.sap_summary_prompt ?? '',
      is_default: tenant?.is_default ?? false,
      is_active: tenant?.is_active ?? true,
    })
    setModalOpen(true)
  }

  const closeModal = () => {
    setModalOpen(false)
    setEditingTenant(null)
    form.resetFields()
  }

  const openAssistantModal = (tenant: WecomTenant) => {
    setAssistantTenant(tenant)
    setAssistantConfig(normalizeDepartmentAssistantConfig(tenant.department_assistant_match_config))
    setAssistantModalOpen(true)
  }

  const closeAssistantModal = () => {
    setAssistantModalOpen(false)
    setAssistantTenant(null)
    setAssistantConfig(normalizeDepartmentAssistantConfig(null))
  }

  const updateAssistantDepartment = (departmentCode: string, assistantStaffIds: string[]) => {
    setAssistantConfig((current) => ({
      ...current,
      departments: current.departments.map((item) =>
        item.department_code === departmentCode
          ? { ...item, assistant_staff_ids: assistantStaffIds }
          : item,
      ),
    }))
  }

  const handleSave = async () => {
    try {
      const values = await form.validateFields()
      const payload = compactPayload(values, Boolean(editingTenant))
      if (editingTenant) {
        await updateMutation.mutateAsync({ id: editingTenant.id, data: payload })
        message.success('机构配置已更新')
      } else {
        await createMutation.mutateAsync(payload)
        message.success('机构配置已新增')
      }
      closeModal()
    } catch (error) {
      message.error(await getApiErrorMessage(error, '保存机构配置失败'))
    }
  }

  const handleSaveAssistantConfig = async () => {
    if (!assistantTenant) return
    try {
      await updateMutation.mutateAsync({
        id: assistantTenant.id,
        data: {
          department_assistant_match_config: compactDepartmentAssistantConfig(assistantConfig),
        },
      })
      message.success('机构科室助理配置已更新')
      closeAssistantModal()
    } catch (error) {
      message.error(await getApiErrorMessage(error, '保存机构科室助理配置失败'))
    }
  }

  const handleToggle = async (tenant: WecomTenant) => {
    try {
      await updateMutation.mutateAsync({ id: tenant.id, data: { is_active: !tenant.is_active } })
      message.success(tenant.is_active ? '机构配置已停用' : '机构配置已启用')
    } catch (error) {
      message.error(await getApiErrorMessage(error, '更新机构状态失败'))
    }
  }

  const handleSetDefault = async (tenant: WecomTenant) => {
    try {
      await updateMutation.mutateAsync({ id: tenant.id, data: { is_default: true, is_active: true } })
      message.success('默认机构配置已更新')
    } catch (error) {
      message.error(await getApiErrorMessage(error, '设置默认机构失败'))
    }
  }

  const handleDelete = async (tenant: WecomTenant) => {
    try {
      await deleteMutation.mutateAsync(tenant.id)
      message.success('机构配置已删除')
    } catch (error) {
      message.error(await getApiErrorMessage(error, '删除机构配置失败'))
    }
  }

  return (
    <div className="operation-page">
      <div className="operation-page__header">
        <div className="operation-page__title">
          <span className="operation-page__marker" aria-hidden="true" />
          <div>
            <h1>机构管理</h1>
            <p>管理机构名称、机构编码、公网入口与企业微信应用配置。</p>
          </div>
        </div>
      </div>

      <div className="operation-card">
        <div className="operation-filter-grid">
          <label className="operation-filter-item">
            <span>关键词</span>
            <Input
              placeholder="机构名称 / 机构编码 / 域名 / CorpID"
              value={filters.keyword}
              onChange={(event) => setFilters((current) => ({ ...current, keyword: event.target.value }))}
              onPressEnter={() => {
                setPage(1)
                setQueryFilters(filters)
              }}
            />
          </label>
          <label className="operation-filter-item">
            <span>状态</span>
            <Select
              value={filters.is_active}
              options={[
                { label: '全部', value: 'all' },
                { label: '启用', value: 'active' },
                { label: '停用', value: 'inactive' },
              ]}
              onChange={(value: ActiveFilter) => setFilters((current) => ({ ...current, is_active: value }))}
            />
          </label>
        </div>

        <div className="operation-toolbar">
          <Space wrap>
            {canManageInstitutionGlobals ? (
              <Button type="primary" icon={<PlusOutlined />} onClick={() => openModal()}>
                新增机构
              </Button>
            ) : null}
          </Space>

          <Space>
            <Button
              type="primary"
              onClick={() => {
                setPage(1)
                setQueryFilters(filters)
              }}
            >
              查询
            </Button>
            <Button onClick={resetFilters}>重置</Button>
          </Space>
        </div>

        <Table
          rowKey="id"
          dataSource={data?.items ?? []}
          loading={isLoading}
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
              title: '机构',
              width: 220,
              render: (_value, row: WecomTenant) => (
                <Space direction="vertical" size={2}>
                  <Space wrap size={6}>
                    <Text strong>{row.name}</Text>
                    {row.is_default ? <Tag color="blue">默认</Tag> : null}
                    <Tag color={row.is_active ? 'success' : 'default'}>{row.is_active ? '启用' : '停用'}</Tag>
                  </Space>
                  <Text type="secondary">机构编码：{row.default_hospital_code || '-'}</Text>
                </Space>
              ),
            },
            {
              title: '企业微信应用',
              width: 260,
              render: (_value, row: WecomTenant) => (
                <Space direction="vertical" size={2}>
                  <Text type="secondary">CorpID：{row.corp_id || '-'}</Text>
                  <Text type="secondary">AgentID：{row.agent_id || '-'}</Text>
                  <Tag color={isWecomConfigured(row) ? 'green' : 'default'}>
                    {isWecomConfigured(row) ? '企微已配置' : '企微待补充'}
                  </Tag>
                </Space>
              ),
            },
            {
              title: '公网入口',
              width: 260,
              render: (_value, row: WecomTenant) => (
                <Space direction="vertical" size={2}>
                  <Text>{row.host || '-'}</Text>
                  <Text type="secondary">{row.frontend_url || '未配置企微入口 URL'}</Text>
                </Space>
              ),
            },
            {
              title: 'SAP总结模板',
              width: 220,
              render: (_value, row: WecomTenant) => {
                const configured = Boolean(row.sap_summary_template || row.sap_summary_prompt)
                return (
                  <Space direction="vertical" size={2}>
                    <Space wrap size={6}>
                      <Tag color={configured ? 'purple' : 'default'}>{configured ? '已配置' : '未配置'}</Tag>
                      {row.sap_summary_template_version ? <Tag>{row.sap_summary_template_version}</Tag> : null}
                    </Space>
                    <Text type="secondary" ellipsis>
                      {row.sap_summary_template_name || '使用系统默认总结口径'}
                    </Text>
                  </Space>
                )
              },
            },
            {
              title: '科室助理',
              width: 180,
              render: (_value, row: WecomTenant) => {
                if (!isChangshaYameiTenant(row)) {
                  return <Text type="secondary">-</Text>
                }
                const summary = departmentAssistantSummary(row.department_assistant_match_config)
                const configured = summary.configuredDepartmentCount > 0
                return (
                  <Space direction="vertical" size={2}>
                    <Tag color={configured ? 'cyan' : 'default'}>{configured ? '已配置' : '未配置'}</Tag>
                    <Text type="secondary">
                      {summary.configuredDepartmentCount} 个科室 / {summary.assistantCount} 人
                    </Text>
                  </Space>
                )
              },
            },
            {
              title: '更新时间',
              width: 180,
              render: (_value, row: WecomTenant) => formatTime(row.updated_at),
            },
            {
              title: '操作',
              width: 260,
              render: (_value, row: WecomTenant) => (
                <Space wrap>
                  {canManageInstitutionGlobals ? (
                    <Button size="small" onClick={() => openModal(row)}>
                      编辑
                    </Button>
                  ) : null}
                  {isChangshaYameiTenant(row) ? (
                    <Button size="small" icon={<TeamOutlined />} onClick={() => openAssistantModal(row)}>
                      科室助理
                    </Button>
                  ) : null}
                  {canManageInstitutionGlobals && !row.is_default ? (
                    <Button size="small" onClick={() => void handleSetDefault(row)}>
                      设为默认
                    </Button>
                  ) : null}
                  {canManageInstitutionGlobals ? (
                    <Button size="small" danger={row.is_active} onClick={() => void handleToggle(row)}>
                      {row.is_active ? '停用' : '启用'}
                    </Button>
                  ) : null}
                  {canManageInstitutionGlobals ? (
                    <Popconfirm title="确定删除这个机构配置吗？" onConfirm={() => void handleDelete(row)}>
                      <Button size="small" danger>
                        删除
                      </Button>
                    </Popconfirm>
                  ) : null}
                </Space>
              ),
            },
          ]}
        />
      </div>

      <Modal
        title={editingTenant ? '编辑机构配置' : '新增机构配置'}
        open={modalOpen}
        width={860}
        onOk={() => void handleSave()}
        onCancel={closeModal}
        confirmLoading={createMutation.isPending || updateMutation.isPending}
        destroyOnClose
      >
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="机构名称" rules={[{ required: true, message: '请输入机构名称' }]}>
            <Input placeholder="例如：米兰柏羽总院" />
          </Form.Item>
          <Form.Item name="default_hospital_code" label="机构编码" rules={[{ required: true, message: '请输入机构编码' }]}>
            <Input placeholder="例如：6101" />
          </Form.Item>
          <Form.Item name="host" label="公网域名">
            <Input placeholder="例如：gongpai.bravou.tech" />
          </Form.Item>
          <Form.Item name="frontend_url" label="企微入口 URL">
            <Input placeholder="例如：https://gongpai.example.com" />
          </Form.Item>
          <Form.Item name="corp_id" label="企业微信 CorpID">
            <Input />
          </Form.Item>
          <Form.Item name="agent_id" label="应用 AgentID">
            <Input />
          </Form.Item>
          <Form.Item
            name="agent_secret"
            label="应用 Secret"
            extra={editingTenant ? '编辑时留空表示不修改现有 Secret。' : undefined}
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          <Form.Item
            name="sap_summary_template_name"
            label="SAP总结模板名称"
            extra="便于区分不同机构的总结口径，不会直接回传给 SAP。"
          >
            <Input placeholder="例如：长沙雅美总结信息 v1" />
          </Form.Item>
          <Form.Item name="sap_summary_template_version" label="SAP总结模板版本">
            <Input placeholder="例如：v1.0" />
          </Form.Item>
          <Form.Item
            name="sap_summary_template"
            label="SAP总结信息模板"
            extra="用于描述本机构希望总结覆盖的大点、小点、顺序和写作风格。后续录音分析会把这段内容加入 system prompt。"
          >
            <Input.TextArea
              rows={7}
              placeholder="例如：按客户背景、决策画像、方案反馈、成交与跟进等段落自然总结；不要机械重复前面的主诉、预算、顾虑、推荐方案。"
            />
          </Form.Item>
          <Form.Item
            name="sap_summary_prompt"
            label="SAP总结写作补充提示词"
            extra="可填写更细的机构口径。若与系统默认口径冲突，以这里的机构配置优先。"
          >
            <Input.TextArea
              rows={5}
              placeholder="例如：语言要像咨询复盘，不要写成字段堆砌；重点说明为什么推荐、客户怎么反应、下一步如何转化。"
            />
          </Form.Item>
          <Space size="large">
            <Form.Item name="is_default" label="默认配置" valuePropName="checked">
              <Switch />
            </Form.Item>
            <Form.Item name="is_active" label="启用状态" valuePropName="checked">
              <Switch />
            </Form.Item>
          </Space>
        </Form>
      </Modal>

      <Modal
        title={`${assistantTenant?.name ?? '机构'}科室助理配置`}
        open={assistantModalOpen}
        width={900}
        onOk={() => void handleSaveAssistantConfig()}
        onCancel={closeAssistantModal}
        confirmLoading={updateMutation.isPending}
        destroyOnClose
      >
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Space>
            <Text type="secondary">启用配置</Text>
            <Switch
              checked={assistantConfig.enabled}
              onChange={(checked) => setAssistantConfig((current) => ({ ...current, enabled: checked }))}
            />
          </Space>
          <Table
            rowKey="department_code"
            dataSource={assistantConfig.departments}
            pagination={false}
            size="small"
            columns={[
              {
                title: '机构科室',
                width: 180,
                render: (_value, row: DepartmentAssistantDepartmentConfig) => (
                  <Space direction="vertical" size={0}>
                    <Text strong>{row.department_name}</Text>
                    <Text type="secondary">{row.department_code}</Text>
                  </Space>
                ),
              },
              {
                title: '科室助理',
                render: (_value, row: DepartmentAssistantDepartmentConfig) => (
                  <Select
                    mode="multiple"
                    allowClear
                    showSearch
                    optionFilterProp="label"
                    maxTagCount="responsive"
                    placeholder="选择人员"
                    loading={assistantStaffLoading}
                    disabled={!assistantConfig.enabled}
                    value={row.assistant_staff_ids}
                    options={staffOptions}
                    style={{ width: '100%' }}
                    onChange={(nextValue) => updateAssistantDepartment(row.department_code, nextValue)}
                  />
                ),
              },
            ]}
          />
        </Space>
      </Modal>
    </div>
  )
}

export default InstitutionsPage
