import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { setAppLanguage, SUPPORTED_LANGUAGES, type SupportedLanguage } from '../i18n'

/** 紧凑语言切换下拉——放页面右上角。复用 setAppLanguage / SUPPORTED_LANGUAGES,与账户页同一套。 */
export function LanguageMenu() {
  const { i18n } = useTranslation()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  const current = SUPPORTED_LANGUAGES.find((l) => l.code === i18n.language) ?? SUPPORTED_LANGUAGES[0]

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  return (
    <div className="lang-menu" ref={ref}>
      <button
        type="button"
        className="lang-menu-trigger"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span className="lang-menu-globe" aria-hidden>🌐</span>
        <span className="lang-menu-label">{current.label}</span>
        <span className={`lang-menu-chev${open ? ' open' : ''}`} aria-hidden>⌄</span>
      </button>
      {open && (
        <div className="lang-menu-pop" role="listbox">
          {SUPPORTED_LANGUAGES.map((lang) => (
            <button
              key={lang.code}
              type="button"
              role="option"
              aria-selected={i18n.language === lang.code}
              className={`lang-menu-item${i18n.language === lang.code ? ' active' : ''}`}
              onClick={() => {
                setAppLanguage(lang.code as SupportedLanguage)
                setOpen(false)
              }}
            >
              {lang.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
