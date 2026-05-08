import { useEffect } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Avatar, Button, Card, Descriptions, Empty, Form, Input, List, Space, Statistic, Tag, Typography, message } from 'antd'
import {
  LockOutlined,
  MobileOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
  UserOutlined,
} from '@ant-design/icons'

import * as authApi from '@/api/auth'
import { getApiErrorMessage } from '@/api/errors'
import { useAuth } from '@/app/use-auth'
import { formatBeijingTime } from '@/utils/time'

const { Text } = Typography

function formatDateTime(value: string | null) {
  return formatBeijingTime(value, 'YYYY-MM-DD HH:mm:ss')
}

function getBadgeOnlineMeta(badge: authApi.MyBadge | undefined) {
  if (badge?.online === true) return { color: 'success', label: '设备在线' }
  if (badge?.online === false) return { color: 'default', label: '设备离线' }
  return { color: 'default', label: '状态待同步' }
}

function getBatteryTagColor(level: number | null | undefined) {
  if (typeof level !== 'number') return 'default'
  if (level > 50) return 'success'
  if (level > 20) return 'warning'
  return 'error'
}

export function ProfilePage() {
  const auth = useAuth()
  const qc = useQueryClient()
  const [profileForm] = Form.useForm()
  const [passwordForm] = Form.useForm()

  const { data, isLoading } = useQuery({
    queryKey: ['account-profile'],
    queryFn: authApi.getAccountProfile,
  })
  const badgeQuery = useQuery({
    queryKey: ['account-my-badge'],
    queryFn: authApi.getMyBadge,
  })

  useEffect(() => {
    if (!data) return
    profileForm.setFieldsValue({ display_name: data.display_name })
  }, [data, profileForm])

  const updateProfileMutation = useMutation({
    mutationFn: authApi.updateAccountProfile,
    onSuccess: async (nextProfile) => {
      qc.setQueryData(['account-profile'], nextProfile)
      await auth.refreshUser()
      message.success('个人资料已更新')
    },
  })

  const changePasswordMutation = useMutation({
    mutationFn: ({ currentPassword, nextPassword }: { currentPassword: string; nextPassword: string }) =>
      authApi.changeAccountPassword(currentPassword, nextPassword),
    onSuccess: (result) => {
      passwordForm.resetFields()
      message.success(result.message)
    },
  })

  const startBadgeRecordingMutation = useMutation({
    mutationFn: authApi.startMyBadgeRecording,
    onSuccess: async (result) => {
      await qc.invalidateQueries({ queryKey: ['account-my-badge'] })
      message.success(result.message)
    },
    onError: async (error) => {
      message.error(await getApiErrorMessage(error, '启动录音失败'))
    },
  })

  const stopBadgeRecordingMutation = useMutation({
    mutationFn: authApi.stopMyBadgeRecording,
    onSuccess: async (result) => {
      await qc.invalidateQueries({ queryKey: ['account-my-badge'] })
      message.success(result.message)
    },
    onError: async (error) => {
      message.error(await getApiErrorMessage(error, '暂停录音失败'))
    },
  })

  const submitProfile = async () => {
    try {
      const values = await profileForm.validateFields()
      await updateProfileMutation.mutateAsync(values.display_name)
    } catch (error) {
      message.error(await getApiErrorMessage(error, '更新个人资料失败'))
    }
  }

  const submitPassword = async () => {
    try {
      const values = await passwordForm.validateFields()
      await changePasswordMutation.mutateAsync({
        currentPassword: values.current_password,
        nextPassword: values.new_password,
      })
    } catch (error) {
      message.error(await getApiErrorMessage(error, '修改密码失败'))
    }
  }

  const badge = badgeQuery.data
  const badgeOnlineMeta = getBadgeOnlineMeta(badge)

  return (
    <div className="operation-page">
      <div className="operation-page__header">
        <div className="operation-page__title">
          <span className="operation-page__marker" aria-hidden="true" />
          <div>
            <h1>个人中心</h1>
            <p>查看账号信息、修改显示名称和登录密码，并回看自己最近的后台操作记录。</p>
          </div>
        </div>
      </div>

      <div className="profile-page__grid">
        <Card bordered={false} loading={isLoading} className="operation-card profile-page__hero">
          <div className="profile-page__hero-content">
            <div className="profile-page__identity">
              <Avatar size={72} icon={<UserOutlined />} className="profile-page__avatar" />
              <div>
                <h2>
                  {data?.display_name ||
                    (auth.status === 'authenticated' ? auth.user.display_name || auth.user.username : '未登录用户')}
                </h2>
                <p>{data?.username || (auth.status === 'authenticated' ? auth.user.username : '-')}</p>
                <Space wrap>
                  <Tag color="purple">{data?.role || (auth.status === 'authenticated' ? auth.user.role : '-')}</Tag>
                  <Tag color={data?.is_active === false ? 'red' : 'green'}>
                    {data?.is_active === false ? '已停用' : '正常'}
                  </Tag>
                </Space>
              </div>
            </div>

            <div className="profile-page__stats">
              <Statistic title="最近操作数" value={data?.activity_count ?? 0} />
              <Statistic title="最近活动" value={formatDateTime(data?.last_activity_at ?? null)} />
            </div>
          </div>
        </Card>

        <Card bordered={false} loading={badgeQuery.isLoading} className="operation-card profile-page__badge-card">
          <div className="profile-page__section-title">
            <strong>我的工牌</strong>
            <span>查看当前账号绑定工牌的在线状态、电量，并直接开始或暂停录音。</span>
          </div>

          {badge?.bound ? (
            <>
              {badge.remote_warning ? (
                <Alert
                  type="warning"
                  showIcon
                  message={badge.remote_warning}
                  className="profile-page__alert"
                />
              ) : null}

              <div className="profile-page__badge-overview">
                <div className="profile-page__badge-device">
                  <div className="profile-page__badge-icon" aria-hidden="true">
                    <MobileOutlined />
                  </div>
                  <div>
                    <h3>{badge.device_name || badge.device_code || '未命名工牌'}</h3>
                    <p>{badge.device_code || '未维护工牌 SN'}</p>
                    <Space wrap>
                      <Tag color={badgeOnlineMeta.color}>{badgeOnlineMeta.label}</Tag>
                      <Tag color={badge.can_control_recording ? 'blue' : 'default'}>
                        {badge.can_control_recording ? '可控制录音' : '暂不可录音'}
                      </Tag>
                      {badge.position_name ? <Tag>{badge.position_name}</Tag> : null}
                      {badge.hospital_short_name ? <Tag>{badge.hospital_short_name}</Tag> : null}
                    </Space>
                  </div>
                </div>

                <div className="profile-page__badge-stats">
                  <div className="profile-page__badge-stat">
                    <span>电量</span>
                    <strong>
                      {typeof badge.battery_level === 'number' ? `${badge.battery_level}%` : '-'}
                    </strong>
                    <Tag color={getBatteryTagColor(badge.battery_level)} icon={<ThunderboltOutlined />}>
                      {typeof badge.battery_level === 'number' ? '已同步' : '未知'}
                    </Tag>
                  </div>
                  <div className="profile-page__badge-stat">
                    <span>录音控制</span>
                    <strong>{badge.can_control_recording ? '可用' : '待配置'}</strong>
                    <Text type="secondary">
                      {badge.can_control_recording ? '已具备开始/暂停录音条件' : '需先完成钉钉侧绑定'}
                    </Text>
                  </div>
                </div>
              </div>

              <Descriptions column={2} size="small" className="profile-page__descriptions profile-page__badge-descriptions">
                <Descriptions.Item label="工牌 SN">{badge.device_code || '-'}</Descriptions.Item>
                <Descriptions.Item label="绑定人员">
                  {badge.staff_name ? `${badge.staff_name}${badge.external_account ? ` / ${badge.external_account}` : ''}` : '-'}
                </Descriptions.Item>
                <Descriptions.Item label="钉钉团队">
                  {badge.team_code ? <Text copyable={{ text: badge.team_code }}>{badge.team_code}</Text> : '未绑定'}
                </Descriptions.Item>
                <Descriptions.Item label="钉钉用户">
                  {badge.user_id ? <Text copyable={{ text: badge.user_id }}>{badge.user_id}</Text> : '未绑定'}
                </Descriptions.Item>
              </Descriptions>

              {!badge.can_control_recording ? (
                <Alert
                  type="info"
                  showIcon
                  message="当前工牌已在系统内绑定，但钉钉侧还未绑定团队或用户，暂时不能控制录音。"
                  className="profile-page__alert"
                />
              ) : null}

              <Space wrap>
                <Button
                  icon={<ReloadOutlined />}
                  loading={badgeQuery.isFetching}
                  onClick={() => void badgeQuery.refetch()}
                >
                  刷新状态
                </Button>
                <Button
                  type="primary"
                  icon={<PlayCircleOutlined />}
                  disabled={!badge.can_control_recording}
                  loading={startBadgeRecordingMutation.isPending}
                  onClick={() => startBadgeRecordingMutation.mutate()}
                >
                  开始录音
                </Button>
                <Button
                  icon={<PauseCircleOutlined />}
                  disabled={!badge.can_control_recording}
                  loading={stopBadgeRecordingMutation.isPending}
                  onClick={() => stopBadgeRecordingMutation.mutate()}
                >
                  暂停录音
                </Button>
              </Space>
            </>
          ) : (
            <Empty description={badge?.reason || '当前账号暂未绑定工牌'}>
              <Button
                icon={<ReloadOutlined />}
                loading={badgeQuery.isFetching}
                onClick={() => void badgeQuery.refetch()}
              >
                重新检查
              </Button>
            </Empty>
          )}
        </Card>

        <Card bordered={false} loading={isLoading} className="operation-card">
          <div className="profile-page__section-title">
            <strong>账号资料</strong>
            <span>维护显示名称，并查看账号基础信息。</span>
          </div>

          <Descriptions column={1} size="small" className="profile-page__descriptions">
            <Descriptions.Item label="登录账号">{data?.username || '-'}</Descriptions.Item>
            <Descriptions.Item label="角色">{data?.role || '-'}</Descriptions.Item>
            <Descriptions.Item label="创建时间">{formatDateTime(data?.created_at ?? null)}</Descriptions.Item>
            <Descriptions.Item label="更新时间">{formatDateTime(data?.updated_at ?? null)}</Descriptions.Item>
          </Descriptions>

          <Form form={profileForm} layout="vertical" className="profile-page__form">
            <Form.Item
              name="display_name"
              label="显示名称"
              rules={[
                { required: true, message: '请输入显示名称' },
                { whitespace: true, message: '显示名称不能为空' },
              ]}
            >
              <Input placeholder="请输入显示名称" maxLength={100} />
            </Form.Item>
            <Button type="primary" onClick={() => void submitProfile()} loading={updateProfileMutation.isPending}>
              保存资料
            </Button>
          </Form>
        </Card>

        <Card bordered={false} className="operation-card">
          <div className="profile-page__section-title">
            <strong>安全设置</strong>
            <span>修改登录密码后，后续登录请使用新密码。</span>
          </div>

          <Alert
            type="info"
            showIcon
            icon={<LockOutlined />}
            message="建议定期修改密码，并避免与其他系统复用同一组口令。"
            className="profile-page__alert"
          />

          <Form form={passwordForm} layout="vertical" className="profile-page__form">
            <Form.Item
              name="current_password"
              label="当前密码"
              rules={[{ required: true, message: '请输入当前密码' }]}
            >
              <Input.Password placeholder="请输入当前密码" />
            </Form.Item>
            <Form.Item
              name="new_password"
              label="新密码"
              rules={[
                { required: true, message: '请输入新密码' },
                { min: 6, message: '新密码至少 6 位' },
              ]}
            >
              <Input.Password placeholder="请输入新密码" />
            </Form.Item>
            <Form.Item
              name="confirm_password"
              label="确认新密码"
              dependencies={['new_password']}
              rules={[
                { required: true, message: '请再次输入新密码' },
                ({ getFieldValue }) => ({
                  validator(_, value) {
                    if (!value || getFieldValue('new_password') === value) {
                      return Promise.resolve()
                    }
                    return Promise.reject(new Error('两次输入的新密码不一致'))
                  },
                }),
              ]}
            >
              <Input.Password placeholder="请再次输入新密码" />
            </Form.Item>
            <Button type="primary" onClick={() => void submitPassword()} loading={changePasswordMutation.isPending}>
              修改密码
            </Button>
          </Form>
        </Card>

        <Card bordered={false} loading={isLoading} className="operation-card profile-page__activity-card">
          <div className="profile-page__section-title">
            <strong>最近活动</strong>
            <span>展示与你当前账号名或显示名称匹配的最近后台操作记录。</span>
          </div>

          {data?.recent_activities?.length ? (
            <List
              itemLayout="vertical"
              dataSource={data.recent_activities}
              renderItem={(item) => (
                <List.Item className="profile-page__activity-item">
                  <div className="profile-page__activity-meta">
                    <Space wrap>
                      <Tag color="purple">{item.module_name || item.action_name || '系统操作'}</Tag>
                      <span>{formatDateTime(item.created_at)}</span>
                      <span>{item.ip_address || 'IP 未记录'}</span>
                    </Space>
                  </div>
                  <strong>{item.action_name || '操作记录'}</strong>
                  <p>{item.content || '暂无详细内容'}</p>
                </List.Item>
              )}
            />
          ) : (
            <Empty description="暂时还没有与你账号匹配的操作记录" />
          )}
        </Card>
      </div>
    </div>
  )
}

export default ProfilePage
