/**
 * TourOverlay — a self-contained coach-mark overlay (no external tour library).
 *
 * Renders, via a portal to document.body:
 *   - a dark scrim with a transparent "hole" spotlight over the target element
 *     (the box-shadow-spread trick: one positioned element whose huge shadow
 *     dims everything around the hole), or a full scrim for center steps;
 *   - a positioned tooltip card (theme-styled, i18n buttons) that flips/clamps
 *     to stay on-screen and points at the target with an arrow.
 *
 * The target rect is re-measured on scroll/resize and polled briefly so the
 * spotlight follows async-loaded content (e.g. the topic list). If the target
 * never appears, the step degrades to a centered card instead of getting stuck.
 */
import { createPortal } from 'react-dom'
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import type { Placement } from './tourStages'

const GAP = 14
const EDGE = 12

type Arrow = 'top' | 'bottom' | 'left' | 'right' | 'none'

interface Props {
  target: string
  center?: boolean
  placement?: Placement
  title: string
  body: string
  isFirst: boolean
  isFinal: boolean
  onNext: () => void
  onBack: () => void
  onSkip: () => void
}

function useTargetRect(target: string, center: boolean): DOMRect | null {
  const [rect, setRect] = useState<DOMRect | null>(null)

  const measure = useCallback(() => {
    if (center) return
    const el = document.querySelector(target) as HTMLElement | null
    setRect(el ? el.getBoundingClientRect() : null)
  }, [target, center])

  useLayoutEffect(() => {
    if (center) {
      setRect(null)
      return
    }
    const el = document.querySelector(target) as HTMLElement | null
    if (el) el.scrollIntoView({ block: 'center', behavior: 'auto', inline: 'nearest' })
    measure()
    const onChange = () => measure()
    window.addEventListener('resize', onChange)
    window.addEventListener('scroll', onChange, true)
    // Poll briefly to catch late layout / async-loaded targets.
    const id = window.setInterval(measure, 400)
    return () => {
      window.removeEventListener('resize', onChange)
      window.removeEventListener('scroll', onChange, true)
      window.clearInterval(id)
    }
  }, [target, center, measure])

  return rect
}

export function TourOverlay({
  target,
  center = false,
  placement = 'bottom',
  title,
  body,
  isFirst,
  isFinal,
  onNext,
  onBack,
  onSkip,
}: Props) {
  const { t } = useTranslation()
  const rect = useTargetRect(target, center)
  const cardRef = useRef<HTMLDivElement | null>(null)
  const [size, setSize] = useState({ w: 340, h: 170 })

  useLayoutEffect(() => {
    if (cardRef.current) {
      const r = cardRef.current.getBoundingClientRect()
      if (Math.abs(r.width - size.w) > 1 || Math.abs(r.height - size.h) > 1) {
        setSize({ w: r.width, h: r.height })
      }
    }
  })

  const vw = typeof window !== 'undefined' ? window.innerWidth : 1280
  const vh = typeof window !== 'undefined' ? window.innerHeight : 800

  // Compute tooltip position + arrow direction.
  let top: number
  let left: number
  let arrow: Arrow

  const isCentered = center || !rect
  if (isCentered) {
    top = vh / 2 - size.h / 2
    left = vw / 2 - size.w / 2
    arrow = 'none'
  } else {
    const cx = rect.left + rect.width / 2
    const cy = rect.top + rect.height / 2
    let pl: Placement = placement
    if (pl === 'bottom' && rect.bottom + GAP + size.h > vh) pl = 'top'
    else if (pl === 'top' && rect.top - GAP - size.h < 0) pl = 'bottom'
    else if (pl === 'left' && rect.left - GAP - size.w < 0) pl = 'right'
    else if (pl === 'right' && rect.right + GAP + size.w > vw) pl = 'left'

    switch (pl) {
      case 'top':
        top = rect.top - GAP - size.h
        left = cx - size.w / 2
        arrow = 'bottom'
        break
      case 'left':
        top = cy - size.h / 2
        left = rect.left - GAP - size.w
        arrow = 'right'
        break
      case 'right':
        top = cy - size.h / 2
        left = rect.right + GAP
        arrow = 'left'
        break
      default:
        top = rect.bottom + GAP
        left = cx - size.w / 2
        arrow = 'top'
        break
    }
    left = Math.max(EDGE, Math.min(left, vw - size.w - EDGE))
    top = Math.max(EDGE, Math.min(top, vh - size.h - EDGE))
  }

  return createPortal(
    <>
      {isCentered ? (
        <div className="mb-tour-scrim" />
      ) : (
        <div
          className="mb-tour-spotlight"
          style={{
            top: rect.top - 6,
            left: rect.left - 6,
            width: rect.width + 12,
            height: rect.height + 12,
          }}
        />
      )}
      <div ref={cardRef} className="mb-tour-tooltip mb-tour-pop" style={{ top, left }}>
        {arrow !== 'none' && <span className={`mb-tour-arrow ${arrow}`} />}
        {title && <h3 className="mb-tour-title">{title}</h3>}
        <div className="mb-tour-body">{body}</div>
        <div className="mb-tour-footer">
          <button type="button" className="mb-tour-skip" onClick={onSkip}>
            {t('tour.skip')}
          </button>
          <div className="mb-tour-nav">
            {!isFirst && (
              <button type="button" className="mb-tour-back" onClick={onBack}>
                {t('tour.back')}
              </button>
            )}
            <button type="button" className="mb-tour-next" onClick={onNext}>
              {isFinal ? t('tour.done') : t('tour.next')}
            </button>
          </div>
        </div>
      </div>
    </>,
    document.body,
  )
}
