/**
 * 音色展示名/描述的多语言映射:provider_key → i18n key。
 * 界面语言切换时,t() 自动返回对应语言的名字/描述(中文界面中文名·英文界面英文名)。
 * 这套 key 的翻译在 src/frontend/src/i18n/locales/{en,zh-CN}.ts 的 workspace 命名空间。
 *
 * 原本只在 WorkspacePages.tsx 里,现抽到 lib 共享给 VoicePicker / VoicePreviewList /
 * Channels 批量 / StudioForm,让所有音色选择器的名字都跟随界面语言。
 */
import type { TtsVoice } from '../types'

export const VOICE_NAME_LABEL_KEYS: Record<string, string> = {
  // —— 英文高级配音 15 音色(8 美音 + 7 英音)——
  // 🚨 名字**永远是英文**(客户要的),口音和性别**跟界面语言**:
  //    英文界面 `Charon · American male` / 中文界面 `Charon · 美音男`。
  // ⚠️ i18n key 里也不带任何供应商名 —— 前端包是公开的,谁都能读。

  qwen_cherry: 'workspace.voiceQwenCherry',
  qwen_serena: 'workspace.voiceQwenSerena',
  qwen_ethan: 'workspace.voiceQwenEthan',
  qwen_chelsie: 'workspace.voiceQwenChelsie',
  qwen_momo: 'workspace.voiceQwenMomo',
  qwen_vivian: 'workspace.voiceQwenVivian',
  qwen_moon: 'workspace.voiceQwenMoon',
  qwen_maia: 'workspace.voiceQwenMaia',
  qwen_kai: 'workspace.voiceQwenKai',
  qwen_nofish: 'workspace.voiceQwenNofish',
  qwen_bella: 'workspace.voiceQwenBella',
  qwen_eldric_sage: 'workspace.voiceQwenEldricSage',
  qwen_mia: 'workspace.voiceQwenMia',
  qwen_mochi: 'workspace.voiceQwenMochi',
  qwen_bellona: 'workspace.voiceQwenBellona',
  qwen_vincent: 'workspace.voiceQwenVincent',
  qwen_bunny: 'workspace.voiceQwenBunny',
  qwen_neil: 'workspace.voiceQwenNeil',
  qwen_elias: 'workspace.voiceQwenElias',
  qwen_arthur: 'workspace.voiceQwenArthur',
  qwen_nini: 'workspace.voiceQwenNini',
  qwen_seren: 'workspace.voiceQwenSeren',
  qwen_pip: 'workspace.voiceQwenPip',
  qwen_stella: 'workspace.voiceQwenStella',
  // —— 历史 Azure / ElevenLabs 音色(兼容旧存档)——
  azure_xiaoxiao: 'workspace.voiceXiaoxiao',
  azure_xiaoyi: 'workspace.voiceXiaoyi',
  azure_yunyang: 'workspace.voiceYunyang',
  azure_yunjian: 'workspace.voiceYunjian',
  azure_yunxi: 'workspace.voiceYunxi',
  azure_xiaochen: 'workspace.voiceXiaochen',
  azure_xiaohan: 'workspace.voiceXiaohan',
  azure_xiaomeng: 'workspace.voiceXiaomeng',
  azure_xiaomo: 'workspace.voiceXiaomo',
  azure_xiaoqiu: 'workspace.voiceXiaoqiu',
  azure_xiaorou: 'workspace.voiceXiaorou',
  azure_xiaorui: 'workspace.voiceXiaorui',
  azure_xiaoshuang: 'workspace.voiceXiaoshuang',
  azure_xiaoyan: 'workspace.voiceXiaoyan',
  azure_xiaoyou: 'workspace.voiceXiaoyou',
  azure_yunfeng: 'workspace.voiceYunfeng',
  azure_yunhao: 'workspace.voiceYunhao',
  azure_yunjie: 'workspace.voiceYunjie',
  azure_yunxia: 'workspace.voiceYunxia',
  azure_yunye: 'workspace.voiceYunye',
  azure_yunze: 'workspace.voiceYunze',
  azure_tw_hsiaochen: 'workspace.voiceTwHsiaochen',
  azure_tw_hsiaoyu: 'workspace.voiceTwHsiaoyu',
  azure_tw_yunjhe: 'workspace.voiceTwYunjhe',
  azure_hk_hiugaai: 'workspace.voiceHkHiugaai',
  azure_hk_hiumaan: 'workspace.voiceHkHiumaan',
  azure_hk_wanlung: 'workspace.voiceHkWanlung',
  azure_aria: 'workspace.voiceAria',
  azure_brian: 'workspace.voiceBrian',
  azure_guy: 'workspace.voiceGuy',
  azure_jenny: 'workspace.voiceJenny',
  azure_davis: 'workspace.voiceDavis',
  azure_steffan: 'workspace.voiceSteffan',
  openai_tts: 'workspace.voiceOpenaiTts',
}

export const VOICE_DESC_LABEL_KEYS: Record<string, string> = {
  // —— 千问 24 音色 ——
  qwen_cherry: 'workspace.voiceDescQwenCherry',
  qwen_serena: 'workspace.voiceDescQwenSerena',
  qwen_ethan: 'workspace.voiceDescQwenEthan',
  qwen_chelsie: 'workspace.voiceDescQwenChelsie',
  qwen_momo: 'workspace.voiceDescQwenMomo',
  qwen_vivian: 'workspace.voiceDescQwenVivian',
  qwen_moon: 'workspace.voiceDescQwenMoon',
  qwen_maia: 'workspace.voiceDescQwenMaia',
  qwen_kai: 'workspace.voiceDescQwenKai',
  qwen_nofish: 'workspace.voiceDescQwenNofish',
  qwen_bella: 'workspace.voiceDescQwenBella',
  qwen_eldric_sage: 'workspace.voiceDescQwenEldricSage',
  qwen_mia: 'workspace.voiceDescQwenMia',
  qwen_mochi: 'workspace.voiceDescQwenMochi',
  qwen_bellona: 'workspace.voiceDescQwenBellona',
  qwen_vincent: 'workspace.voiceDescQwenVincent',
  qwen_bunny: 'workspace.voiceDescQwenBunny',
  qwen_neil: 'workspace.voiceDescQwenNeil',
  qwen_elias: 'workspace.voiceDescQwenElias',
  qwen_arthur: 'workspace.voiceDescQwenArthur',
  qwen_nini: 'workspace.voiceDescQwenNini',
  qwen_seren: 'workspace.voiceDescQwenSeren',
  qwen_pip: 'workspace.voiceDescQwenPip',
  qwen_stella: 'workspace.voiceDescQwenStella',
  // —— 历史音色 ——
  azure_xiaoxiao: 'workspace.voiceDescXiaoxiao',
  azure_xiaoyi: 'workspace.voiceDescXiaoyi',
  azure_yunyang: 'workspace.voiceDescYunyang',
  azure_yunjian: 'workspace.voiceDescYunjian',
  azure_yunxi: 'workspace.voiceDescYunxi',
  azure_xiaochen: 'workspace.voiceDescXiaochen',
  azure_xiaohan: 'workspace.voiceDescXiaohan',
  azure_xiaomeng: 'workspace.voiceDescXiaomeng',
  azure_xiaomo: 'workspace.voiceDescXiaomo',
  azure_xiaoqiu: 'workspace.voiceDescXiaoqiu',
  azure_xiaorou: 'workspace.voiceDescXiaorou',
  azure_xiaorui: 'workspace.voiceDescXiaorui',
  azure_xiaoshuang: 'workspace.voiceDescXiaoshuang',
  azure_xiaoyan: 'workspace.voiceDescXiaoyan',
  azure_xiaoyou: 'workspace.voiceDescXiaoyou',
  azure_yunfeng: 'workspace.voiceDescYunfeng',
  azure_yunhao: 'workspace.voiceDescYunhao',
  azure_yunjie: 'workspace.voiceDescYunjie',
  azure_yunxia: 'workspace.voiceDescYunxia',
  azure_yunye: 'workspace.voiceDescYunye',
  azure_yunze: 'workspace.voiceDescYunze',
  azure_tw_hsiaochen: 'workspace.voiceDescTwHsiaochen',
  azure_tw_hsiaoyu: 'workspace.voiceDescTwHsiaoyu',
  azure_tw_yunjhe: 'workspace.voiceDescTwYunjhe',
  azure_hk_hiugaai: 'workspace.voiceDescHkHiugaai',
  azure_hk_hiumaan: 'workspace.voiceDescHkHiumaan',
  azure_hk_wanlung: 'workspace.voiceDescHkWanlung',
  azure_ava: 'workspace.voiceDescAva',
  azure_andrew: 'workspace.voiceDescAndrew',
  azure_aria: 'workspace.voiceDescAria',
  azure_brian: 'workspace.voiceDescBrian',
  azure_guy: 'workspace.voiceDescGuy',
  azure_jenny: 'workspace.voiceDescJenny',
  azure_davis: 'workspace.voiceDescDavis',
  azure_steffan: 'workspace.voiceDescSteffan',
  openai_tts: 'workspace.voiceDescOpenaiTts',
}

// 音色展示名:命中 provider_key 映射 → 走 i18n(跟界面语言);否则退回原始 display_name。
export function voiceDisplayName(t: (k: string) => any, v: TtsVoice): string {
  const key = VOICE_NAME_LABEL_KEYS[v.provider_key]
  return key ? t(key) : v.display_name
}
export function voiceDescription(t: (k: string) => any, v: TtsVoice): string {
  const key = VOICE_DESC_LABEL_KEYS[v.provider_key]
  return key ? t(key) : v.description
}

// 已知的**供应商前缀**。音色 id 形如 `elevenlabs_adam` / `qwen_cherry`,
// 第一段就是厂商名。
//
//    牵扯到商业竞争问题,不能在系统里明晃晃地写着我们用的是人家的东西。」
const VENDOR_PREFIXES = [
  'elevenlabs_', 'eleven_', 'azure_', 'openai_', 'qwen_', 'aliyun_',
  'minimax_', 'volcano_', 'bytedance_', 'google_', 'deepseek_',
]

/**
 * 只拿到音色 **id 字符串**时的安全展示名。
 *
 * 🚨 **兜底也绝不能把厂商名露出去。** 命中映射表就走 i18n;
 *    没命中就**砍掉厂商前缀**只留音色名(`elevenlabs_adam` → `Adam`)。
 *    直接把 id 打在界面上,等于告诉所有人我们用了谁家的服务。
 *
 * ⚠️ 新增供应商时**必须往 `VENDOR_PREFIXES` 里加一行** ——
 *    漏了的话那家的名字就会原样出现在客户屏幕上。
 */
export function voiceLabelFromKey(t: (k: string) => any, key: string): string {
  const raw = String(key || '').trim()
  if (!raw) return ''
  const mapped = VOICE_NAME_LABEL_KEYS[raw]
  if (mapped) return t(mapped)

  let name = raw
  for (const p of VENDOR_PREFIXES) {
    if (name.toLowerCase().startsWith(p)) {
      name = name.slice(p.length)
      break
    }
  }
  // 还带着下划线的(`adam_li`)换成空格,再首字母大写
  name = name.replaceAll('_', ' ').trim()
  return name ? name.charAt(0).toUpperCase() + name.slice(1) : ''
}
