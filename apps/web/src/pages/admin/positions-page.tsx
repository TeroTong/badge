import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Form, Input, message, Modal, Popconfirm, Select, Space, Table, Tag } from 'antd'
import { PlusOutlined } from '@ant-design/icons'

import { roleLabel } from '@/app/roles'
import type { PositionProfile } from '@/api/admin'
import * as adminApi from '@/api/admin'
import { getApiErrorMessage } from '@/api/errors'

const POSITION_TYPE_OPTIONS = [
  { label: '管理岗', value: 'management' },
  { label: '普通岗位', value: 'staff' },
]

const ROLE_OPTIONS = [
  { label: '超级管理员', value: 'super_admin' },
  { label: '系统管理员', value: 'system_admin' },
  { label: '机构管理员', value: 'hospital_admin' },
  { label: '普通员工', value: 'staff' },
]

type SuperAdminFilter = 'all' | 'yes' | 'no'

export function PositionsPage() {
  const qc = useQueryClient()
  const [modalOpen, setModalOpen] = useState(false)
  const [editingPosition, setEditingPosition] = useState<PositionProfile | null>(null)
  const [filters, setFilters] = useState({
    keyword: '',
    position_type: undefined as string | undefined,
    is_super_admin: 'all' as SuperAdminFilter,
  })
  const [queryFilters, setQueryFilters] = useState(filters)
  const [form] = Form.useForm()

  const { data: positions = [], isLoading } = useQuery({
    queryKey: ['positions', queryFilters],
    queryFn: () =>
      adminApi.fetchPositions({
        keyword: queryFilters.keyword || undefined,
        position_type: queryFilters.position_type,
        is_super_admin:
          queryFilters.is_super_admin === 'all'
            ? undefined
            : queryFilters.is_super_admin === 'yes',
      }),
  })

  const invalidate = async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ['positions'] }),
      qc.invalidateQueries({ queryKey: ['staff'] }),
      qc.invalidateQueries({ queryKey: ['audit-logs'] }),
    ])
  }

  const createMutation = useMutation({
    mutationFn: adminApi.createPosition,
    onSuccess: () => void invalidate(),
  })
  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Partial<PositionProfile> }) => adminApi.updatePosition(id, data),
    onSuccess: () => void invalidate(),
  })
  const deleteMutation = useMutation({
    mutationFn: adminApi.deletePosition,
    onSuccess: () => void invalidate(),
  })

  const resetFilters = () => {
    const next = {
      keyword: '',
      position_type: undefined,
      is_super_admin: 'all' as SuperAdminFilter,
    }
    setFilters(next)
    setQueryFilters(next)
  }

  const openModal = (position?: PositionProfile) => {
    setEditingPosition(position ?? null)
    form.setFieldsValue({
      name: position?.name ?? '',
      position_type: position?.position_type ?? 'staff',
      mapped_role: position?.mapped_role ?? 'staff',
      is_super_admin: position?.is_super_admin ?? false,
      note: position?.note ?? '',
      is_active: position?.is_active ?? true,
    })
    setModalOpen(true)
  }

  const handleSave = async () => {
    try {
      const values = await form.validateFields()
      if (editingPosition) {
        await updateMutation.mutateAsync({ id: editingPosition.id, data: values })
        message.success('岗位已更新')
      } else {
        await createMutation.mutateAsync(values)
        message.success('岗位已新增')
      }
      setModalOpen(false)
      form.resetFields()
    } catch (error) {
      message.error(await getApiErrorMessage(error, '保存岗位失败'))
    }
  }

  const handleDelete = async (position: PositionProfile) => {
    try {
      await deleteMutation.mutateAsync(position.id)
      message.success('岗位已删除')
    } catch (error) {
      message.error(await getApiErrorMessage(error, '删除岗位失败'))
    }
  }

  const handleToggle = async (position: PositionProfile) => {
    try {
      await updateMutation.mutateAsync({
        id: position.id,
        data: { is_active: !position.is_active },
      })
      message.success(position.is_active ? '岗位已禁用' : '岗位已启用')
    } catch (error) {
      message.error(await getApiErrorMessage(error, '更新岗位状态失败'))
    }
  }

  return (
    <div className="operation-page">
      <div className="operation-page__header">
        <div className="operation-page__title">
          <span className="operation-page__marker" aria-hidden="true" />
          <div>
            <h1>角色管理</h1>
            <p>维护岗位类型、角色映射和超级管理员权限。</p>
          </div>
        </div>
      </div>

      <div className="operation-card">
        <div className="operation-filter-grid">
          <label className="operation-filter-item">
            <span>岗位管理</span>
            <Input
              placeholder="请输入岗位名称"
              value={filters.keyword}
              onChange={(event) => setFilters((current) => ({ ...current, keyword: event.target.value }))}
            />
          </label>
          <label className="operation-filter-item">
            <span>岗位类型</span>
            <Select
              allowClear
              placeholder="请选择岗位类型"
              options={POSITION_TYPE_OPTIONS}
              value={filters.position_type}
              onChange={(value) => setFilters((current) => ({ ...current, position_type: value }))}
            />
          </label>
          <label className="operation-filter-item">
            <span>超级管理员</span>
            <Select
              value={filters.is_super_admin}
              options={[
                { label: '全部', value: 'all' },
                { label: '是', value: 'yes' },
                { label: '否', value: 'no' },
              ]}
              onChange={(value: SuperAdminFilter) =>
                setFilters((current) => ({ ...current, is_super_admin: value }))
              }
            />
          </label>
        </div>

        <div className="operation-toolbar">
          <Space wrap>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => openModal()}>
              新增岗位
            </Button>
          </Space>

          <Space>
            <Button type="primary" onClick={() => setQueryFilters(filters)}>
              查询
            </Button>
            <Button onClick={resetFilters}>重置</Button>
          </Space>
        </div>

        <Table
          rowKey="id"
          dataSource={positions}
          loading={isLoading}
          pagination={{ pageSize: 10, showSizeChanger: false, showTotal: (total) => `共 ${total} 条` }}
          columns={[
            { title: '岗位名称', dataIndex: 'name' },
            {
              title: '岗位类型',
              dataIndex: 'position_type',
              render: (value) => (value === 'management' ? '管理岗' : '普通岗位'),
            },
            {
              title: '映射角色',
              dataIndex: 'mapped_role',
              render: (value) => roleLabel(value),
            },
            {
              title: '超级管理员',
              dataIndex: 'is_super_admin',
              render: (value) => <Tag color={value ? 'success' : 'default'}>{value ? '是' : '否'}</Tag>,
            },
            { title: '备注', dataIndex: 'note', render: (value) => value || '-' },
            {
              title: '操作',
              width: 220,
              render: (_value, row: PositionProfile) => (
                <Space wrap>
                  <Button size="small" onClick={() => openModal(row)}>
                    编辑
                  </Button>
                  <Button size="small" danger={row.is_active} onClick={() => void handleToggle(row)}>
                    {row.is_active ? '禁用' : '启用'}
                  </Button>
                  <Popconfirm title="确定删除这个岗位吗？" onConfirm={() => void handleDelete(row)}>
                    <Button size="small" danger>
                      删除
                    </Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </div>

      <Modal
        title={editingPosition ? '编辑岗位' : '新增岗位'}
        open={modalOpen}
        onOk={() => void handleSave()}
        onCancel={() => {
          setModalOpen(false)
          setEditingPosition(null)
          form.resetFields()
        }}
        confirmLoading={createMutation.isPending || updateMutation.isPending}
        destroyOnClose
      >
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="岗位名称" rules={[{ required: true, message: '请输入岗位名称' }]}>
            <Input />
          </Form.Item>
          <Form.Item name="position_type" label="岗位类型" rules={[{ required: true, message: '请选择岗位类型' }]}>
            <Select options={POSITION_TYPE_OPTIONS} />
          </Form.Item>
          <Form.Item name="mapped_role" label="映射角色" rules={[{ required: true, message: '请选择映射角色' }]}>
            <Select options={ROLE_OPTIONS} />
          </Form.Item>
          <Form.Item name="is_super_admin" label="是否超级管理员">
            <Select
              options={[
                { label: '是', value: true },
                { label: '否', value: false },
              ]}
            />
          </Form.Item>
          <Form.Item name="note" label="备注">
            <Input.TextArea rows={3} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}

export default PositionsPage
