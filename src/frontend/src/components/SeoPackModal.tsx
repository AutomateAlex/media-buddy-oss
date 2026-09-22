/**
 * SeoPackModal — shared presentation for a generated SEO Pack (the "publish"
 * module output). Extracted from WorkspacePages so both the Publish Center
 * and the per-project detail page ("发布本片") render the same modal.
 */
import { useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'

import { api } from '../api/client'
import type { PublishProject, SeoPack } from '../types'

/**
 * Copy text to the clipboard, robust across contexts. The async Clipboard API
 * silently no-ops in non-secure contexts / some embedded webviews, so fall
 * back to a hidden-textarea + execCommand('copy'). Returns whether it worked.
 */
async function robustCopy(text: string): Promise<boolean> {
  if (!text) return false
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    /* fall through to the textarea fallback */
  }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.top = '-1000px'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.focus()
    ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch {
    return false
  }
}

export function platformLabel(formatOrPlatform: string, t: (key: string) => string) {
  const value = (formatOrPlatform || '').toLowerCase()
  if (value === 'youtube_landscape' || value === 'youtube_long') return t('workspace.longVideo')
  if (value === 'youtube_shorts') return 'YouTube Shorts'
  if (value === 'tiktok') return 'TikTok'
  if (value === 'instagram_reels') return 'Instagram Reels'
  if (value === 'instagram_feed') return 'Instagram Feed'
  return value || t('workspace.unknownPlatform')
}

function copyText(text: string) {
  void robustCopy(text)
}

function SeoField({ label, value }: { label: string; value: string }) {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)
  if (!value) return null
  const onCopy = async () => {
    if (await robustCopy(value)) {
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    }
  }
  return (
    <div className="seo-field">
      <div className="seo-field-head">
        <span>{label}</span>
        <button type="button" className={`btn-row-action${copied ? ' copied' : ''}`} onClick={onCopy}>
          {copied ? `✓ ${t('workspace.copied')}` : t('workspace.copy')}
        </button>
      </div>
      <p>{value}</p>
    </div>
  )
}

function SeoSection({
  title,
  count,
  children,
  defaultOpen = false,
}: {
  title: string
  count: string
  children: ReactNode
  defaultOpen?: boolean
}) {
  return (
    <details className="seo-section" open={defaultOpen}>
      <summary>
        <span>{title}</span>
        <em>{count}</em>
      </summary>
      <div className="seo-section-body">{children}</div>
    </details>
  )
}

export function SeoPackDetailModal({
  pack,
  project,
  onClose,
}: {
  pack: SeoPack
  project?: PublishProject
  onClose: () => void
}) {
  const { t } = useTranslation()
  const [copiedKey, setCopiedKey] = useState<string | null>(null)
  const handleCopy = async (text: string, key: string) => {
    if (await robustCopy(text)) {
      setCopiedKey(key)
      window.setTimeout(() => setCopiedKey((k) => (k === key ? null : k)), 1500)
    }
  }
  const tagsText = pack.tags.join(', ')
  const hashtagsText = pack.hashtags.join(' ')
  const chaptersText = pack.chapters.map((c) => `${c.time} ${c.title}`).join('\n')
  const isLongVideo = (project?.output_format || pack.platform) === 'youtube_landscape' || pack.platform === 'youtube_long'
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-content publish-detail-modal" onClick={(e) => e.stopPropagation()}>
        <button className="modal-close" onClick={onClose}>×</button>
        <div className="modal-body">
          <header className="publish-detail-head">
            <div>
              <span className="workspace-eyebrow">{platformLabel(project?.output_format || pack.platform, t)}</span>
              <h3>{pack.project_name}</h3>
            </div>
            <button type="button" className="btn-secondary" onClick={() => copyText(project?.output_path || '')}>
              {t('workspace.copyVideoPath')}
            </button>
          </header>

          <section className="publish-detail-grid">
            <div className="publish-video-preview">
              {project ? (
                <video
                  src={api.projects.outputUrl(project.id)}
                  controls
                  preload="metadata"
                />
              ) : (
                <div className="publish-video-empty">{t('workspace.noVideoPreview')}</div>
              )}
              <p>{project?.output_path || t('workspace.noVideoPath')}</p>
            </div>
            <div className="publish-asset-status">
              <strong>{t('workspace.uploadReady')}</strong>
              <span>{t('workspace.videoFileLabel')}{project?.output_path ? t('workspace.ready') : t('workspace.missing')}</span>
              <span>{t('workspace.seoPackGenerated')}</span>
              <span>{t('workspace.coverLabel')}{isLongVideo ? t('workspace.coverNeededLong') : t('workspace.coverNotNeededShort')}</span>
            </div>
            {isLongVideo && project?.output_path && /\.mp4$/i.test(project.output_path) && (
              <div className="publish-cover-preview">
                <span className="publish-cover-title">{t('workspace.coverPreview')}</span>
                <img
                  src={project.output_path.replace(/\.mp4$/i, '.cover.png')}
                  alt="cover"
                  loading="lazy"
                  onError={(e) => {
                    const el = e.currentTarget.closest('.publish-cover-preview') as HTMLElement | null
                    if (el) el.style.display = 'none'
                  }}
                />
                <a
                  className="btn-secondary"
                  href={project.output_path.replace(/\.mp4$/i, '.cover.png')}
                  target="_blank"
                  rel="noreferrer"
                >
                  {t('workspace.downloadCover')}
                </a>
              </div>
            )}
          </section>

          <section className="seo-detail-sections">
            <SeoSection title={t('workspace.titleOptions')} count={t('workspace.itemsCount', { count: pack.title_options.length })} defaultOpen>
              <div className="seo-title-list">
                {pack.title_options.map((title, i) => {
                  const key = `title-${i}`
                  const isCopied = copiedKey === key
                  return (
                    <div key={`${title}-${i}`} className="seo-title-row">
                      <div className="seo-title-main">
                        <span>{t('workspace.titleN', { n: i + 1 })}</span>
                        <strong>{title}</strong>
                      </div>
                      <button
                        type="button"
                        className={`btn-row-action seo-title-copy${isCopied ? ' copied' : ''}`}
                        onClick={() => handleCopy(title, key)}
                      >
                        {isCopied ? `✓ ${t('workspace.copied')}` : t('workspace.copy')}
                      </button>
                    </div>
                  )
                })}
              </div>
            </SeoSection>
            <SeoSection title={t('workspace.descriptionCaption')} count={pack.description ? t('workspace.paragraphsCount', { count: 1 }) : t('workspace.paragraphsCount', { count: 0 })}>
              <SeoField label={t('workspace.descriptionContent')} value={pack.description} />
            </SeoSection>
            <SeoSection title="Hashtags" count={t('workspace.itemsCount', { count: pack.hashtags.length })}>
              <SeoField label={t('workspace.hashtagsContent')} value={hashtagsText} />
            </SeoSection>
            <SeoSection title="Tags" count={t('workspace.itemsCount', { count: pack.tags.length })}>
              <SeoField label={t('workspace.tagsContent')} value={tagsText} />
            </SeoSection>
            {pack.chapters.length > 0 && (
              <SeoSection title={t('workspace.chapterTimeline')} count={t('workspace.itemsCount', { count: pack.chapters.length })}>
                <SeoField label={t('workspace.chapterContent')} value={chaptersText} />
              </SeoSection>
            )}
            <SeoSection title={t('workspace.pinnedComment')} count={pack.pinned_comment ? t('workspace.linesCount', { count: 1 }) : t('workspace.linesCount', { count: 0 })}>
              <SeoField label={t('workspace.pinnedCommentContent')} value={pack.pinned_comment} />
            </SeoSection>
            <SeoSection title={t('workspace.publishTips')} count={pack.publish_notes ? t('workspace.linesCount', { count: 1 }) : t('workspace.linesCount', { count: 0 })}>
              <SeoField label={t('workspace.publishTipsContent')} value={pack.publish_notes} />
            </SeoSection>
          </section>
        </div>
      </div>
    </div>
  )
}
