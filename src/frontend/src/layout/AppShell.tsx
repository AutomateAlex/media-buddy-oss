import { useEffect, useState } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { Sidebar } from './Sidebar'
import brandLogo from '../assets/logo-wordmark.png'

type MaintenanceInfo = {
  start: string | null
  end: string | null
  message_zh: string | null
  message_en: string | null
} | null

export function AppShell({ maintenance }: { maintenance?: MaintenanceInfo }) {
  const { t, i18n } = useTranslation()
  const location = useLocation()
  // Mobile/tablet only: off-canvas nav drawer. No effect on desktop (the
  // hamburger top bar is display:none ≥1100px, so navOpen stays false).
  const [navOpen, setNavOpen] = useState(false)

  // Close the drawer whenever the route changes (tapping a nav link navigates).
  useEffect(() => { setNavOpen(false) }, [location.pathname])

  // Scheduled-maintenance notice (ops-controlled backend flag). Show once per
  // session per window; dismissal remembered in sessionStorage keyed by window.
  const [showMaintenance, setShowMaintenance] = useState(false)
  const maintKey = maintenance
    ? `media-buddy.maintenance-notice.${maintenance.start ?? ''}-${maintenance.end ?? ''}`
    : ''
  useEffect(() => {
    if (!maintenance) { setShowMaintenance(false); return }
    if (sessionStorage.getItem(maintKey)) return
    setShowMaintenance(true)
  }, [maintKey, maintenance])
  const dismissMaintenance = () => {
    if (maintKey) sessionStorage.setItem(maintKey, 'shown')
    setShowMaintenance(false)
  }

  return (
    <div className={navOpen ? 'app-shell nav-open' : 'app-shell'}>
      <div className="mobile-topbar">
        <button
          type="button"
          className="nav-toggle"
          aria-label={t('nav.openMenu', { defaultValue: '菜单' })}
          onClick={() => setNavOpen(true)}
        >
          <span /><span /><span />
        </button>
        <div className="mobile-topbar-brand">
          <img src={brandLogo} alt="Media Buddy" className="mobile-topbar-logo" />
        </div>
      </div>
      <Sidebar />
      {navOpen && <div className="nav-backdrop" onClick={() => setNavOpen(false)} />}
      <div className="main-col">
        <Outlet />
      </div>
      {showMaintenance && maintenance && (
        <div className="update-notice-backdrop">
          <div className="update-notice">
            <h2>{t('maintenance.title')}</h2>
            <p style={{ whiteSpace: 'pre-line' }}>
              {(i18n.language?.startsWith('zh') ? maintenance.message_zh : maintenance.message_en)
                || maintenance.message_zh || maintenance.message_en || t('maintenance.fallback')}
            </p>
            <div className="update-notice-actions">
              <button type="button" className="account-btn-primary" onClick={dismissMaintenance}>
                {t('maintenance.gotIt')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
