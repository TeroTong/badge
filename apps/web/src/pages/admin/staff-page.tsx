import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Dropdown,
  Form,
  Input,
  message,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  type MenuProps,
  type TableProps,
} from 'antd'
import {
  CheckCircleOutlined,
  EditOutlined,
  MoreOutlined,
  PlusOutlined,
  StopOutlined,
  UserAddOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'

import { isHospitalAdminOrAbove, isSystemAdminOrAbove, normalizeRole, roleLabel } from '@/app/roles'
import { useAuth } from '@/app/use-auth'
import type { PositionProfile, Staff, StaffAccountActionResult, StaffDirectorySyncStatus } from '@/api/admin'
import * as adminApi from '@/api/admin'
import { getApiErrorMessage } from '@/api/errors'
import { formatBeijingTime } from '@/utils/time'

const GENDER_OPTIONS = [
  { label: '女', value: 'female' },
  { label: '男', value: 'male' },
]

const ACCOUNT_STATUS_OPTIONS = [
  { label: '未开通', value: 'not_opened' },
  { label: '正常', value: 'active' },
  { label: '已停用', value: 'disabled' },
]

const STAFF_IDENTITY_SOURCE_LABELS: Record<string, string> = {
  dingtalk_api: '钉钉通讯录',
  staff_directory: '员工目录',
  visit_order: 'SAP到诊单',
  dingtalk_export: '钉钉通讯录缓存',
}

const ROLE_FLAG_MAP: { key: keyof Staff; label: string }[] = [
  { key: 'is_doctor', label: '医生' },
  { key: 'is_nurse', label: '护士' },
  { key: 'is_anesthetist', label: '麻醉师' },
  { key: 'is_cashier', label: '收银员' },
  { key: 'is_guide', label: '导医' },
  { key: 'is_pre_advisor', label: '院前顾问' },
  { key: 'is_onsite_advisor', label: '现场顾问' },
  { key: 'is_advisor_assistant', label: '顾问助理' },
  { key: 'is_doctor_assistant', label: '医助' },
  { key: 'is_vip_service', label: '客服' },
]

function asText(value: unknown): string | null {
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return null
}

function asDisplayText(value: unknown, fallback = '-'): string {
  return asText(value) ?? fallback
}

const GLOBAL_PERMISSION_ROLES = new Set(['super_admin', 'system_admin'])
const ALL_INSTITUTIONS_LABEL = '所有机构'

function isGlobalPermissionRole(role: unknown): boolean {
  return GLOBAL_PERMISSION_ROLES.has(normalizeRole(asText(role)))
}

function getAdvisorCode(staff: Pick<Staff, 'external_account' | 'badge_id'> | null | undefined): string | null {
  if (!staff) return null

  const externalAccount = asText(staff.external_account)
  if (externalAccount) return externalAccount

  const badgeId = asText(staff.badge_id)
  if (badgeId && /^\d{6,12}$/.test(badgeId)) return badgeId

  return null
}

function getDeviceBadgeId(staff: Pick<Staff, 'external_account' | 'badge_id'> | null | undefined): string | null {
  if (!staff) return null

  const externalAccount = asText(staff.external_account)
  const badgeId = asText(staff.badge_id)
  if (!badgeId) return null

  if (externalAccount) return badgeId === externalAccount ? null : badgeId
  if (/[A-Za-z]/.test(badgeId)) return badgeId

  return null
}

function getPreferredLoginAccount(staff: Pick<Staff, 'external_account' | 'phone'> | null | undefined) {
  const employeeCode = asText(staff?.external_account)
  if (employeeCode) {
    return {
      username: employeeCode,
      sourceField: 'external_account',
      sourceLabel: '员工编号',
    } as const
  }

  const phone = asText(staff?.phone)
  if (phone) {
    return {
      username: phone,
      sourceField: 'phone',
      sourceLabel: '手机号',
    } as const
  }

  return null
}

function buildDefaultPasswordPreview(username: string) {
  const suffix = username.length > 4 ? username.slice(-4) : username
  return `${suffix}@Abcd`
}

function formatSyncTimestamp(value: string | null | undefined): string {
  return value ? formatBeijingTime(value, 'YYYY-MM-DD HH:mm:ss') : '暂无记录'
}

function formatLastLogin(value: string | null | undefined): string {
  return value ? formatBeijingTime(value, 'YYYY-MM-DD HH:mm') : '暂无登录记录'
}

function shouldShowPermissionHint(positionLabel: string, permissionLabel: string): boolean {
  if (permissionLabel === '-' || permissionLabel === positionLabel) return false
  if (permissionLabel === '超级管理员') return false
  return true
}

function formatSyncInterval(seconds: number | undefined): string {
  if (!seconds || seconds <= 0) return '未配置'
  if (seconds % 86400 === 0) return `${seconds / 86400} 天`
  if (seconds % 3600 === 0) return `${seconds / 3600} 小时`
  if (seconds % 60 === 0) return `${seconds / 60} 分钟`
  return `${seconds} 秒`
}

function getSyncHeadline(syncStatus: StaffDirectorySyncStatus | undefined, isLoading: boolean): string {
  if (isLoading) return '员工状态定时同步状态加载中'
  if (!syncStatus) return '员工状态定时同步状态暂不可用'
  if (!syncStatus.scheduler_enabled) return '员工状态定时同步未启动'
  if (!syncStatus.scheduler_running) return '员工状态定时同步未在运行'
  return `员工状态定时同步运行中，执行间隔 ${formatSyncInterval(syncStatus.interval_seconds)}`
}

function getSyncAlertType(
  syncStatus: StaffDirectorySyncStatus | undefined,
  isLoading: boolean,
): 'success' | 'info' | 'warning' | 'error' {
  if (isLoading || !syncStatus) return 'info'
  if (!syncStatus.scheduler_enabled) return 'warning'
  if (!syncStatus.scheduler_running || syncStatus.last_sync_status === 'failed') return 'error'
  if (syncStatus.last_sync_status === 'success') return 'success'
  return 'info'
}

function getSyncSchedulerState(syncStatus: StaffDirectorySyncStatus | undefined, isLoading: boolean): string {
  if (isLoading) return '加载中'
  if (!syncStatus) return '未知'
  if (!syncStatus.scheduler_enabled) return '未启动'
  if (!syncStatus.scheduler_running) return '异常停止'
  return '运行中'
}

function getLastSyncResultText(syncStatus: StaffDirectorySyncStatus | undefined, isLoading: boolean): string {
  if (isLoading) return '加载中'
  if (!syncStatus || syncStatus.last_sync_status === 'not_started') return '暂无同步记录'
  if (syncStatus.last_sync_status === 'failed') return '同步失败'
  return `成功，检查 ${syncStatus.checked_count ?? 0} 人，更新 ${syncStatus.updated_count ?? 0} 人，停用 ${syncStatus.deactivated_count ?? 0} 人，未匹配 ${syncStatus.missing_count ?? 0} 人`
}

export function StaffPage() {
  const qc = useQueryClient()
  const auth = useAuth()
  const navigate = useNavigate()
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [filters, setFilters] = useState({
    keyword: '',
    position_id: undefined as string | undefined,
    hospital_code: undefined as string | undefined,
    badge_id: '',
    account_status: undefined as 'not_opened' | 'active' | 'disabled' | undefined,
  })
  const [queryFilters, setQueryFilters] = useState(filters)
  const canManagePositions = auth.status === 'authenticated' && isSystemAdminOrAbove(auth.user.role)
  const canManageAccounts = auth.status === 'authenticated' && isHospitalAdminOrAbove(auth.user.role)
  const currentUserRole = auth.status === 'authenticated' ? normalizeRole(auth.user.role) : 'staff'
  const currentUserHospitalCode = auth.status === 'authenticated' ? asText(auth.user.hospital_code) : null

  const [modalOpen, setModalOpen] = useState(false)
  const [editingStaff, setEditingStaff] = useState<Staff | null>(null)
  const [staffForm] = Form.useForm()
  const selectedStaffFormPositionId = Form.useWatch('position_id', staffForm)

  const { data: staffData, isLoading } = useQuery({
    queryKey: ['staff', queryFilters, page, pageSize],
    queryFn: () =>
      adminApi.fetchStaff({
        keyword: queryFilters.keyword || undefined,
        position_id: queryFilters.position_id,
        hospital_code: queryFilters.hospital_code,
        badge_id: queryFilters.badge_id || undefined,
        account_status: queryFilters.account_status,
        page,
        page_size: pageSize,
      }),
  })
  const { data: positions = [] } = useQuery({
    queryKey: ['positions'],
    queryFn: () => adminApi.fetchPositions(),
  })
  const { data: hospitalOptions = [] } = useQuery({
    queryKey: ['staff', 'hospital-options'],
    queryFn: () => adminApi.fetchStaffHospitalOptions(),
  })
  const { data: syncStatus, isLoading: isSyncStatusLoading } = useQuery({
    queryKey: ['staff-directory-sync-status'],
    queryFn: adminApi.fetchStaffDirectorySyncStatus,
    refetchInterval: 60 * 1000,
  })
  const rows = (staffData?.items ?? []).map((row, index) => ({
    ...row,
    id: asText(row.id) ?? `staff-${index}`,
    name: asDisplayText(row.name, ''),
    phone: asText(row.phone),
    external_account: asText(row.external_account),
    wecom_user_id: asText(row.wecom_user_id),
    wecom_corp_id: asText(row.wecom_corp_id),
    gender: asText(row.gender),
    hospital_code: asText(row.hospital_code),
    hospital_short_name: asText(row.hospital_short_name),
    position_id: asText(row.position_id),
    position_name: asText(row.position_name),
    role: asDisplayText(row.role, 'consultant'),
    permission_role: asDisplayText(row.permission_role, 'staff'),
    badge_id: asText(row.badge_id),
    is_active: Boolean(row.is_active),
    account_opened: Boolean(row.account_opened),
    account_username: asText(row.account_username),
    account_is_active: row.account_is_active == null ? null : Boolean(row.account_is_active),
    account_last_login_at: asText(row.account_last_login_at),
  }))

  const positionOptions = positions.flatMap((item: PositionProfile) => {
    const label = asText(item.name)
    const value = asText(item.id)
    return label && value ? [{ label, value }] : []
  })
  const staffFormPositions =
    currentUserRole === 'hospital_admin'
      ? positions.filter((item: PositionProfile) => {
          const mappedRole = normalizeRole(asText(item.mapped_role))
          if (!editingStaff) return mappedRole === 'staff' || mappedRole === 'hospital_admin'
          return mappedRole === 'staff' || mappedRole === normalizeRole(asText(editingStaff.permission_role))
        })
      : positions
  const staffFormPositionOptions = staffFormPositions.flatMap((item: PositionProfile) => {
    const label = asText(item.name)
    const value = asText(item.id)
    return label && value ? [{ label, value }] : []
  })
  const selectedStaffFormPosition = positions.find(
    (item) => asText(item.id) === asText(selectedStaffFormPositionId),
  )
  const selectedStaffFormPermissionRole = normalizeRole(
    asText(selectedStaffFormPosition?.mapped_role) ?? asText(editingStaff?.permission_role),
  )
  const isGlobalStaffFormRole = isGlobalPermissionRole(selectedStaffFormPermissionRole)
  const defaultStaffPositionId =
    asText(positions.find((item) => asText(item.name) === '普通员工')?.id) ||
    asText(positions.find((item) => asText(item.position_type) === 'staff' && asText(item.mapped_role) === 'staff')?.id) ||
    asText(positions.find((item) => asText(item.mapped_role) === 'staff')?.id)
  const institutionOptions = hospitalOptions.map((item) => ({
    value: item.hospital_code,
    label:
      item.hospital_name && item.hospital_name !== item.hospital_code
        ? `${item.hospital_name}（${item.hospital_code}）`
        : item.hospital_code,
  }))

  const refreshAll = async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ['staff'] }),
      qc.invalidateQueries({ queryKey: ['positions'] }),
      qc.invalidateQueries({ queryKey: ['audit-logs'] }),
      qc.invalidateQueries({ queryKey: ['staff-directory-sync-status'] }),
    ])
  }

  const createStaffMutation = useMutation({
    mutationFn: adminApi.createStaff,
    onSuccess: () => void refreshAll(),
  })
  const lookupStaffIdentityMutation = useMutation({
    mutationFn: adminApi.lookupStaffIdentity,
  })
  const updateStaffMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Partial<Staff> }) => adminApi.updateStaff(id, data),
    onSuccess: () => void refreshAll(),
  })
  const deleteStaffMutation = useMutation({
    mutationFn: adminApi.deleteStaff,
    onSuccess: () => void refreshAll(),
  })
  const enableStaffAccountMutation = useMutation({
    mutationFn: adminApi.enableStaffAccount,
    onSuccess: () => void refreshAll(),
  })
  const resetStaffAccountMutation = useMutation({
    mutationFn: adminApi.resetStaffAccountPassword,
    onSuccess: () => void refreshAll(),
  })
  const disableStaffAccountMutation = useMutation({
    mutationFn: adminApi.disableStaffAccount,
    onSuccess: () => void refreshAll(),
  })
  const activateStaffAccountMutation = useMutation({
    mutationFn: adminApi.activateStaffAccount,
    onSuccess: () => void refreshAll(),
  })

  const resetFilters = () => {
    const next = {
      keyword: '',
      position_id: undefined,
      hospital_code: undefined,
      badge_id: '',
      account_status: undefined,
    }
    setFilters(next)
    setQueryFilters(next)
    setPage(1)
  }

  const openStaffModal = (staff?: Staff) => {
    const isCreating = !staff
    setEditingStaff(staff ?? null)
    staffForm.setFieldsValue({
      name: staff?.name ?? '',
      phone: staff?.phone ?? '',
      external_account: getAdvisorCode(staff) ?? '',
      wecom_user_id: staff?.wecom_user_id ?? '',
      gender: staff?.gender ?? undefined,
      hospital_code: isGlobalPermissionRole(staff?.permission_role)
        ? undefined
        : staff?.hospital_code ?? (isCreating && currentUserRole === 'hospital_admin' ? currentUserHospitalCode ?? '' : ''),
      position_id: staff?.position_id ?? (isCreating ? defaultStaffPositionId ?? undefined : undefined),
    })
    setModalOpen(true)
  }

  const handleLookupStaffIdentity = async (options?: { silent?: boolean }) => {
    const externalAccount = String(staffForm.getFieldValue('external_account') || '').trim()
    if (!externalAccount) {
      if (!options?.silent) message.warning('请先填写员工编号')
      return
    }
    try {
      const identity = await lookupStaffIdentityMutation.mutateAsync({
        external_account: externalAccount,
        hospital_code: isGlobalStaffFormRole ? null : staffForm.getFieldValue('hospital_code') || null,
      })
      const nextValues: Record<string, string> = {}
      if (identity.name && (!staffForm.getFieldValue('name') || !editingStaff)) {
        nextValues.name = identity.name
      }
      if (identity.phone && !staffForm.getFieldValue('phone')) {
        nextValues.phone = identity.phone
      }
      if (!isGlobalStaffFormRole && identity.hospital_code && !staffForm.getFieldValue('hospital_code')) {
        nextValues.hospital_code = identity.hospital_code
      }
      if (Object.keys(nextValues).length > 0) {
        staffForm.setFieldsValue(nextValues)
      }
      if (!options?.silent) {
        const sourceLabel = STAFF_IDENTITY_SOURCE_LABELS[identity.source] ?? '外部数据源'
        message.success(`已从${sourceLabel}获取姓名：${identity.name}`)
      }
    } catch (error) {
      if (!options?.silent) {
        message.warning(await getApiErrorMessage(error, '未根据员工编号找到姓名，请手动填写'))
      }
    }
  }

  useEffect(() => {
    if (!modalOpen || editingStaff) return
    if (!staffForm.getFieldValue('position_id') && defaultStaffPositionId) {
      staffForm.setFieldValue('position_id', defaultStaffPositionId)
    }
    if (
      currentUserRole === 'hospital_admin' &&
      currentUserHospitalCode &&
      !staffForm.getFieldValue('hospital_code')
    ) {
      staffForm.setFieldValue('hospital_code', currentUserHospitalCode)
    }
  }, [currentUserHospitalCode, currentUserRole, defaultStaffPositionId, editingStaff, modalOpen, staffForm])

  useEffect(() => {
    if (!modalOpen || !isGlobalStaffFormRole) return
    if (staffForm.getFieldValue('hospital_code')) {
      staffForm.setFieldValue('hospital_code', undefined)
    }
  }, [isGlobalStaffFormRole, modalOpen, staffForm])

  const handleSaveStaff = async () => {
    try {
      const values = await staffForm.validateFields()
      const payload = {
        ...values,
        name: values.name || null,
        phone: values.phone || null,
        external_account: values.external_account || null,
        wecom_user_id: values.wecom_user_id || null,
        gender: values.gender || null,
        hospital_code: isGlobalStaffFormRole ? null : values.hospital_code || null,
        position_id: values.position_id || null,
      }

      if (editingStaff) {
        await updateStaffMutation.mutateAsync({ id: editingStaff.id, data: payload })
        message.success('人员信息已更新')
      } else {
        await createStaffMutation.mutateAsync(payload)
        message.success('人员已新增')
      }

      setModalOpen(false)
      staffForm.resetFields()
    } catch (error) {
      message.error(await getApiErrorMessage(error, '保存人员失败'))
    }
  }

  const handleToggleStatus = async (staff: Staff) => {
    try {
      await updateStaffMutation.mutateAsync({
        id: staff.id,
        data: { is_active: !staff.is_active },
      })
      message.success(staff.is_active ? '人员已禁用' : '人员已启用')
    } catch (error) {
      message.error(await getApiErrorMessage(error, '更新状态失败'))
    }
  }

  const showAccountCredentials = (result: StaffAccountActionResult, title: string) => {
    const password = result.temporary_password
    Modal.success({
      title,
      content: (
        <Space direction="vertical" size={8}>
          <div>员工：{result.staff_name}</div>
          <div>登录账号：{result.username}</div>
          {password ? <div>默认密码：{password}</div> : <div>账号状态：{result.is_active ? '正常' : '已停用'}</div>}
        </Space>
      ),
    })
  }

  const handleEnableAccount = async (staff: Staff) => {
    const identifier = getPreferredLoginAccount(staff)
    if (!identifier) {
      message.warning('请先补员工工号或手机号，再开通账号')
      return
    }

    Modal.confirm({
      title: '确认开通账号',
      content: (
        <Space direction="vertical" size={8}>
          <div>员工：{staff.name}</div>
          <div>
            登录账号将使用{identifier.sourceLabel}：{identifier.username}
          </div>
          <div>默认密码：{buildDefaultPasswordPreview(identifier.username)}</div>
        </Space>
      ),
      okText: '确认开通',
      cancelText: '取消',
      onOk: async () => {
        try {
          const result = await enableStaffAccountMutation.mutateAsync(staff.id)
          if (result.created) {
            showAccountCredentials(result, '账号已开通')
          } else {
            message.success(result.message)
          }
        } catch (error) {
          message.error(await getApiErrorMessage(error, '开通账号失败'))
        }
      },
    })
  }

  const handleResetAccountPassword = async (staff: Staff) => {
    try {
      const result = await resetStaffAccountMutation.mutateAsync(staff.id)
      showAccountCredentials(result, '密码已重置')
    } catch (error) {
      message.error(await getApiErrorMessage(error, '重置密码失败'))
    }
  }

  const handleDisableAccount = async (staff: Staff) => {
    try {
      const result = await disableStaffAccountMutation.mutateAsync(staff.id)
      message.success(result.message)
    } catch (error) {
      message.error(await getApiErrorMessage(error, '停用账号失败'))
    }
  }

  const handleActivateAccount = async (staff: Staff) => {
    try {
      const result = await activateStaffAccountMutation.mutateAsync(staff.id)
      message.success(result.message)
    } catch (error) {
      message.error(await getApiErrorMessage(error, '启用账号失败'))
    }
  }

  const handleDeleteStaff = async (staff: Staff) => {
    try {
      await deleteStaffMutation.mutateAsync(staff.id)
      message.success('人员已删除')
    } catch (error) {
      message.error(await getApiErrorMessage(error, '删除人员失败'))
    }
  }

  type StaffActionKey =
    | 'account-reset'
    | 'edit'
    | 'toggle-status'
    | 'delete'

  const openDeleteConfirm = (staff: Staff) => {
    Modal.confirm({
      title: '确定删除这条人员记录吗？',
      okText: '删除',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: async () => {
        await handleDeleteStaff(staff)
      },
    })
  }

  const openToggleStatusConfirm = (staff: Staff) => {
    Modal.confirm({
      title: staff.is_active ? '确定禁用这位人员吗？' : '确定启用这位人员吗？',
      okText: staff.is_active ? '禁用' : '启用',
      cancelText: '取消',
      okButtonProps: staff.is_active ? { danger: true } : undefined,
      onOk: async () => {
        await handleToggleStatus(staff)
      },
    })
  }

  const openAccountDisableConfirm = (staff: Staff) => {
    Modal.confirm({
      title: '确定停用这个登录账号吗？',
      okText: '停用',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: async () => {
        await handleDisableAccount(staff)
      },
    })
  }

  const handleActionMenuClick = async (staff: Staff, action: StaffActionKey) => {
    switch (action) {
      case 'account-reset':
        await handleResetAccountPassword(staff)
        return
      case 'edit':
        openStaffModal(staff)
        return
      case 'toggle-status':
        openToggleStatusConfirm(staff)
        return
      case 'delete':
        openDeleteConfirm(staff)
        return
      default:
        return
    }
  }

  const getActionMenuItems = (row: Staff): MenuProps['items'] => {
    const items: NonNullable<MenuProps['items']> = []

    if (canManageAccounts && row.account_opened) {
      items.push({
        key: 'account-reset',
        label: '重置密码',
      })
      items.push({ type: 'divider' })
    }

    items.push(
      { key: 'toggle-status', label: row.is_active ? '禁用人员' : '启用人员', danger: row.is_active },
      { key: 'delete', label: '删除人员', danger: true },
    )

    return items
  }

  const renderAccountActionButton = (row: Staff) => {
    if (!canManageAccounts) return null
    const identifier = getPreferredLoginAccount(row)
    if (!row.account_opened) {
      return (
        <Button
          size="small"
          type="primary"
          className="staff-page__action-account staff-page__action-account--open"
          icon={<UserAddOutlined />}
          disabled={!row.is_active || !identifier}
          loading={enableStaffAccountMutation.isPending}
          onClick={() => void handleEnableAccount(row)}
        >
          开通
        </Button>
      )
    }
    if (row.account_is_active) {
      return (
        <Button
          size="small"
          danger
          className="staff-page__action-account staff-page__action-account--disable"
          icon={<StopOutlined />}
          loading={disableStaffAccountMutation.isPending}
          onClick={() => openAccountDisableConfirm(row)}
        >
          停用
        </Button>
      )
    }
    return (
      <Button
        size="small"
        type="primary"
        className="staff-page__action-account staff-page__action-account--activate"
        icon={<CheckCircleOutlined />}
        disabled={!row.is_active}
        loading={activateStaffAccountMutation.isPending}
        onClick={() => void handleActivateAccount(row)}
      >
        启用
      </Button>
    )
  }

  const columns: TableProps<Staff>['columns'] = [
    {
      title: '人员信息',
      key: 'staff_profile',
      width: 138,
      render: (_value, row) => (
        <div className="staff-page__cell">
          <strong>{row.name || '-'}</strong>
          <div className="staff-page__meta-row">
            <span className="staff-page__meta-label">员工编号</span>
            <span className="staff-page__meta-value">{asDisplayText(getAdvisorCode(row))}</span>
          </div>
          <div className="staff-page__meta-row">
            <span className="staff-page__meta-label">手机号</span>
            <span className="staff-page__meta-value">{asDisplayText(row.phone)}</span>
          </div>
        </div>
      ),
    },
    {
      title: '账号信息',
      key: 'account',
      width: 170,
      render: (_value, row) => (
        <div className="staff-page__cell">
          <div className="staff-page__headline">
            <strong>{row.account_opened ? asDisplayText(row.account_username) : '-'}</strong>
            <Tag className="staff-page__status-tag" color={row.account_opened && row.account_is_active ? 'success' : 'default'}>
              {row.account_opened ? (row.account_is_active ? '正常' : '已停用') : '未开通'}
            </Tag>
          </div>
          <div className="staff-page__meta-row">
            <span className="staff-page__meta-label">企微号</span>
            <span className="staff-page__meta-value">{asDisplayText(row.wecom_user_id)}</span>
          </div>
          <div className="staff-page__meta-row">
            <span className="staff-page__meta-label">企微主体</span>
            <span className="staff-page__meta-value">{asDisplayText(row.wecom_corp_id)}</span>
          </div>
          <span className="staff-page__hint">
            {row.account_opened ? `最近登录：${formatLastLogin(row.account_last_login_at)}` : '未开通账号'}
          </span>
        </div>
      ),
    },
    {
      title: '机构归属',
      key: 'hospital',
      width: 110,
      render: (_value, row) => (
        <div className="staff-page__cell">
          <strong>{asDisplayText(row.hospital_short_name || row.hospital_code)}</strong>
          <div className="staff-page__meta-row">
            <span className="staff-page__meta-label">机构编码</span>
            <span className="staff-page__meta-value">{asDisplayText(row.hospital_code)}</span>
          </div>
        </div>
      ),
    },
    {
      title: '岗位权限',
      key: 'role_profile',
      width: 154,
      render: (_value, row) => {
        const labels = ROLE_FLAG_MAP.filter((item) => Boolean(row[item.key as keyof Staff])).map((item) => item.label)
        const positionLabel = asDisplayText(row.position_name)
        const permissionLabel = roleLabel(asText(row.permission_role))
        return (
          <div className="staff-page__cell">
            <strong>{positionLabel}</strong>
            {shouldShowPermissionHint(positionLabel, permissionLabel) ? (
              <span className="staff-page__hint">权限：{permissionLabel}</span>
            ) : null}
            {labels.length > 0 ? (
              <div className="staff-page__tag-list">
                {labels.map((label) => (
                  <Tag key={label} className="staff-page__role-tag">
                    {label}
                  </Tag>
                ))}
              </div>
            ) : (
              <span className="staff-page__hint">未设置岗位标识</span>
            )}
          </div>
        )
      },
    },
    {
      title: '设备工牌号',
      key: 'badge_id',
      width: 104,
      render: (_value, row) => (
        <div className="staff-page__cell">
          <strong>{asDisplayText(getDeviceBadgeId(row))}</strong>
          <span className="staff-page__hint">用于设备绑定</span>
        </div>
      ),
    },
    {
      title: '操作',
      width: 176,
      align: 'center',
      render: (_value, row) => (
        <div className="staff-page__actions">
          <Button size="small" className="staff-page__action-edit" icon={<EditOutlined />} onClick={() => openStaffModal(row)}>
            编辑
          </Button>
          {renderAccountActionButton(row)}
          <Dropdown
            trigger={['click']}
            menu={{
              items: getActionMenuItems(row),
              onClick: ({ key }) => {
                void handleActionMenuClick(row, key as StaffActionKey)
              },
            }}
          >
            <Button size="small" className="staff-page__action-more" icon={<MoreOutlined />} aria-label={`${row.name} 更多操作`} />
          </Dropdown>
        </div>
      ),
    },
  ]

  return (
    <div className="operation-page">
      <div className="operation-page__header">
        <div className="operation-page__title">
          <span className="operation-page__marker" aria-hidden="true" />
          <div>
            <h1>人员管理</h1>
            <p>维护人员资料、员工编号、企业微信 UserId、机构归属和岗位信息。人员与工牌绑定统一在“朗姿工牌”页面操作。</p>
          </div>
        </div>
      </div>

      <div className="operation-card">
        <Alert
          showIcon
          style={{ marginBottom: 16 }}
          type={getSyncAlertType(syncStatus, isSyncStatusLoading)}
          message={getSyncHeadline(syncStatus, isSyncStatusLoading)}
          description={
            <Space direction="vertical" size={4}>
              <span>服务状态：{getSyncSchedulerState(syncStatus, isSyncStatusLoading)}</span>
              <span>服务启动时间：{formatSyncTimestamp(syncStatus?.scheduler_started_at)}</span>
              <span>最近一次同步：{formatSyncTimestamp(syncStatus?.last_synced_at)}</span>
              <span>下次计划执行：{formatSyncTimestamp(syncStatus?.next_scheduled_at)}</span>
              <span>最近结果：{getLastSyncResultText(syncStatus, isSyncStatusLoading)}</span>
              {syncStatus?.scheduler_note ? <span>说明：{syncStatus.scheduler_note}</span> : null}
              {syncStatus?.last_sync_status === 'failed' && syncStatus.error_message ? (
                <span>错误信息：{syncStatus.error_message}</span>
              ) : null}
            </Space>
          }
        />

        <div className="operation-filter-grid">
          <label className="operation-filter-item">
            <span>关键字</span>
            <Input
              placeholder="请输入姓名、手机号、员工编号或企业微信 UserId"
              value={filters.keyword}
              onChange={(event) => setFilters((current) => ({ ...current, keyword: event.target.value }))}
            />
          </label>
          <label className="operation-filter-item">
            <span>岗位</span>
            <Select
              allowClear
              placeholder="请选择岗位"
              options={positionOptions}
              value={filters.position_id}
              onChange={(value) => setFilters((current) => ({ ...current, position_id: value }))}
            />
          </label>
          <label className="operation-filter-item">
            <span>机构</span>
            <Select
              allowClear
              showSearch
              placeholder="请选择机构"
              optionFilterProp="label"
              options={institutionOptions}
              value={filters.hospital_code}
              onChange={(value) => setFilters((current) => ({ ...current, hospital_code: value }))}
            />
          </label>
          <label className="operation-filter-item">
            <span>设备工牌号</span>
            <Input
              placeholder="请输入设备工牌号"
              value={filters.badge_id}
              onChange={(event) => setFilters((current) => ({ ...current, badge_id: event.target.value }))}
            />
          </label>
          <label className="operation-filter-item">
            <span>账号状态</span>
            <Select
              allowClear
              placeholder="请选择账号状态"
              options={ACCOUNT_STATUS_OPTIONS}
              value={filters.account_status}
              onChange={(value) => setFilters((current) => ({ ...current, account_status: value }))}
            />
          </label>
        </div>

        <div className="operation-toolbar">
          <Space wrap>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => openStaffModal()}>
              新增人员
            </Button>
            {canManagePositions && <Button onClick={() => navigate('/admin/positions')}>岗位管理</Button>}
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
          className="staff-page__table"
          rowKey="id"
          dataSource={rows}
          loading={isLoading}
          columns={columns}
          scroll={{ x: 960 }}
          size="small"
          pagination={{
            current: page,
            pageSize,
            total: staffData?.total ?? 0,
            showSizeChanger: true,
            showTotal: (total) => `共 ${total} 条`,
            onChange: (nextPage, nextPageSize) => {
              setPage(nextPage)
              setPageSize(nextPageSize)
            },
          }}
        />
      </div>

      <Modal
        title={editingStaff ? '编辑人员' : '新增人员'}
        open={modalOpen}
        onOk={() => void handleSaveStaff()}
        onCancel={() => {
          setModalOpen(false)
          staffForm.resetFields()
        }}
        confirmLoading={createStaffMutation.isPending || updateStaffMutation.isPending}
        destroyOnClose
      >
        <Form form={staffForm} layout="vertical">
          <Form.Item
            name="name"
            label="姓名"
            rules={editingStaff ? [{ required: true, whitespace: true, message: '请输入姓名' }] : []}
            extra={editingStaff ? undefined : '新增时可不填姓名，系统会根据员工编号自动查询。'}
          >
            <Input placeholder="可由员工编号自动获取" />
          </Form.Item>
          <Form.Item name="phone" label="手机号">
            <Input />
          </Form.Item>
          <Form.Item
            name="external_account"
            label="员工编号"
            rules={!editingStaff ? [{ required: true, whitespace: true, message: '请输入员工编号' }] : []}
            extra="录音 JSON 中的 FZUER 应录入这里，作为员工编号，不是设备工牌号；新增时可点击查询自动补姓名。"
          >
            <Input.Search
              placeholder="例如 81019369"
              enterButton="查询"
              loading={lookupStaffIdentityMutation.isPending}
              onSearch={() => void handleLookupStaffIdentity()}
              onBlur={() => {
                if (!editingStaff && !staffForm.getFieldValue('name')) {
                  void handleLookupStaffIdentity({ silent: true })
                }
              }}
            />
          </Form.Item>
          <Form.Item name="wecom_user_id" label="企业微信 UserId" extra="用于企业微信工作台免密登录绑定，可在企业微信管理后台或通讯录导出中获取。">
            <Input placeholder="例如 zhangsan" />
          </Form.Item>
          {isGlobalStaffFormRole ? (
            <Form.Item label="机构归属">
              <Input disabled value={ALL_INSTITUTIONS_LABEL} />
            </Form.Item>
          ) : (
            <Form.Item name="hospital_code" label="机构编码">
              <Select
                allowClear
                showSearch
                placeholder="请选择机构编码"
                optionFilterProp="label"
                options={institutionOptions}
              />
            </Form.Item>
          )}
          <p className="staff-page__form-note">
            {isGlobalStaffFormRole
              ? '超级管理员和系统管理员归属所有机构，不绑定单个机构或企微 CorpID。'
              : '企业微信 CorpID 将根据所选机构编码自动读取，不能在人员资料中手动修改。'}
          </p>
          <Form.Item name="gender" label="性别">
            <Select allowClear options={GENDER_OPTIONS} />
          </Form.Item>
          <Form.Item name="position_id" label="岗位">
            <Select allowClear options={staffFormPositionOptions} />
          </Form.Item>
        </Form>
      </Modal>

    </div>
  )
}

export default StaffPage
