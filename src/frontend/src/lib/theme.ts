/**
 * Theme management. Three named palettes — picked explicitly, no
 * OS-follow / "auto" mode (per product decision, simpler is better).
 *
 *   - 'light' — bright white surfaces, dark text
 *   - 'dark'  — current near-black surfaces, light text (default for
 *               first launch)
 *   - 'gray'  — premium warm neutral mid-tones, a "softer dark"
 *
 * Persistence: localStorage 'mb_theme_preference'. The HTML root gets
 * `data-theme="light|dark|gray"`; CSS in styles.css declares matching
 * `:root[data-theme="..."]` blocks for each.
 */

export type ThemeChoice = 'light' | 'dark' | 'gray'

const STORAGE_KEY = 'mb_theme_preference'
// Default to the bright/white theme on first launch (per product: cleaner look).
// Users who already saved a preference keep their choice.
const DEFAULT_THEME: ThemeChoice = 'light'

export function readThemeChoice(): ThemeChoice {
  if (typeof window === 'undefined') return DEFAULT_THEME
  try {
    const v = window.localStorage.getItem(STORAGE_KEY)
    if (v === 'light' || v === 'dark' || v === 'gray') return v
  } catch {
    /* ignore */
  }
  return DEFAULT_THEME
}

export function writeThemeChoice(choice: ThemeChoice): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(STORAGE_KEY, choice)
  } catch {
    /* ignore */
  }
}

/**
 * Apply the chosen theme to <html data-theme="...">. Pure DOM write,
 * idempotent. Default 'dark' matches the previous behavior so existing
 * users without a saved preference see no change.
 */
export function applyTheme(theme: ThemeChoice): void {
  if (typeof document === 'undefined') return
  document.documentElement.setAttribute('data-theme', theme)
}
