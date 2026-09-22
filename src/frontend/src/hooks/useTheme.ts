import { useEffect, useState, useCallback } from 'react'
import {
  applyTheme,
  readThemeChoice,
  writeThemeChoice,
  type ThemeChoice,
} from '../lib/theme'

/**
 * React hook for theme state. Called once at App.tsx top-level to
 * initialize + on any toggle UI for setChoice. No OS-follow logic —
 * theme is purely user-selected from three named palettes.
 */
export function useTheme(): {
  choice: ThemeChoice
  setChoice: (next: ThemeChoice) => void
} {
  const [choice, setChoiceState] = useState<ThemeChoice>('dark')

  useEffect(() => {
    const initial = readThemeChoice()
    setChoiceState(initial)
    applyTheme(initial)
  }, [])

  const setChoice = useCallback((next: ThemeChoice) => {
    setChoiceState(next)
    writeThemeChoice(next)
    applyTheme(next)
  }, [])

  return { choice, setChoice }
}
