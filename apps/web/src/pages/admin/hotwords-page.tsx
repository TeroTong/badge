import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
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
import { DownloadOutlined, LinkOutlined, PlusOutlined, SearchOutlined } from '@ant-design/icons'

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

function normalizeHotwordKey(value: string) {
  return value.trim().toLocaleLowerCase()
}

function parseHotwordInput(value: unknown) {
  const seen = new Set<string>()
  const uniqueWords: string[] = []
  const duplicateWords: string[] = []
  String(value ?? '')
    .split(/[\n,，]/)
    .map((item) => item.trim())
    .filter(Boolean)
    .forEach((word) => {
      const key = normalizeHotwordKey(word)
      if (!key) return
      if (seen.has(key)) {
        duplicateWords.push(word)
        return
      }
      seen.add(key)
      uniqueWords.push(word)
    })
  return { uniqueWords, duplicateWords }
}

function renderPreviewWords(words: string[]) {
  if (!words.length) return null
  const visibleWords = words.slice(0, 12)
  return (
    <>
      {visibleWords.map((word) => (
        <Tag key={word}>{word}</Tag>
      ))}
      {words.length > visibleWords.length ? <Tag>+{words.length - visibleWords.length}</Tag> : null}
    </>
  )
}

function buildExportFileName(group: HotwordGroup) {
  const safeName = group.name.trim().replace(/[\\/:*?"<>|]+/g, '_') || '热词库'
  return `${safeName}_热词_${new Date().toISOString().slice(0, 10)}.txt`
}

function downloadTextFile(filename: string, content: string) {
  const blob = new Blob([`\ufeff${content}`], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

export function HotwordsPage() {
  const qc = useQueryClient()
  const [activeScope, setActiveScope] = useState<LibraryScope>('public')
  const [selectedGroupId, setSelectedGroupId] = useState<string | null>(null)
  const [wordSearch, setWordSearch] = useState('')

  const [groupModalOpen, setGroupModalOpen] = useState(false)
  const [editingGroup, setEditingGroup] = useState<HotwordGroup | null>(null)
  const [groupForm] = Form.useForm()

  const [wordModalOpen, setWordModalOpen] = useState(false)
  const [editingWord, setEditingWord] = useState<Hotword | null>(null)
  const [wordGroupId, setWordGroupId] = useState<string | null>(null)
  const [wordForm] = Form.useForm()
  const wordsTextValue = Form.useWatch('wordsText', wordForm)

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
  const searchKeyword = wordSearch.trim()
  const searchMatches = useMemo(() => {
    const keyword = normalizeHotwordKey(searchKeyword)
    if (!keyword) return []
    return groups
      .flatMap((group) =>
        group.words
          .filter((word) => normalizeHotwordKey(word.word).includes(keyword))
          .map((word) => ({
            group,
            word,
            exact: normalizeHotwordKey(word.word) === keyword,
          })),
      )
      .sort((a, b) => Number(b.exact) - Number(a.exact) || a.group.name.localeCompare(b.group.name) || a.word.word.localeCompare(b.word.word))
  }, [groups, searchKeyword])
  const batchPreview = useMemo(() => {
    const parsed = parseHotwordInput(wordsTextValue)
    const targetGroup = groups.find((group) => group.id === wordGroupId)
    const existingByKey = new Map((targetGroup?.words ?? []).map((word) => [normalizeHotwordKey(word.word), word.word]))
    const existingWords: string[] = []
    const newWords: string[] = []
    for (const word of parsed.uniqueWords) {
      const existingWord = existingByKey.get(normalizeHotwordKey(word))
      if (existingWord) {
        existingWords.push(existingWord)
      } else {
        newWords.push(word)
      }
    }
    return { ...parsed, existingWords, newWords }
  }, [groups, wordGroupId, wordsTextValue])

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
  const wordBulkCreate = useMutation({
    mutationFn: ({ groupId, data }: { groupId: string; data: { words: string[]; weight?: number; is_active?: boolean } }) =>
      adminApi.createHotwordsBulk(groupId, data),
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
    wordForm.resetFields()
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
      const parsed = parseHotwordInput(values.wordsText)
      const targetGroup = groups.find((group) => group.id === wordGroupId)
      const existingKeys = new Set((targetGroup?.words ?? []).map((word) => normalizeHotwordKey(word.word)))
      const wordsToCreate = parsed.uniqueWords.filter((word) => !existingKeys.has(normalizeHotwordKey(word)))

      if (!parsed.uniqueWords.length) {
        message.warning('请至少输入一个热词')
        return
      }
      if (!wordsToCreate.length) {
        message.warning('输入的热词都已存在于当前词库，没有新增内容')
        return
      }

      const result = await wordBulkCreate.mutateAsync({
        groupId: wordGroupId,
        data: { words: wordsToCreate, weight: values.weight, is_active: values.is_active },
      })
      await invalidate()
      const skippedExistingCount = parsed.uniqueWords.length - wordsToCreate.length
      const skippedCount = parsed.duplicateWords.length + skippedExistingCount + result.skipped_existing.length + result.skipped_duplicate.length
      message.success(`已添加 ${result.created.length} 个热词${skippedCount ? `，跳过 ${skippedCount} 个已存在或重复词` : ''}`)
    }

    setWordModalOpen(false)
  }

  const handleExportCurrentGroup = () => {
    if (!selectedGroup) return
    const words = selectedWords.map((word) => word.word.trim()).filter(Boolean)
    if (!words.length) {
      message.warning('当前词库没有可导出的热词')
      return
    }
    downloadTextFile(buildExportFileName(selectedGroup), words.join('\n'))
    message.success(`已导出 ${words.length} 个热词`)
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
                  <Button icon={<DownloadOutlined />} disabled={!selectedWords.length} onClick={handleExportCurrentGroup}>
                    导出当前词库
                  </Button>
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

              <div className="hotword-search-panel">
                <div className="hotword-search-panel__control">
                  <Input
                    allowClear
                    prefix={<SearchOutlined />}
                    placeholder="搜索热词，查看是否已在词库中"
                    value={wordSearch}
                    onChange={(event) => setWordSearch(event.target.value)}
                  />
                </div>
                {searchKeyword ? (
                  <div className="hotword-search-panel__result">
                    <span>
                      {searchMatches.length
                        ? `找到 ${searchMatches.length} 个匹配热词`
                        : `未找到“${searchKeyword}”`}
                    </span>
                    {searchMatches.length ? (
                      <div className="hotword-search-panel__matches">
                        {searchMatches.slice(0, 16).map((match) => (
                          <button
                            key={`${match.group.id}-${match.word.id}`}
                            type="button"
                            onClick={() => {
                              setActiveScope(match.group.library_scope)
                              setSelectedGroupId(match.group.id)
                            }}
                          >
                            <strong>{match.word.word}</strong>
                            <small>
                              {match.group.library_scope === 'personal' ? '我的热词库' : '公共词库'} / {match.group.name}
                              {match.exact ? ' / 完全匹配' : ''}
                            </small>
                          </button>
                        ))}
                      </div>
                    ) : null}
                  </div>
                ) : null}
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
        confirmLoading={wordBulkCreate.isPending || wordUpdate.isPending}
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

          {!editingWord && wordModalOpen && batchPreview.uniqueWords.length ? (
            <div className="hotword-batch-preview">
              {batchPreview.existingWords.length ? (
                <Alert
                  type="warning"
                  showIcon
                  message={`当前词库已有 ${batchPreview.existingWords.length} 个词，提交时会自动跳过`}
                  description={<div className="hotword-batch-preview__tags">{renderPreviewWords(batchPreview.existingWords)}</div>}
                />
              ) : null}
              {batchPreview.duplicateWords.length ? (
                <Alert
                  type="info"
                  showIcon
                  message={`输入内容中有 ${batchPreview.duplicateWords.length} 个重复词，提交时只保留第一次出现的词`}
                  description={<div className="hotword-batch-preview__tags">{renderPreviewWords(batchPreview.duplicateWords)}</div>}
                />
              ) : null}
              <Alert
                type={batchPreview.newWords.length ? 'success' : 'warning'}
                showIcon
                message={batchPreview.newWords.length ? `将新增 ${batchPreview.newWords.length} 个热词` : '没有可新增的热词'}
                description={<div className="hotword-batch-preview__tags">{renderPreviewWords(batchPreview.newWords)}</div>}
              />
            </div>
          ) : null}
        </Form>
      </Modal>
    </div>
  )
}

export default HotwordsPage
