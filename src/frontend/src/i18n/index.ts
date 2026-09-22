import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'

import en from './locales/en'
import zhCN from './locales/zh-CN'
import zhHant from './locales/zh-Hant'

export const LANGUAGE_STORAGE_KEY = 'media_buddy_language'

// Only the two languages we actually ship. fr/es/ja were offered in the switcher
// mostly-English UI with a French label on it. Retired rather than half-shipped.
// A user with a retired code in localStorage falls through initialLanguage()'s
// membership check and lands on 'en'.
export const SUPPORTED_LANGUAGES = [
  { code: 'en', label: 'English' },
  { code: 'zh-CN', label: '简体中文' },
  { code: 'zh-Hant', label: '繁體中文' },
] as const

export type SupportedLanguage = typeof SUPPORTED_LANGUAGES[number]['code']

const resources = {
  en: { translation: en },
  'zh-CN': { translation: zhCN },
  'zh-Hant': { translation: zhHant },
}

// 首次进入(没手动选过语言)时,按浏览器/操作系统语言自动切。只有中/英两种:
// 浏览器/系统语言是中文 → 'zh-CN',其余(fr/es/ja/de…)→ 'en'。手写 3 行、零依赖
// (不引 i18next-browser-languagedetector,免得它自建 i18nextLng 缓存 key 与
// media_buddy_language 双 key 冲突、把检测值当"用户选择"持久化)。
function detectBrowserLanguage(): SupportedLanguage {
  if (typeof navigator === 'undefined') return 'en'
  const langs = [navigator.language, ...(navigator.languages || [])].map((l) => String(l || '').toLowerCase())
  if (langs.some((l) => l.startsWith('zh') && /(tw|hk|mo|hant)/.test(l))) return 'zh-Hant'
  if (langs.some((l) => l.startsWith('zh'))) return 'zh-CN'
  return 'en'
}

function initialLanguage(): SupportedLanguage {
  const saved = localStorage.getItem(LANGUAGE_STORAGE_KEY)
  if (SUPPORTED_LANGUAGES.some((l) => l.code === saved)) {
    return saved as SupportedLanguage  // 用户手动选过(setAppLanguage 写的 key)→ 尊重,不自动检测
  }
  // 首次/没手动选过 → 按浏览器语言自动切。刻意不写 localStorage:保持"未选择"状态,
  // 让老用户/手动选择逻辑不变(点了切换器才落 key、之后固定)。
  return detectBrowserLanguage()
}

void i18n.use(initReactI18next).init({
  resources,
  lng: initialLanguage(),
  fallbackLng: 'en',
  interpolation: {
    escapeValue: false,
  },
})

export function setAppLanguage(language: SupportedLanguage) {
  localStorage.setItem(LANGUAGE_STORAGE_KEY, language)
  void i18n.changeLanguage(language)
}

// Content output language (zh|en) follows the UI language: Chinese UI → Chinese
// content, every other UI (en/fr/es/ja) → English content (the pipeline only
// supports zh|en output). Drives topic recommendations + the batch/project
// output_language so an English-UI user gets fully English videos.
export function outputLanguageFromUi(): 'zh' | 'en' {
  return String(i18n.language || '').toLowerCase().startsWith('zh') ? 'zh' : 'en'
}

// 注意:这和 outputLanguageFromUi 不同——繁体的【内容/视频】语言仍是 'zh'(中文出片),
export function billingLangFromUi(): 'zh' | 'en' {
  return i18n.language === 'zh-CN' ? 'zh' : 'en'
}

// 字幕语言的【界面】默认：中文界面 → 简体，繁体界面 → 繁体，其余 → 英文。
//
//    真正该用的是 `effectiveSubtitleLanguage()` —— 它会先看出片语言。
//    原因：客户在中文界面下把出片语言切成英文，字幕却还停在简体中文，
//    忘了手动改 → 英文片配中文字幕 → 整条废掉 → 客户来退钱。
//    （双语字幕仍然只能手动选，中文配音 + 中英双语字幕是常见做法。）
export function subtitleLanguageFromUi(): 'zh-Hans' | 'zh-Hant' | 'en' {
  const lng = String(i18n.language || '').toLowerCase()
  if (lng === 'zh-hant') return 'zh-Hant'  // 繁体界面 → 默认繁体字幕(后端 opencc s2twp 支持)
  return lng.startsWith('zh') ? 'zh-Hans' : 'en'
}

export type SubtitleLangValue = 'zh-Hans' | 'zh-Hant' | 'en' | 'zh-Hans+en' | 'zh-Hant+en'

/**
 * 字幕到底用哪个语言（界面上显示的、以及真正发给后端的）。
 *
 * 客户自己存过就用客户的；没存过就按【出片语言】推，而不是按界面语言。
 *
 *    旧逻辑默认给简体中文字幕 → 英文片底下跑中文字 → 整条报废。
 *    而且【存过频道默认值】的客户根本不会触发任何 onChange，
 *    所以光在切语言时修还不够，【提交时的兜底】也必须看出片语言。
 */
export function effectiveSubtitleLanguage(
  saved: string | undefined,
  output: 'zh' | 'en' | undefined,
): SubtitleLangValue {
  if (saved) return saved as SubtitleLangValue
  return output === 'en' ? 'en' : subtitleLanguageFromUi()
}

/**
 * 出片语言刚被切换 → 字幕语言要不要跟着改？
 * 返回新值；返回 `undefined` = 不用改。
 *
 * 规则：**只在字幕里压根没有目标语言时才动它**。
 *   出英文 + 字幕是纯中文  → 换成 'en'
 *   出中文 + 字幕是纯英文  → 换回界面对应的中文（繁体界面给繁体）
 *   双语 'zh-*+en'         → **两个方向都不动**，那是客户特意挑的
 *
 * 🚨 和【配音】的处理保持一致（见 WorkspacePages 的 `onLanguageChange`）：
 *    配音也是「已经对得上就不动、对不上才换」。不搞「客户碰过没碰过」的标记 ——
 *    配音那边就没有，两套逻辑不一致只会更难懂。
 */
export function subtitleLanguageForOutput(
  output: 'zh' | 'en',
  current: string | undefined,
): SubtitleLangValue | undefined {
  // 🚨 比的是【生效值】不是原始字段：中文界面下 `current` 为空时，
  //    字段看不出东西，但实际生效的就是 'zh-Hans' —— 正是要修的那个 case。
  //    这里故意按【切换前】的出片语言还原客户当时看到的默认。
  const cur = effectiveSubtitleLanguage(current, output === 'en' ? 'zh' : 'en')
  const hasEn = cur === 'en' || cur.endsWith('+en')
  const hasZh = cur.startsWith('zh')
  if (output === 'en') return hasEn ? undefined : 'en'
  return hasZh ? undefined : subtitleLanguageFromUi()
}

// 配音试听样本文案:跟随该场景的语言。中文页/中文批量 → 中文文案;英文页/英文批量
// → 英文文案。千问 TTS 从文本自动判定语种(_qwen_language),所以传对语言的文案 =
// 出对语言的试听音频。中文串与后端 /api/tts/preview 默认文案一致(缓存/体验统一)。
export function ttsPreviewText(lang: 'zh' | 'en'): string {
  return lang === 'en'
    ? 'Hi, this is a Media Buddy voiceover preview. Every great story begins with curiosity.'
    : '你好，这是 Media Buddy 的配音试听。'
}

export default i18n
