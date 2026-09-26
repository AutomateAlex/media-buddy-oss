import { useEffect, useRef, useState, type ChangeEvent } from 'react'
import { NavLink, useLocation, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import axios from 'axios'
import { api } from '../api/client'
import { alertDialog } from '../components/Dialog'
import { useAuthState } from '../hooks/useAuthState'
import { useTheme } from '../hooks/useTheme'
import { Avatar } from '../components/Avatar'
import type { ThemeChoice } from '../lib/theme'
import { setAppLanguage, SUPPORTED_LANGUAGES, type SupportedLanguage } from '../i18n'

const VERSION = import.meta.env.VITE_APP_VERSION ?? 'dev'
const BUILD_DATE = import.meta.env.VITE_BUILD_DATE ?? ''
const GIT_COMMIT = import.meta.env.VITE_GIT_COMMIT ?? ''

const THEME_CARDS: { value: ThemeChoice; labelKey: string; swatch: string }[] = [
  { value: 'light', labelKey: 'account.themeLight', swatch: 'account-theme-swatch-light' },
  { value: 'dark', labelKey: 'account.themeDark', swatch: 'account-theme-swatch-dark' },
  { value: 'gray', labelKey: 'account.themeGray', swatch: 'account-theme-swatch-gray' },
]

function emailInitial(email: string | null): string {
  if (!email) return 'A'
  return email.charAt(0).toUpperCase()
}

function statusLabel(
  authenticated: boolean,
  subStatus: string | null,
  normal: string,
  signedIn: string,
  signedOut: string,
): string {
  if (!authenticated) return signedOut
  if (subStatus === 'active' || subStatus === 'trialing') return normal
  if (!subStatus) return signedIn
  return subStatus
}

// 打开外链(如「我的后台」跳 media-buddy.com)。web 走 window.open ✅;electronAPI 分支为桌面死代码 ⛔。
function openExternal(url: string): void {
  const api = (window as unknown as {
    electronAPI?: { openExternal?: (url: string) => Promise<void> }
  }).electronAPI
  if (api?.openExternal) {
    void api.openExternal(url) // ⛔【DESKTOP 死代码·退役2026-08】
  } else {
    window.open(url, '_blank') // ✅【WEB 唯一路径】
  }
}

export function Account() {
  const { status: authState, refresh } = useAuthState()
  const { choice: themeChoice, setChoice: setThemeChoice } = useTheme()
  const navigate = useNavigate()
  const location = useLocation()
  const { t, i18n } = useTranslation()

  const [nameInput, setNameInput] = useState('')
  const [nameSaving, setNameSaving] = useState(false)
  const [nameSaved, setNameSaved] = useState(false)
  const [aboutOpen, setAboutOpen] = useState(false)
  const [languageOpen, setLanguageOpen] = useState(false)
  const [avatarUploading, setAvatarUploading] = useState(false)
  const aboutRef = useRef<HTMLDialogElement>(null)
  const avatarInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (authState?.display_name) {
      setNameInput(authState.display_name)
    } else if (authState?.email) {
      setNameInput(authState.email.split('@')[0])
    }
  }, [authState?.display_name, authState?.email])

  useEffect(() => {
    const d = aboutRef.current
    if (!d) return
    if (aboutOpen && !d.open) d.showModal()
    if (!aboutOpen && d.open) d.close()
  }, [aboutOpen])


  useEffect(() => {
    const state = location.state as { openAbout?: boolean } | null
    if (state?.openAbout) {
      setAboutOpen(true)
      navigate(location.pathname, { replace: true, state: {} })
    }
  }, [location.pathname, location.state, navigate])

  if (!authState) {
    return (
      <>
        <header className="page-header">
          <div className="crumb">
            <span className="here">{t('account.loadingTitle')}</span>
          </div>
        </header>
        <div className="content-card">
          <p className="lede">{t('common.loading')}</p>
        </div>
      </>
    )
  }

  async function handleAvatarUpload() {
    // Web (cloud) mode: no Electron file picker — open the browser file input,
    avatarInputRef.current?.click()
  }

  async function handleWebAvatarFile(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    e.target.value = '' // allow re-picking the same file
    if (!file) return
    setAvatarUploading(true)
    try {
      await api.auth.uploadAvatar(file)
      await refresh()
    } catch (err) {
      void alertDialog(`${t('account.avatarSaveFailed')}: ${err instanceof Error ? err.message : 'upload failed'}`)
    } finally {
      setAvatarUploading(false)
    }
  }

  async function handleSaveName() {
    const trimmed = nameInput.trim()
    if (!trimmed || trimmed === authState?.display_name) return
    setNameSaving(true)
    setNameSaved(false)
    try {
      await axios.post('/api/auth/display-name', { display_name: trimmed })
      await refresh()
      setNameSaved(true)
      setTimeout(() => setNameSaved(false), 2000)
    } catch (e) {
      void alertDialog(`${t('account.saveFailed')}: ${e instanceof Error ? e.message : 'unknown'}`)
    } finally {
      setNameSaving(false)
    }
  }

  const initial = emailInitial(authState.email)
  const nameDirty = nameInput.trim() !== '' && nameInput.trim() !== (authState.display_name ?? '')
  const statusText = statusLabel(
    authState.authenticated,
    authState.sub_status,
    t('account.statusNormal'),
    t('account.statusSignedIn'),
    t('account.statusSignedOut'),
  )
  const currentLanguage = SUPPORTED_LANGUAGES.find((lang) => lang.code === i18n.language)
    ?? SUPPORTED_LANGUAGES[0]

  return (
    <>
      <header className="page-header account-header">
        <div className="crumb">
          <span className="here">{t('account.title')}</span>
        </div>
      </header>

      <div className="account-page">
        <section className="account-card account-hero">
          <div className="account-identity">
            <Avatar src={authState.avatar_path} fallback={initial} size="lg" />
            <div className="account-identity-copy">
              <div className="account-eyebrow">{t('account.currentAccount')}</div>
              <h1>{authState.display_name ?? authState.email?.split('@')[0] ?? t('account.localWorkstation')}</h1>
              <p>
                {authState.authenticated
                  ? t('settingsPage.keysOk')
                  : t('settingsPage.keysMissing')}
              </p>
            </div>
          </div>
          <div className="account-status-grid">
            <div>
              <span>{t('account.status')}</span>
              <strong>{statusText}</strong>
            </div>
            <div>
              <span>{t('nav.settings')}</span>
              <strong><NavLink to="/settings">{t('settingsPage.openSettings')}</NavLink></strong>
            </div>
          </div>
        </section>

        <div className="account-layout">
          <div className="account-column">
            <section className="account-card account-panel">
              <div className="account-card-head">
                <h2>{t('account.profile')}</h2>
                <button
                  type="button"
                  className="account-text-btn"
                  onClick={handleAvatarUpload}
                  disabled={avatarUploading}
                >
                  {avatarUploading ? '…' : t('account.changeAvatar')}
                </button>
                <input
                  ref={avatarInputRef}
                  type="file"
                  accept="image/png,image/jpeg,image/webp"
                  style={{ display: 'none' }}
                  onChange={handleWebAvatarFile}
                />
              </div>

              <div className="account-fields">
                <div className="account-field">
                  <label className="account-label" htmlFor="account-name">
                    {t('account.displayName')}
                  </label>
                  <div className="account-field-row">
                    <input
                      id="account-name"
                      className="account-input"
                      value={nameInput}
                      onChange={(e) => setNameInput(e.target.value)}
                      placeholder={t('account.displayNamePlaceholder')}
                      maxLength={120}
                      disabled={nameSaving}
                    />
                    <button
                      type="button"
                      className="account-btn-primary"
                      onClick={handleSaveName}
                      disabled={!nameDirty || nameSaving}
                    >
                      {nameSaving ? t('common.saving') : nameSaved ? t('common.saved') : t('common.save')}
                    </button>
                  </div>
                </div>
              </div>
            </section>

            <section className="account-card account-panel">
              <button
                type="button"
                className="account-collapse-head"
                onClick={() => setLanguageOpen((v) => !v)}
                aria-expanded={languageOpen}
              >
                <span>{t('account.language')}</span>
                <strong>{currentLanguage.label}</strong>
                <span className={`account-collapse-chev${languageOpen ? ' open' : ''}`}>⌄</span>
              </button>
              {languageOpen && (
                <div className="account-language-grid account-language-grid-collapsed">
                  {SUPPORTED_LANGUAGES.map((lang) => (
                    <button
                      key={lang.code}
                      type="button"
                      className={`account-language-option ${i18n.language === lang.code ? 'account-language-option-active' : ''}`}
                      onClick={() => {
                        setAppLanguage(lang.code as SupportedLanguage)
                        setLanguageOpen(false)
                      }}
                    >
                      {lang.label}
                    </button>
                  ))}
                </div>
              )}
            </section>

            <section className="account-card account-panel">
              <div className="account-card-head">
                <h2>{t('settingsPage.title')}</h2>
                <span className="account-pill">{statusText}</span>
              </div>
              <p className="hint">{t('settingsPage.accountBlurb')}</p>
              <div className="account-actions-row">
                <NavLink to="/settings" className="account-btn-primary">{t('settingsPage.openSettings')}</NavLink>
                <button type="button" className="account-btn-secondary" onClick={() => openExternal('https://media-buddy.com/?utm_source=oss-app&utm_medium=account&utm_campaign=oss')}>
                  {t('settingsPage.hostedCta')}
                </button>
              </div>
            </section>
          </div>

          <div className="account-column">
            <section className="account-card account-panel">
              <div className="account-card-head">
                <h2>{t('account.appearance')}</h2>
              </div>
              <div className="account-theme-segments" role="group" aria-label={t('account.appearance')}>
                {THEME_CARDS.map((opt) => (
                  <button
                    key={opt.value}
                    type="button"
                    className={`account-theme-segment ${themeChoice === opt.value ? 'account-theme-segment-active' : ''}`}
                    onClick={() => setThemeChoice(opt.value)}
                  >
                    <span className={`account-theme-swatch ${opt.swatch}`} />
                    <span>{t(opt.labelKey)}</span>
                  </button>
                ))}
              </div>
            </section>

          </div>
        </div>
      </div>

      <dialog ref={aboutRef} className="about-dialog" onClose={() => setAboutOpen(false)}>
        <div className="about-dialog-inner">
          <div className="about-header">
            <div className="about-logo">A</div>
            <div>
              <div className="about-name">Media Buddy</div>
              <div className="about-tagline">AI Video Studio</div>
            </div>
          </div>
          <div className="about-fields">
            <div className="about-row">
              <span className="about-label">{t('account.version')}</span>
              <span className="about-value">v{VERSION}</span>
            </div>
            {BUILD_DATE && (
              <div className="about-row">
                <span className="about-label">{t('account.buildDate')}</span>
                <span className="about-value">{new Date(BUILD_DATE).toLocaleString()}</span>
              </div>
            )}
            {GIT_COMMIT && GIT_COMMIT !== 'unknown' && (
              <div className="about-row">
                <span className="about-label">Commit</span>
                <span className="about-value about-mono">{GIT_COMMIT.slice(0, 7)}</span>
              </div>
            )}
          </div>
          <div className="about-actions">
            <button type="button" className="about-btn-secondary" onClick={() => openExternal('https://media-buddy.com/?utm_source=oss-app&utm_medium=account&utm_campaign=oss')}>
              {t('common.visitWebsite')}
            </button>
            <button type="button" className="about-btn-primary" onClick={() => setAboutOpen(false)}>
              {t('common.close')}
            </button>
          </div>
        </div>
      </dialog>
    </>
  )
}
