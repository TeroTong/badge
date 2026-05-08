export const WECOM_LOGIN_STATE = 'smart_badge_wecom_login'
export const WECOM_DEFAULT_BADGE_PATH = '/wecom/badge'
export const WECOM_RECORDINGS_TAB_PATH = '/wecom/recordings?tab=recordings'

export function isWecomBrowser(userAgent?: string) {
  const ua =
    userAgent
    ?? (typeof window !== 'undefined' ? window.navigator.userAgent : '')
  return /wxwork/i.test(ua)
}

export function isMobileDevice(userAgent?: string, viewportWidth?: number) {
  const ua =
    userAgent
    ?? (typeof window !== 'undefined' ? window.navigator.userAgent : '')
  const width =
    viewportWidth
    ?? (typeof window !== 'undefined' ? window.innerWidth : undefined)
  return /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini|Mobile/i.test(ua)
    || (typeof width === 'number' && width < 960)
}

export function getDefaultPortalPath(options?: { preferWecom?: boolean }) {
  const preferWecom = options?.preferWecom ?? isWecomBrowser()
  return preferWecom ? WECOM_DEFAULT_BADGE_PATH : '/admin/dashboard'
}

function remapLegacyAdminRedirectToWecom(rawRedirect: string): string {
  try {
    const parsed = new URL(rawRedirect, 'http://smart-badge.local')
    const suffix = parsed.pathname.startsWith('/admin')
      ? parsed.pathname.slice('/admin'.length)
      : parsed.pathname
    const search = parsed.search
    const hash = parsed.hash

    if (!suffix || suffix === '/' || suffix === '/dashboard' || suffix === '/profile') {
      return WECOM_DEFAULT_BADGE_PATH
    }

    if (suffix === '/recordings') {
      return `/wecom/recordings${search}${hash}`
    }

    if (suffix.startsWith('/recordings/')) {
      return `/wecom${suffix}${search}${hash}`
    }

    if (suffix === '/customers' || suffix.startsWith('/customers/')) {
      return `/wecom${suffix}${search}${hash}`
    }

    if (suffix === '/visits' || suffix.startsWith('/visits/')) {
      return `/wecom${suffix}${search}${hash}`
    }
  } catch {
    return WECOM_DEFAULT_BADGE_PATH
  }

  return WECOM_DEFAULT_BADGE_PATH
}

function normalizeLegacyWecomRedirect(rawRedirect: string): string {
  try {
    const parsed = new URL(rawRedirect, 'http://smart-badge.local')
    if (parsed.pathname === '/wecom' || parsed.pathname === '/wecom/') {
      return WECOM_DEFAULT_BADGE_PATH
    }
    if (parsed.pathname === '/wecom/recordings' && !parsed.search && !parsed.hash) {
      return WECOM_DEFAULT_BADGE_PATH
    }
  } catch {
    return WECOM_DEFAULT_BADGE_PATH
  }
  return rawRedirect
}

export function normalizeRedirectPath(
  rawRedirect: string | null,
  options?: { preferWecom?: boolean },
): string {
  const defaultPath = getDefaultPortalPath(options)
  if (!rawRedirect) return defaultPath
  if (!rawRedirect.startsWith('/') || rawRedirect.startsWith('//')) return defaultPath
  if (options?.preferWecom ?? isWecomBrowser()) {
    if (rawRedirect.startsWith('/admin')) {
      return remapLegacyAdminRedirectToWecom(rawRedirect)
    }
    return normalizeLegacyWecomRedirect(rawRedirect)
  }
  return rawRedirect
}
