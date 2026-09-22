import { useEffect, useState, useCallback } from 'react'
import { api } from '../api/client'

export interface AuthStatus {
  authenticated: boolean
  plan: string | null
  sub_status: string | null
  /** Optional identity for the sidebar widget; null in this build. */
  email: string | null
  display_name: string | null
  /** Absolute path to local avatar file (~/.media-buddy-oss/avatar.png).
   * Renderer wraps with `file://` to load via <img>. Null when the user
   * hasn't uploaded one. Persists across login/logout — staying local
   * means it survives a logout without re-uploading. */
  avatar_path: string | null
  /** Scheduled-maintenance notice for the whole app. Ops-controlled via a
   * backend flag file (flippable without redeploy). Null unless a window is
   * active. Shown as a dismissible banner in AppShell. */
  maintenance: {
    start: string | null
    end: string | null
    message_zh: string | null
    message_en: string | null
  } | null
}

export function useAuthState() {
  const [status, setStatus] = useState<AuthStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      // Shared client supplies baseURL (env-aware: local on desktop, cloud
      // on web) + X-MediaBuddy-Token + (web) Authorization: Bearer. The
      // multi-tenant backend reads the user from the Bearer sub.
      const data = await api.auth.status()
      setStatus(data as AuthStatus)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'unknown'
      setError(msg)
      const signedOutStatus = {
        authenticated: false,
        plan: null,
        sub_status: null,
        email: null,
        display_name: null,
        avatar_path: null,
        maintenance: null,
      }
      // A failed status check can be a temporary local/backend/network blip.
      // If the app already knows the user is signed in, keep that state until
      // the backend explicitly reports authenticated=false. This prevents
      // long batch runs from being interrupted by a transient status poll.
      setStatus((prev) => (prev?.authenticated ? prev : signedOutStatus))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  return { status, loading, error, refresh }
}
