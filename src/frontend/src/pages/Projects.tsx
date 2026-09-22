import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { ChannelSubTabs } from '../components/ChannelSubTabs'
import { alertDialog, confirmDialog } from '../components/Dialog'
import { projectDisplayName } from '../lib/projectDisplay'
import { retentionState, REMIND_DAYS } from '../lib/retention'
import { batchDownloadProjects } from '../lib/batchDownload'
import type { Project } from '../types'

/** 「还在等」的状态。
 *
 * ⚠️ 后端的项目状态和出片任务状态不是一回事：
 *   项目建好 → `pending`；worker 认领后 → `running`。
 *   任务侧的 `queued`/`claimed` 偶尔也会映射到项目上。
 *   这四个都算「还没开始做」，客户眼里就是「在排队」。
 */
const WAITING_STATUSES = new Set(['pending', 'queued', 'claimed'])

function formatDateTime(iso?: string | null): string {
  if (!iso) return ''
  const hasTz = /[zZ]|[+-]\d{2}:?\d{2}$/.test(iso)
  const d = new Date(hasTz ? iso : iso + 'Z')
  if (isNaN(d.getTime())) return ''
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

export function Projects() {
  const { t } = useTranslation()
  const { id: channelId } = useParams()

  const formatLabel = (value: string) => {
    if (value === 'youtube_landscape' || value === 'youtube_long') return t('projectsPage.formatLong')
    if (value === 'youtube_shorts') return t('projectsPage.formatShort')
    return value.replaceAll('_', ' ')
  }

  const [projects, setProjects] = useState<Project[]>([])
  const [loading, setLoading] = useState(true)
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [actionId, setActionId] = useState<string | null>(null)
  // 全局视图(无 channelId)下:series_id → 频道名,给每条视频标上所属频道。
  const [channelNames, setChannelNames] = useState<Record<string, string>>({})
  // 多选批量删除。
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [bulkDeleting, setBulkDeleting] = useState(false)
  const [bulkDownloading, setBulkDownloading] = useState(false)
  const [favOnly, setFavOnly] = useState(false)  // 只看收藏(★)的视频
  // 存储保留:收藏切换 + 批量续存弹窗。
  const [favoritingId, setFavoritingId] = useState<string | null>(null)

  useEffect(() => {
    setLoading(true)
    setSelected(new Set())
    const req = channelId ? api.series.listProjects(channelId) : api.projects.list()
    req.then(setProjects).finally(() => setLoading(false))
    if (!channelId) {
      api.series.list()
        .then((rows) => setChannelNames(Object.fromEntries(rows.map((s) => [s.id, s.name]))))
        .catch(() => setChannelNames({}))
    }
  }, [channelId])

  const updateProject = (next: Project) => {
    setProjects((prev) => prev.map((p) => (p.id === next.id ? next : p)))
  }

  const handleDelete = async (p: Project, e: React.MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    const displayName = projectDisplayName(p)
    if (!(await confirmDialog({ message: t('projectsPage.confirmDelete', { name: displayName }), tone: 'danger' }))) return
    setDeletingId(p.id)
    try {
      await api.projects.delete(p.id)
      setProjects((prev) => prev.filter((x) => x.id !== p.id))
    } catch (err: any) {
      void alertDialog(err?.response?.data?.detail || err.message || t('projectsPage.deleteFailed'))
    } finally {
      setDeletingId(null)
    }
  }

  const handleStop = async (p: Project, e: React.MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    if (!(await confirmDialog(t('projectsPage.confirmStop', { name: projectDisplayName(p) })))) return
    setActionId(p.id)
    try {
      updateProject(await api.projects.stop(p.id))
    } catch (err: any) {
      void alertDialog(err?.response?.data?.detail || err.message || t('projectsPage.stopFailed'))
    } finally {
      setActionId(null)
    }
  }

  const handleRestart = async (p: Project, e: React.MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    if (!(await confirmDialog(t('projectsPage.confirmRestart', { name: projectDisplayName(p) })))) return
    setActionId(p.id)
    try {
      updateProject(await api.projects.restart(p.id))
    } catch (err: any) {
      void alertDialog(err?.response?.data?.detail || err.message || t('projectsPage.restartFailed'))
    } finally {
      setActionId(null)
    }
  }

  const toggleSelect = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const favoriteCount = projects.filter((p) => p.retention_intent === 'keep').length
  const visibleProjects = favOnly ? projects.filter((p) => p.retention_intent === 'keep') : projects
  // 队列汇总：制作中几条、排队中几条。全部从已有数据算，不发新请求。
  const queueSummary = {
    running: visibleProjects.filter((p) => p.status === 'running').length,
    waiting: visibleProjects.filter((p) => WAITING_STATUSES.has(p.status)).length,
    get total() { return this.running + this.waiting },
  }

  const toggleSelectAll = () => {
    setSelected((prev) =>
      prev.size === visibleProjects.length ? new Set() : new Set(visibleProjects.map((p) => p.id)),
    )
  }

  const handleBulkDelete = async () => {
    if (selected.size === 0) return
    if (!(await confirmDialog({ message: t('projectsPage.confirmBulkDelete', { count: selected.size }), tone: 'danger' }))) return
    setBulkDeleting(true)
    const ids = Array.from(selected)
    const results = await Promise.allSettled(ids.map((id) => api.projects.delete(id)))
    const okIds = new Set(ids.filter((_, i) => results[i].status === 'fulfilled'))
    const failed = results.length - okIds.size
    setProjects((prev) => prev.filter((p) => !okIds.has(p.id)))
    setSelected(new Set())
    setBulkDeleting(false)
    if (failed > 0) void alertDialog(t('projectsPage.bulkDeletePartial', { failed }))
  }

  const handleBulkDownload = async () => {
    // 只下已完成、未清除的;选中里非完成的忽略。
    const ids = Array.from(selected).filter((id) => {
      const p = projects.find((x) => x.id === id)
      return p && p.status === 'completed' && p.output_path && !p.storage_purged_at
    })
    if (ids.length === 0) {
      void alertDialog(t('projectsPage.downloadNoneEligible'))
      return
    }
    setBulkDownloading(true)
    try {
      await batchDownloadProjects(ids)
      setSelected(new Set())
    } finally {
      setBulkDownloading(false)
    }
  }

  // ── 存储保留 ───────────────────────────────────────────────────────
  const now = new Date()

  const handleToggleFavorite = async (p: Project, e: React.MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    const next = p.retention_intent === 'keep' ? 'auto_expire' : 'keep'
    setFavoritingId(p.id)
    try {
      updateProject(await api.projects.setRetentionIntent(p.id, next))
    } catch (err: any) {
      void alertDialog(err?.response?.data?.detail || err.message || t('projectsPage.retentionFavoriteFailed'))
    } finally {
      setFavoritingId(null)
    }
  }

  const renderRetentionChip = (p: Project) => {
    const s = retentionState(p.expires_at, p.storage_purged_at, now)
    if (s.tone === 'none') return null
    let label: string
    if (s.purged) label = t('projectsPage.retentionPurged')
    else if (s.daysLeft! <= 0) label = t('projectsPage.retentionExpired')
    else if (s.daysLeft === 1) label = t('projectsPage.retentionTomorrow')
    else if (s.tone === 'green') label = t('projectsPage.retentionSafe', { days: s.daysLeft })
    else label = t('projectsPage.retentionDaysLeft', { days: s.daysLeft })
    return <span className={`retention-chip retention-${s.tone}`}>{label}</span>
  }

  return (
    <>
      <header className="page-header">
        <div className="crumb">
          <Link to="/channels" className="seg">{t('projectsPage.crumbChannels')}</Link>
          <span className="sep">/</span>
          <span className="here">{channelId ? t('projectsPage.titleChannelProjects') : t('projectsPage.titleProjectLibrary')}</span>
        </div>
        <Link to={channelId ? `/channels/${channelId}` : '/channels'} className="page-back-link">{t('projectsPage.backLink')}</Link>
      </header>
      <ChannelSubTabs channelId={channelId} active="projects" />
      <div className="content-card">
        <header className="page-head">
          <h1>{channelId ? t('projectsPage.titleChannelProjects') : t('projectsPage.titleProjectLibrary')}</h1>
          <p className="lede">{channelId ? t('projectsPage.ledeChannel') : t('projectsPage.ledeAll')}</p>
        </header>
        {loading ? (
          <p className="hint">{t('projectsPage.loading')}</p>
        ) : projects.length === 0 ? (
          <p className="hint">
            {t('projectsPage.emptyState')}{' '}
            <Link to={channelId ? `/channels/${channelId}` : '/channels'} className="link-out">{t('projectsPage.backToWorkbench')}</Link>
          </p>
        ) : (
          <>
            <div className="bulk-bar">
              <label className="bulk-select-all">
                <input
                  type="checkbox"
                  checked={visibleProjects.length > 0 && selected.size === visibleProjects.length}
                  ref={(el) => {
                    if (el) el.indeterminate = selected.size > 0 && selected.size < visibleProjects.length
                  }}
                  onChange={toggleSelectAll}
                />
                <span>{t('projectsPage.selectAll')}</span>
              </label>
              <button
                type="button"
                className={`btn-fav-filter${favOnly ? ' active' : ''}`}
                onClick={() => { setFavOnly((v) => !v); setSelected(new Set()) }}
                aria-pressed={favOnly}
              >
                {favOnly ? '★' : '☆'} {t('projectsPage.favoritesOnly')}{favoriteCount > 0 ? ` (${favoriteCount})` : ''}
              </button>
              {selected.size > 0 && (
                <div className="bulk-actions">
                  <span className="bulk-count">{t('projectsPage.selectedCount', { count: selected.size })}</span>
                  <button
                    type="button"
                    className="btn-row-action btn-row-action-primary"
                    onClick={handleBulkDownload}
                    disabled={bulkDeleting || bulkDownloading}
                  >
                    {bulkDownloading ? t('projectsPage.downloading') : t('projectsPage.downloadSelected', { count: selected.size })}
                  </button>
                  <button
                    type="button"
                    className="btn-row-action"
                    onClick={() => setSelected(new Set())}
                    disabled={bulkDeleting || bulkDownloading}
                  >
                    {t('projectsPage.clearSelection')}
                  </button>
                  <button
                    type="button"
                    className="btn-bulk-delete"
                    onClick={handleBulkDelete}
                    disabled={bulkDeleting}
                  >
                    {bulkDeleting
                      ? t('projectsPage.deleting')
                      : t('projectsPage.bulkDelete', { count: selected.size })}
                  </button>
                </div>
              )}
            </div>
          {queueSummary.total > 0 && (
            <p className="queue-summary hint">
              {t('projectsPage.queueSummary', {
                running: queueSummary.running,
                waiting: queueSummary.waiting,
              })}
            </p>
          )}
          {favOnly && visibleProjects.length === 0 ? (
            <p className="hint">{t('projectsPage.noFavorites')}</p>
          ) : (
          <ul className="project-list">
            {visibleProjects.map((p) => {
              const displayName = projectDisplayName(p)
              // 排在第几：只数**这一页里排在它前面、且同样在等**的条数。
              // 后端也有精确的排队位置接口，但那是单条项目的；
              // 列表页为每一行都去请求一次不值得，这个近似值够用。
              const waitIdx = WAITING_STATUSES.has(p.status)
                ? visibleProjects.filter(
                    (q, qi) => WAITING_STATUSES.has(q.status)
                      && qi < visibleProjects.indexOf(p),
                  ).length
                : -1
              return (
                <li key={p.id} className={`project-row${selected.has(p.id) ? ' selected' : ''}`}>
                  <label className="project-select" onClick={(e) => e.stopPropagation()}>
                    <input
                      type="checkbox"
                      checked={selected.has(p.id)}
                      onChange={() => toggleSelect(p.id)}
                      aria-label={t('projectsPage.selectOne', { name: displayName })}
                    />
                  </label>
                  <Link to={`/project/${p.id}`} className="project-row-main">
                    <span className="project-name-cell">
                      <span className="project-name-line">
                        <span className="project-name">{displayName}</span>
                        {!channelId && p.series_id && channelNames[p.series_id] && (
                          <span className="project-channel">· {channelNames[p.series_id]}</span>
                        )}
                        {p.downloaded_at && (
                          <span className="project-downloaded">✓ {t('projectsPage.downloaded')}</span>
                        )}
                      </span>
                      <span className="project-meta">
                        {p.created_at && <span>{formatDateTime(p.created_at)}</span>}
                        {renderRetentionChip(p)}
                      </span>
                    </span>
                    <span className={`status status-${p.status}`}>
                      {/* 撞排队上限时后端会把 current_stage 标成 queue_full。
                          不认这个标记的话，客户看到的还是干巴巴的「生成失败」，
                          而真实原因（排队满了）他永远不知道。 */}
                      {p.status === 'failed' && p.current_stage === 'queue_full'
                        ? t('projectsPage.statusLabels.queue_full')
                        : t(`projectsPage.statusLabels.${p.status}` as never, { defaultValue: p.status })}
                      {waitIdx > 0 && (
                        <em className="queue-ahead">
                          {t('projectsPage.queueAhead', { ahead: waitIdx })}
                        </em>
                      )}
                    </span>
                    <span className="project-mode">{p.mode}</span>
                    <span className="project-format">{formatLabel(p.output_format)}</span>
                  </Link>
                  <div className="project-row-actions">
                    {p.status === 'completed' && !p.storage_purged_at && (
                      <button
                        type="button"
                        className={`btn-favorite${p.retention_intent === 'keep' ? ' is-kept' : ''}`}
                        onClick={(e) => handleToggleFavorite(p, e)}
                        disabled={favoritingId === p.id}
                        title={p.retention_intent === 'keep' ? t('projectsPage.retentionKept') : t('projectsPage.retentionKeep')}
                        aria-label={p.retention_intent === 'keep' ? t('projectsPage.retentionKept') : t('projectsPage.retentionKeep')}
                      >
                        {p.retention_intent === 'keep' ? '★' : '☆'}
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn-row-action"
                      onClick={(e) => handleStop(p, e)}
                      disabled={actionId === p.id || !['pending', 'running', 'paused_auth_required'].includes(p.status)}
                    >
                      {t('projectsPage.stop')}
                    </button>
                    <button
                      type="button"
                      className="btn-row-action btn-row-action-primary"
                      onClick={(e) => handleRestart(p, e)}
                      disabled={actionId === p.id || p.status === 'running'}
                    >
                      {t('projectsPage.restart')}
                    </button>
                  </div>
                  <button
                    type="button"
                    className="btn-delete-row"
                    onClick={(e) => handleDelete(p, e)}
                    disabled={deletingId === p.id}
                    title={t('projectsPage.deleteTitle', { name: displayName })}
                    aria-label={t('projectsPage.deleteAriaLabel', { name: displayName })}
                  >
                    {deletingId === p.id ? '...' : '×'}
                  </button>
                </li>
              )
            })}
          </ul>
          )}
          </>
        )}
      </div>
    </>
  )
}
