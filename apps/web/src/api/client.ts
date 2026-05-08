import ky from 'ky'

/** 从 localStorage 读取 JWT 令牌 */
export function getToken(): string | null {
  return localStorage.getItem('access_token')
}

export function setToken(token: string): void {
  localStorage.setItem('access_token', token)
}

export function clearToken(): void {
  localStorage.removeItem('access_token')
  localStorage.removeItem('refresh_token')
}

export function getRefreshToken(): string | null {
  return localStorage.getItem('refresh_token')
}

export function setRefreshToken(token: string): void {
  localStorage.setItem('refresh_token', token)
}

function isWecomBrowser(): boolean {
  return /wxwork/i.test(window.navigator.userAgent)
}

function buildLoginUrl(): string {
  const redirect = `${window.location.pathname}${window.location.search}${window.location.hash}`
  const params = new URLSearchParams({ redirect })
  if (isWecomBrowser()) {
    params.set('wecom', '1')
  }
  return `/login?${params.toString()}`
}

/** 通用分页响应类型 */
export type PaginatedResponse<T> = {
  items: T[]
  total: number
  page: number
  page_size: number
  pages: number
}

/**
 * 尝试用 refresh_token 换取新的 access_token。
 * 成功返回 true, 失败返回 false。
 */
let refreshPromise: Promise<boolean> | null = null

async function tryRefresh(): Promise<boolean> {
  const rt = getRefreshToken()
  if (!rt) return false

  // 避免并发刷新
  if (refreshPromise) return refreshPromise

  refreshPromise = (async () => {
    try {
      const res = await ky.post('/api/v1/auth/refresh', { json: { refresh_token: rt } }).json<{
        access_token: string
        refresh_token: string
      }>()
      setToken(res.access_token)
      setRefreshToken(res.refresh_token)
      return true
    } catch {
      clearToken()
      return false
    } finally {
      refreshPromise = null
    }
  })()

  return refreshPromise
}

/**
 * 全局 ky 实例 — 自动附加 Authorization 请求头，
 * 并在 401 时尝试刷新令牌。
 */
export const api = ky.create({
  prefixUrl: '/api/v1',
  retry: {
    limit: 1,
    statusCodes: [401],
  },
  hooks: {
    beforeRequest: [
      (request) => {
        const token = getToken()
        if (token) {
          request.headers.set('Authorization', `Bearer ${token}`)
        }
      },
    ],
    beforeRetry: [
      async ({ request }) => {
        // beforeRetry only fires for statusCodes we configured (401)
        const refreshed = await tryRefresh()
        if (refreshed) {
          request.headers.set('Authorization', `Bearer ${getToken()}`)
        } else {
          // 刷新失败，跳转登录
          if (!window.location.pathname.startsWith('/login')) {
            window.location.href = buildLoginUrl()
          }
          throw new Error('refresh failed')
        }
      },
    ],
    afterResponse: [
      (_request, _options, response) => {
        // 最终响应仍然 401（说明刷新也失败了），跳登录
        if (response.status === 401) {
          clearToken()
          if (!window.location.pathname.startsWith('/login')) {
            window.location.href = buildLoginUrl()
          }
        }
      },
    ],
  },
})
