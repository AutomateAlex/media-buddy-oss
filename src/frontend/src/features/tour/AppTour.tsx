/**
 * AppTour — the first-time onboarding coach-mark tour (custom, no library).
 *
 * Mounted once at the app root (only renders when authenticated, since App
 * gates the router on auth). Picks the current route's stage and shows one
 * step at a time via TourOverlay. Cross-page transitions are driven here;
 * progress persists in useTourStore.
 */
import { useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useTourStore } from './useTourStore'
import { STAGES, detectStage } from './tourStages'
import { TourOverlay } from './TourOverlay'

export function AppTour() {
  const { t } = useTranslation()
  const location = useLocation()
  const navigate = useNavigate()
  const { active, seen, completed, runId, start, completeStage, skip, finish } = useTourStore()
  const [stepIndex, setStepIndex] = useState(0)

  // Auto-start on first login (this component only mounts when authenticated).
  useEffect(() => {
    if (!seen && !active) start()
  }, [seen, active, start])

  const stageKey = detectStage(location.pathname)
  const stage = stageKey ? STAGES[stageKey] : null
  const shouldRun = active && !!stage && !!stageKey && !completed.includes(stageKey)

  // Reset the step counter on a new stage/page — and on every (re)start
  // (runId) so replay always begins at the welcome step.
  useEffect(() => {
    setStepIndex(0)
  }, [stageKey, runId])

  if (!shouldRun || !stage) return null
  const safeIndex = Math.min(stepIndex, stage.steps.length - 1)
  const step = stage.steps[safeIndex]
  if (!step) return null

  const advanceOrComplete = () => {
    const nextIndex = safeIndex + 1
    if (nextIndex < stage.steps.length) {
      setStepIndex(nextIndex)
      return
    }
    if (stageKey) completeStage(stageKey)
    if (stage.onDone.type === 'navigate') navigate(stage.onDone.to)
    else if (stage.onDone.type === 'finish') finish()
    // 'wait': do nothing — the next stage runs when the user reaches its route.
  }

  return (
    <TourOverlay
      key={`${stageKey}-${safeIndex}`}
      target={step.target}
      center={step.center}
      placement={step.placement}
      title={t(step.titleKey)}
      body={t(step.bodyKey)}
      isFirst={safeIndex === 0}
      isFinal={Boolean(step.last)}
      onNext={advanceOrComplete}
      onBack={() => setStepIndex(Math.max(0, safeIndex - 1))}
      onSkip={skip}
    />
  )
}
