/**
 * First-time onboarding tour state.
 *
 * The tour is route-aware and progressive: it runs one "stage" per page and
 * resumes as the user actually moves through the create-first-video flow
 * (channels → create → channel hub → workspace). Because AuthStatus has no
 * server-side onboarding flag, "seen" is persisted to localStorage only —
 * same trade-off as the existing AiGenerationNotice (re-shows per browser).
 */
import { create } from 'zustand'
import { persist } from 'zustand/middleware'

interface TourState {
  /** true once the user finished or skipped the whole tour — gates auto-start. */
  seen: boolean
  /** the tour is currently running (spans several pages/navigations). */
  active: boolean
  /** stage keys already completed, so a finished stage never re-runs. */
  completed: string[]
  /** bumped on every (re)start so the step counter resets to the welcome step,
   *  even when replay is triggered while already on the channels page. */
  runId: number
  start: () => void
  completeStage: (key: string) => void
  skip: () => void
  finish: () => void
  /** re-run from scratch (the "使用引导" button in the sidebar). */
  replay: () => void
}

export const useTourStore = create<TourState>()(
  persist(
    (set) => ({
      seen: false,
      active: false,
      completed: [],
      runId: 0,
      start: () => set((s) => ({ active: true, runId: s.runId + 1 })),
      completeStage: (key) =>
        set((s) => (s.completed.includes(key) ? s : { completed: [...s.completed, key] })),
      skip: () => set({ active: false, seen: true }),
      finish: () => set({ active: false, seen: true }),
      replay: () => set((s) => ({ active: true, seen: false, completed: [], runId: s.runId + 1 })),
    }),
    { name: 'media-buddy.tour', version: 1 },
  ),
)
