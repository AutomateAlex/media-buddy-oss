/**
 * VoicePicker — server-driven ElevenLabs voice catalogue for the Studio
 * right-rail. Loads `/api/tts/voices` once on mount and renders a
 * scrollable card list with one preview-audio button per voice.
 *
 * Selection state is persisted in localStorage under VOICE_PICK_KEY so
 * the choice survives chat reloads. The pipeline reads the same key
 * when it builds the TTS request, so picking here drives video gen.
 */
import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { api } from '../api/client'
import { outputLanguageFromUi, ttsPreviewText } from '../i18n'
import { voiceDisplayName, voiceDescription } from '../lib/voiceLabels'
import type { TtsVoice } from '../types'

const VOICE_PICK_KEY = 'media-buddy.tts.selected-voice'

export function getSelectedVoiceProvider(): string | null {
  return localStorage.getItem(VOICE_PICK_KEY)
}

const OPEN_KEY = 'media-buddy.tts.picker-open'
const SPEED_KEY = 'media-buddy.tts.preview-speed'

// 试听语速档:正常1.0 / 稍快1.2 / 快1.4(默认) / 极快1.6 —— 与批量出片语速档一致。
const PREVIEW_SPEEDS: { value: number; labelKey: string }[] = [
  { value: 1.0, labelKey: 'voicePicker.speedNormal' },
  { value: 1.2, labelKey: 'voicePicker.speedSlightlyFast' },
  { value: 1.4, labelKey: 'voicePicker.speedFast' },
  { value: 1.6, labelKey: 'voicePicker.speedVeryFast' },
]

function readPreviewSpeed(): number {
  const raw = Number(localStorage.getItem(SPEED_KEY))
  return raw >= 0.7 && raw <= 2.0 ? raw : 1.4
}

export function VoicePicker() {
  const { t } = useTranslation()
  const [voices, setVoices] = useState<TtsVoice[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(() =>
    localStorage.getItem(VOICE_PICK_KEY),
  )
  const [playing, setPlaying] = useState<string | null>(null)
  const [previewLoading, setPreviewLoading] = useState<string | null>(null)
  const [speed, setSpeed] = useState<number>(() => readPreviewSpeed())
  const [open, setOpen] = useState<boolean>(
    () => localStorage.getItem(OPEN_KEY) === '1',
  )
  const audioRef = useRef<HTMLAudioElement | null>(null)

  const toggleOpen = () => {
    setOpen((prev) => {
      const next = !prev
      localStorage.setItem(OPEN_KEY, next ? '1' : '0')
      // Pause any preview when collapsing so audio doesn't keep playing
      // out of sight.
      if (!next && audioRef.current) {
        audioRef.current.pause()
        audioRef.current = null
        setPlaying(null)
      }
      return next
    })
  }

  useEffect(() => {
    let cancelled = false
    api.tts
      .listVoices()
      .then((res) => {
        if (cancelled) return
        setVoices(res.voices)
        // Auto-select the first voice if user hasn't picked one yet — Studio's
        // pipeline always needs *some* voice, and the first entry is curated
        // to be the recommended default.
        if (!localStorage.getItem(VOICE_PICK_KEY) && res.voices[0]) {
          const k = res.voices[0].provider_key
          localStorage.setItem(VOICE_PICK_KEY, k)
          setSelected(k)
        }
      })
      .catch((e) => {
        if (cancelled) return
        const msg =
          e?.response?.data?.detail ||
          e?.message ||
          t('voicePicker.loadFailed')
        setError(String(msg))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  // Stop any preview audio when the component unmounts (e.g. user
  // navigates away from Studio).
  useEffect(() => {
    return () => {
      audioRef.current?.pause()
      audioRef.current = null
    }
  }, [])

  const handleSelect = (providerKey: string) => {
    setSelected(providerKey)
    localStorage.setItem(VOICE_PICK_KEY, providerKey)
  }

  const handlePreview = (voice: TtsVoice) => {
    // Toggle: if this voice is already playing/loading, stop. Otherwise swap.
    if (playing === voice.provider_key || previewLoading === voice.provider_key) {
      audioRef.current?.pause()
      audioRef.current = null
      setPlaying(null)
      setPreviewLoading(null)
      return
    }
    audioRef.current?.pause()
    // 千问音色目录没有静态 sample_url(见 api/tts.py)→ 走 /api/tts/preview 现场按
    // 当前所选语速合成(~1-2s)。试听语言跟随 UI 语言(中文页中文试听/英文页英文试听)。
    const src = voice.sample_url
      || api.tts.previewUrl(voice.provider_key, speed, ttsPreviewText(outputLanguageFromUi()))
    const audio = new Audio(src)
    audio.onplaying = () => {
      if (audioRef.current === audio) {
        setPreviewLoading(null)
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
        setPreviewLoading(null)
        setError(t('voicePicker.cannotPlaySample', { name: voiceDisplayName(t, voice) }))
      }
    }
    audioRef.current = audio
    setPreviewLoading(voice.provider_key)
    setError(null)
    audio.play().catch((e) => {
      audioRef.current = null
      setPlaying(null)
      setPreviewLoading(null)
      setError(t('voicePicker.playbackFailed', { error: e?.message ?? e }))
    })
  }

  const handleSpeedChange = (next: number) => {
    setSpeed(next)
    localStorage.setItem(SPEED_KEY, String(next))
    // 换速度时停掉正在播的试听,避免和新速度混淆。
    audioRef.current?.pause()
    audioRef.current = null
    setPlaying(null)
    setPreviewLoading(null)
  }

  const selectedVoice = voices.find((v) => v.provider_key === selected) ?? null
  const summary = loading
    ? t('voicePicker.loading')
    : selectedVoice
      ? voiceDisplayName(t, selectedVoice)
      : voices.length === 0
        ? t('voicePicker.noVoices')
        : t('voicePicker.notSelected')

  return (
    <div className={`voice-picker-card${open ? ' open' : ''}`}>
      <button
        type="button"
        className="voice-picker-header"
        onClick={toggleOpen}
        aria-expanded={open}
      >
        <span className="voice-picker-title">
          🎙️ <span className="voice-picker-label">{t('voicePicker.voiceover')}</span>
          {!loading && (
            <span className="voice-picker-current"> · {summary}</span>
          )}
        </span>
        <span className={`voice-picker-caret${open ? ' open' : ''}`} aria-hidden>
          ▾
        </span>
      </button>
      {open && (
        <div className="voice-picker-body">
          {loading && <p className="hint">{t('voicePicker.loading')}</p>}
          {error && <div className="error-banner small">{error}</div>}
          {!loading && voices.length === 0 && !error && (
            <p className="hint">{t('voicePicker.noVoices')}</p>
          )}
          {!loading && voices.length > 0 && (
            <div className="voice-speed-row">
              <span className="voice-speed-label">{t('voicePicker.previewSpeed')}</span>
              <div className="voice-speed-options">
                {PREVIEW_SPEEDS.map((s) => (
                  <button
                    key={s.value}
                    type="button"
                    className={`voice-speed-chip${speed === s.value ? ' active' : ''}`}
                    onClick={() => handleSpeedChange(s.value)}
                  >
                    {t(s.labelKey)}
                  </button>
                ))}
              </div>
            </div>
          )}
          <ul className="voice-list">
            {voices.map((v) => {
              const isSelected = selected === v.provider_key
              const isPlaying = playing === v.provider_key
              const isPreviewLoading = previewLoading === v.provider_key
              const initial = voiceDisplayName(t, v).charAt(0).toUpperCase()
              return (
                <li
                  key={v.provider_key}
                  className={`voice-row${isSelected ? ' selected' : ''}`}
                  onClick={() => handleSelect(v.provider_key)}
                  role="button"
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault()
                      handleSelect(v.provider_key)
                    }
                  }}
                >
                  <div className={`voice-avatar gender-${v.gender} lang-${v.language_hint}`}>
                    {initial}
                  </div>
                  <div className="voice-meta">
                    <div className="voice-name">
                      {voiceDisplayName(t, v)}
                      {isSelected && <span className="voice-pick"> · {t('voicePicker.selectedTag')}</span>}
                    </div>
                    <div className="voice-desc">{voiceDescription(t, v)}</div>
                  </div>
                  <button
                    type="button"
                    className={`voice-play${isPlaying ? ' playing' : ''}${isPreviewLoading ? ' loading' : ''}`}
                    onClick={(e) => {
                      e.stopPropagation()
                      handlePreview(v)
                    }}
                    title={isPlaying || isPreviewLoading ? t('voicePicker.stopPreview') : t('voicePicker.preview')}
                    aria-label={isPlaying || isPreviewLoading ? t('voicePicker.stopPreview') : t('voicePicker.preview')}
                  >
                    {isPreviewLoading ? '⋯' : isPlaying ? '■' : '▶'}
                  </button>
                </li>
              )
            })}
          </ul>
        </div>
      )}
    </div>
  )
}
