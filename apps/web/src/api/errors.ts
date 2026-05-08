import { HTTPError } from 'ky'

export async function getApiErrorMessage(error: unknown, fallback: string) {
  if (error instanceof HTTPError) {
    try {
      const payload = await error.response.json<{ detail?: string }>()
      if (payload?.detail) {
        return payload.detail
      }
    } catch {
      return fallback
    }
  }

  if (error instanceof Error && error.message) {
    return error.message
  }

  return fallback
}
