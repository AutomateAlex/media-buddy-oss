import { useState, useEffect, useRef } from 'react'
import { api } from '../api/client'
import type { PipelineStatus } from '../types'

export function usePipeline(projectId: string | null) {
  const [status, setStatus] = useState<PipelineStatus | null>(null)
  const [isPolling, setIsPolling] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const startPipeline = async () => {
    if (!projectId) return
    try {
      await api.pipeline.run(projectId)
      setIsPolling(true)
      setError(null)
    } catch (e: any) {
      setError(e?.response?.data?.detail || e.message || 'Failed to start')
    }
  }

  useEffect(() => {
    if (!projectId) return
    let cancelled = false

    const poll = async () => {
      try {
        const s = await api.pipeline.status(projectId)
        if (cancelled) return
        setStatus(s)
        const done = s.overall_status === 'completed' || s.overall_status === 'failed'
        setIsPolling(!done)
        if (done && intervalRef.current) {
          clearInterval(intervalRef.current)
          intervalRef.current = null
        }
      } catch (e) {
        // swallow polling errors silently
      }
    }

    // 挂载即建立 2 秒轮询,只要不是终态(completed/failed)就一直轮 —— 修此前"任务在
    // queued/claimed 态落地项目页时轮询根本不启动(isPolling 初值 false + 仅 running 才
    poll()
    intervalRef.current = setInterval(poll, 2000)
    return () => {
      cancelled = true
      if (intervalRef.current) {
        clearInterval(intervalRef.current)
        intervalRef.current = null
      }
    }
  }, [projectId])

  return { status, startPipeline, isPolling, error }
}
