import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { api } from '../api/client'
import type { StudioSession, StudioSpec } from '../types'
import { VoicePicker } from '../components/VoicePicker'

const ACTIVE_SESSION_KEY = 'media-buddy.studio.active-session-id'
const DRAFT_INPUT_KEY = 'media-buddy.studio.draft-input'

// First-draft turns call out to the web for research + fact-checking and can
// take ~1 minute (single blocking request, no streaming). We detect that
// specific slow turn so we can show staged progress instead of a static
// "thinking..." that reads as hung. Detection: the spec direction is already
// locked (status === 'confirming') and no assistant message yet carries a
// draft marker like "[第 1 稿 · ...]" — once a draft exists, later turns are
// fast edits/chat and keep the plain placeholder.
const DRAFT_MARKER_RE = /第\s*\d+\s*稿/

function hasDraftMarker(session: StudioSession): boolean {
  return session.messages.some(
    (m) => m.role === 'assistant' && typeof m.content === 'string' && DRAFT_MARKER_RE.test(m.content),
  )
}

function researchStageKey(elapsedSeconds: number): string {
  if (elapsedSeconds < 20) return 'studio.stageSearching'
  if (elapsedSeconds < 40) return 'studio.stageVerifying'
  return 'studio.stageWriting'
}

// Long-video async draft: the backend enqueues a background grounded long-script
// job (spec.pending_long_draft.stage advances queued→researching→drafting→
// quality_checking→completed/failed). While a stage is active the UI polls and
// shows staged progress; the finished draft arrives as a new assistant message.
const LONG_ACTIVE_STAGES = ['queued', 'researching', 'drafting', 'quality_checking']
const LONG_STAGE_KEY: Record<string, string> = {
  queued: 'studio.stageSearching',
  researching: 'studio.stageSearching',
  drafting: 'studio.stageWriting',
  quality_checking: 'studio.longStageChecking',
}
function pendingLongStage(session: StudioSession | null): string | null {
  const st = (session?.spec as any)?.pending_long_draft?.stage
  return st && LONG_ACTIVE_STAGES.includes(st) ? st : null
}

export function Studio() {
  const nav = useNavigate()
  const { t } = useTranslation()
  const [session, setSession] = useState<StudioSession | null>(null)
  const [input, setInput] = useState(() => localStorage.getItem(DRAFT_INPUT_KEY) || '')
  const [sending, setSending] = useState(false)
  const [researchWait, setResearchWait] = useState(false)
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const messagesEndRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)

  const focusInput = () => {
    window.setTimeout(() => inputRef.current?.focus(), 0)
  }

  // Resume the active Studio conversation across route changes. Only create a
  // new session when there is no usable draft conversation.
  useEffect(() => {
    let cancelled = false

    async function load() {
      const savedId = localStorage.getItem(ACTIVE_SESSION_KEY)
      try {
        if (savedId) {
          const existing = await api.studio.getSession(savedId)
          if (!cancelled && existing.status !== 'submitted' && existing.status !== 'aborted') {
            setSession(existing)
            focusInput()
            return
          }
          localStorage.removeItem(ACTIVE_SESSION_KEY)
        }
        const created = await api.studio.createSession({})
        if (!cancelled) {
          localStorage.setItem(ACTIVE_SESSION_KEY, created.id)
          setSession(created)
          focusInput()
        }
      } catch (e: any) {
        if (!cancelled) {
          setError(e?.response?.data?.detail || e.message)
        }
      }
    }

    load()
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    if (session?.id && session.status !== 'submitted' && session.status !== 'aborted') {
      localStorage.setItem(ACTIVE_SESSION_KEY, session.id)
    }
    if (session?.status === 'submitted' || session?.status === 'aborted') {
      localStorage.removeItem(ACTIVE_SESSION_KEY)
      localStorage.removeItem(DRAFT_INPUT_KEY)
    }
  }, [session?.id, session?.status])

  useEffect(() => {
    if (input) localStorage.setItem(DRAFT_INPUT_KEY, input)
    else localStorage.removeItem(DRAFT_INPUT_KEY)
  }, [input])

  // Auto-scroll to bottom on new message
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
    if (session?.status !== 'submitted') focusInput()
  }, [session?.messages?.length])

  useEffect(() => {
    if (!sending && session?.status !== 'submitted') focusInput()
  }, [sending, session?.status])

  // Tick the elapsed-seconds counter that drives the staged research banner.
  // Only runs while a first-draft research turn is in flight.
  useEffect(() => {
    if (!sending || !researchWait) return
    const timer = window.setInterval(() => {
      setElapsedSeconds((s) => s + 1)
    }, 1000)
    return () => window.clearInterval(timer)
  }, [sending, researchWait])

  // Long-video async draft: while a background long-script job is running
  // (spec.pending_long_draft.stage active), poll the session every 4s so the
  // staged progress advances and the finished draft appears automatically —
  // the user never has to send another message. Survives refresh (state is on
  // the session row). Stops once the stage leaves the active set.
  useEffect(() => {
    if (!session || !pendingLongStage(session)) return
    let cancelled = false
    const timer = window.setInterval(async () => {
      try {
        const fresh = await api.studio.getSession(session.id)
        if (!cancelled) setSession(fresh)
      } catch {
        /* transient network error — keep polling */
      }
    }, 4000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [session?.id, (session?.spec as any)?.pending_long_draft?.stage])

  // If session ended (submitted) and a project was created, jump to it
  useEffect(() => {
    if (!session) return
    if (session.status === 'submitted') {
      const pid = session.created_project_ids?.[0]
      const bid = session.created_batch_run_id
    if (pid) {
      const t = setTimeout(() => nav(`/project/${pid}`), 2500)
      return () => clearTimeout(t)
      }
    }
  }, [session?.status, session?.created_project_ids, session?.created_batch_run_id, nav, session?.series_id])

  const send = async (e?: React.FormEvent) => {
    e?.preventDefault()
    if (!session || !input.trim() || sending) return
    const text = input.trim()
    const isResearchWait = session.status === 'confirming' && !hasDraftMarker(session)
    setInput('')
    setSending(true)
    setResearchWait(isResearchWait)
    setElapsedSeconds(0)
    setError(null)
    try {
      const res = await api.studio.sendMessage(session.id, text)
      setSession(res.session)
    } catch (err: any) {
      setError(err?.response?.data?.detail || err.message)
    } finally {
      setSending(false)
      setResearchWait(false)
      focusInput()
    }
  }

  if (!session) {
    return (
      <>
        <header className="page-header">
          <div className="crumb"><span className="here">{t('studio.title')}</span></div>
        </header>
        <div className="content-card">
          <p className="hint">{error || t('studio.initializing')}</p>
        </div>
      </>
    )
  }

  return (
    <>
      <header className="page-header">
        <div className="crumb">
          <span className="seg">Studio</span>
          <span className="sep">/</span>
          <span className="here">{t('studio.title')}</span>
        </div>
      </header>

      <div className="studio-layout">
        <div className="studio-chat">
          <header className="page-head">
            <div>
              <h1>{t('studio.assistantTitle')}</h1>
              <p className="lede">{t('studio.subtitle')}</p>
            </div>
          </header>

          <div className="chat-messages">
            {session.messages.length === 0 && (
              <div className="chat-empty">
                <p>👋 {t('studio.emptyGreeting')}</p>
                <p className="hint">
                  {t('studio.emptyHint')}
                </p>
              </div>
            )}
            {session.messages.map((m, i) => (
              <ChatBubble key={i} message={m} />
            ))}
            {sending && researchWait && (
              <div className="chat-bubble assistant thinking">
                <div className="chat-bubble-role">🤖 {t('studio.ai')}</div>
                <div className="chat-bubble-content">
                  <div>⏳ {t('studio.researchTopBanner')}</div>
                  <div>{t(researchStageKey(elapsedSeconds))}</div>
                  <div className="hint">{t('studio.waitedSeconds', { seconds: elapsedSeconds })}</div>
                </div>
              </div>
            )}
            {pendingLongStage(session) && (
              <div className="chat-bubble assistant thinking">
                <div className="chat-bubble-role">🤖 {t('studio.ai')}</div>
                <div className="chat-bubble-content">
                  <div>⏳ {t('studio.longDraftBanner')}</div>
                  <div>{t(LONG_STAGE_KEY[pendingLongStage(session)!] || 'studio.stageWriting')}</div>
                </div>
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>

          <form className="chat-input-row" onSubmit={send}>
            <input
              ref={inputRef}
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder={
                session.status === 'submitted'
                  ? t('studio.submittedPlaceholder')
                  : pendingLongStage(session)
                    ? t('studio.longDraftPlaceholder')
                    : sending
                      ? t('studio.thinkingPlaceholder')
                      : t('studio.inputPlaceholder')
              }
              disabled={sending || !!pendingLongStage(session) || session.status === 'submitted'}
              autoFocus
            />
            <button
              type="submit"
              className="btn-primary"
              disabled={sending || !!pendingLongStage(session) || !input.trim() || session.status === 'submitted'}
            >
              {sending ? '...' : t('studio.send')}
            </button>
          </form>

          {error && <div className="error-banner">{error}</div>}
        </div>

        <aside className="studio-spec">
          <SpecCard spec={session.spec} status={session.status} />
          <VoicePicker />
        </aside>
      </div>
    </>
  )
}

function ChatBubble({ message }: { message: any }) {
  const { t } = useTranslation()
  const role = message.role
  if (role === 'tool' || role === 'system') return null
  const isAssistant = role === 'assistant'
  const isBlocked = message.blocked_by_safety
  return (
    <div className={`chat-bubble ${isAssistant ? 'assistant' : 'user'}${isBlocked ? ' blocked' : ''}`}>
      <div className="chat-bubble-role">
        {isAssistant ? `🤖 ${t('studio.ai')}` : `🧑 ${t('studio.you')}`}
        {isBlocked && <span className="safety-tag"> · {t('studio.safetyBlocked')}</span>}
      </div>
      <div className="chat-bubble-content">{message.content}</div>
      {message.tool_call && (
        <div className="chat-tool-call">
          🔧 {t('studio.toolCalled')} <code>{message.tool_call.name}</code>
          {message.tool_call.error && <span className="tool-error"> · {t('studio.failed')}: {message.tool_call.error}</span>}
        </div>
      )}
    </div>
  )
}

function SpecCard({ spec, status }: { spec: StudioSpec; status: string }) {
  const { t } = useTranslation()
  const Field = ({ label, value }: { label: string; value: string | number | null | undefined }) => (
    <div className="spec-row">
      <span className="spec-label">{label}</span>
      <span className={`spec-value${value ? '' : ' empty'}`}>{value || '—'}</span>
    </div>
  )

  const aspectLabel = spec.aspect_ratio === '9:16' ? `9:16 (${t('studio.portrait')})`
    : spec.aspect_ratio === '16:9' ? `16:9 (${t('studio.landscape')})`
    : spec.aspect_ratio || null

  return (
    <div className="spec-card">
      <h3>📋 {t('studio.capturedInfo')}</h3>
      <Field label={t('studio.type')} value={spec.video_type} />
      <Field label={t('studio.direction')} value={spec.direction} />
      <Field label={t('studio.platform')} value={spec.platform} />
      <Field label={t('studio.aspectRatio')} value={aspectLabel} />
      <Field label={t('studio.duration')} value={spec.duration_seconds ? `${spec.duration_seconds}s` : null} />
      <Field label={t('studio.count')} value={spec.count} />
      <hr />
      <Field label={t('studio.referenceVideo')} value={spec.reference_video_url ? t('studio.provided') : null} />
      <Field label={t('studio.localAssets')} value={spec.own_assets_folder} />
      <Field label={t('studio.seriesChannel')} value={spec.series_id ? t('studio.bound') : null} />
      <Field label={t('studio.voice')} value={spec.voice} />
      <hr />
      <div className="spec-status">
        <span className="spec-label">{t('studio.status')}</span>
        <span className={`status-pill status-${status}`}>{translateStatus(status, t)}</span>
      </div>
      {spec.topics && spec.topics.length > 0 && (
        <>
          <hr />
          <div className="spec-row spec-block">
            <span className="spec-label">{t('studio.topics')}</span>
            <ol className="spec-topics">
              {spec.topics.map((t, i) => <li key={i}>{t}</li>)}
            </ol>
          </div>
        </>
      )}
    </div>
  )
}

function translateStatus(s: string, t: (key: string) => string): string {
  return ({
    gathering: t('studio.statusGathering'),
    confirming: t('studio.statusConfirming'),
    submitted: `✓ ${t('studio.statusSubmitted')}`,
    aborted: t('studio.statusAborted'),
  } as Record<string, string>)[s] || s
}
