/**
 * Unified in-app dialog — one clean styled modal that replaces every native
 * window.alert / window.confirm across the app. Imperative + promise-based so
 * call sites read almost like the natives they replace:
 *
 *   await alertDialog(t('foo.saveFailed'))                 // one OK button
 *   if (!(await confirmDialog(t('foo.confirmDelete')))) return   // cancel / OK
 *
 * Design notes:
 *  - FIFO queue: two dialogs requested back-to-back (e.g. two error paths
 *    firing at once) both resolve — the second shows after the first closes.
 *    Never overwrite a pending resolver.
 *  - Dismissal (overlay click / Escape) always resolves FALSE, never true —
 *    an accidental dismiss must never proceed a destructive confirm.
 *  - Mount <DialogHost/> exactly once at the app root (inside i18n provider).
 */
import { createPortal } from 'react-dom'
import { useEffect, useReducer } from 'react'
import { useTranslation } from 'react-i18next'

type DialogTone = 'default' | 'danger'

export type DialogRequest = {
  kind: 'alert' | 'confirm'
  message: string
  title?: string
  confirmText?: string
  cancelText?: string
  tone?: DialogTone
  icon?: string
}

type QueueItem = { req: DialogRequest; resolve: (v: boolean) => void }

let queue: QueueItem[] = []
let notify: (() => void) | null = null

function _register(fn: () => void): () => void {
  notify = fn
  return () => {
    if (notify === fn) notify = null
  }
}

function _current(): DialogRequest | null {
  return queue[0]?.req ?? null
}

function _resolveTop(value: boolean): void {
  const item = queue.shift()
  item?.resolve(value)
  notify?.()
}

function enqueue(req: DialogRequest): Promise<boolean> {
  return new Promise((resolve) => {
    queue.push({ req, resolve })
    notify?.()
  })
}

type DialogOpts = Omit<Partial<DialogRequest>, 'kind'> & { message: string }

// Some call sites pass a raw API error `detail` that may not be a string (or
// even a proper opts object). Coerce defensively so the modal never renders
// blank — matches the old native alert()'s stringify behavior.
function normalize(opts: string | DialogOpts): Omit<DialogRequest, 'kind'> {
  if (typeof opts === 'string') return { message: opts }
  if (opts && typeof opts.message === 'string') return opts
  return { message: String((opts as { message?: unknown })?.message ?? opts ?? '') }
}

/** Styled replacement for window.confirm — resolves true (OK) / false (cancel/dismiss). */
export function confirmDialog(opts: string | DialogOpts): Promise<boolean> {
  return enqueue({ kind: 'confirm', ...normalize(opts) })
}

/** Styled replacement for window.alert — one OK button, resolves when closed. */
export function alertDialog(opts: string | DialogOpts): Promise<boolean> {
  return enqueue({ kind: 'alert', ...normalize(opts) })
}

export function DialogHost() {
  const { t } = useTranslation()
  const [, force] = useReducer((x: number) => x + 1, 0)

  useEffect(() => _register(force), [])

  const req = _current()

  useEffect(() => {
    if (!req) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') _resolveTop(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [req])

  if (!req) return null
  const isConfirm = req.kind === 'confirm'

  return createPortal(
    <div className="mb-dialog-overlay" role="presentation" onClick={() => _resolveTop(false)}>
      <div
        className="mb-dialog"
        role={isConfirm ? 'alertdialog' : 'dialog'}
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
      >
        {req.icon && <div className="mb-dialog-icon" aria-hidden="true">{req.icon}</div>}
        {req.title && <h3 className="mb-dialog-title">{req.title}</h3>}
        <p className="mb-dialog-msg">{req.message}</p>
        <div className="mb-dialog-actions">
          {isConfirm && (
            <button type="button" className="mb-dialog-cancel" onClick={() => _resolveTop(false)}>
              {req.cancelText ?? t('common.cancel')}
            </button>
          )}
          <button
            type="button"
            className={`mb-dialog-ok${req.tone === 'danger' ? ' danger' : ''}`}
            autoFocus
            onClick={() => _resolveTop(true)}
          >
            {req.confirmText ?? t('common.ok')}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
