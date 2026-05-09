import {
  AudioOutlined,
  FileTextOutlined,
  HomeOutlined,
  IdcardOutlined,
  LeftOutlined,
  MobileOutlined,
} from '@ant-design/icons'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'

import { WECOM_RECORDINGS_TAB_PATH } from '@/app/wecom'
import { useAuth } from '@/app/use-auth'

type WecomTabItem = {
  path: string
  to?: string
  label: string
  icon: typeof HomeOutlined
}

type WecomShellMeta = {
  eyebrow: string
  title: string
  backTarget: string | null
  showTabBar: boolean
  activeTabPath?: string | null
}

const TAB_ITEMS: WecomTabItem[] = [
  { path: '/wecom/badge', label: '工牌', icon: MobileOutlined },
  { path: '/wecom/recordings', to: WECOM_RECORDINGS_TAB_PATH, label: '录音', icon: AudioOutlined },
  { path: '/wecom/sap-reviews', label: 'SAP', icon: FileTextOutlined },
  { path: '/wecom/customers', label: '客户', icon: IdcardOutlined },
  { path: '/wecom/overview', label: '总览', icon: HomeOutlined },
]

function resolveShellMeta(pathname: string, search: string): WecomShellMeta {
  const searchParams = new URLSearchParams(search)
  const explicitBackTarget = (() => {
    const raw = (searchParams.get('back_to') || '').trim()
    if (!raw.startsWith('/wecom/')) return null
    return raw
  })()
  const fromVisitId = searchParams.get('from_visit_id')
  const fromCustomerId = searchParams.get('from_customer_id')
  const fromRecordingId = searchParams.get('from_recording_id')
  const archiveItemId = searchParams.get('archive_item_id')
  const fromRecordingMatch = searchParams.get('from_recording_match') === '1'
  const filteredVisitId = searchParams.get('visit_id')
  const fromKeyword = searchParams.get('from_keyword')
  const fromPage = searchParams.get('from_page')
  const inferActiveTabPath = (target: string | null | undefined) => {
    if (!target) return null
    if (target.startsWith('/wecom/recordings')) return '/wecom/recordings'
    if (target.startsWith('/wecom/sap-reviews')) return '/wecom/sap-reviews'
    if (target.startsWith('/wecom/customers')) return '/wecom/customers'
    if (target.startsWith('/wecom/overview')) return '/wecom/overview'
    if (target.startsWith('/wecom/badge')) return '/wecom/badge'
    if (target.startsWith('/wecom/visits')) return '/wecom/visits'
    return null
  }

  if (/^\/wecom\/recordings\/[^/]+$/.test(pathname)) {
    const visitBackTarget = fromVisitId
      ? (() => {
          const params = new URLSearchParams()
          if (fromCustomerId) params.set('from_customer_id', fromCustomerId)
          return `/wecom/visits/${fromVisitId}${params.toString() ? `?${params.toString()}` : ''}`
        })()
      : null
    return {
      eyebrow: '录音复盘',
      title: '录音详情',
      backTarget: explicitBackTarget
        ? explicitBackTarget
        : visitBackTarget
        ? visitBackTarget
        : filteredVisitId
          ? `/wecom/recordings?visit_id=${filteredVisitId}`
          : '/wecom/recordings',
      showTabBar: true,
      activeTabPath: inferActiveTabPath(explicitBackTarget) ?? '/wecom/recordings',
    }
  }
  if (/^\/wecom\/visits\/[^/]+$/.test(pathname)) {
    const recordingBackTarget = (() => {
      if (!fromRecordingMatch || !fromRecordingId) return null
      const params = new URLSearchParams()
      if (archiveItemId) params.set('archive_item_id', archiveItemId)
      if (fromVisitId) params.set('from_visit_id', fromVisitId)
      if (fromCustomerId) params.set('from_customer_id', fromCustomerId)
      return `/wecom/recordings/${fromRecordingId}${params.toString() ? `?${params.toString()}` : ''}`
    })()
    return {
      eyebrow: '接诊档案',
      title: '接诊详情',
      backTarget: explicitBackTarget
        ? explicitBackTarget
        : recordingBackTarget
        ? recordingBackTarget
        : fromCustomerId
          ? `/wecom/customers/${fromCustomerId}`
          : '/wecom/visits',
      showTabBar: true,
      activeTabPath: inferActiveTabPath(explicitBackTarget)
        ?? (recordingBackTarget
        ? '/wecom/recordings'
        : fromCustomerId
          ? '/wecom/customers'
          : '/wecom/visits'),
    }
  }
  if (/^\/wecom\/customers\/[^/]+$/.test(pathname)) {
    const listBackTarget = (() => {
      if (fromKeyword || fromPage) {
        const params = new URLSearchParams()
        if (fromKeyword) params.set('q', fromKeyword)
        if (fromPage) params.set('page', fromPage)
        return `/wecom/customers${params.toString() ? `?${params.toString()}` : ''}`
      }
      return '/wecom/customers'
    })()
    return {
      eyebrow: '客户画像',
      title: '客户档案',
      backTarget: explicitBackTarget
        ? explicitBackTarget
        : fromVisitId
          ? `/wecom/visits/${fromVisitId}`
        : fromRecordingId
          ? `/wecom/recordings/${fromRecordingId}`
          : listBackTarget,
      showTabBar: true,
      activeTabPath: inferActiveTabPath(explicitBackTarget) ?? '/wecom/customers',
    }
  }
  if (/^\/wecom\/sap-reviews\/[^/]+$/.test(pathname)) {
    return {
      eyebrow: 'SAP回写',
      title: '咨询备注',
      backTarget: explicitBackTarget ?? '/wecom/sap-reviews',
      showTabBar: true,
      activeTabPath: '/wecom/sap-reviews',
    }
  }
  const matched = TAB_ITEMS.find(
    (item) => pathname === item.path || pathname.startsWith(`${item.path}/`),
  )
  if (matched) {
    if (matched.path === '/wecom/badge') {
      return { eyebrow: '我的设备', title: '工牌与账号', backTarget: null, showTabBar: true, activeTabPath: matched.path }
    }
    if (matched.path === '/wecom/recordings') {
      return { eyebrow: '录音与关联', title: '录音中心', backTarget: null, showTabBar: true, activeTabPath: matched.path }
    }
    if (matched.path === '/wecom/customers') {
      return { eyebrow: '客户与画像', title: '客户档案', backTarget: null, showTabBar: true, activeTabPath: matched.path }
    }
    if (matched.path === '/wecom/sap-reviews') {
      return { eyebrow: 'SAP回写', title: '咨询备注', backTarget: null, showTabBar: true, activeTabPath: matched.path }
    }
    if (matched.path === '/wecom/overview') {
      return { eyebrow: '数据看板', title: '业务总览', backTarget: null, showTabBar: true, activeTabPath: matched.path }
    }
  }
  return {
    eyebrow: '企业微信工作台',
    title: '朗姿智能工牌',
    backTarget: null,
    showTabBar: true,
    activeTabPath: null,
  }
}

export function WecomShell() {
  const auth = useAuth()
  const location = useLocation()
  const navigate = useNavigate()
  const meta = resolveShellMeta(location.pathname, location.search)
  const backTarget = meta.backTarget
  const canHistoryBack = typeof window !== 'undefined'
    && typeof window.history.state?.idx === 'number'
    && window.history.state.idx > 0
  const shouldUseHistoryBack =
    /^\/wecom\/visits\/[^/]+$/.test(location.pathname) && canHistoryBack

  const isTabItemActive = (path: string) => {
    if (meta.activeTabPath) return meta.activeTabPath === path
    return location.pathname === path || location.pathname.startsWith(`${path}/`)
  }

  return (
    <div className="wc-shell">
      <header className="wc-header">
        <div className="wc-header__inner">
          {backTarget ? (
            <button
              className="wc-header__back"
              onClick={() => {
                if (shouldUseHistoryBack) {
                  navigate(-1)
                  return
                }
                navigate(backTarget)
              }}
              type="button"
            >
              <LeftOutlined />
            </button>
          ) : (
            <div className="wc-header__logo">朗姿</div>
          )}
          <div className="wc-header__copy">
            <span className="wc-header__eyebrow">{meta.eyebrow}</span>
            <h1 className="wc-header__title">{meta.title}</h1>
          </div>
          {!backTarget && auth.status === 'authenticated' ? (
            <NavLink className="wc-header__avatar" to="/wecom/badge">
              {(auth.user.display_name || auth.user.username).slice(0, 1).toUpperCase()}
            </NavLink>
          ) : (
            <span className="wc-header__spacer" />
          )}
        </div>
      </header>

      <main className={`wc-body${meta.showTabBar ? ' wc-body--tabbed' : ''}`}>
        <Outlet />
      </main>

      {meta.showTabBar && (
        <nav className="wc-tabbar">
          <div className="wc-tabbar__inner">
            {TAB_ITEMS.map((item) => {
              const Icon = item.icon
              const isActive = isTabItemActive(item.path)
              return (
                <NavLink
                  key={item.path}
                  className={() => `wc-tabbar__item${isActive ? ' wc-tabbar__item--active' : ''}`}
                  to={item.to ?? item.path}
                >
                  <Icon />
                  <span>{item.label}</span>
                </NavLink>
              )
            })}
          </div>
        </nav>
      )}
    </div>
  )
}
