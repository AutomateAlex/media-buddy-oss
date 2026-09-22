/**
 * VoicePreviewList — a controlled voice picker with per-row preview.
 *
 * Unlike VoicePicker (self-contained, localStorage-driven, for the Studio
 * rail), this is a plain controlled component: pass `voices`, the selected
 * `value` (provider_key), and `onChange`. Each row has a ▶/■ button that
 * auditions that voice's `sample_url` without committing the selection —
 * click the row to select. Used in the channel batch-settings dialog so
 * users can hear voices before picking, same as Studio.
 */
import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { api } from '../api/client'
import { ttsPreviewText } from '../i18n'
import { voiceDisplayName, voiceDescription } from '../lib/voiceLabels'
import type { TtsVoice } from '../types'

export function VoicePreviewList({
  voices,
  value,
  onChange,
  speed = 1.0,
  lang = 'zh',
}: {
  voices: TtsVoice[]
  value: string
  onChange: (providerKey: string) => void
  // 试听语速:跟随批量设置里选的语速档,让"不同语速的试听"真实反映出片速度。
  speed?: number
  // 试听语言:跟随批量输出语言(中文批量中文试听/英文批量英文试听)。
  lang?: 'zh' | 'en'
}) {
  const { t } = useTranslation()
  const [playing, setPlaying] = useState<string | null>(null)
  const [loading, setLoading] = useState<string | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)

  // Stop any preview when the list unmounts (dialog closes / nav away).
  useEffect(() => {
    return () => {
      audioRef.current?.pause()
      audioRef.current = null
    }
  }, [])

  const handlePreview = (voice: TtsVoice) => {
    // Toggle: clicking the playing/loading voice stops it; otherwise swap.
    if (playing === voice.provider_key || loading === voice.provider_key) {
      audioRef.current?.pause()
      audioRef.current = null
      setPlaying(null)
      setLoading(null)
      return
    }
    audioRef.current?.pause()
    setFailed(null)
    // ElevenLabs 普通话目录没有静态 sample_url(见 api/tts.py),改走现场合成
    // /api/tts/preview(client 的 previewUrl 会带上 baseURL + token)。有静态
    const src = voice.sample_url || api.tts.previewUrl(voice.provider_key, speed, ttsPreviewText(lang))
    const audio = new Audio(src)
    audio.onplaying = () => {
      if (audioRef.current === audio) {
        setLoading(null)
        setPlaying(voice.provider_key)
      }
    }
    audio.onended = () => {
      if (audioRef.current === audio) {
        audioRef.current = null
        setPlaying(null)
      }
    }
    audio.onerror = () => {
      if (audioRef.current === audio) {
        audioRef.current = null
        setPlaying(null)
        setLoading(null)
        setFailed(voice.provider_key)
      }
    }
    audioRef.current = audio
    setLoading(voice.provider_key) // 现场合成 ~1-2s,先转圈
    audio.play().catch(() => {
      audioRef.current = null
      setPlaying(null)
      setLoading(null)
      setFailed(voice.provider_key)
    })
  }

  if (!voices.length) {
    return <p className="hint">{t('voicePicker.noVoices')}</p>
  }

  return (
    <ul className="voice-list voice-list-compact">
      {voices.map((v) => {
        const isSelected = value === v.provider_key
        const isPlaying = playing === v.provider_key
        const isLoading = loading === v.provider_key
        const isFailed = failed === v.provider_key
        const initial = voiceDisplayName(t, v).charAt(0).toUpperCase()
        return (
          <li
            key={v.provider_key}
            className={`voice-row${isSelected ? ' selected' : ''}`}
            onClick={() => onChange(v.provider_key)}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                onChange(v.provider_key)
              }
            }}
          >
            <div className={`voice-avatar gender-${v.gender} lang-${v.language_hint}`}>
              {initial}
            </div>
            <div className="voice-meta">
              <div className="voice-name">
                {voiceDisplayName(t, v)}
                {isSelected && (
                  <span className="voice-pick"> · {t('voicePicker.selectedTag')}</span>
                )}
              </div>
              {voiceDescription(t, v) && <div className="voice-desc">{voiceDescription(t, v)}</div>}
            </div>
            <button
              type="button"
              className={`voice-play${isPlaying ? ' playing' : ''}${isLoading ? ' loading' : ''}`}
              onClick={(e) => {
                e.stopPropagation()
                handlePreview(v)
              }}
              title={
                isFailed
                  ? t('voicePicker.cannotPlaySample', { name: voiceDisplayName(t, v) })
                  : isPlaying || isLoading
                    ? t('voicePicker.stopPreview')
                    : t('voicePicker.preview')
              }
              aria-label={isPlaying ? t('voicePicker.stopPreview') : t('voicePicker.preview')}
            >
              {isLoading ? '⋯' : isFailed ? '–' : isPlaying ? '■' : '▶'}
            </button>
          </li>
        )
      })}
    </ul>
  )
}
