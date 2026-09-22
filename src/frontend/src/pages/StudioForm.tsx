import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, apiErrorText } from '../api/client'
import { alertDialog } from '../components/Dialog'
import { QWEN_VOICES, QWEN_VOICE_DEFAULT } from '../data/qwenVoices'
import { voiceDisplayName } from '../lib/voiceLabels'
import type { ProjectMode } from '../types'

export function StudioForm() {
  const nav = useNavigate()
  const { t } = useTranslation()
  const [mode, setMode] = useState<ProjectMode>('script')
  const [name, setName] = useState('')
  const [scriptText, setScriptText] = useState('')
  const [prompt, setPrompt] = useState('')
  const [outputFormat, setOutputFormat] = useState('youtube_landscape')
  const [ttsProvider, setTtsProvider] = useState(QWEN_VOICE_DEFAULT)
  const [includeSubtitles, setIncludeSubtitles] = useState(true)
  const [submitting, setSubmitting] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setSubmitting(true)
    try {
      const project = await api.projects.create({
        name: name || `Untitled ${new Date().toLocaleString()}`,
        mode,
        script_text: mode === 'script' ? scriptText : undefined,
        prompt: mode === 'creative' ? prompt : undefined,
        output_format: outputFormat,
        tts_provider: ttsProvider,
        include_subtitles: includeSubtitles,
      })
      await api.pipeline.run(project.id)
      nav(`/project/${project.id}`)
    } catch (e: any) {
      void alertDialog(apiErrorText(e))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <>
      <header className="page-header">
        <div className="crumb">
          <span className="seg">Studio</span>
          <span className="sep">/</span>
          <span className="here">{t('studioForm.title')}</span>
        </div>
      </header>
      <div className="content-card">
        <header className="page-head">
          <h1>{t('studioForm.title')}</h1>
          <p className="lede">{t('studioForm.lede')}</p>
        </header>
        <form onSubmit={handleSubmit} className="form">
          <label>
            {t('studioForm.projectName')}
            <input value={name} onChange={(e) => setName(e.target.value)} />
          </label>

          <div className="mode-toggle">
            <button
              type="button"
              className={mode === 'script' ? 'active' : ''}
              onClick={() => setMode('script')}
            >
              Script Mode
            </button>
            <button
              type="button"
              className={mode === 'creative' ? 'active' : ''}
              onClick={() => setMode('creative')}
            >
              Creative Mode
            </button>
          </div>

          {mode === 'script' ? (
            <label>
              {t('studioForm.scriptLabel')}
              <textarea rows={8} value={scriptText} onChange={(e) => setScriptText(e.target.value)} required />
            </label>
          ) : (
            <label>
              {t('studioForm.creativePromptLabel')}
              <textarea rows={4} value={prompt} onChange={(e) => setPrompt(e.target.value)} required />
            </label>
          )}

          <label>
            {t('studioForm.outputFormat')}
            <select value={outputFormat} onChange={(e) => setOutputFormat(e.target.value)}>
              <option value="youtube_landscape">{t('studioForm.formatYoutubeLandscape')}</option>
              <option value="youtube_shorts">{t('studioForm.formatYoutubeShorts')}</option>
              <option value="tiktok">{t('studioForm.formatTiktok')}</option>
              <option value="instagram_reels">{t('studioForm.formatInstagramReels')}</option>
            </select>
          </label>

          <label>
            {t('studioForm.ttsProvider')}
            <select value={ttsProvider} onChange={(e) => setTtsProvider(e.target.value)}>
              <optgroup label={t('studioForm.cloudVoices')}>
                {QWEN_VOICES.map((v) => (
                  <option key={v.provider_key} value={v.provider_key}>{voiceDisplayName(t, v)}</option>
                ))}
              </optgroup>
            </select>
          </label>

          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={includeSubtitles}
              onChange={(e) => setIncludeSubtitles(e.target.checked)}
            />
            <span>
              <span className="checkbox-label">{t('studioForm.includeSubtitles')}</span>
              <span className="checkbox-hint">{t('studioForm.includeSubtitlesHint')}</span>
            </span>
          </label>

          <button type="submit" className="btn-primary" disabled={submitting}>
            {submitting ? t('studioForm.generating') : t('studioForm.startGenerate')}
          </button>
        </form>
      </div>
    </>
  )
}
