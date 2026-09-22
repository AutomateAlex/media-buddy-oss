import { useState, useEffect } from 'react'

interface Props {
  /** Absolute file path returned by Python backend (or null in LOCAL mode
   *  without an uploaded avatar). Renderer wraps with `file://`. */
  src: string | null
  /** Single character to render when no image / image load fails. */
  fallback: string
  /** Visual size variant. */
  size?: 'sm' | 'md' | 'lg'
}

/**
 * Avatar — shows a local avatar image when one exists, else the fallback
 * letter on the accent-colored gradient. The image source is a `file://`
 * URL so Electron's contextIsolation has no issue (no IPC for display).
 *
 * Re-renders when `src` changes — the parent (Sidebar) keys this on the
 * authState.avatar_path, which the backend refreshes whenever we
 * `refresh()` after an upload.
 */
export function Avatar({ src, fallback, size = 'md' }: Props) {
  const [errored, setErrored] = useState(false)

  // Reset error state when src changes (e.g. user uploaded a new file).
  useEffect(() => {
    setErrored(false)
  }, [src])

  const sizeClass = `avatar avatar-${size}`

  if (!src || errored) {
    return (
      <div className={`${sizeClass} avatar-fallback`} aria-hidden>
        {fallback || '?'}
      </div>
    )
  }

  // Desktop returns a local filesystem path — wrap with file:// for Electron.
  // (Cache-busting ?v=… is already appended to the web URL by the backend.)
  const imgSrc = /^https?:\/\//i.test(src) ? src : `file://${src}`
  return (
    <img
      className={sizeClass}
      src={imgSrc}
      alt=""
      onError={() => setErrored(true)}
    />
  )
}
