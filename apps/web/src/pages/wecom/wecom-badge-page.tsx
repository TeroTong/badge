import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  LogoutOutlined,
  MobileOutlined,
  SyncOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { Modal } from 'antd'
import { useSearchParams } from 'react-router-dom'

import {
  getManagedBadges,
  getMyBadge,
  startMyBadgeRecording,
  stopMyBadgeRecording,
  type MyBadge,
} from '@/api/auth'
import { getApiErrorMessage } from '@/api/errors'
import { roleLabel } from '@/app/roles'
import { useAuth } from '@/app/use-auth'
import { formatBeijingTime } from '@/utils/time'

const MY_BADGE_QUERY_KEY = ['account-my-badge'] as const
const MANAGED_BADGES_QUERY_KEY = ['account-managed-badges'] as const
const BADGE_POLL_INTERVAL_MS = 4000
const BADGE_RECORDING_POLL_INTERVAL_MS = 2000
const BADGE_IDLE_POLL_INTERVAL_MS = 8000
const BADGE_NOTICE_MS = 3200
const OPTIMISTIC_RECORDING_GRACE_MS = 6000

type BadgeNotice = {
  id: number
  tone: 'default' | 'warn'
  message: string
}

type PendingBadgeAction = 'start' | 'stop' | null
type BadgeAction = Exclude<PendingBadgeAction, null>
type OptimisticRecordingState = Pick<MyBadge, 'is_recording' | 'recording_started_at'> & {
  expiresAt: number
}

function isPageVisible() {
  if (typeof document === 'undefined') return true
  return document.visibilityState !== 'hidden'
}

function formatDateTime(value: string | null | undefined) {
  return formatBeijingTime(value, 'M/D HH:mm', '--')
}

function getStatusMeta(badge: MyBadge | undefined) {
  if (badge?.online === true) {
    return {
      tone: 'success',
      value: '在线',
      detail: '实时同步中',
    }
  }
  if (badge?.online === false) {
    return {
      tone: 'default',
      value: '离线',
      detail: '当前未在线',
    }
  }
  return {
    tone: 'default',
    value: '待同步',
    detail: '等待状态更新',
  }
}

function getBatteryMeta(level: number | null | undefined) {
  if (typeof level !== 'number') {
    return {
      tone: 'default',
      value: '--',
      detail: '电量待同步',
    }
  }
  if (level <= 20) {
    return {
      tone: 'danger',
      value: `${level}%`,
      detail: '电量偏低',
    }
  }
  if (level <= 50) {
    return {
      tone: 'amber',
      value: `${level}%`,
      detail: '请留意续航',
    }
  }
  return {
    tone: 'success',
    value: `${level}%`,
    detail: '电量充足',
  }
}

function getRecordingMeta(badge: MyBadge | undefined) {
  if (badge?.is_recording) {
    return {
      title: '正在录音',
      detail: badge.recording_started_at
        ? `开始时间：${formatDateTime(badge.recording_started_at)}`
        : '工牌正在持续录音中',
    }
  }
  if (badge?.can_control_recording) {
    return {
      title: '当前未录音',
      detail: '点击下方按钮即可开始录音',
    }
  }
  return {
    title: '暂不可控制录音',
    detail: '需要先把当前工牌完成钉钉侧绑定后，手机端才能控制录音',
  }
}

function getBadgeActionFallback(action: BadgeAction) {
  return action === 'start' ? '开始录音失败，请稍后重试' : '结束录音失败，请稍后重试'
}

function getOfflinePrompt(action: BadgeAction) {
  return action === 'start'
    ? '工牌未开机，请先开机后再开始录音'
    : '工牌未开机，请先开机后再停止录音'
}

function mapBadgeActionErrorMessage(rawMessage: string, action: BadgeAction) {
  const message = rawMessage.trim()
  if (!message) return getBadgeActionFallback(action)

  const normalized = message.toLowerCase()
  if (
    normalized.includes('device.status.offline')
    || normalized.includes('status is offline')
    || (normalized.includes('当前工牌远端状态不可用') && normalized.includes('offline'))
    || normalized.includes('当前未在线')
  ) {
    return getOfflinePrompt(action)
  }
  if (
    normalized.includes('尚未完成钉钉侧绑定')
    || normalized.includes('暂不能控制录音')
    || normalized.includes('还未完成绑定')
  ) {
    return '当前工牌还未完成绑定，暂时不能控制录音'
  }
  if (normalized.includes('暂未绑定工牌')) {
    return '当前账号暂未绑定工牌'
  }
  if (
    normalized.includes('already recording')
    || normalized.includes('recording already')
    || normalized.includes('already start')
  ) {
    return '工牌已经在录音中'
  }
  if (
    normalized.includes('not recording')
    || normalized.includes('no recording')
    || normalized.includes('not in recording')
  ) {
    return '工牌当前未在录音'
  }
  if (
    normalized.includes('too many requests')
    || normalized.includes('rate limit')
    || normalized.includes('频繁')
  ) {
    return '操作过于频繁，请稍后再试'
  }
  if (
    normalized.includes('timeout')
    || normalized.includes('timed out')
    || normalized.includes('network')
    || normalized.includes('failed to fetch')
    || normalized.includes('fetcherror')
  ) {
    return '网络连接异常，请稍后再试'
  }
  if (
    normalized.includes('远端状态不可用')
    || normalized.includes('状态未同步')
    || normalized.includes('device not found')
  ) {
    return '工牌状态同步异常，请稍后再试'
  }
  if (normalized.includes('钉钉接口调用失败') || normalized.includes('code=')) {
    return getBadgeActionFallback(action)
  }
  return message
}

function buildOptimisticRecordingState(
  next: Pick<MyBadge, 'is_recording' | 'recording_started_at'>,
): OptimisticRecordingState {
  return {
    ...next,
    expiresAt: Date.now() + OPTIMISTIC_RECORDING_GRACE_MS,
  }
}

export function WecomBadgePage() {
  const auth = useAuth()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const [notice, setNotice] = useState<BadgeNotice | null>(null)
  const [pendingAction, setPendingAction] = useState<PendingBadgeAction>(null)
  const [pageVisible, setPageVisible] = useState(() => isPageVisible())
  const [optimisticRecordingState, setOptimisticRecordingState] = useState<OptimisticRecordingState | null>(null)
  const [autoStartHandledKey, setAutoStartHandledKey] = useState<string | null>(null)
  const autoStartRequested = searchParams.get('action') === 'start'
  const autoStartVisitOrderNo = searchParams.get('visit_order_no')?.trim() ?? ''
  const autoStartKey = autoStartRequested ? `start:${autoStartVisitOrderNo || 'unknown'}` : ''

  const badgeQuery = useQuery({
    queryKey: MY_BADGE_QUERY_KEY,
    queryFn: getMyBadge,
    staleTime: 0,
    refetchOnMount: 'always',
    refetchOnReconnect: true,
    refetchOnWindowFocus: 'always',
    refetchIntervalInBackground: false,
    refetchInterval: (query) => {
      if (!pageVisible) return false
      const badge = query.state.data as MyBadge | undefined
      if (badge?.is_recording) return BADGE_RECORDING_POLL_INTERVAL_MS
      if (badge?.bound) return BADGE_POLL_INTERVAL_MS
      return BADGE_IDLE_POLL_INTERVAL_MS
    },
  })
  const managedBadgesQuery = useQuery({
    queryKey: MANAGED_BADGES_QUERY_KEY,
    queryFn: getManagedBadges,
    staleTime: 0,
    refetchOnMount: 'always',
    refetchOnReconnect: true,
    refetchOnWindowFocus: 'always',
    refetchIntervalInBackground: false,
    refetchInterval: (query) => {
      if (!pageVisible) return false
      const badges = query.state.data as MyBadge[] | undefined
      if (badges?.some((item) => item.is_recording)) return BADGE_POLL_INTERVAL_MS
      return BADGE_IDLE_POLL_INTERVAL_MS
    },
  })
  const refetchBadge = badgeQuery.refetch
  const refetchManagedBadges = managedBadgesQuery.refetch

  useEffect(() => {
    if (typeof window === 'undefined' || typeof document === 'undefined') {
      return undefined
    }

    const syncVisibility = () => {
      const visible = isPageVisible()
      setPageVisible(visible)
      if (visible) {
        void refetchBadge()
        void refetchManagedBadges()
      }
    }

    document.addEventListener('visibilitychange', syncVisibility)
    window.addEventListener('focus', syncVisibility)
    window.addEventListener('pageshow', syncVisibility)
    window.addEventListener('online', syncVisibility)

    return () => {
      document.removeEventListener('visibilitychange', syncVisibility)
      window.removeEventListener('focus', syncVisibility)
      window.removeEventListener('pageshow', syncVisibility)
      window.removeEventListener('online', syncVisibility)
    }
  }, [refetchBadge, refetchManagedBadges])

  useEffect(() => {
    if (!notice || typeof window === 'undefined') {
      return undefined
    }
    const timer = window.setTimeout(() => {
      setNotice((current) => (current?.id === notice.id ? null : current))
    }, BADGE_NOTICE_MS)
    return () => {
      window.clearTimeout(timer)
    }
  }, [notice])

  useEffect(() => {
    if (!optimisticRecordingState) {
      return undefined
    }
    if (typeof window === 'undefined') {
      return undefined
    }

    const remoteConfirmed = badgeQuery.data?.is_recording === optimisticRecordingState.is_recording
    const delay = remoteConfirmed ? 0 : Math.max(0, optimisticRecordingState.expiresAt - Date.now())
    const timer = window.setTimeout(() => {
      setOptimisticRecordingState(null)
    }, delay)
    return () => {
      window.clearTimeout(timer)
    }
  }, [badgeQuery.data?.is_recording, optimisticRecordingState])

  const showNotice = (tone: BadgeNotice['tone'], message: string) => {
    setNotice({
      id: Date.now(),
      tone,
      message,
    })
  }

  const showActionWarningModal = (message: string) => {
    Modal.warning({
      title: '当前无法执行录音操作',
      content: message,
      okText: '我知道了',
      centered: true,
      wrapClassName: 'wc-badge-action-modal',
    })
  }

  const reportBadgeActionError = async (error: unknown, action: BadgeAction) => {
    const rawMessage = await getApiErrorMessage(error, getBadgeActionFallback(action))
    showActionWarningModal(mapBadgeActionErrorMessage(rawMessage, action))
  }

  const startMutation = useMutation({
    mutationFn: startMyBadgeRecording,
    onMutate: async () => {
      setNotice(null)
      const nextRecordingState = {
        is_recording: true,
        recording_started_at: new Date().toISOString(),
      }
      setOptimisticRecordingState(buildOptimisticRecordingState(nextRecordingState))
      await queryClient.cancelQueries({ queryKey: MY_BADGE_QUERY_KEY })
      const previousBadge = queryClient.getQueryData<MyBadge>(MY_BADGE_QUERY_KEY)
      return { previousBadge }
    },
    onSuccess: (result) => {
      showNotice('default', result.message || '录音已开始')
    },
    onError: async (error, _variables, context) => {
      setOptimisticRecordingState(null)
      if (context?.previousBadge) {
        queryClient.setQueryData(MY_BADGE_QUERY_KEY, context.previousBadge)
      }
      await reportBadgeActionError(error, 'start')
    },
    onSettled: async () => {
      await queryClient.invalidateQueries({ queryKey: MY_BADGE_QUERY_KEY })
    },
  })

  const stopMutation = useMutation({
    mutationFn: stopMyBadgeRecording,
    onMutate: async () => {
      setNotice(null)
      const nextRecordingState = {
        is_recording: false,
        recording_started_at: null,
      }
      setOptimisticRecordingState(buildOptimisticRecordingState(nextRecordingState))
      await queryClient.cancelQueries({ queryKey: MY_BADGE_QUERY_KEY })
      const previousBadge = queryClient.getQueryData<MyBadge>(MY_BADGE_QUERY_KEY)
      return { previousBadge }
    },
    onSuccess: () => {
      showNotice('default', '录音已停止')
    },
    onError: async (error, _variables, context) => {
      setOptimisticRecordingState(null)
      if (context?.previousBadge) {
        queryClient.setQueryData(MY_BADGE_QUERY_KEY, context.previousBadge)
      }
      await reportBadgeActionError(error, 'stop')
    },
    onSettled: async () => {
      await queryClient.invalidateQueries({ queryKey: MY_BADGE_QUERY_KEY })
    },
  })

  const clearAutoStartParams = () => {
    const nextParams = new URLSearchParams(searchParams)
    nextParams.delete('action')
    nextParams.delete('visit_order_no')
    setSearchParams(nextParams, { replace: true })
  }

  useEffect(() => {
    if (auth.status !== 'authenticated' || !autoStartRequested || !autoStartKey) return
    if (autoStartHandledKey === autoStartKey) return
    if (badgeQuery.isLoading || (!badgeQuery.data && badgeQuery.isFetching)) return

    const badge = badgeQuery.data
    setAutoStartHandledKey(autoStartKey)

    if (!badge?.bound) {
      Modal.warning({
        title: '无法开始录音',
        content: badge?.reason || '当前账号暂未绑定工牌',
        okText: '我知道了',
        centered: true,
        wrapClassName: 'wc-badge-action-modal',
        onOk: clearAutoStartParams,
      })
      return
    }

    if (badge.online === false) {
      Modal.warning({
        title: '工牌未开机',
        content: getOfflinePrompt('start'),
        okText: '我知道了',
        centered: true,
        wrapClassName: 'wc-badge-action-modal',
        onOk: clearAutoStartParams,
      })
      return
    }

    if (!badge.can_control_recording) {
      Modal.warning({
        title: '当前无法开始录音',
        content: '当前工牌还未完成绑定，暂时不能控制录音',
        okText: '我知道了',
        centered: true,
        wrapClassName: 'wc-badge-action-modal',
        onOk: clearAutoStartParams,
      })
      return
    }

    if (badge.is_recording) {
      Modal.confirm({
        title: '工牌正在录音',
        content: autoStartVisitOrderNo
          ? `到诊单 ${autoStartVisitOrderNo} 需要开始新录音。是否停止当前录音并开始新的录音？`
          : '是否停止当前录音并开始新的录音？',
        okText: '停止并开始新录音',
        cancelText: '暂不处理',
        centered: true,
        wrapClassName: 'wc-badge-action-modal',
        onOk: async () => {
          try {
            await stopMutation.mutateAsync()
            await startMutation.mutateAsync()
          } finally {
            clearAutoStartParams()
          }
        },
        onCancel: clearAutoStartParams,
      })
      return
    }

    void startMutation.mutateAsync().finally(clearAutoStartParams)
  }, [
    auth.status,
    autoStartHandledKey,
    autoStartKey,
    autoStartRequested,
    autoStartVisitOrderNo,
    badgeQuery.data,
    badgeQuery.isFetching,
    badgeQuery.isLoading,
    searchParams,
    setSearchParams,
    startMutation,
    stopMutation,
  ])

  if (auth.status !== 'authenticated') {
    return <div className="wc-empty">请先登录后查看工牌状态。</div>
  }

  const badge = badgeQuery.data
  const managedBadges = managedBadgesQuery.data ?? []
  const managedRecordingCount = managedBadges.filter((item) => item.is_recording).length
  const effectiveOptimisticRecordingState = optimisticRecordingState && badge
    && badge.is_recording !== optimisticRecordingState.is_recording
    ? optimisticRecordingState
    : null
  const displayBadge = badge ? { ...badge, ...(effectiveOptimisticRecordingState ?? {}) } : badge
  const statusMeta = getStatusMeta(displayBadge)
  const batteryMeta = getBatteryMeta(displayBadge?.battery_level)
  const recordingMeta = getRecordingMeta(displayBadge)
  const effectivePendingAction =
    pendingAction
    && displayBadge?.can_control_recording
    && !((pendingAction === 'start' && displayBadge.is_recording) || (pendingAction === 'stop' && !displayBadge.is_recording))
      ? pendingAction
      : null
  const actionSubmitting = startMutation.isPending || stopMutation.isPending
  const canTriggerRecording = Boolean(displayBadge?.bound && displayBadge.can_control_recording)
  const showBindingHint = Boolean(displayBadge?.bound && !displayBadge.can_control_recording)
  const pendingActionLabel = effectivePendingAction === 'stop' ? '停止录音' : '开始录音'
  const heroDetail = displayBadge?.bound
    ? [statusMeta.detail, batteryMeta.detail].filter(Boolean).join(' · ')
    : '绑定工牌后即可在这里查看状态并控制录音'
  const remoteWarningMessage = displayBadge?.remote_warning
    ? mapBadgeActionErrorMessage(displayBadge.remote_warning, displayBadge?.is_recording ? 'stop' : 'start')
    : null
  const statusDetail = remoteWarningMessage || recordingMeta.detail

  const handlePrimaryAction = () => {
    if (!displayBadge) return
    const nextAction: BadgeAction = displayBadge.is_recording ? 'stop' : 'start'
    if (displayBadge.online === false) {
      showActionWarningModal(getOfflinePrompt(nextAction))
      return
    }
    if (!displayBadge.can_control_recording) return
    setPendingAction(nextAction)
  }

  const handleCancelPendingAction = () => {
    setPendingAction(null)
  }

  const handleConfirmPendingAction = () => {
    if (!displayBadge?.can_control_recording || !effectivePendingAction) return
    setPendingAction(null)
    if (effectivePendingAction === 'stop') {
      stopMutation.mutate()
      return
    }
    startMutation.mutate()
  }

  return (
    <div className="wc-page wc-my-badge-page">
      <section className="wc-card wc-card--sky wc-card--hero">
        <div className="wc-my-badge-hero">
          <div className="wc-my-badge-hero__main">
            <div className="wc-my-badge-hero__icon" aria-hidden="true">
              <MobileOutlined />
            </div>
            <div className="wc-my-badge-hero__copy">
              <span className="wc-my-badge-hero__eyebrow">我的工牌</span>
              <strong>{displayBadge?.device_name || displayBadge?.device_code || '暂未绑定工牌'}</strong>
              <div className="wc-my-badge-hero__chips">
                <span className={`wc-my-badge-chip wc-my-badge-chip--${statusMeta.tone}`}>
                  <MobileOutlined />
                  {statusMeta.value}
                </span>
                <span className={`wc-my-badge-chip wc-my-badge-chip--${batteryMeta.tone}`}>
                  <ThunderboltOutlined />
                  {batteryMeta.value}
                </span>
              </div>
              <span className="wc-my-badge-hero__detail">{heroDetail}</span>
            </div>
          </div>
          {showBindingHint ? <div className="wc-my-badge-binding-note">录音控制待完成绑定</div> : null}
        </div>
      </section>

      {notice ? (
        <div
          aria-live="polite"
          className={`wc-my-badge-toast${notice.tone === 'warn' ? ' wc-my-badge-toast--warn' : ''}`}
          role="status"
        >
          <span>{notice.message}</span>
        </div>
      ) : null}

      {displayBadge?.bound ? (
        <>
          <section className="wc-card wc-card--mint wc-card--compact">
            <div className="wc-my-badge-control">
              <div className="wc-my-badge-status">
                <strong
                  className={`wc-my-badge-status__title${
                    displayBadge.is_recording
                      ? ' wc-my-badge-status__title--recording'
                      : canTriggerRecording
                        ? ' wc-my-badge-status__title--idle'
                        : ' wc-my-badge-status__title--disabled'
                  }`}
                >
                  {recordingMeta.title}
                </strong>
                <span
                  className={`wc-my-badge-status__detail${
                    remoteWarningMessage ? ' wc-my-badge-status__detail--warn' : ''
                  }`}
                >
                  {statusDetail}
                </span>
              </div>

              <button
                className={`wc-my-badge-orb ${
                  displayBadge.is_recording
                    ? 'wc-my-badge-orb--stop'
                    : canTriggerRecording
                      ? 'wc-my-badge-orb--record'
                      : 'wc-my-badge-orb--disabled'
                }`}
                aria-label={actionSubmitting ? '处理中' : displayBadge.is_recording ? '停止录音' : '开始录音'}
                disabled={!canTriggerRecording || actionSubmitting}
                onClick={handlePrimaryAction}
                type="button"
              >
                <span
                  className={`wc-my-badge-orb__symbol ${
                    displayBadge.is_recording
                      ? 'wc-my-badge-orb__symbol--stop'
                      : 'wc-my-badge-orb__symbol--record'
                  }`}
                  aria-hidden="true"
                />
              </button>
            </div>

            {!displayBadge.can_control_recording ? (
              <div className="wc-my-badge-hint wc-my-badge-hint--warn">
                当前工牌已经绑定到系统账号，但钉钉侧还没完成绑定，暂时不能控制录音。
              </div>
            ) : null}
          </section>
        </>
      ) : (
        <section className="wc-card wc-card--compact">
          <div className="wc-empty">{displayBadge?.reason || '当前账号暂未绑定工牌。'}</div>
        </section>
      )}

      {managedBadges.length > 0 || managedBadgesQuery.isLoading ? (
        <section className="wc-card wc-card--compact wc-managed-badges-card">
          <div className="wc-card__head wc-card__head--subtle">
            <h2 className="wc-card__title">我管理的员工工牌</h2>
            <span className={`wc-chip ${managedRecordingCount > 0 ? 'wc-chip--danger' : 'wc-chip--default'}`}>
              {managedRecordingCount > 0 ? `${managedRecordingCount} 人录音中` : `${managedBadges.length} 人`}
            </span>
          </div>

          {managedBadgesQuery.isLoading ? (
            <div className="wc-empty">正在同步员工工牌状态…</div>
          ) : (
            <div className="wc-managed-badges-list">
              {managedBadges.map((managedBadge) => {
                const itemStatusMeta = getStatusMeta(managedBadge)
                const itemBatteryMeta = getBatteryMeta(managedBadge.battery_level)
                const itemRecordingMeta = getRecordingMeta(managedBadge)
                const itemKey = managedBadge.device_id || managedBadge.staff_id || managedBadge.device_code || managedBadge.staff_name || 'managed-badge'
                return (
                  <article
                    key={itemKey}
                    className={`wc-managed-badge${managedBadge.is_recording ? ' wc-managed-badge--recording' : ''}`}
                  >
                    <div className="wc-managed-badge__main">
                      <div className="wc-managed-badge__avatar" aria-hidden="true">
                        <MobileOutlined />
                      </div>
                      <div className="wc-managed-badge__copy">
                        <div className="wc-managed-badge__name-row">
                          <strong>{managedBadge.staff_name || '未命名员工'}</strong>
                          <span>{managedBadge.position_name || managedBadge.external_account || '--'}</span>
                        </div>
                        <p>
                          {managedBadge.bound
                            ? managedBadge.device_name || managedBadge.device_code || '已绑定工牌'
                            : managedBadge.reason || '暂未绑定工牌'}
                        </p>
                      </div>
                    </div>
                    <div className="wc-managed-badge__state">
                      <span className={`wc-my-badge-chip wc-my-badge-chip--${itemStatusMeta.tone}`}>
                        {itemStatusMeta.value}
                      </span>
                      <span className={`wc-my-badge-chip wc-my-badge-chip--${itemBatteryMeta.tone}`}>
                        {itemBatteryMeta.value}
                      </span>
                      <span className={`wc-managed-badge__recording${managedBadge.is_recording ? ' wc-managed-badge__recording--active' : ''}`}>
                        {managedBadge.is_recording ? '录音中' : itemRecordingMeta.title}
                      </span>
                    </div>
                    {managedBadge.is_recording && managedBadge.recording_started_at ? (
                      <div className="wc-managed-badge__time">
                        开始时间：{formatDateTime(managedBadge.recording_started_at)}
                      </div>
                    ) : null}
                    {managedBadge.remote_warning ? (
                      <div className="wc-managed-badge__warning">
                        {mapBadgeActionErrorMessage(managedBadge.remote_warning, managedBadge.is_recording ? 'stop' : 'start')}
                      </div>
                    ) : null}
                  </article>
                )
              })}
            </div>
          )}
        </section>
      ) : null}

      <section className="wc-card wc-card--compact wc-my-badge-account-card">
        <div className="wc-card__head wc-card__head--subtle">
          <h2 className="wc-card__title">账号与操作</h2>
          <span className="wc-chip wc-chip--default">{roleLabel(auth.user.role)}</span>
        </div>
        <div className="wc-my-badge-account-card__body">
          <div className="wc-info-list">
            <div className="wc-info-row"><label>登录账号</label><span>{auth.user.username}</span></div>
            <div className="wc-info-row"><label>员工姓名</label><span>{auth.user.staff_name || '未绑定'}</span></div>
            <div className="wc-info-row"><label>企业微信</label><span>{auth.user.staff_wecom_user_id || '未绑定'}</span></div>
          </div>
          <div className="wc-my-badge-quick-actions">
            <button
              className="wc-my-badge-quick-action"
              onClick={() => {
                void badgeQuery.refetch()
                void managedBadgesQuery.refetch()
              }}
              type="button"
            >
              <SyncOutlined />
              <span>刷新数据</span>
            </button>
            <button className="wc-my-badge-quick-action wc-my-badge-quick-action--danger" onClick={auth.logout} type="button">
              <LogoutOutlined />
              <span>退出登录</span>
            </button>
          </div>
        </div>
      </section>

      <Modal
        centered
        open={Boolean(effectivePendingAction)}
        title={`确认${pendingActionLabel}`}
        wrapClassName="wc-badge-action-modal"
        footer={[
          <button
            key="cancel"
            className="wc-btn wc-btn--ghost"
            disabled={actionSubmitting}
            onClick={handleCancelPendingAction}
            type="button"
          >
            取消
          </button>,
          <button
            key="confirm"
            className="wc-btn wc-btn--primary"
            disabled={actionSubmitting}
            onClick={handleConfirmPendingAction}
            type="button"
          >
            确认
          </button>,
        ]}
        onCancel={() => {
          if (actionSubmitting) return
          handleCancelPendingAction()
        }}
      >
        <p className="wc-modal-copy">
          {effectivePendingAction === 'stop'
            ? '确认现在停止这块工牌的录音吗？'
            : '确认现在开始这块工牌的录音吗？'}
        </p>
      </Modal>
    </div>
  )
}

export default WecomBadgePage
