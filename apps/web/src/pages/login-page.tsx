import { useEffect, useMemo, useState, type FormEvent } from 'react'

import * as authApi from '@/api/auth'
import { getApiErrorMessage } from '@/api/errors'
import { AuthRequestError } from '@/app/auth-store'
import { useAuth } from '@/app/use-auth'
import { isWecomBrowser, normalizeRedirectPath, WECOM_LOGIN_STATE } from '@/app/wecom'

export function LoginPage() {
  const { login, loginWithWecomCode } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [wecomLoading, setWecomLoading] = useState(false)
  const [wecomAttempted, setWecomAttempted] = useState(false)

  const searchParams = useMemo(() => new URLSearchParams(window.location.search), [])
  const wecomCode = searchParams.get('code')
  const wecomState = searchParams.get('state')
  const forceWecom = searchParams.get('wecom') === '1' || wecomState === WECOM_LOGIN_STATE
  const shouldUseWecom = forceWecom || isWecomBrowser()
  const redirectPath = normalizeRedirectPath(searchParams.get('redirect'), { preferWecom: shouldUseWecom })

  useEffect(() => {
    if (!shouldUseWecom || wecomAttempted) {
      return
    }

    let cancelled = false

    const run = async () => {
      setWecomLoading(true)
      setError('')

      if (wecomCode) {
        try {
          await loginWithWecomCode(wecomCode)
          if (!cancelled) {
            window.location.replace(redirectPath)
          }
          return
        } catch (authError) {
          if (!cancelled) {
            const message =
              authError instanceof AuthRequestError ? authError.message : '企业微信登录失败，请稍后重试'
            setError(message)
            setWecomAttempted(true)
            setWecomLoading(false)
          }
          return
        }
      }

      try {
        const { authorize_url } = await authApi.getWecomAuthorizeUrl(redirectPath)
        if (!cancelled) {
          window.location.replace(authorize_url)
        }
      } catch (authorizeError) {
        if (!cancelled) {
          setError(await getApiErrorMessage(authorizeError, '企业微信免密登录不可用，请联系管理员检查配置'))
          setWecomAttempted(true)
          setWecomLoading(false)
        }
      }
    }

    void run()

    return () => {
      cancelled = true
    }
  }, [loginWithWecomCode, redirectPath, shouldUseWecom, wecomAttempted, wecomCode])

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await login(username, password)
      window.location.replace(redirectPath)
    } catch (authError) {
      if (authError instanceof AuthRequestError) {
        setError(authError.message)
      } else {
        setError('登录失败，请稍后重试')
      }
    } finally {
      setLoading(false)
    }
  }

  const handleRetryWecomLogin = async () => {
    setWecomAttempted(false)
    setWecomLoading(true)
    setError('')
    try {
      const { authorize_url } = await authApi.getWecomAuthorizeUrl(redirectPath)
      window.location.replace(authorize_url)
    } catch (authorizeError) {
      setError(await getApiErrorMessage(authorizeError, '企业微信免密登录不可用，请联系管理员检查配置'))
      setWecomAttempted(true)
      setWecomLoading(false)
    }
  }

  return (
    <div className="login-page">
      <div className="login-shell">
        <form className="login-card login-card--single" onSubmit={handleSubmit}>
          <div className="login-brand login-brand--simple login-brand--single">
            <div className="login-brand__mark" aria-hidden="true">
              <span className="login-brand__mark-top">朗姿</span>
              <span className="login-brand__mark-bottom" />
            </div>
            <div className="login-brand__copy">
              <p className="eyebrow">朗姿智能工牌</p>
              <h1>朗姿智能工牌系统</h1>
              <p>管理后台登录入口</p>
            </div>
          </div>

          <div className="login-header">
            <p className="eyebrow">Sign In</p>
            <h2>登录管理后台</h2>
            <p>使用系统账号登录。</p>
          </div>

          {shouldUseWecom ? (
            <div className="login-notice login-notice--info">
              检测到企业微信环境，系统会优先尝试企业微信免密登录。
            </div>
          ) : null}

          {error ? <div className="login-notice login-notice--error">{error}</div> : null}

          {wecomLoading ? (
            <div className="login-notice login-notice--loading">企业微信登录中，请稍候…</div>
          ) : null}

          <label className="login-field">
            <span>用户名</span>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoFocus
              required
            />
          </label>

          <label className="login-field">
            <span>密码</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>

          <div className="login-actions">
            <button className="login-btn" type="submit" disabled={loading || wecomLoading}>
              {loading ? '登录中…' : '登录'}
            </button>

            {shouldUseWecom ? (
              <button
                className="login-btn login-btn--secondary"
                type="button"
                disabled={wecomLoading}
                onClick={() => void handleRetryWecomLogin()}
              >
                {wecomLoading ? '企业微信登录中…' : '重新发起企业微信登录'}
              </button>
            ) : null}
          </div>
        </form>
      </div>
    </div>
  )
}

export default LoginPage
