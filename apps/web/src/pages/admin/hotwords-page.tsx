import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Button,
  Card,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Switch,
  Tag,
  message,
} from 'antd'
import { LinkOutlined, PlusOutlined } from '@ant-design/icons'

import type { Hotword, HotwordGroup } from '@/api/admin'
import * as adminApi from '@/api/admin'
import { formatBeijingTime } from '@/utils/time'

const GROUP_TYPES = ['竞品', '顾虑', '项目', '行业', '通用']
const LIBRARY_SCOPE_OPTIONS = [
  { value: 'personal', label: '我的热词库' },
  { value: 'public', label: '公共词库' },
] as const

type LibraryScope = (typeof LIBRARY_SCOPE_OPTIONS)[number]['value']

function formatDateTime(value: string | undefined) {
  return formatBeijingTime(value, 'YYYY-MM-DD HH:mm')
}

function getWeightBuckets(words: Hotword[]) {
  return {
    high: words.filter((item) => item.weight >= 100).length,
    medium: words.filter((item) => item.weight >= 11 && item.weight < 100).length,
    low: words.filter((item) => item.weight <= 10).length,
  }
}

export function HotwordsPage() {
  const qc = useQueryClient()
  const [activeScope, setActiveScope] = useState<LibraryScope>('public')
  const [selectedGroupId, setSelectedGroupId] = useState<string | null>(null)

  const [groupModalOpen, setGroupModalOpen] = useState(false)
  const [editingGroup, setEditingGroup] = useState<HotwordGroup | null>(null)
  const [groupForm] = Form.useForm()

  const [wordModalOpen, setWordModalOpen] = useState(false)
  const [editingWord, setEditingWord] = useState<Hotword | null>(null)
  const [wordGroupId, setWordGroupId] = useState<string | null>(null)
  const [wordForm] = Form.useForm()

  const { data: groups = [], isLoading } = useQuery({
    queryKey: ['hotword-groups'],
    queryFn: () => adminApi.fetchHotwordGroups(),
  })

  const scopeOptions = LIBRARY_SCOPE_OPTIONS.filter((option) =>
    groups.some((group) => group.library_scope === option.value),
  )
  const effectiveScope = scopeOptions.some((option) => option.value === activeScope)
    ? activeScope
    : (scopeOptions[0]?.value ?? 'public')
  const visibleGroups = groups.filter((group) => group.library_scope === effectiveScope)
  const selectedGroup = visibleGroups.find((group) => group.id === selectedGroupId) ?? visibleGroups[0] ?? null
  const selectedWords = [...(selectedGroup?.words ?? [])].sort((a, b) => b.weight - a.weight || a.word.localeCompare(b.word))
  const weightBuckets = getWeightBuckets(selectedWords)

  const invalidate = async () => {
    await qc.invalidateQueries({ queryKey: ['hotword-groups'] })
  }

  const groupCreate = useMutation({
    mutationFn: adminApi.createHotwordGroup,
    onSuccess: async (group) => {
      await invalidate()
      setActiveScope(group.library_scope)
      setSelectedGroupId(group.id)
    },
  })
  const groupUpdate = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Partial<HotwordGroup> }) => adminApi.updateHotwordGroup(id, data),
    onSuccess: invalidate,
  })
  const groupDelete = useMutation({
    mutationFn: adminApi.deleteHotwordGroup,
    onSuccess: async () => {
      await invalidate()
      setSelectedGroupId(null)
    },
  })
  const wordCreate = useMutation({
    mutationFn: ({ groupId, data }: { groupId: string; data: { word: string; weight?: number; is_active?: boolean } }) =>
      adminApi.createHotword(groupId, data),
    onSuccess: invalidate,
  })
  const wordUpdate = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Partial<Hotword> }) => adminApi.updateHotword(id, data),
    onSuccess: invalidate,
  })
  const wordDelete = useMutation({
    mutationFn: adminApi.deleteHotword,
    onSuccess: invalidate,
  })

  const openGroupModal = (group?: HotwordGroup) => {
    const next = group ?? null
    setEditingGroup(next)
    groupForm.setFieldsValue(
      next ?? {
        name: '',
        group_type: '行业',
        library_scope: activeScope,
        source_label: activeScope === 'personal' ? '自定义' : '行业',
      },
    )
    setGroupModalOpen(true)
  }

  const handleGroupSubmit = async () => {
    const values = await groupForm.validateFields()
    if (editingGroup) {
      await groupUpdate.mutateAsync({ id: editingGroup.id, data: values })
      message.success('词库已更新')
    } else {
      await groupCreate.mutateAsync(values)
      message.success('词库已创建')
    }
    setGroupModalOpen(false)
  }

  const openWordModal = (groupId: string, word?: Hotword) => {
    setWordGroupId(groupId)
    setEditingWord(word ?? null)
    wordForm.setFieldsValue(
      word
        ? { word: word.word, weight: word.weight, is_active: word.is_active }
        : { wordsText: '', weight: 10, is_active: true },
    )
    setWordModalOpen(true)
  }

  const handleWordSubmit = async () => {
    if (!wordGroupId) return
    const values = await wordForm.validateFields()

    if (editingWord) {
      await wordUpdate.mutateAsync({
        id: editingWord.id,
        data: {
          word: values.word.trim(),
          weight: values.weight,
          is_active: values.is_active,
        },
      })
      message.success('词汇已更新')
    } else {
      const words = String(values.wordsText)
        .split(/[\n,，]/)
        .map((item) => item.trim())
        .filter(Boolean)

      if (!words.length) {
        message.warning('请至少输入一个热词')
        return
      }

      let created = 0
      for (const word of words) {
        await wordCreate.mutateAsync({
          groupId: wordGroupId,
          data: { word, weight: values.weight, is_active: values.is_active },
        })
        created += 1
      }
      message.success(`已添加 ${created} 个热词`)
    }

    setWordModalOpen(false)
  }

  return (
    <div className="hotword-page">
      <div className="hotword-page__header">
        <div>
          <p className="visit-page__eyebrow">配置管理 / 热词管理</p>
          <h1>热词管理</h1>
          <p className="visit-page__summary">
            统一维护当前正在使用的热词库，服务于对话分析、竞品识别、顾虑挖掘与规则匹配。
          </p>
        </div>
      </div>

      <div className="hotword-workspace">
        <Card bordered={false} className="hotword-sidebar">
          {scopeOptions.length > 1 && (
            <div className="hotword-sidebar__tabs">
              {scopeOptions.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  className={option.value === effectiveScope ? 'is-active' : ''}
                  onClick={() => setActiveScope(option.value)}
                >
                  {option.label}
                </button>
              ))}
            </div>
          )}

          <div className="hotword-sidebar__list">
            {visibleGroups.length ? (
              visibleGroups.map((group) => (
                <button
                  key={group.id}
                  type="button"
                  className={`hotword-library-card ${selectedGroup?.id === group.id ? 'is-active' : ''}`}
                  onClick={() => setSelectedGroupId(group.id)}
                >
                  <div className="hotword-library-card__top">
                    <div>
                      <strong>{group.name}</strong>
                      <span>{group.words.length} 个词</span>
                    </div>
                    <span
                      onClick={(event) => event.stopPropagation()}
                      onKeyDown={(event) => event.stopPropagation()}
                      role="presentation"
                    >
                      <Switch
                        size="small"
                        checked={group.is_active}
                        onChange={(checked) => groupUpdate.mutate({ id: group.id, data: { is_active: checked } })}
                      />
                    </span>
                  </div>

                  <div className="hotword-library-card__meta">
                    <span>来源：{group.source_label}</span>
                    <span>类型：{group.group_type}</span>
                  </div>

                  <div className="hotword-library-card__actions">
                    <span
                      role="button"
                      tabIndex={0}
                      onClick={(event) => {
                        event.stopPropagation()
                        openGroupModal(group)
                      }}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault()
                          openGroupModal(group)
                        }
                      }}
                    >
                      编辑
                    </span>
                    <Popconfirm
                      title="确认删除这个词库吗？"
                      description="词库下的所有热词也会一起删除。"
                      onConfirm={(event) => {
                        event?.stopPropagation()
                        groupDelete.mutate(group.id)
                      }}
                    >
                      <span
                        role="button"
                        tabIndex={0}
                        className="is-danger"
                        onClick={(event) => event.stopPropagation()}
                        onKeyDown={(event) => {
                          if (event.key === 'Enter' || event.key === ' ') {
                            event.preventDefault()
                          }
                        }}
                      >
                        删除
                      </span>
                    </Popconfirm>
                  </div>
                </button>
              ))
            ) : (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={effectiveScope === 'personal' ? '还没有个人词库' : '还没有公共词库'}
              />
            )}
          </div>

          <Button type="primary" size="large" icon={<PlusOutlined />} block onClick={() => openGroupModal()}>
            新建词库
          </Button>
        </Card>

        <Card bordered={false} className="hotword-main">
          {selectedGroup ? (
            <div className="hotword-main__content">
              <div className="hotword-main__topbar">
                <div>
                  {scopeOptions.length > 1 && (
                    <p className="hotword-main__scope">
                      {selectedGroup.library_scope === 'personal' ? '我的热词库' : '公共词库'}
                    </p>
                  )}
                  <h2>{selectedGroup.name}</h2>
                </div>

                <div className="hotword-main__actions">
                  <Button type="primary" ghost icon={<PlusOutlined />} onClick={() => openWordModal(selectedGroup.id)}>
                    添加词汇
                  </Button>
                  <Button type="primary" icon={<LinkOutlined />} onClick={() => openGroupModal(selectedGroup)}>
                    编辑词库
                  </Button>
                </div>
              </div>

              <div className="hotword-main__stats">
                <Tag className="hotword-stat">权重：100 {weightBuckets.high}</Tag>
                <Tag className="hotword-stat">权重：11-99 {weightBuckets.medium}</Tag>
                <Tag className="hotword-stat">权重：1-10 {weightBuckets.low}</Tag>
                <span className="hotword-main__updated">最后更新时间：{formatDateTime(selectedGroup.updated_at)}</span>
              </div>

              <div className="hotword-main__summary">
                <div className="hotword-summary-card">
                  <span>词库来源</span>
                  <strong>{selectedGroup.source_label}</strong>
                </div>
                <div className="hotword-summary-card">
                  <span>词库类型</span>
                  <strong>{selectedGroup.group_type}</strong>
                </div>
                <div className="hotword-summary-card">
                  <span>启用状态</span>
                  <strong>{selectedGroup.is_active ? '已启用' : '已停用'}</strong>
                </div>
                <div className="hotword-summary-card">
                  <span>词汇数量</span>
                  <strong>{selectedWords.length}</strong>
                </div>
              </div>

              <div className="hotword-token-panel">
                {selectedWords.length ? (
                  selectedWords.map((word) => (
                    <Tag
                      key={word.id}
                      className={`hotword-token ${word.is_active ? '' : 'is-muted'}`}
                      closable
                      onClose={(event) => {
                        event.preventDefault()
                        void wordDelete.mutateAsync(word.id).then(() => message.success('热词已删除'))
                      }}
                    >
                      <span
                        role="button"
                        tabIndex={0}
                        onClick={() => openWordModal(selectedGroup.id, word)}
                        onKeyDown={(event) => {
                          if (event.key === 'Enter' || event.key === ' ') {
                            event.preventDefault()
                            openWordModal(selectedGroup.id, word)
                          }
                        }}
                      >
                        {word.word}
                      </span>
                      <em>W{word.weight}</em>
                    </Tag>
                  ))
                ) : (
                  <Empty description="这个词库里还没有热词，先添加一些吧。" />
                )}
              </div>
            </div>
          ) : (
            <div className="hotword-main__empty">
              <Empty
                description={isLoading ? '热词库加载中…' : '当前词库为空，先新建一个词库吧。'}
                image={Empty.PRESENTED_IMAGE_SIMPLE}
              />
              <Button type="primary" icon={<PlusOutlined />} onClick={() => openGroupModal()}>
                新建词库
              </Button>
            </div>
          )}
        </Card>
      </div>

      <Modal
        title={editingGroup ? '编辑词库' : '新建词库'}
        open={groupModalOpen}
        onOk={() => void handleGroupSubmit()}
        onCancel={() => setGroupModalOpen(false)}
        confirmLoading={groupCreate.isPending || groupUpdate.isPending}
      >
        <Form form={groupForm} layout="vertical">
          <Form.Item name="name" label="词库名称" rules={[{ required: true, message: '请输入词库名称' }]}>
            <Input placeholder="例如：竞品机构热词" />
          </Form.Item>
          <Form.Item name="group_type" label="词库类型" rules={[{ required: true, message: '请选择词库类型' }]}>
            <Select options={GROUP_TYPES.map((item) => ({ label: item, value: item }))} />
          </Form.Item>
          <Form.Item name="library_scope" label="词库归属" rules={[{ required: true, message: '请选择词库归属' }]}>
            <Select options={LIBRARY_SCOPE_OPTIONS.map((item) => ({ label: item.label, value: item.value }))} />
          </Form.Item>
          <Form.Item name="source_label" label="来源" rules={[{ required: true, message: '请输入来源' }]}>
            <Input placeholder="例如：行业 / 运营 / 机构 / 自定义" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={editingWord ? '编辑词汇' : '添加词汇'}
        open={wordModalOpen}
        onOk={() => void handleWordSubmit()}
        onCancel={() => setWordModalOpen(false)}
        confirmLoading={wordCreate.isPending || wordUpdate.isPending}
      >
        <Form form={wordForm} layout="vertical">
          {editingWord ? (
            <Form.Item name="word" label="热词" rules={[{ required: true, message: '请输入热词' }]}>
              <Input placeholder="请输入热词" />
            </Form.Item>
          ) : (
            <Form.Item
              name="wordsText"
              label="热词列表"
              rules={[{ required: true, message: '请输入至少一个热词' }]}
              extra="支持英文逗号、中文逗号或换行批量添加。"
            >
              <Input.TextArea rows={5} placeholder="例如：米兰柏羽，新丽美，美莱" />
            </Form.Item>
          )}

          <Form.Item name="weight" label="权重" rules={[{ required: true, message: '请填写权重' }]}>
            <InputNumber min={1} max={100} style={{ width: '100%' }} />
          </Form.Item>

          <Form.Item name="is_active" label="启用状态" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}

export default HotwordsPage
