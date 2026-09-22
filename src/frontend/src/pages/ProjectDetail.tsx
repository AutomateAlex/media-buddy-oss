import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useParams, useNavigate, useLocation } from 'react-router-dom'
import { api, apiErrorText } from '../api/client'
import { alertDialog } from '../components/Dialog'
import { usePipeline } from '../hooks/usePipeline'
import { PipelineProgress } from '../components/pipeline/PipelineProgress'
import { Timeline } from '../components/timeline/Timeline'
import { SeoPackDetailModal } from '../components/SeoPackModal'
import { projectDisplayName } from '../lib/projectDisplay'
import { retentionState } from '../lib/retention'
import type { Project, PublishProject, SeoPack, StudioSession } from '../types'

// 分镜时间线是内部诊断块(暴露每句的镜头/素材来源/搜索词/审核分数),
// 客户不该看到。默认隐藏;要排查取材问题时改回 true 即可。
const SHOW_DIAGNOSTIC_TIMELINE = false

function formatDateTime(iso?: string | null): string {
  if (!iso) return ''
  const hasTz = /[zZ]|[+-]\d{2}:?\d{2}$/.test(iso)
  const d = new Date(hasTz ? iso : iso + 'Z')
  if (isNaN(d.getTime())) return ''
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

export function ProjectDetail() {
  const { t } = useTranslation()
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const [project, setProject] = useState<Project | null>(null)
  const [folderToast, setFolderToast] = useState<string | null>(null)
  const [studioSession, setStudioSession] = useState<StudioSession | null>(null)
  const [chatExpanded, setChatExpanded] = useState(false)
  const [downloaded, setDownloaded] = useState(false)
  // 发布本片 → 触发 SEO 模块(/api/publish/seo-pack),出标题/简介/标签/话题等发布素材。
  const [seoPack, setSeoPack] = useState<SeoPack | null>(null)
  const [seoLoading, setSeoLoading] = useState(false)
  const [seoError, setSeoError] = useState<string | null>(null)
  const [showSeo, setShowSeo] = useState(false)
  const [retentionBusy, setRetentionBusy] = useState(false)
  const { status, startPipeline, error } = usePipeline(id || null)

  const handleToggleFavorite = async () => {
    if (!id || !project || retentionBusy) return
    const next = project.retention_intent === 'keep' ? 'auto_expire' : 'keep'
    setRetentionBusy(true)
    try {
      setProject(await api.projects.setRetentionIntent(id, next))
    } catch (e) {
      void alertDialog(apiErrorText(e))
    } finally {
      setRetentionBusy(false)
    }
  }

  const handlePublish = async () => {
    if (!id || seoLoading) return
    setSeoError(null)
    setSeoLoading(true)
    try {
      // 已有则复用(后端 overwrite=false 跳过重算),首次才走一次 LLM。
      const res = await api.publish.generateSeoPack([id])
      const pack = res.items[0]
      if (!pack) throw new Error('empty seo pack')
      setSeoPack(pack)
      setShowSeo(true)
    } catch (e) {
      setSeoError(apiErrorText(e))
    } finally {
      setSeoLoading(false)
    }
  }

  useEffect(() => {
    if (id) api.projects.get(id).then(setProject)
  }, [id, status?.overall_status])

  useEffect(() => {
    if (project?.studio_session_id) {
      api.studio
        .getSession(project.studio_session_id)
        .then(setStudioSession)
        .catch(() => setStudioSession(null))
    } else {
      setStudioSession(null)
    }
  }, [project?.studio_session_id])

  const isComplete = status?.overall_status === 'completed' && status?.output_path
  const displayName = project ? projectDisplayName(project) : t('projectDetail.loading')
  const backTo = project?.series_id ? `/channels/${project.series_id}/projects` : '/channels'

  const handleOpenFolder = async () => {
    if (!status?.output_path) return
    // ⛔【DESKTOP 死代码·退役 2026-08】桌面端打开本地输出文件夹;web 恒无 electronAPI。
    const electronAPI = (window as any).electronAPI
    if (electronAPI?.openOutputFolder) {
      electronAPI.openOutputFolder(status.output_path)
      return
    }
    // ✅【WEB 唯一路径】Browser fallback: copy path to clipboard
    try {
      await navigator.clipboard.writeText(status.output_path)
      setFolderToast(t('projectDetail.pathCopiedToClipboard'))
      setTimeout(() => setFolderToast(null), 3000)
    } catch {
      setFolderToast(t('projectDetail.copyFailed'))
      setTimeout(() => setFolderToast(null), 3000)
    }
  }

  const handleCopyPath = async () => {
    if (!status?.output_path) return
    try {
      await navigator.clipboard.writeText(status.output_path)
      setFolderToast(t('projectDetail.pathCopied'))
      setTimeout(() => setFolderToast(null), 2000)
    } catch {/* noop */}
  }

  const handleCopyId = async () => {
    if (!project?.id) return
    try {
      await navigator.clipboard.writeText(project.id)
      setFolderToast(t('projectDetail.projectIdCopied'))
      setTimeout(() => setFolderToast(null), 2000)
    } catch {/* noop */}
  }

  // P1-7 一键再来一条 —— 从哪出来的就回哪(保真):
  //   · 从「排队执行/批量」出的(batch_run_id 非空)→ 回频道工作台(那里有排队执行)。
  //   · 从单条工作台出的 → 回对应 长/短 工作台,并把「对话式(chat)/粘贴文案(script)」按这条
  //     视频当初的模式(verbatim)预置好(写 item5 的记忆 key,落地即恢复该模式)。
  //   清掉该频道上一条的 studio 会话+草稿 → 新一条是干净起点。原成片仍在项目汇总可查。
  //   localStorage key 格式与 item5(Channels.tsx / WorkspacePages.tsx)保持一致。
  const handleAnotherOne = () => {
    const channelId = project?.series_id || ''
    const isLong = (project?.output_format || '') === 'youtube_landscape'
    const videoType: 'long' | 'short' = isLong ? 'long' : 'short'
    const fromBatch = !!project?.batch_run_id
    const wasPaste = project?.verbatim === true
    const sKey = isLong ? 'media-buddy.youtube.active-session-id' : 'media-buddy.studio.active-session-id'
    const dKey = isLong ? 'media-buddy.youtube.draft-input' : 'media-buddy.studio.draft-input'
    try {
      if (channelId) {
        // 记住这条视频的长/短(频道工作台的排队执行开关据此预选)。
        localStorage.setItem(`media-buddy.channel.default-entry:${channelId}`, videoType)
        // 单条工作台:精确回到这条视频当初的模式(对话式 / 粘贴文案)。
        if (!fromBatch) {
          localStorage.setItem(`media-buddy.entry-mode.${channelId}.${videoType}`, wasPaste ? 'script' : 'chat')
        }
        localStorage.removeItem(`${sKey}.${channelId}`)
        localStorage.removeItem(`${dKey}.${channelId}`)
      }
      localStorage.removeItem(sKey)
      localStorage.removeItem(dKey)
    } catch {/* noop */}
    navigate(
      fromBatch && channelId
        ? `/channels/${channelId}`
        : channelId
          ? `/channels/${channelId}/${isLong ? 'youtube' : 'shorts'}`
          : '/channels',
    )
  }

  return (
    <>
      <header className="page-header">
        <div className="crumb">
          <span className="seg">Projects</span>
          <span className="sep">/</span>
          <span className="here">{displayName}</span>
        </div>
        <Link
          to={backTo}
          className="page-back-link"
          onClick={(e) => {
            // 真正"返回上一级":用户从短/长视频工作台进来的,点返回应回到那里,
            // 而不是永远跳到项目总览。有站内历史(location.key!=='default')就走浏览器
            // 历史真返回;直接开链接/刷新进来的没有站内历史,才兜底到 backTo(总览)。
            if (location.key !== 'default') {
              e.preventDefault()
              navigate(-1)
            }
          }}
        >← {t('projectDetail.backToParent')}</Link>
      </header>
      <div className="content-card">
        {!project ? (
          <p className="hint">{t('projectDetail.loading')}</p>
        ) : (
          <>
            <header className="page-head">
              <h1>{displayName}</h1>
              <div className="meta">
                <span>{t('projectDetail.mode')}: {project.mode}</span>
                <span>{t('projectDetail.format')}: {project.output_format}</span>
                <span>{t('projectDetail.status')}: {status?.overall_status || project.status}</span>
                {project.created_at && (
                  <span>{t('projectDetail.createdAt')}: {formatDateTime(project.created_at)}</span>
                )}
                {(downloaded || !!project.downloaded_at) && (
                  <span className="meta-downloaded">✓ {t('projectsPage.downloaded')}</span>
                )}
              </div>
              <button
                type="button"
                className="project-id-chip"
                onClick={handleCopyId}
                title={t('projectDetail.copyIdTooltip')}
              >
                {t('projectDetail.projectId')}: <code>{project.id}</code> <span aria-hidden>📋 {t('projectDetail.copy')}</span>
              </button>
            </header>

            <PipelineProgress status={status} outputFormat={project?.output_format} />

            {(project.status === 'paused_auth_required' || status?.overall_status === 'paused_auth_required') && (
              <div className="error-banner">
                {t('projectDetail.authPausedBanner')}
              </div>
            )}

            {['pending', 'paused_auth_required'].includes(project.status) && (
              <button className="btn-primary" onClick={startPipeline}>{t('projectDetail.startPipeline')}</button>
            )}

            {isComplete && id && (
              <div className="output">
                <h3>{t('projectDetail.generationComplete')} ✓</h3>
                <video
                  className="output-video"
                  src={api.projects.outputUrl(id)}
                  controls
                  // Strip the player's whole ⋮ overflow menu (download / playback
                  // rate / cast / picture-in-picture). The native "download" there
                  // bypasses our handler so it never marks 已下载; with every
                  // overflow item gone, Chrome hides the ⋮ entirely → the ONLY
                  // download path is the tracked "下载视频" button.
                  controlsList="nodownload noplaybackrate noremoteplayback"
                  disablePictureInPicture
                  preload="metadata"
                />
                <div className="path-row">
                  <code className="path" title={t('projectDetail.clickToCopy')} onClick={handleCopyPath}>
                    {status!.output_path}
                  </code>
                </div>
                <div className="output-actions">
                  <button className="btn-secondary" onClick={handleOpenFolder}>
                    📁 {t('projectDetail.openFolder')}
                  </button>
                  <a
                    href={api.projects.outputUrl(id)}
                    download={`${project.name || 'video'}.mp4`}
                    className="btn-secondary"
                    onClick={() => { setDownloaded(true); if (id) api.projects.markDownloaded(id).catch(() => {}) }}
                  >
                    ⬇ {t('projectDetail.downloadVideo')}
                  </a>
                  <button
                    className="btn-primary"
                    onClick={handlePublish}
                    disabled={seoLoading}
                    title={t('projectDetail.publishVideoHint')}
                  >
                    {seoLoading ? `⏳ ${t('projectDetail.publishing')}` : `🚀 ${t('projectDetail.publishVideo')}`}
                  </button>
                  <button
                    className="btn-secondary"
                    onClick={handleAnotherOne}
                    title={t('projectDetail.anotherOneHint')}
                  >
                    ➕ {t('projectDetail.anotherOne')}
                  </button>
                </div>
                {seoError && <div className="error-banner small">{seoError}</div>}
                {folderToast && <div className="folder-toast">{folderToast}</div>}
                {project.expires_at && !project.storage_purged_at && (() => {
                  const s = retentionState(project.expires_at, project.storage_purged_at)
                  const kept = project.retention_intent === 'keep'
                  const statusText =
                    s.daysLeft !== null && s.daysLeft <= 0
                      ? t('projectsPage.retentionExpired')
                      : s.daysLeft === 1
                      ? t('projectsPage.retentionTomorrow')
                      : s.tone === 'green'
                      ? t('projectsPage.retentionSafe', { days: s.daysLeft })
                      : t('projectsPage.retentionDaysLeft', { days: s.daysLeft })
                  return (
                    <div className="retention-card">
                      <div className="retention-card-left">
                        <span className="retention-card-icon">☁</span>
                        <div className="retention-card-text">
                          <span className="retention-card-title">{t('projectDetail.retentionTitle')}</span>
                          <span className="retention-card-status">
                            <i className={`retention-dot retention-dot-${s.tone}`} />
                            {statusText}
                          </span>
                        </div>
                      </div>
                      <div className="retention-card-actions">
                        <button
                          type="button"
                          className={`retention-fav-btn${kept ? ' is-kept' : ''}`}
                          onClick={handleToggleFavorite}
                          disabled={retentionBusy}
                          title={kept ? t('projectsPage.retentionKept') : t('projectsPage.retentionKeep')}
                        >
                          <span className="star">{kept ? '★' : '☆'}</span>
                          {kept ? t('projectsPage.retentionKept') : t('projectsPage.retentionKeep')}
                        </button>
                      </div>
                    </div>
                  )
                })()}
              </div>
            )}

            {showSeo && seoPack && (
              <SeoPackDetailModal
                pack={seoPack}
                project={{
                  id: id!,
                  name: project.name || '',
                  output_format: project.output_format || seoPack.platform,
                  output_path: status?.output_path || '',
                  created_at: project.created_at || '',
                  has_seo_pack: true,
                  series_id: project.series_id ?? null,
                } satisfies PublishProject}
                onClose={() => setShowSeo(false)}
              />
            )}

            {error && <div className="error-banner">{error}</div>}

            {studioSession && studioSession.messages.length > 0 && (
              <section className="chat-history-section">
                <header
                  className="chat-history-header"
                  onClick={() => setChatExpanded((v) => !v)}
                >
                  <span className="chat-history-title">
                    💬 {t('projectDetail.chatHistoryTitle')}
                    <span className="chat-history-count">
                      ({t('projectDetail.messageCount', { count: studioSession.messages.length })} · {t('projectDetail.turnCount', { count: studioSession.turn_count })})
                    </span>
                  </span>
                  <span className="chat-history-toggle">
                    {chatExpanded ? `${t('projectDetail.collapse')} ▴` : `${t('projectDetail.expand')} ▾`}
                  </span>
                </header>
                {chatExpanded && (
                  <div className="chat-history-body">
                    {studioSession.messages.map((m, i) => (
                      <div
                        key={i}
                        className={`chat-history-bubble chat-${m.role}${
                          m.blocked_by_safety ? ' chat-blocked' : ''
                        }`}
                      >
                        <div className="chat-history-role">
                          {m.role === 'user' ? t('projectDetail.roleYou') : m.role === 'assistant' ? 'AI' : m.role}
                          {m.ts && (
                            <span className="chat-history-ts">
                              {new Date(m.ts).toLocaleString('zh-CN', {
                                month: '2-digit',
                                day: '2-digit',
                                hour: '2-digit',
                                minute: '2-digit',
                              })}
                            </span>
                          )}
                        </div>
                        <div className="chat-history-content">{m.content}</div>
                        {m.tool_call?.name && (
                          <div className="chat-history-tool">
                            🔧 {m.tool_call.name}
                            {m.tool_call.error && (
                              <span className="chat-history-error">
                                {' '}— {m.tool_call.error}
                              </span>
                            )}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </section>
            )}

            {SHOW_DIAGNOSTIC_TIMELINE && id && <Timeline projectId={id} />}
          </>
        )}
      </div>
    </>
  )
}
