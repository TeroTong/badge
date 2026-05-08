import { useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Card, Empty, Radio, Spin, Tag, message } from 'antd'
import { CheckCircleOutlined, DeleteOutlined, ReloadOutlined, SaveOutlined, SendOutlined } from '@ant-design/icons'
import { HTTPError } from 'ky'

import {
  fetchPreferenceProfile,
  type PreferenceProfile,
  type PreferenceSettings,
  updatePreferenceProfile,
} from '@/api/preferences'
import {
  deleteCurrentWecomMenu,
  fetchCurrentWecomMenu,
  fetchDefaultWecomMenu,
  publishDefaultWecomMenu,
  type WecomMenuEntry,
} from '@/api/wecom'

const LINKING_CAPABILITIES = [
  {
    title: '多条录音可绑定同一到诊单',
    description: '同一客户由咨询师、医生或多人分段录音时，可归并到同一张到诊单，并合并生成一条咨询单。',
  },
  {
    title: '一条录音可绑定多个到诊单',
    description: '同行客户、连续接待或忘记停止录音时，可关联多张到诊单，并通过多客户对应确认分别分析和回传。',
  },
]

const BOOLEAN_OPTIONS = [
  { value: true, label: '开启' },
  { value: false, label: '不开启' },
]

function PreferenceSection({
  title,
  description,
  children,
}: {
  title: string
  description?: string
  children: ReactNode
}) {
  return (
    <Card bordered={false} className="preference-card">
      <div className="preference-section">
        <div className="preference-section__title">
          <span className="preference-section__marker" aria-hidden="true" />
          <div>
            <h2>{title}</h2>
            {description && <p>{description}</p>}
          </div>
        </div>
        {children}
      </div>
    </Card>
  )
}

function resolveWecomMenuError(error: unknown) {
  if (error instanceof HTTPError) {
    return `请求失败（${error.response.status}）`
  }
  if (error instanceof Error && error.message.trim()) {
    return error.message
  }
  return '请稍后重试'
}

function WecomMenuEntryList({ entries }: { entries: WecomMenuEntry[] }) {
  if (entries.length === 0) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前没有菜单入口" />
  }

  return (
    <div style={{ display: 'grid', gap: 12 }}>
      {entries.map((entry) => (
        <div
          key={`${entry.level}-${entry.label}-${entry.target_path ?? entry.target_url ?? 'empty'}`}
          style={{
            padding: '12px 14px',
            borderRadius: 12,
            border: '1px solid rgba(15, 23, 42, 0.08)',
            background: entry.level > 1 ? 'rgba(248, 250, 252, 0.9)' : '#fff',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <strong>{entry.label}</strong>
            <Tag color={entry.level > 1 ? 'gold' : 'blue'}>{entry.level > 1 ? '二级入口' : '底部按钮'}</Tag>
          </div>
          <div style={{ marginTop: 6, color: '#475569', fontSize: 13 }}>
            {entry.target_path || entry.target_url || '-'}
          </div>
        </div>
      ))}
    </div>
  )
}

function WecomMenuManager() {
  const qc = useQueryClient()
  const defaultMenuQuery = useQuery({
    queryKey: ['wecom-menu', 'default'],
    queryFn: fetchDefaultWecomMenu,
  })
  const currentMenuQuery = useQuery({
    queryKey: ['wecom-menu', 'current'],
    queryFn: fetchCurrentWecomMenu,
  })

  const publishMutation = useMutation({
    mutationFn: publishDefaultWecomMenu,
    onSuccess: () => {
      message.success('企业微信会话菜单已发布')
      qc.invalidateQueries({ queryKey: ['wecom-menu'] })
    },
    onError: (error) => {
      message.error(`发布失败：${resolveWecomMenuError(error)}`)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: deleteCurrentWecomMenu,
    onSuccess: () => {
      message.success('企业微信会话菜单已删除')
      qc.invalidateQueries({ queryKey: ['wecom-menu'] })
    },
    onError: (error) => {
      message.error(`删除失败：${resolveWecomMenuError(error)}`)
    },
  })

  return (
    <PreferenceSection
      title="企业微信会话菜单"
      description="发布后，咨询师在手机企业微信里与“智能工牌”应用会话时，底部会显示快捷按钮。"
    >
      <div className="preference-page__helper" style={{ marginTop: 0 }}>
        <Tag color="blue">默认菜单</Tag>
        <p>当前默认放出 3 个高频入口：我的工牌、录音中心、客户中心。</p>
      </div>

      {defaultMenuQuery.isLoading ? (
        <Spin />
      ) : defaultMenuQuery.isError || !defaultMenuQuery.data ? (
        <Empty description={`默认菜单加载失败：${resolveWecomMenuError(defaultMenuQuery.error)}`} />
      ) : (
        <WecomMenuEntryList entries={defaultMenuQuery.data.entries} />
      )}

      <div
        style={{
          display: 'flex',
          gap: 12,
          flexWrap: 'wrap',
          marginTop: 20,
          marginBottom: 20,
        }}
      >
        <Button
          type="primary"
          icon={<SendOutlined />}
          onClick={() => publishMutation.mutate()}
          loading={publishMutation.isPending}
        >
          发布默认菜单
        </Button>
        <Button
          icon={<ReloadOutlined />}
          onClick={() => {
            void currentMenuQuery.refetch()
            void defaultMenuQuery.refetch()
          }}
          loading={currentMenuQuery.isFetching || defaultMenuQuery.isFetching}
        >
          刷新状态
        </Button>
        <Button
          danger
          icon={<DeleteOutlined />}
          onClick={() => deleteMutation.mutate()}
          loading={deleteMutation.isPending}
        >
          删除当前菜单
        </Button>
      </div>

      <div className="preference-page__helper" style={{ marginTop: 0 }}>
        <Tag color={currentMenuQuery.data?.exists ? 'green' : 'default'}>
          {currentMenuQuery.data?.exists ? '当前菜单已生效' : '当前未发布自定义菜单'}
        </Tag>
        {currentMenuQuery.data?.agent_id ? <Tag>AgentId: {currentMenuQuery.data.agent_id}</Tag> : null}
      </div>

      {currentMenuQuery.isLoading ? (
        <Spin />
      ) : currentMenuQuery.isError || !currentMenuQuery.data ? (
        <Empty description={`当前菜单读取失败：${resolveWecomMenuError(currentMenuQuery.error)}`} />
      ) : (
        <WecomMenuEntryList entries={currentMenuQuery.data.entries} />
      )}
    </PreferenceSection>
  )
}

function PreferenceEditor({ profile }: { profile: PreferenceProfile }) {
  const qc = useQueryClient()
  const [draft, setDraft] = useState<PreferenceSettings>(profile.settings)

  const saveMutation = useMutation({
    mutationFn: updatePreferenceProfile,
    onSuccess: (nextProfile) => {
      qc.setQueryData(['preference-profile'], nextProfile)
      message.success('偏好设置已保存')
    },
  })

  const dirty = JSON.stringify(draft) !== JSON.stringify(profile.settings)
  const autoMatchLabel = draft.auto_match_recording ? '已开启' : '未开启'

  const handleSave = () => {
    saveMutation.mutate(draft)
  }

  return (
    <div className="preference-page">
      <div className="preference-page__header">
        <div>
          <p className="visit-page__eyebrow">配置管理 / 偏好设置</p>
          <h1>偏好设置</h1>
          <p className="visit-page__summary">
            这里只保留当前真正会影响录音绑定与自动匹配的核心配置；录音与到诊单关联已按多对多能力落地。
          </p>
        </div>

        <div className="preference-page__actions">
          <Tag color="blue">多对多关联</Tag>
          <Tag color={draft.auto_match_recording ? 'green' : 'default'}>{autoMatchLabel}</Tag>

          <Button
            type="primary"
            size="large"
            icon={<SaveOutlined />}
            onClick={handleSave}
            loading={saveMutation.isPending}
            disabled={!dirty}
          >
            保存设置
          </Button>
        </div>
      </div>

      <PreferenceSection
        title="录音与到诊单关联能力"
        description="当前规则不再二选一：既支持多条录音绑定同一张到诊单，也支持一条录音绑定多张到诊单。"
      >
        <div className="preference-capability-grid">
          {LINKING_CAPABILITIES.map((item) => (
            <div className="preference-capability-card" key={item.title}>
              <span className="preference-capability-card__icon" aria-hidden="true">
                <CheckCircleOutlined />
              </span>
              <div>
                <h3>{item.title}</h3>
                <p>{item.description}</p>
              </div>
            </div>
          ))}
        </div>
        <div className="preference-page__helper">
          <Tag color="green">已生效</Tag>
          <p>绑定时仍会保留二次确认，涉及一条录音多客户时会进入“多客户对应确认”，避免误关联后直接回传。</p>
        </div>
      </PreferenceSection>

      <PreferenceSection
        title="录音自动匹配"
        description="开启后，系统会结合录音时间、录音人、客户姓名和业务语义，自动尝试把录音关联到对应接诊单。"
      >
        <Radio.Group
          value={draft.auto_match_recording}
          onChange={(event) =>
            setDraft((current) => ({ ...current, auto_match_recording: Boolean(event.target.value) }))
          }
        >
          {BOOLEAN_OPTIONS.map((option) => (
            <Radio key={String(option.value)} value={option.value}>
              {option.label}
            </Radio>
          ))}
        </Radio.Group>
        <div className="preference-page__helper">
          <Tag color={draft.auto_match_recording ? 'green' : 'default'}>
            {draft.auto_match_recording ? '自动匹配已启用' : '当前仅支持手工绑定'}
          </Tag>
          <p>
            建议在录音人、时间窗口和客户识别规则稳定后再开启全量自动匹配，避免把录音误绑定到错误接诊单。
          </p>
        </div>
      </PreferenceSection>

      <WecomMenuManager />
    </div>
  )
}

export function PreferencesPage() {
  const { data, error, isLoading } = useQuery({
    queryKey: ['preference-profile'],
    queryFn: fetchPreferenceProfile,
  })

  if (isLoading) {
    return <Spin style={{ display: 'block', margin: '80px auto' }} size="large" />
  }

  if (error || !data) {
    return (
      <Card bordered={false}>
        <Empty description="偏好设置暂时加载失败，请稍后重试。" />
      </Card>
    )
  }

  return <PreferenceEditor key={`${data.id}:${data.updated_at}`} profile={data} />
}

export default PreferencesPage
