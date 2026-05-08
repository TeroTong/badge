import { createContext } from 'react'

import type { User } from '@/api/auth'

export class AuthRequestError extends Error {
  code: 'invalid_credentials' | 'service_unavailable' | 'server_error' | 'unknown'

  constructor(
    code: 'invalid_credentials' | 'service_unavailable' | 'server_error' | 'unknown',
    message: string,
  ) {
    super(message)
    this.name = 'AuthRequestError'
    this.code = code
  }
}

export type AuthState =
  | { status: 'loading' }
  | { status: 'authenticated'; user: User }
  | { status: 'unauthenticated' }

export type AuthContextValue = AuthState & {
  login: (username: string, password: string) => Promise<void>
  loginWithWecomCode: (code: string) => Promise<void>
  logout: () => void
  refreshUser: () => Promise<void>
}

export const AuthContext = createContext<AuthContextValue | null>(null)
