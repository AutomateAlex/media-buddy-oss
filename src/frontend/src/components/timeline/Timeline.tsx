import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { api } from '../../api/client'
import type { ChunkInfo, ShotInfo } from '../../types'

interface Props {
  projectId: string
}

export function Timeline({ projectId }: Props) {
  const { t } = useTranslation()
  const [chunks, setChunks] = useState<ChunkInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const refresh = async () => {
    try {
      const data = await api.projects.chunks(projectId)
      setChunks(data)
    } catch {
      // Backend may return empty list for projects that haven't generated yet.
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void refresh() }, [projectId])

  if (loading) return <p className="hint">{t('timeline.loadingChunks')}</p>
  if (chunks.length === 0) {
    return (
      <p className="hint">
        {t('timeline.emptyHint')}
      </p>
    )
  }

  const selected = chunks.find((c) => c.id === selectedId) || null

  return (
    <section className="timeline-editor">
      <header className="page-head">
        <div>
          <h2>{t('timeline.title')}</h2>
          <p className="lede">
            {t('timeline.subtitle')}
          </p>
        </div>
        <button className="btn-secondary" type="button" onClick={refresh}>
          {t('timeline.refresh')}
        </button>
      </header>

      <div className="timeline-track">
        <span className="timeline-track-label">{t('timeline.trackLabel')}</span>
        <div className="timeline-chunks">
          {chunks.map((c) => {
            const flagged = c.status === 'failed' || c.status === 'rejected_retry'
            const warn = (c.mm_critic_avg ?? 10) < 7.5
            const cls = [
              'timeline-chunk',
              flagged ? 'flagged' : '',
              !flagged && warn ? 'warn' : '',
              c.id === selectedId ? 'selected' : '',
            ].filter(Boolean).join(' ')
            return (
              <button
                key={c.id}
                className={cls}
                onClick={() => setSelectedId(c.id === selectedId ? null : c.id)}
                title={c.sentence}
              >
                <div className="chunk-idx">#{c.chunk_index}</div>
                {c.mm_critic_avg != null && (
                  <div className="chunk-score">{c.mm_critic_avg.toFixed(1)}</div>
                )}
              </button>
            )
          })}
        </div>
      </div>

      {selected && <ChunkInfoPanel chunk={selected} />}
    </section>
  )
}

function valueOrDash(value: string | number | null | undefined) {
  if (value === null || value === undefined || value === '') return '-'
  return value
}

function score(value: number | null | undefined) {
  return value == null ? '-' : value.toFixed(1)
}

function ChunkInfoPanel({ chunk }: { chunk: ChunkInfo }) {
  const { t } = useTranslation()
  return (
    <div className="chunk-edit-panel">
      <header className="page-head">
        <div>
          <h3>
            Chunk #{chunk.chunk_index} · {chunk.shot_type}
            {chunk.is_hero_shot ? ' · HERO' : ''}
          </h3>
          <p className="lede">{chunk.sentence}</p>
        </div>
      </header>

      <div className="chunk-shots">
        <div className="shot-row">
          <span>{t('timeline.status')}</span>
          <span>{chunk.status}</span>
          <span>{t('timeline.textScore')} {score(chunk.text_critic_avg)}</span>
          <span>{t('timeline.visualScore')} {score(chunk.mm_critic_avg)}</span>
        </div>
        <div className="shot-row">
          <span>{t('timeline.voiceoverDuration')}</span>
          <span>{chunk.tts_duration_seconds ? `${chunk.tts_duration_seconds.toFixed(2)}s` : '-'}</span>
          <span>{t('timeline.retryCount')}</span>
          <span>{chunk.retry_count}</span>
        </div>
      </div>

      <div className="chunk-shots">
        {chunk.shots.length === 0 && <p className="hint">{t('timeline.noShotData')}</p>}
        {chunk.shots.map((s) => <ShotInfoRow key={s.id} shot={s} />)}
      </div>
    </div>
  )
}

function ShotInfoRow({ shot }: { shot: ShotInfo }) {
  return (
    <div className="shot-row">
      <span className="shot-idx">shot {shot.shot_index}</span>
      <span className="shot-source">{valueOrDash(shot.selected_source)}</span>
      <span className="shot-score">MM {score(shot.mm_critic_score)}</span>
      <span className="shot-query">{valueOrDash(shot.query)}</span>
    </div>
  )
}
