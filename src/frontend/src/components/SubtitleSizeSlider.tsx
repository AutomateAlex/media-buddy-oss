import { useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

/**
 * 字幕大小拉杆 + 悬浮实时预览 —— 横向布局,和其他出片设置行(config-row)一致:
 * 左边「字幕大小」标签(88px 列),右边滑杆 + 档位(1fr 列)。返回 Fragment 的两个子项,
 * 直接落进父级 .config-row 网格的两列(否则会被挤进 88px 标签列、竖着摞成"字幕大小")。
 * 鼠标悬停/拖动时,旁边空白区浮现大预览窗(portal 挂 body + fixed,不被设置列裁剪),
 * 字幕随拖动实时变大变小(含双语/繁体)。预览字号 = 画面高 × 基准% × 系数,同后端口径。
 */
export const SUBTITLE_SCALE_MIN = 0.6
export const SUBTITLE_SCALE_MAX = 1.8
export const SUBTITLE_SCALE_DEFAULT = 1.0

const BASE_PCT_LANDSCAPE = 6.2
const BASE_PCT_PORTRAIT = 6.4

const SAMPLE_HANS = '巴菲特为什么坚持只投自己看得懂的生意'
const SAMPLE_HANT = '巴菲特為什麼堅持只投自己看得懂的生意'
const SAMPLE_EN = 'Why Buffett only invests in what he understands'

type SizeLabels = {
  title: string
  hint: string
  small: string
  standard: string
  large: string
  xlarge: string
}

function tierName(scale: number, labels: SizeLabels): string {
  if (scale < 0.9) return labels.small
  if (scale < 1.2) return labels.standard
  if (scale < 1.5) return labels.large
  return labels.xlarge
}

function sampleFor(lang?: string): { primary: string; secondary?: string } {
  const l = lang || 'zh-Hans'
  if (l === 'en') return { primary: SAMPLE_EN }
  const primary = l.startsWith('zh-Hant') ? SAMPLE_HANT : SAMPLE_HANS
  const secondary = l.includes('+en') ? SAMPLE_EN : undefined
  return { primary, secondary }
}

export function SubtitleSizeSlider({
  value,
  onChange,
  labels,
  subtitleLanguage,
  portrait = false,
}: {
  value: number
  onChange: (v: number) => void
  labels: SizeLabels
  subtitleLanguage?: string
  portrait?: boolean
}) {
  const v = Number.isFinite(value) ? value : SUBTITLE_SCALE_DEFAULT
  const basePct = portrait ? BASE_PCT_PORTRAIT : BASE_PCT_LANDSCAPE
  const { primary, secondary } = sampleFor(subtitleLanguage)
  const fillPct = ((v - SUBTITLE_SCALE_MIN) / (SUBTITLE_SCALE_MAX - SUBTITLE_SCALE_MIN)) * 100

  const ctrlRef = useRef<HTMLDivElement>(null)
  const [show, setShow] = useState(false)
  const [pos, setPos] = useState<{ left: number; top: number }>({ left: 0, top: 0 })

  const popW = portrait ? 176 : 320
  const popH = portrait ? Math.round(popW * 16 / 9) : Math.round(popW * 9 / 16)

  useLayoutEffect(() => {
    if (!show || !ctrlRef.current) return
    const r = ctrlRef.current.getBoundingClientRect()
    const gap = 16
    let left = r.right + gap
    if (left + popW > window.innerWidth - 8) left = r.left - popW - gap
    if (left < 8) left = 8
    let top = r.top + r.height / 2 - popH / 2
    top = Math.max(8, Math.min(top, window.innerHeight - popH - 8))
    setPos({ left, top })
  }, [show, v, popW, popH])

  return (
    <>
      <span className="sub-size-title">
        {labels.title}
        <em className="sub-size-hint">{labels.hint}</em>
      </span>

      <div
        className="sub-size-control"
        ref={ctrlRef}
        onMouseEnter={() => setShow(true)}
        onMouseLeave={() => setShow(false)}
      >
        <div className="sub-size-bar">
          <input
            type="range"
            min={SUBTITLE_SCALE_MIN}
            max={SUBTITLE_SCALE_MAX}
            step={0.05}
            value={v}
            onChange={(e) => onChange(Number(e.target.value))}
            onFocus={() => setShow(true)}
            onBlur={() => setShow(false)}
            aria-label={labels.title}
            style={{ ['--fill' as string]: `${fillPct}%` }}
          />
          <strong className="sub-size-current">{tierName(v, labels)}</strong>
        </div>
        <div className="sub-size-ticks">
          <span onClick={() => onChange(0.75)}>{labels.small}</span>
          <span onClick={() => onChange(1.0)}>{labels.standard}</span>
          <span onClick={() => onChange(1.35)}>{labels.large}</span>
          <span onClick={() => onChange(1.7)}>{labels.xlarge}</span>
        </div>

        {show && createPortal(
          <div
            className={`sub-size-pop${portrait ? ' portrait' : ''}`}
            style={{
              left: pos.left, top: pos.top, width: popW, height: popH,
              ['--sub-scale' as string]: String(v), ['--sub-base' as string]: String(basePct),
            }}
          >
            <span className="sub-size-pop-tag">{labels.title} · {tierName(v, labels)}</span>
            <div className="sub-size-pop-cap">
              <span className="main">{primary}</span>
              {secondary && <span className="sub">{secondary}</span>}
            </div>
          </div>,
          document.body,
        )}
      </div>
    </>
  )
}
