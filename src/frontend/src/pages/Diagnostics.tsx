import { useEffect, useMemo, useState } from 'react'
import { NavLink } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { api } from '../api/client'
import type { DiagnosticsReport } from '../types'

function formatSummary(report: DiagnosticsReport, t: (key: string) => string): string {
  return [
    t('diagnostics.summaryTitle'),
    `${t('diagnostics.generatedAt')}: ${report.generated_at}`,
    `${t('diagnostics.authStatus')}: ${report.cloud.desktop_authenticated ? t('diagnostics.signedIn') : t('diagnostics.signedOut')}`,
    `${t('diagnostics.recentFailures')}: ${report.failed_projects.length}`,
    '',
    `${t('diagnostics.failureList')}:`,
    ...(report.failed_projects.length
      ? report.failed_projects.map((p) =>
          `- ${p.name}: ${Object.values(p.errors).join('；') || t('diagnostics.noFailures')}`,
        )
      : [`- ${t('diagnostics.noFailures')}`]),
  ].join('\n')
}

function formatFullReport(report: DiagnosticsReport, t: (key: string) => string): string {
  return [
    formatSummary(report, t),
    '',
    `${t('diagnostics.recentProjects')}:`,
    ...report.recent_projects.map((p) =>
      `- ${p.name} (${p.id}) status=${p.status} stage=${p.current_stage || '-'} output=${p.output_path || '-'}`,
    ),
    '',
    `${t('diagnostics.backendLogs')}:`,
    ...report.log_lines,
  ].join('\n')
}

export function Diagnostics() {
  const { t } = useTranslation()
  const [report, setReport] = useState<DiagnosticsReport | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  const summaryText = useMemo(() => (report ? formatSummary(report, t) : ''), [report, t])
  const fullText = useMemo(() => (report ? formatFullReport(report, t) : ''), [report, t])

  const load = async () => {
    setLoading(true)
    setError(null)
    setCopied(false)
    try {
      setReport(await api.logs.diagnostics(500))
    } catch (e: any) {
      setError(e?.response?.data?.detail || e.message || t('diagnostics.readFailed'))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  const copySummary = async () => {
    if (!summaryText) return
    try {
      await navigator.clipboard.writeText(summaryText)
      setCopied(true)
    } catch (e: any) {
      setError(e?.message || t('diagnostics.copyFailed'))
    }
  }

  const downloadFullReport = () => {
    if (!fullText) return
    const blob = new Blob([fullText], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `media-buddy-diagnostics-${Date.now()}.txt`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <>
      <header className="page-header">
        <div className="crumb">
          <span className="seg">System</span>
          <span className="sep">/</span>
          <span className="here">{t('diagnostics.title')}</span>
        </div>
        <NavLink to="/account" className="page-back-link">
          ← {t('common.backToAccount')}
        </NavLink>
      </header>

      <div className="diagnostics-page">
        <section className="content-card diagnostics-panel">
          <header className="page-head diagnostics-head">
            <div>
              <h1>{t('diagnostics.title')}</h1>
              <p className="lede">{t('diagnostics.subtitle')}</p>
            </div>
            <div className="diagnostics-actions">
              <button className="btn-secondary" onClick={() => void load()} disabled={loading}>
                {t('common.refresh')}
              </button>
              <button className="btn-secondary" onClick={downloadFullReport} disabled={!report}>
                {t('diagnostics.exportFull')}
              </button>
              <button className="btn-primary" onClick={() => void copySummary()} disabled={!report}>
                {copied ? t('diagnostics.copied') : t('diagnostics.copySummary')}
              </button>
            </div>
          </header>

          {error && <div className="error-banner">{error}</div>}
          {loading && <p className="hint">{t('diagnostics.loading')}</p>}

          {report && (
            <>
              <div className="diagnostics-summary">
                <div>
                  <span>{t('diagnostics.authStatus')}</span>
                  <strong>{report.cloud.desktop_authenticated ? t('diagnostics.signedIn') : t('diagnostics.signedOut')}</strong>
                </div>
                <div>
                  <span>{t('diagnostics.recentFailures')}</span>
                  <strong>{report.failed_projects.length}</strong>
                </div>
                <div>
                  <span>{t('diagnostics.recentProjects')}</span>
                  <strong>{report.recent_projects.length}</strong>
                </div>
              </div>

              <p className="diagnostics-note">
                {t('diagnostics.note')}
              </p>

              <section className="diagnostics-section">
                <h2>{t('diagnostics.recentFailures')}</h2>
                {report.failed_projects.length === 0 ? (
                  <p className="hint">{t('diagnostics.noRecentFailures')}</p>
                ) : (
                  report.failed_projects.map((p) => (
                    <div className="diagnostics-error" key={p.id}>
                      <div className="diagnostics-error-title">{p.name}</div>
                      <div className="diagnostics-error-text">
                        {Object.values(p.errors).join('；') || t('diagnostics.noErrorDetail')}
                      </div>
                    </div>
                  ))
                )}
              </section>

              <section className="diagnostics-section">
                <h2>{t('diagnostics.recentProjects')}</h2>
                <div className="diagnostics-project-list">
                  {report.recent_projects.slice(0, 5).map((p) => (
                    <div className="diagnostics-project" key={p.id}>
                      <span>{p.name}</span>
                      <strong className={`status-pill status-${p.status}`}>{p.status}</strong>
                    </div>
                  ))}
                </div>
              </section>
            </>
          )}
        </section>
      </div>
    </>
  )
}
