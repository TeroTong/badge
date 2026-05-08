import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Empty, Space, Spin, Switch, Tag, message } from 'antd'
import { ReloadOutlined, SaveOutlined } from '@ant-design/icons'

import {
  fetchIotCapabilities,
  type IotCapabilityDefinition,
  type IotCapabilityState,
  updateIotCapabilities,
} from '@/api/iot'

function groupDefinitions(definitions: IotCapabilityDefinition[]) {
  return definitions.reduce<Record<string, IotCapabilityDefinition[]>>((groups, item) => {
    const key = item.group || '其它能力'
    groups[key] = groups[key] || []
    groups[key].push(item)
    return groups
  }, {})
}

function riskTag(level: string) {
  if (level === 'high') return <Tag color="red">高风险</Tag>
  return <Tag color="gold">需确认</Tag>
}

function callbackPathFor(item: IotCapabilityDefinition) {
  if (!item.key.startsWith('callback_')) return null
  return `/api/v1/iot/callbacks/${item.key.replace(/^callback_/, '').replaceAll('_', '-')}`
}

function IotCapabilityEditor({ state }: { state: IotCapabilityState }) {
  const qc = useQueryClient()
  const [draft, setDraft] = useState<Record<string, boolean>>({ ...state.capabilities })
  const groups = useMemo(() => groupDefinitions(state.definitions), [state.definitions])
  const enabledCount = state.definitions.filter((item) => draft[item.key]).length
  const dirty = state.definitions.some((item) => Boolean(draft[item.key]) !== Boolean(state.capabilities[item.key]))

  const saveMutation = useMutation({
    mutationFn: updateIotCapabilities,
    onSuccess: (nextState) => {
      qc.setQueryData(['iot-capabilities'], nextState)
      setDraft({ ...nextState.capabilities })
      message.success('IOT 能力开关已保存')
    },
    onError: () => {
      setDraft({ ...state.capabilities })
      message.error('IOT 能力开关保存失败，请稍后重试')
    },
  })

  const toggleCapability = (key: string, enabled: boolean) => {
    const nextDraft = { ...draft, [key]: enabled }
    setDraft(nextDraft)
    saveMutation.mutate(nextDraft)
  }

  return (
    <div className="iot-capability-page">
      <div className="preference-page__header">
        <div>
          <p className="visit-page__eyebrow">规则配置 / IOT配置</p>
          <h1>IOT配置</h1>
          <p className="visit-page__summary">
            长沙雅美工牌已使用的基础链路保持不变；这里管理额外 IOT 控制、任务和回调能力，拨动开关后会自动保存。
          </p>
        </div>
        <div className="preference-page__actions">
          <Space wrap>
            <Tag color={enabledCount > 0 ? 'green' : 'default'}>已开启 {enabledCount} 项</Tag>
            <Tag color="blue">默认关闭</Tag>
          </Space>
          <Space wrap>
            <Button
              icon={<ReloadOutlined />}
              onClick={() => setDraft({ ...state.capabilities })}
              disabled={!dirty || saveMutation.isPending}
            >
              恢复当前
            </Button>
            <Button
              type="primary"
              icon={<SaveOutlined />}
              disabled={!dirty}
              loading={saveMutation.isPending}
              onClick={() => saveMutation.mutate(draft)}
            >
              保存开关
            </Button>
          </Space>
        </div>
      </div>

      <Alert
        type="info"
        showIcon
        message="开关关闭时，后端不会调用对应 IOT 接口，也不会处理对应平台回调。"
      />

      {Object.entries(groups).map(([group, items]) => (
        <Card key={group} bordered={false} className="iot-capability-card">
          <div className="iot-capability-group">
            <div className="iot-capability-group__title">
              <h2>{group}</h2>
              <span>{items.filter((item) => draft[item.key]).length} / {items.length}</span>
            </div>
            <div className="iot-capability-list">
              {items.map((item) => {
                const callbackPath = callbackPathFor(item)
                return (
                  <div className="iot-capability-row" key={item.key}>
                    <div className="iot-capability-row__main">
                      <div className="iot-capability-row__title">
                        <strong>{item.title}</strong>
                        {riskTag(item.risk_level)}
                        <Tag color={draft[item.key] ? 'green' : 'default'}>
                          {draft[item.key] ? '已开启' : '未开启'}
                        </Tag>
                      </div>
                      <p>{item.description}</p>
                      {callbackPath && <code>{callbackPath}</code>}
                    </div>
                    <Switch
                      checked={Boolean(draft[item.key])}
                      disabled={saveMutation.isPending}
                      onChange={(checked) => toggleCapability(item.key, checked)}
                    />
                  </div>
                )
              })}
            </div>
          </div>
        </Card>
      ))}
    </div>
  )
}

export function IotCapabilitiesPage() {
  const { data, error, isLoading } = useQuery({
    queryKey: ['iot-capabilities'],
    queryFn: fetchIotCapabilities,
  })

  if (isLoading) {
    return <Spin style={{ display: 'block', margin: '80px auto' }} size="large" />
  }

  if (error || !data) {
    return (
      <Card bordered={false}>
        <Empty description="IOT配置暂时加载失败，请稍后重试。" />
      </Card>
    )
  }

  return <IotCapabilityEditor key={JSON.stringify(data.capabilities)} state={data} />
}

export default IotCapabilitiesPage
