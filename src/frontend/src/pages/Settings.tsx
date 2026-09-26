import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import type { SettingsKeyGroup, SettingsKeyRow } from '../types'

/**
 * API Keys — the only configuration this source-available build needs.
 *
 * Keys are stored by the backend (OS keychain) and take effect immediately;
 * the page only ever sees "configured / not configured", never the value.
 */
const HOSTED_URL = 'https://media-buddy.com/?utm_source=oss-app&utm_medium=settings&utm_campaign=oss'

function openExternal(url: string): void {
  window.open(url, '_blank', 'noopener')
}

function KeyRow({ row, onSaved }: { row: SettingsKeyRow; onSaved: () => void }) {
  const { t } = useTranslation()
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)

  const save = async () => {
    const v = value.trim()
    if (!v) return
    setBusy(true); setMsg(null)
    try {
      await api.settings.setKey(row.name, v)
      setValue('')
      setMsg(t('settingsPage.saved'))
      onSaved()
    } catch (e) {
      setMsg((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || t('settingsPage.saveFailed'))
    } finally {
      setBusy(false)
    }
  }
  const clear = async () => {
    setBusy(true); setMsg(null)
    try {
      await api.settings.deleteKey(row.name)
      onSaved()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="account-field" style={{ padding: '10px 0', borderTop: '1px solid rgba(128,128,128,0.15)' }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' }}>
        <strong>{row.label}</strong>
        <code style={{ fontSize: 12, opacity: 0.7 }}>{row.name}</code>
        <span className="account-pill" style={{ marginLeft: 'auto' }}>
          {row.configured ? `✅ ${t('settingsPage.configured')}` : row.required ? `❌ ${t('settingsPage.required')}` : t('settingsPage.optional')}
        </span>
      </div>
      <p className="hint" style={{ margin: '4px 0 8px' }}>{row.what}</p>
      <div className="account-field-row">
        <input
          className="account-input"
          type="password"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder={row.configured ? t('settingsPage.replacePlaceholder') : t('settingsPage.pastePlaceholder')}
          disabled={busy}
          autoComplete="off"
        />
        <button type="button" className="account-btn-primary" onClick={save} disabled={busy || !value.trim()}>
          {busy ? t('common.saving') : t('common.save')}
        </button>
        {row.configured && (
          <button type="button" className="account-btn-secondary" onClick={clear} disabled={busy}>
            {t('settingsPage.clear')}
          </button>
        )}
        <button type="button" className="account-text-btn" onClick={() => openExternal(row.hint)}>
          {t('settingsPage.whereToGet')} →
        </button>
      </div>
      {msg && <p className="hint" style={{ marginTop: 6 }}>{msg}</p>}
    </div>
  )
}

export function Settings() {
  const { t } = useTranslation()
  const [groups, setGroups] = useState<SettingsKeyGroup[]>([])
  const [missing, setMissing] = useState<string[]>([])
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    try {
      const res = await api.settings.listKeys()
      setGroups(res.groups)
      setMissing(res.missing_required)
    } catch {
      setGroups([])
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  return (
    <>
      <header className="page-header account-header">
        <div className="crumb">
          <span className="here">{t('settingsPage.title')}</span>
        </div>
      </header>
      <div className="account-page">
        <section className="account-card account-hero">
          <div className="account-identity-copy">
            <div className="account-eyebrow">{t('settingsPage.eyebrow')}</div>
            <h1>{t('settingsPage.title')}</h1>
            <p>{t('settingsPage.lede')}</p>
          </div>
        </section>

        {!loading && missing.length > 0 && (
          <div className="channel-batch-error" style={{ margin: '12px 0' }}>
            {t('settingsPage.missingRequired', { keys: missing.join('、') })}
          </div>
        )}

        <div className="account-layout">
          <div className="account-column">
            {loading ? (
              <p className="hint">{t('common.loading')}</p>
            ) : groups.map((g) => (
              <section key={g.id} className="account-card account-panel">
                <div className="account-card-head">
                  <h2>{g.label}</h2>
                </div>
                {g.keys.map((row) => <KeyRow key={row.name} row={row} onSaved={refresh} />)}
              </section>
            ))}
          </div>
          <div className="account-column">
            <section className="account-card account-panel">
              <div className="account-card-head">
                <h2>{t('settingsPage.hostedTitle')}</h2>
              </div>
              <p className="hint">{t('settingsPage.hostedBlurb')}</p>
              <div className="account-actions-row">
                <button type="button" className="account-btn-secondary" onClick={() => openExternal(HOSTED_URL)}>
                  {t('settingsPage.hostedCta')}
                </button>
              </div>
            </section>
          </div>
        </div>
      </div>
    </>
  )
}
