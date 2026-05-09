import { useCallback, useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Badge,
  Button,
  Card,
  Checkbox,
  DatePicker,
  Drawer,
  Form,
  Input,
  message,
  Modal,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  ApiOutlined,
  AudioOutlined,
  CloudDownloadOutlined,
  LinkOutlined,
  DisconnectOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  SoundOutlined,
  ThunderboltOutlined,
  MobileOutlined,
} from '@ant-design/icons'
import dayjs from 'dayjs'
import { HTTPError } from 'ky'

import * as adminApi from '@/api/admin'
import * as dingtalkApi from '@/api/dingtalk'
import { getApiErrorMessage } from '@/api/errors'
import { useHospitalScopeFilter } from '@/hooks/use-hospital-scope-filter'
import { formatRecordingDisplayName } from '@/utils/recording-display'
import { formatBeijingTime } from '@/utils/time'

const { Title, Text } = Typography
const DEFAULT_DINGTALK_TEAM_CODE = '123fcc84-8a8e-4f7c-8452-ce7f00f3137b'

// ── merged device row (device info + status) ────────────

type MergedDevice = dingtalkApi.DviDevice & {
  online?: boolean
  batteryLevel?: number
}

type BindingState = 'none' | 'remote_only' | 'system_only' | 'both'

function readNestedPrimitive(value: unknown): string | number | undefined {
  if (typeof value === 'string' || typeof value === 'number') {
    return value
  }
  if (typeof value === 'object' && value !== null && 'value' in value) {
    const nested = value.value
    if (typeof nested === 'string' || typeof nested === 'number') {
      return nested
    }
  }
  return undefined
}

function readNestedNumber(value: unknown): number | undefined {
  const primitive = readNestedPrimitive(value)
  if (typeof primitive === 'number') return primitive
  if (typeof primitive === 'string' && primitive.trim()) {
    const parsed = Number(primitive)
    return Number.isFinite(parsed) ? parsed : undefined
  }
  return undefined
}

function readNestedString(value: unknown): string | undefined {
  const primitive = readNestedPrimitive(value)
  return typeof primitive === 'string' ? primitive : undefined
}

function readNestedTimestamp(value: unknown): number | undefined {
  if (typeof value !== 'object' || value === null || !('timestamp' in value)) {
    return undefined
  }
  const timestamp = value.timestamp
  return typeof timestamp === 'number' ? timestamp : undefined
}

function getBindingState(device: MergedDevice): BindingState {
  const remoteUserId = typeof device.userId === 'string' ? device.userId.trim() : ''
  const remoteReady = remoteUserId || device.remoteProvider === 'iot'
  const systemBinding = device.systemBinding ?? null
  if (!remoteReady && !systemBinding) return 'none'
  if (remoteReady && systemBinding) return 'both'
  if (remoteReady && !systemBinding) return 'remote_only'
  return 'system_only'
}

function getSystemAccountStatusColor(device: MergedDevice): string | undefined {
  const systemBinding = device.systemBinding
  if (!systemBinding?.accountOpened) return undefined
  return systemBinding.accountIsActive === false ? 'default' : 'success'
}

function getSystemAccountStatusLabel(device: MergedDevice): string {
  const systemBinding = device.systemBinding
  if (!systemBinding) return '未绑定系统人员'
  if (!systemBinding.accountOpened) return '未开通账号'
  return systemBinding.accountIsActive === false ? '账号已停用' : '账号正常'
}

function compactIdentifier(value: string | undefined, head = 6, tail = 4): string {
  const normalized = typeof value === 'string' ? value.trim() : ''
  if (!normalized) return '-'
  if (normalized.length <= head + tail + 3) return normalized
  return `${normalized.slice(0, head)}...${normalized.slice(-tail)}`
}

function StatCard({
  label,
  value,
  meta,
  tone = 'default',
}: {
  label: string
  value: number
  meta: string
  tone?: 'default' | 'success' | 'brand'
}) {
  return (
    <div className={`badge-device-page__metric badge-device-page__metric--${tone}`}>
      <span className="badge-device-page__metric-label">{label}</span>
      <strong className="badge-device-page__metric-value">{value}</strong>
      <span className="badge-device-page__metric-meta">{meta}</span>
    </div>
  )
}

type DeviceBindingFormValues = {
  staffId: string
  effectiveStart?: dayjs.Dayjs
  effectiveEnd?: dayjs.Dayjs
}

type DeviceBindingOverlapDetail = {
  code?: string
  message?: string
  conflicts?: Array<{
    staffName?: string | null
    effectiveStart?: string | null
    effectiveEnd?: string | null
  }>
}

function formatBindingRangeLabel(start?: string | null, end?: string | null) {
  const startLabel = start ? formatBeijingTime(start, 'YYYY-MM-DD HH:mm') : '最早录音'
  const endLabel = end ? formatBeijingTime(end, 'YYYY-MM-DD HH:mm') : '之后所有录音'
  return `${startLabel} - ${endLabel}`
}

async function readDeviceBindingOverlapDetail(error: unknown): Promise<DeviceBindingOverlapDetail | null> {
  if (!(error instanceof HTTPError) || error.response.status !== 409) {
    return null
  }
  try {
    const payload = await error.response.clone().json() as { detail?: DeviceBindingOverlapDetail }
    if (payload?.detail?.code === 'device_binding_overlap') {
      return payload.detail
    }
  } catch {
    return null
  }
  return null
}

// ── device list + status ────────────────────────────────

function useDevicesWithStatus(hospitalCode?: string, enabled = true) {
  const devicesQuery = useQuery({
    queryKey: ['dingtalk', 'devices', hospitalCode || 'all'],
    queryFn: () => dingtalkApi.listDevices({ hospitalCode, syncStatus: true }),
    enabled,
    staleTime: 0,
    refetchOnMount: 'always',
    refetchOnWindowFocus: true,
  })

  const merged: MergedDevice[] = (devicesQuery.data?.result ?? []).map((dev) => {
    const statusVal = readNestedString(dev.status)
    const batteryVal = readNestedNumber(dev.battery)
    const recordingStartTime = readNestedNumber(dev.recordingStartTime)
    const statusStr = typeof statusVal === 'string' ? statusVal : undefined
    const rawOnline = typeof dev.online === 'boolean' ? dev.online : undefined
    return {
      ...dev,
      statusValue: statusStr,
      online: statusStr ? statusStr !== 'offline' : rawOnline,
      batteryLevel: batteryVal,
      recording: typeof recordingStartTime === 'number',
      recordingStartTime,
      batteryTimestamp: readNestedTimestamp(dev.battery),
      statusTimestamp: readNestedTimestamp(dev.status),
      firmwareVersion: readNestedString(dev.firmware),
    }
  })

  return {
    devices: merged,
    isLoading: devicesQuery.isLoading,
    isError: devicesQuery.isError,
    isRefetching: devicesQuery.isRefetching,
    refetch: () => devicesQuery.refetch(),
  }
}

// ── bind / unbind modal ─────────────────────────────────

function BindModal({
  device,
  open,
  onClose,
  staffBindings,
  isStaffBindingsLoading,
}: {
  device: MergedDevice | null
  open: boolean
  onClose: () => void
  staffBindings: adminApi.StaffBadgeBindingCandidate[]
  isStaffBindingsLoading: boolean
}) {
  const [form] = Form.useForm()
  const queryClient = useQueryClient()
  const selectableStaff = staffBindings.filter((item) => item.is_active)
  const staffOptions = selectableStaff.map((item) => {
    const parts = [item.name]
    if (item.external_account) parts.push(`员工编号 ${item.external_account}`)
    if (item.position_name) parts.push(item.position_name)
    if (item.hospital_short_name) parts.push(item.hospital_short_name)
    return {
      value: item.id,
      label: parts.join(' / '),
    }
  })

  const bindMutation = useMutation({
    mutationFn: (vals: DeviceBindingFormValues & { overrideOverlap?: boolean }) =>
      dingtalkApi.bindSystemDevice(
        device!.sn,
        vals.staffId,
        device?.name,
        vals.effectiveStart?.toISOString() ?? null,
        vals.effectiveEnd?.toISOString() ?? null,
        vals.overrideOverlap ?? false,
      ),
    onSuccess: () => {
      message.success('系统人员绑定成功')
      queryClient.invalidateQueries({ queryKey: ['dingtalk', 'devices'] })
      queryClient.invalidateQueries({ queryKey: ['staff'] })
      onClose()
    },
  })

  useEffect(() => {
    if (open && device) {
      form.setFieldsValue({
        staffId: device.systemBinding?.staffId,
        effectiveStart: undefined,
        effectiveEnd: undefined,
      })
    }
  }, [open, device, form])

  async function submitBinding(values: DeviceBindingFormValues, overrideOverlap = false) {
    try {
      await bindMutation.mutateAsync({
        ...values,
        overrideOverlap,
      })
    } catch (error) {
      const overlapDetail = await readDeviceBindingOverlapDetail(error)
      if (overlapDetail) {
        Modal.confirm({
          title: '绑定时间段与已有绑定重叠',
          okText: '确认覆盖',
          cancelText: '取消',
          content: (
            <Space direction="vertical" size={8}>
              <div>新的绑定会覆盖重叠时间段，非重叠时间仍保留原归属。</div>
              {(overlapDetail.conflicts ?? []).slice(0, 3).map((item, index) => (
                <div key={`${item.staffName ?? 'staff'}-${index}`}>
                  {item.staffName || '已绑定员工'}：{formatBindingRangeLabel(item.effectiveStart, item.effectiveEnd)}
                </div>
              ))}
            </Space>
          ),
          onOk: () => submitBinding(values, true),
        })
        return
      }
      message.error(await getApiErrorMessage(error, '系统人员绑定失败'))
    }
  }

  return (
    <Modal
      title={`绑定设备 ${device?.sn ?? ''}`}
      open={open}
      onCancel={onClose}
      onOk={() => form.submit()}
      confirmLoading={bindMutation.isPending}
    >
      <Form form={form} layout="vertical" onFinish={(values) => void submitBinding(values)}>
        <Form.Item
          name="staffId"
          label="选择已登记员工"
          extra="这里只维护系统内正式绑定关系，不依赖钉钉 UserId 映射。"
          rules={[{ required: true, message: '请选择系统员工' }]}
        >
            <Select
              allowClear
              showSearch
              loading={isStaffBindingsLoading}
              placeholder="按姓名、员工编号或机构搜索"
              options={staffOptions}
              optionFilterProp="label"
            />
        </Form.Item>
        <Form.Item
          name="effectiveStart"
          label="绑定开始时间"
          extra="留空表示从最早录音开始生效。"
        >
          <DatePicker allowClear showTime style={{ width: '100%' }} format="YYYY-MM-DD HH:mm:ss" />
        </Form.Item>
        <Form.Item
          name="effectiveEnd"
          label="绑定终止时间"
          extra="留空表示从开始时间以后所有录音都归属该员工。"
          dependencies={['effectiveStart']}
          rules={[
            ({ getFieldValue }) => ({
              validator(_, value?: dayjs.Dayjs) {
                const start = getFieldValue('effectiveStart') as dayjs.Dayjs | undefined
                if (!value || !start || value.isAfter(start)) {
                  return Promise.resolve()
                }
                return Promise.reject(new Error('绑定终止时间必须晚于开始时间'))
              },
            }),
          ]}
        >
          <DatePicker allowClear showTime style={{ width: '100%' }} format="YYYY-MM-DD HH:mm:ss" />
        </Form.Item>
      </Form>
    </Modal>
  )
}

// ── audio drawer ────────────────────────────────────────

function AudioDrawer({
  device,
  open,
  onClose,
}: {
  device: MergedDevice | null
  open: boolean
  onClose: () => void
}) {
  const audioQuery = useQuery({
    queryKey: ['dingtalk', 'audio', device?.sn],
    queryFn: async () => {
      const data = await dingtalkApi.listAudioFiles({ sn: device!.sn })
      // Sort newest first
      const sorted = [...(data.result ?? [])].sort(
        (a, b) => (b.createTime ?? 0) - (a.createTime ?? 0),
      )
      return { ...data, result: sorted }
    },
    enabled: open && !!device?.sn,
    staleTime: 0,
  })

  const audioItems = audioQuery.data?.result ?? []

  const archiveMutation = useMutation({
    mutationFn: (audio: dingtalkApi.DviAudioFile) =>
      dingtalkApi.archiveAudioFile({
        sn: device!.sn,
        fileId: audio.fileId,
        fileName: audio.fileName,
        duration: typeof audio.duration === 'number' ? audio.duration : undefined,
        fileSize: typeof audio.fileSize === 'number' ? audio.fileSize : undefined,
        createTime: typeof audio.createTime === 'number' ? audio.createTime : undefined,
        downloadUrl:
          typeof audio.downloadUrl === 'string'
            ? audio.downloadUrl
            : typeof audio.fileUrl === 'string'
              ? audio.fileUrl
              : undefined,
        source:
          typeof audio.remoteProvider === 'string'
            ? audio.remoteProvider
            : typeof audio.source === 'string'
              ? audio.source
              : undefined,
      }),
    onSuccess: (data) => {
      if (data.status === 'skipped') {
        message.info(data.savedPath ? `音频已归档：${data.savedPath}` : '音频已归档')
        return
      }
      message.success(data.savedPath ? `归档完成：${data.savedPath}` : '归档完成')
    },
    onError: async (err) => message.error(await getApiErrorMessage(err, '归档音频失败')),
  })

  const archiveDeviceMutation = useMutation({
    mutationFn: () =>
      dingtalkApi.archiveAudioFiles({
        snList: device?.sn ? [device.sn] : [],
      }),
    onSuccess: (data) => {
      message.success(
        `设备归档完成：新增 ${data.downloaded} 条，已存在 ${data.skipped} 条，失败 ${data.failed} 条`,
      )
    },
    onError: async (err) => message.error(await getApiErrorMessage(err, '批量归档设备音频失败')),
  })

  const audioCols: ColumnsType<dingtalkApi.DviAudioFile> = [
    {
      title: '录音文件名',
      dataIndex: 'fileName',
      key: 'fileName',
      width: 240,
      ellipsis: true,
      render: (value: string | null | undefined, row) =>
        value
          ? formatRecordingDisplayName(
              value,
              typeof row.createTime === 'number' ? row.createTime : undefined,
            )
          : '-',
    },
    {
      title: '时长',
      dataIndex: 'duration',
      key: 'duration',
      width: 72,
      render: (v: number) => (v ? `${Math.round(v / 1000)}s` : '-'),
    },
    {
      title: '大小',
      dataIndex: 'fileSize',
      key: 'fileSize',
      width: 76,
      render: (v: number) => (v ? `${(v / 1024).toFixed(0)} KB` : '-'),
    },
    {
      title: '创建时间',
      dataIndex: 'createTime',
      key: 'createTime',
      width: 136,
      render: (v: number) => formatBeijingTime(v, 'YYYY-MM-DD HH:mm'),
    },
    {
      title: '操作',
      key: 'actions',
      width: 64,
      render: (_, r) => (
        <Tooltip title="归档到系统">
          <Button
            type="link"
            size="small"
            icon={<CloudDownloadOutlined />}
            loading={archiveMutation.isPending}
            onClick={() => archiveMutation.mutate(r)}
          />
        </Tooltip>
      ),
    },
  ]

  return (
    <Drawer
      title={<><SoundOutlined /> 音频文件 – {device?.sn}</>}
      open={open}
      onClose={onClose}
      width={700}
      extra={
        <Button
          type="primary"
          icon={<CloudDownloadOutlined />}
          loading={archiveDeviceMutation.isPending}
          disabled={!device?.sn || audioItems.length === 0}
          onClick={() => archiveDeviceMutation.mutate()}
        >
          归档本设备全部音频
        </Button>
      }
    >
      {audioQuery.isLoading ? (
        <Spin />
      ) : (
        <Table
          dataSource={audioItems}
          columns={audioCols}
          rowKey="fileId"
          size="small"
          pagination={{ pageSize: 10 }}
        />
      )}
    </Drawer>
  )
}


// ── dingtalk bind modal ─────────────────────────────────

function DingtalkBindModal({
  device,
  open,
  onClose,
}: {
  device: MergedDevice | null
  open: boolean
  onClose: () => void
}) {
  const [form] = Form.useForm()
  const queryClient = useQueryClient()

  const bindMutation = useMutation({
    mutationFn: (vals: { teamCode: string; userId: string }) =>
      dingtalkApi.bindDevice(device!.sn, vals.teamCode || DEFAULT_DINGTALK_TEAM_CODE, vals.userId),
    onSuccess: () => {
      message.success('钉钉绑定成功')
      queryClient.invalidateQueries({ queryKey: ['dingtalk', 'devices'] })
      onClose()
    },
    onError: async (err) => message.error(await getApiErrorMessage(err, '钉钉绑定失败')),
  })

  const unbindMutation = useMutation({
    mutationFn: () =>
      dingtalkApi.unbindDevice(device!.sn, device!.teamCode!, device!.userId as string),
    onSuccess: () => {
      message.success('钉钉解绑成功')
      queryClient.invalidateQueries({ queryKey: ['dingtalk', 'devices'] })
      onClose()
    },
    onError: async (err) => message.error(await getApiErrorMessage(err, '钉钉解绑失败')),
  })

  const isBound = !!(device?.userId && device?.teamCode)
  const remoteUserId = typeof device?.userId === 'string' ? device.userId.trim() : ''
  const staffCode = device?.systemBinding?.externalAccount?.trim() ?? ''
  const lookupHospitalCode = device?.systemBinding?.hospitalCode || device?.hospitalCode || null
  const shouldLookupUserId = open && !!device && !remoteUserId && !!staffCode
  const userIdLookupQuery = useQuery({
    queryKey: ['staff', 'dingtalk-user-id', staffCode, lookupHospitalCode],
    queryFn: () =>
      adminApi.lookupStaffIdentity({
        external_account: staffCode,
        hospital_code: lookupHospitalCode,
      }),
    enabled: shouldLookupUserId,
    retry: false,
  })
  const lookedUpUserId = userIdLookupQuery.data?.dingtalk_user_id?.trim() ?? ''
  const isUserIdLookupLoading = shouldLookupUserId && userIdLookupQuery.isFetching
  const userIdLookupHint = !shouldLookupUserId
    ? null
    : userIdLookupQuery.isError
      ? '自动查询钉钉 UserID 未成功，可手动填写。'
      : userIdLookupQuery.data && !lookedUpUserId
        ? '未从现有钉钉通讯录数据匹配到 UserID，可手动填写。'
        : lookedUpUserId
          ? `已按员工编号 ${staffCode} 自动带出，可手动修改。`
          : null

  useEffect(() => {
    if (!open || !device) {
      return
    }

    form.setFieldsValue({
      teamCode: device.teamCode || DEFAULT_DINGTALK_TEAM_CODE,
      userId: remoteUserId,
    })
  }, [open, device, remoteUserId, form])

  useEffect(() => {
    if (!shouldLookupUserId || !lookedUpUserId) {
      return
    }
    const currentUserId = String(form.getFieldValue('userId') ?? '').trim()
    if (!currentUserId) {
      form.setFieldsValue({ userId: lookedUpUserId })
    }
  }, [shouldLookupUserId, lookedUpUserId, form])

  return (
    <Modal
      title={`钉钉绑定 – ${device?.sn ?? ''}`}
      open={open}
      onCancel={onClose}
      footer={
        <Space>
          <Button onClick={onClose}>取消</Button>
          {isBound ? (
            <Button
              danger
              loading={unbindMutation.isPending}
              onClick={() =>
                Modal.confirm({
                  title: '确认解绑钉钉',
                  content: `将设备 ${device?.sn} 从钉钉用户 ${device?.userId} 解绑？`,
                  onOk: () => unbindMutation.mutateAsync(),
                })
              }
            >
              解绑钉钉
            </Button>
          ) : null}
          <Button type="primary" loading={bindMutation.isPending} onClick={() => form.submit()}>
            绑定钉钉
          </Button>
        </Space>
      }
    >
      <Form form={form} layout="vertical" onFinish={(v) => bindMutation.mutate(v)}>
        <Form.Item
          name="teamCode"
          label="团队编码 (teamCode)"
          rules={[{ required: true, message: '请输入 teamCode' }]}
          extra="系统已统一使用当前默认团队编码，无需手动填写。"
        >
          <Input disabled />
        </Form.Item>
        <Form.Item
          name="userId"
          label="钉钉用户 ID (userId)"
          extra={
            <Space direction="vertical" size={2}>
              <span>优先按系统绑定员工编号自动带出钉钉 UserID，可手动修改。</span>
              {isUserIdLookupLoading ? <Text type="secondary">正在查询现有钉钉通讯录数据...</Text> : null}
              {!isUserIdLookupLoading && userIdLookupHint ? <Text type="secondary">{userIdLookupHint}</Text> : null}
            </Space>
          }
          rules={[{ required: true, message: '请输入钉钉 userId' }]}
        >
          <Input placeholder="例如 03431037061720755381" />
        </Form.Item>
      </Form>
    </Modal>
  )
}

// ── main page ───────────────────────────────────────────

export default function DingtalkBadgePage() {
  const hospitalScope = useHospitalScopeFilter()
  const activeHospitalCode = hospitalScope.hospitalCode
  const devicesEnabled = hospitalScope.isReady && Boolean(activeHospitalCode)
  const { devices: rawDevices, isLoading, isError, isRefetching, refetch } = useDevicesWithStatus(activeHospitalCode, devicesEnabled)
  const queryClient = useQueryClient()

  const [bindTarget, setBindTarget] = useState<MergedDevice | null>(null)
  const [audioTarget, setAudioTarget] = useState<MergedDevice | null>(null)
  const [dingtalkBindTarget, setDingtalkBindTarget] = useState<MergedDevice | null>(null)
  const staffBindingsQuery = useQuery({
    queryKey: ['staff', 'badge-binding-candidates', activeHospitalCode ?? ''],
    queryFn: () => adminApi.fetchStaffBadgeBindingCandidates({ hospital_code: activeHospitalCode, include_inactive: true }),
    enabled: devicesEnabled,
  })
  const staffBindings = staffBindingsQuery.data ?? []
  const hospitalOptions = hospitalScope.selectOptions
  const devices = rawDevices

  const unbindSystemMutation = useMutation({
    mutationFn: ({ sn, clearRecordingOwners }: { sn: string; clearRecordingOwners: boolean }) =>
      dingtalkApi.unbindSystemDevice(sn, true, clearRecordingOwners),
    onSuccess: () => {
      message.success('系统人员已解绑')
      queryClient.invalidateQueries({ queryKey: ['dingtalk', 'devices'] })
      queryClient.invalidateQueries({ queryKey: ['staff'] })
    },
    onError: async (err) => message.error(await getApiErrorMessage(err, '系统人员解绑失败')),
  })

  // recording
  const startRecMutation = useMutation({
    mutationFn: ({ teamCode, userId }: { teamCode: string; userId: string }) =>
      dingtalkApi.startRecording(teamCode, userId),
    onSuccess: () => message.success('录音已启动'),
    onError: async (err) => message.error(await getApiErrorMessage(err, '启动录音失败')),
  })

  const stopRecMutation = useMutation({
    mutationFn: ({ teamCode, userId }: { teamCode: string; userId: string }) =>
      dingtalkApi.stopRecording(teamCode, userId),
    onSuccess: () => message.success('录音已停止'),
    onError: async (err) => message.error(await getApiErrorMessage(err, '停止录音失败')),
  })

  const handleUnbindSystem = useCallback(
    (dev: MergedDevice) => {
      if (!dev.systemBinding) {
        message.warning('该设备未绑定系统人员')
        return
      }
      let clearRecordingOwners = false
      Modal.confirm({
        title: '确认解绑系统人员',
        okText: '确认解绑',
        cancelText: '取消',
        content: (
          <Space direction="vertical" size={8}>
            <div>确定将设备 {dev.sn} 与系统人员 {dev.systemBinding.staffName} 解绑？</div>
            <Text type="secondary">
              默认仅解绑工牌与系统人员，不修改已归档录音的上传者归属。
            </Text>
            <Checkbox onChange={(event) => { clearRecordingOwners = event.target.checked }}>
              同时清空这块工牌已归档录音的归属者
            </Checkbox>
          </Space>
        ),
        onOk: () => unbindSystemMutation.mutateAsync({ sn: dev.sn, clearRecordingOwners }),
      })
    },
    [unbindSystemMutation],
  )

  const columns: ColumnsType<MergedDevice> = [
    {
      title: '设备',
      key: 'device',
      width: 172,
      render: (_, row) => (
        <div className="badge-device-page__stack">
          <Text copyable={{ text: row.sn }} className="badge-device-page__mono">
            {row.sn}
          </Text>
          <Text type="secondary" className="badge-device-page__subline">
            {typeof row.name === 'string' && row.name.trim() ? row.name.trim() : '钉钉工牌设备'}
          </Text>
          <Text type="secondary" className="badge-device-page__subline">
            归属 {row.hospitalShortName || row.hospitalCode || '未分配机构'}
          </Text>
        </div>
      ),
    },
    {
      title: '设备状态',
      key: 'hardware',
      width: 118,
      render: (_, row) => {
        const battery = row.batteryLevel
        const batteryColor = battery == null ? 'default' : battery > 50 ? 'success' : battery > 20 ? 'warning' : 'error'
        const statusBadge = (() => {
          if (row.statusValue === undefined) return <Tag>状态未知</Tag>
          if (row.statusValue === 'online' || row.statusValue === 'recording') return <Badge status="success" text="在线" />
          if (row.statusValue === 'idle') return <Badge status="processing" text="待机" />
          return <Badge status="default" text="离线" />
        })()
        const batUpdated =
          typeof row.batteryTimestamp === 'number' && row.batteryTimestamp > 0
            ? formatBeijingTime(row.batteryTimestamp, 'MM-DD HH:mm')
            : ''
        return (
          <div className="badge-device-page__stack">
            <div>
              {statusBadge}
              {row.recording ? (
                <Tag color="red" icon={<AudioOutlined />} style={{ marginLeft: 4 }}>录音中</Tag>
              ) : null}
            </div>
            <div>
              {battery == null ? (
                <Text type="secondary" className="badge-device-page__subline">
                  电量未知
                </Text>
              ) : (
                <Tooltip title={batUpdated ? `上报于 ${batUpdated}` : undefined}>
                  <Tag color={batteryColor} icon={<ThunderboltOutlined />}>
                    {battery}%
                  </Tag>
                </Tooltip>
              )}
            </div>
          </div>
        )
      },
    },
    {
      title: '设备平台',
      key: 'remoteBinding',
      width: 188,
      render: (_, row) =>
        row.remoteProvider === 'iot' ? (
          <div className="badge-device-page__stack">
            <Space size={[6, 4]} wrap>
              <Tag color="geekblue">IOT接口</Tag>
              <Tag color="blue">可录音</Tag>
            </Space>
            <Text type="secondary" className="badge-device-page__subline">
              状态、电量与录音控制走设备管理平台
            </Text>
          </div>
        ) : row.userId || row.teamCode ? (
          <div className="badge-device-page__stack">
            <Space size={[6, 4]} wrap>
              <Tag color={row.userId ? 'blue' : 'default'}>{row.userId ? '可录音' : '待绑钉钉'}</Tag>
              {row.teamCode ? (
                <Text copyable={{ text: row.teamCode }} className="badge-device-page__mono badge-device-page__subline">
                  团队 {compactIdentifier(row.teamCode, 4, 4)}
                </Text>
              ) : (
                <Text type="secondary" className="badge-device-page__subline">
                  未分配团队
                </Text>
              )}
            </Space>
            {row.userId ? (
              <Text copyable={{ text: row.userId }} className="badge-device-page__mono badge-device-page__subline">
                用户 {compactIdentifier(row.userId, 6, 4)}
              </Text>
            ) : (
              <Text type="secondary" className="badge-device-page__subline">
                未绑定远端用户，暂不可录音
              </Text>
            )}
          </div>
        ) : (
          <Text type="secondary">未配置钉钉信息</Text>
        ),
    },
    {
      title: '系统人员 / 账号',
      key: 'systemBinding',
      width: 244,
      render: (_, row) =>
        row.systemBinding ? (
          <div className="badge-device-page__stack">
            <Space size={[6, 4]} wrap>
              <Text strong>{row.systemBinding.staffName}</Text>
              {!row.systemBinding.isActive ? <Tag>人员已停用</Tag> : null}
              <Tag color={getSystemAccountStatusColor(row)}>{getSystemAccountStatusLabel(row)}</Tag>
            </Space>
            <Text type="secondary" className="badge-device-page__subline">
              {row.systemBinding.externalAccount ? `员工编号 ${row.systemBinding.externalAccount}` : '未维护员工编号'}
            </Text>
            <Space size={[6, 4]} wrap>
              {row.systemBinding.accountOpened ? (
                <Text
                  copyable={{ text: row.systemBinding.accountUsername ?? '' }}
                  className="badge-device-page__mono badge-device-page__subline"
                >
                  账号 {row.systemBinding.accountUsername ?? '-'}
                </Text>
              ) : (
                <Text type="secondary" className="badge-device-page__subline">
                  未开通登录账号
                </Text>
              )}
              {row.systemBinding.positionName ? <Tag>{row.systemBinding.positionName}</Tag> : null}
              {row.systemBinding.hospitalShortName ? <Tag>{row.systemBinding.hospitalShortName}</Tag> : null}
            </Space>
          </div>
        ) : (
          <Text type="secondary">未绑定系统人员</Text>
        ),
    },
    {
      title: '进度',
      key: 'bindingState',
      width: 84,
      align: 'center',
      render: (_, row) => {
        switch (getBindingState(row)) {
          case 'both':
            return <Tag color="success">完整绑定</Tag>
          case 'remote_only':
            return <Tag color="warning">待绑人员</Tag>
          case 'system_only':
            return <Tag color="orange">待绑钉钉</Tag>
          default:
            return <Tag>未配置</Tag>
        }
      },
    },
    {
      title: '操作',
      key: 'actions',
      width: 148,
      render: (_, dev) => (
        <Space size={6} wrap className="badge-device-page__action-group">
          <Tooltip title={dev.systemBinding ? '更换系统人员' : '绑定系统人员'}>
            <Button
              size="small"
              type="primary"
              className="badge-device-page__action-btn"
              icon={<LinkOutlined />}
              onClick={() => setBindTarget(dev)}
            />
          </Tooltip>
          {dev.systemBinding ? (
            <Tooltip title="解绑系统人员">
              <Button
                size="small"
                danger
                className="badge-device-page__action-btn"
                icon={<DisconnectOutlined />}
                onClick={() => handleUnbindSystem(dev)}
                loading={unbindSystemMutation.isPending}
              />
            </Tooltip>
          ) : null}
          <Tooltip title={dev.userId && dev.teamCode ? '钉钉绑定(已绑)' : '绑定钉钉'}>
            <Button
              size="small"
              className="badge-device-page__action-btn"
              icon={<ApiOutlined />}
              type={dev.userId && dev.teamCode ? 'default' : 'dashed'}
              onClick={() => setDingtalkBindTarget(dev)}
            />
          </Tooltip>
          <Tooltip title="开始录音">
            <Button
              size="small"
              className="badge-device-page__action-btn"
              icon={<PlayCircleOutlined />}
              disabled={!dev.teamCode || !dev.userId}
              onClick={() =>
                startRecMutation.mutate({
                  teamCode: dev.teamCode!,
                  userId: dev.userId!,
                })
              }
              loading={startRecMutation.isPending}
            />
          </Tooltip>
          <Tooltip title="停止录音">
            <Button
              size="small"
              className="badge-device-page__action-btn"
              icon={<PauseCircleOutlined />}
              disabled={!dev.teamCode || !dev.userId}
              onClick={() =>
                stopRecMutation.mutate({
                  teamCode: dev.teamCode!,
                  userId: dev.userId!,
                })
              }
              loading={stopRecMutation.isPending}
            />
          </Tooltip>
          <Tooltip title="查看音频">
            <Button
              size="small"
              className="badge-device-page__action-btn"
              icon={<AudioOutlined />}
              onClick={() => setAudioTarget(dev)}
            />
          </Tooltip>
        </Space>
      ),
    },
  ]

  // stats
  const totalDevices = devices.length
  const onlineCount = devices.filter((d) => d.online).length
  const boundCount = devices.filter((d) => d.userId || d.remoteProvider === 'iot').length
  const systemBoundCount = devices.filter((d) => d.systemBinding).length

  return (
    <div className="module-page badge-device-page">
      <section className="module-page__header badge-device-page__hero">
        <div className="badge-device-page__hero-copy">
          <p className="eyebrow">朗姿智能工牌</p>
          <Title level={3} className="badge-device-page__hero-title">
            <MobileOutlined /> 朗姿工牌管理
          </Title>
          <p className="module-page__subtitle">
            这里直接维护工牌和系统人员的正式绑定关系；表格已改为紧凑布局，优先保证常用信息能在页面内完整看到。
          </p>
        </div>
        <div className="module-page__actions badge-device-page__hero-actions">
          <div className="badge-device-page__legend">
            <Tag color="success">完整绑定</Tag>
            <Tag color="warning">待绑人员</Tag>
            <Tag color="orange">待绑钉钉</Tag>
          </div>
          <Select
            showSearch
            className="badge-device-page__hospital-filter"
            placeholder="机构范围"
            value={activeHospitalCode}
            loading={hospitalScope.isLoading}
            options={hospitalOptions}
            optionFilterProp="label"
            onChange={(value) => hospitalScope.setHospitalCode(value)}
          />
          <Button icon={<ReloadOutlined />} loading={isRefetching} onClick={refetch}>
            刷新列表
          </Button>
        </div>
      </section>

      <section className="badge-device-page__metrics">
        <StatCard label="设备总数" value={totalDevices} meta="已接入的工牌设备" />
        <StatCard label="在线设备" value={onlineCount} meta={`${onlineCount} / ${totalDevices}`} tone="success" />
        <StatCard label="钉钉已绑" value={boundCount} meta={`${boundCount} 台可直接录音`} tone="brand" />
        <StatCard label="系统已绑" value={systemBoundCount} meta={`${systemBoundCount} 台已关联人员`} tone="brand" />
      </section>

      {isError ? (
        <Alert
          showIcon
          type="error"
          message="设备列表加载失败"
          description="当前无法读取工牌列表，请稍后重试；如果一直失败，请联系系统管理员查看后端日志。"
          style={{ marginBottom: 12 }}
        />
      ) : null}

      <Card size="small" className="badge-device-page__table-card">
        <Table
          className="badge-device-page__table"
          dataSource={devices}
          columns={columns}
          rowKey="sn"
          size="small"
          tableLayout="fixed"
          loading={isLoading}
          pagination={false}
          scroll={{ x: 936 }}
        />
      </Card>

      <BindModal
        device={bindTarget}
        open={!!bindTarget}
        onClose={() => setBindTarget(null)}
        staffBindings={staffBindings}
        isStaffBindingsLoading={staffBindingsQuery.isLoading}
      />
      <AudioDrawer device={audioTarget} open={!!audioTarget} onClose={() => setAudioTarget(null)} />
      <DingtalkBindModal device={dingtalkBindTarget} open={!!dingtalkBindTarget} onClose={() => setDingtalkBindTarget(null)} />
    </div>
  )
}
