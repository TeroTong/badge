import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { HTTPError, TimeoutError } from 'ky'

import * as authApi from '@/api/auth'
import type { TokenResponse } from '@/api/auth'
import { clearToken, getToken, setRefreshToken, setToken } from '@/api/client'
import { AuthContext, AuthRequestError, type AuthState } from '@/app/auth-store'

async function normalizeAuthError(error: unknown, invalidCredentialMessage: string): Promise<AuthRequestError> {
  if (error instanceof AuthRequestError) {
    return error
  }

  if (error instanceof HTTPError) {
    let detail = ''
    try {
      const payload = (await error.response.clone().json()) as { detail?: string }
      detail = payload?.detail ?? ''
    } catch {
      detail = ''
    }
    if (error.response.status === 401) {
      return new AuthRequestError('invalid_credentials', detail || invalidCredentialMessage)
    }
    if (error.response.status === 400 || error.response.status === 403) {
      return new AuthRequestError('unknown', detail || '登录失败，请稍后重试')
    }
    if (error.response.status >= 500) {
      return new AuthRequestError('server_error', detail || '登录服务异常，请稍后重试')
    }
  }

  if (error instanceof TimeoutError || error instanceof TypeError) {
    return new AuthRequestError('service_unavailable', '后端服务未启动或不可达，请先确认 API 服务')
  }

  return new AuthRequestError('unknown', '登录失败，请稍后重试')
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>(() =>
    getToken() ? { status: 'loading' } : { status: 'unauthenticated' },
  )

  const completeLogin = useCallback(async (tokens: TokenResponse) => {
    setToken(tokens.access_token)
    setRefreshToken(tokens.refresh_token)

    try {
      const user = await authApi.getMe()
      setState({ status: 'authenticated', user })
    } catch {
      clearToken()
      setState({ status: 'unauthenticated' })
      throw new AuthRequestError('unknown', '登录成功但用户信息读取失败，请稍后重试')
    }
  }, [])

  useEffect(() => {
    const token = getToken()
    if (!token) {
      return
    }

    let cancelled = false

    authApi
      .getMe()
      .then((user) => {
        if (!cancelled) {
          setState({ status: 'authenticated', user })
        }
      })
      .catch(() => {
        clearToken()
        if (!cancelled) {
          setState({ status: 'unauthenticated' })
        }
      })

    return () => {
      cancelled = true
    }
  }, [])

  const login = useCallback(async (username: string, password: string) => {
    try {
      const tokens = await authApi.login(username, password)
      await completeLogin(tokens)
    } catch (error) {
      throw await normalizeAuthError(error, '用户名或密码错误')
    }
  }, [completeLogin])

  const loginWithWecomCode = useCallback(async (code: string) => {
    try {
      const tokens = await authApi.loginWithWecomCode(code)
      await completeLogin(tokens)
    } catch (error) {
      throw await normalizeAuthError(error, '企业微信登录失败，请稍后重试')
    }
  }, [completeLogin])

  const refreshUser = useCallback(async () => {
    try {
      const user = await authApi.getMe()
      setState({ status: 'authenticated', user })
    } catch {
      clearToken()
      setState({ status: 'unauthenticated' })
    }
  }, [])

  const logout = useCallback(() => {
    clearToken()
    setState({ status: 'unauthenticated' })
  }, [])

  return (
    <AuthContext.Provider value={{ ...state, login, loginWithWecomCode, logout, refreshUser }}>
      {children}
    </AuthContext.Provider>
  )
}
