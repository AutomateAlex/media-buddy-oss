import { HashRouter, Routes, Route, Navigate, NavLink } from 'react-router-dom'
import { AppShell } from './layout/AppShell'
import { Studio } from './pages/Studio'
import { StudioForm } from './pages/StudioForm'
import { ChannelDetail, ChannelNew, ChannelsHome } from './pages/Channels'
import {
  ShortsWorkspace,
  YoutubeWorkspace,
} from './pages/WorkspacePages'
import { Projects } from './pages/Projects'
import { ProjectDetail } from './pages/ProjectDetail'
import { Diagnostics } from './pages/Diagnostics'
import { Account } from './pages/Account'
import { Settings } from './pages/Settings'
import { DialogHost } from './components/Dialog'
import { AppTour } from './features/tour/AppTour'
import { useAuthState } from './hooks/useAuthState'
import { useTheme } from './hooks/useTheme'
import { useTranslation } from 'react-i18next'

function Placeholder({
  titleKey,
  crumb,
  backToAccount = false,
}: {
  titleKey: string
  crumb?: string
  backToAccount?: boolean
}) {
  const { t } = useTranslation()
  const title = t(titleKey)
  return (
    <>
      <header className="page-header">
        <div className="crumb">
          {crumb && (
            <>
              <span className="seg">{crumb}</span>
              <span className="sep">/</span>
            </>
          )}
          <span className="here">{title}</span>
        </div>
        {backToAccount && (
          <NavLink to="/account" className="page-back-link">
            ← {t('common.backToAccount')}
          </NavLink>
        )}
      </header>
      <div className="content-card">
        <header className="page-head">
          <h1>{title}</h1>
          <p className="lede">{t('placeholder.phaseHint')}</p>
        </header>
      </div>
    </>
  )
}

export function App() {
  // Theme must be initialized as early as possible to avoid FOUC.
  // useTheme reads localStorage on mount + sets data-theme on <html>.
  useTheme()
  const { t } = useTranslation()
  const { status, loading } = useAuthState()

  if (loading) {
    return (
      <div
        style={{
          position: 'fixed',
          inset: 0,
          background: '#0d0d10',
          color: '#888',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontFamily: 'system-ui',
        }}
      >
        {t('common.loading')}
      </div>
    )
  }

  return (
    <>
    <DialogHost />
    <HashRouter>
      <AppTour />
      <Routes>
        <Route element={<AppShell maintenance={status?.maintenance ?? null} />}>
          <Route path="/" element={<Navigate to="/channels" replace />} />
          <Route path="/home" element={<Navigate to="/channels" replace />} />
          <Route path="/studio" element={<Navigate to="/channels" replace />} />
          <Route path="/channels" element={<ChannelsHome />} />
          <Route path="/channels/new" element={<ChannelNew />} />
          <Route path="/channels/:id" element={<ChannelDetail />} />
          <Route path="/channels/:id/shorts" element={<ShortsWorkspace />} />
          <Route path="/channels/:id/youtube" element={<YoutubeWorkspace />} />
          <Route path="/channels/:id/projects" element={<Projects />} />
          <Route path="/agent" element={<Studio />} />
          <Route path="/studio/form" element={<StudioForm />} />
          <Route path="/shorts" element={<Navigate to="/channels" replace />} />
          <Route path="/youtube" element={<Navigate to="/channels" replace />} />
          <Route path="/editor" element={<Navigate to="/channels" replace />} />
          <Route path="/publish" element={<Navigate to="/channels" replace />} />
          <Route path="/series" element={<Navigate to="/channels" replace />} />
          <Route path="/series/:id" element={<Navigate to="/channels" replace />} />
          <Route path="/projects" element={<Projects />} />
          <Route path="/project/:id" element={<ProjectDetail />} />
          <Route path="/assets" element={<Navigate to="/channels" replace />} />
          <Route path="/voices" element={<Navigate to="/channels" replace />} />
          <Route path="/script-library" element={<Navigate to="/channels" replace />} />
          <Route path="/trends" element={<Placeholder titleKey="placeholder.trends" crumb="Insights" />} />
          <Route path="/analytics" element={<Placeholder titleKey="placeholder.analytics" crumb="Insights" />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/diagnostics" element={<Diagnostics />} />
          <Route path="/account" element={<Account />} />
        </Route>
      </Routes>
    </HashRouter>
    </>
  )
}
