/**
 * 预设频道名字：**把库里冻住的那个名字，翻回当前界面语言。**
 *
 *
 * 「财商觉醒·财富智慧频道」「UFO 悬案档案频道」「心理学与人生思考频道」，
 * 而旁边的 Reading & Ideas / Wildlife Science 却是英文。
 *
 * ## 🚨 为什么会这样
 *
 * 这些频道**不是用户自己起的名字，是我们系统的预设**。而建频道时是这么存的：
 *
 * ```ts
 * // pages/Channels.tsx
 * name: customName?.trim() || t(template.nameKey)   // ← 按「当时」的界面语言取值
 * ```
 *
 * 也就是说：**建的那一刻界面是中文，中文名就被写死进了数据库**。
 * 之后不管界面怎么切，库里那一行永远是中文。
 * 那几个英文的，只是建的时候界面恰好是英文而已。
 *
 * `pages/Channels.tsx` 里**早就有**一个 `channelDisplay()` 在做这件事，
 * `series.name`，中文就这么漏出来了。
 *
 * ## 这里比 `channelDisplay()` 多做了一件事
 *
 * 那个函数只拿 `en` 和 `zh-CN` 两个语言去比对：
 *
 * ```ts
 * channel.name === t(tpl.nameKey, { lng: 'en' }) || channel.name === t(tpl.nameKey, { lng: 'zh-CN' })
 * ```
 *
 * **繁体建的频道对不上** —— 名字存的是繁体，两边都不等，于是被当成
 * 「用户自定义名」永远不翻。这里改成**遍历所有已注册的语言**，
 * 以后加语言也不用回来改。
 *
 * ## ⚠️ 不碰用户自己起的名字
 *
 * 只有**和某门语言的预设名一字不差**才翻。用户自己打的名字原样显示 ——
 * 那是他的东西，我们没资格改。
 */

import i18n from '../i18n'

/** 预设名字 → i18n key 的反查表。**懒加载 + 缓存**，语言包不会中途变。 */
let _index: Map<string, string> | null = null

function nameIndex(): Map<string, string> {
  if (_index) return _index
  const map = new Map<string, string>()
  // 遍历所有注册过的语言 —— 不写死 en/zh-CN,繁体和以后新加的语言都能覆盖。
  for (const lng of Object.keys((i18n.options.resources || {}) as object)) {
    let bundle: Record<string, unknown> | undefined
    try {
      bundle = i18n.getResourceBundle(lng, 'translation')?.channels
    } catch {
      bundle = undefined                    // 语言包没加载完就跳过,不炸
    }
    if (!bundle) continue
    for (const [key, val] of Object.entries(bundle)) {
      // 只认 `xxxName`：`xxxDirection` 是频道方向描述,不是名字。
      if (!key.endsWith('Name') || typeof val !== 'string') continue
      const text = val.trim()
      if (text) map.set(text, `channels.${key}`)
    }
  }
  _index = map
  return map
}

/** 测试用：清掉缓存。生产代码不该调。 */
export function _resetPresetNameIndex(): void {
  _index = null
}

/**
 * 把一个频道名翻成当前界面语言。
 *
 * ```
 * 'UFO 悬案档案频道'  →  'UFO Case Files'      // 英文界面下
 * '我自己起的名字'     →  '我自己起的名字'        // 不是预设,原样返回
 * ''                 →  ''
 * ```
 *
 * ⚠️ 用全局 `i18n.t` 而不是组件传进来的 `t`：预设名字在 `translation`
 *    直接用它会查不到。
 */
export function presetChannelName(name: string | null | undefined): string {
  const raw = (name || '').trim()
  if (!raw) return ''
  const key = nameIndex().get(raw)
  if (!key) return raw                      // 用户自己起的名字,原样显示
  const out = i18n.t(key)
  return out === key ? raw : String(out)    // 查不到就退回库里那个,绝不显示 key
}
