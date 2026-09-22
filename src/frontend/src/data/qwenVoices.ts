/**
 * tts_service.py `QWEN_VOICES` 三处一致(provider_key 对齐)。
 *
 * sample_url 留空 → 前端一律走 /api/tts/preview 现场按【当前所选语速】合成试听
 * (见 VoicePicker / VoicePreviewList 的 previewUrl fallback)。
 * language_hint='zh' 仅作展示;千问音色多语种,英文内容也能用(pipeline 已豁免 qwen_)。
 */
import type { TtsVoice } from '../types'

export const QWEN_VOICE_DEFAULT = 'qwen_cherry'

export const QWEN_VOICES: TtsVoice[] = [
  { voice_id: 'Cherry',      provider_key: 'qwen_cherry',      display_name: '芊悦 · 阳光女声（默认）', gender: 'female', language_hint: 'zh', description: '阳光积极、亲切自然，适合大多数讲解口播。', sample_url: '' },
  { voice_id: 'Maia',        provider_key: 'qwen_maia',        display_name: '四月 · 知性女声',        gender: 'female', language_hint: 'zh', description: '知性温柔、自然成熟，适合产品介绍和知识讲解。', sample_url: '' },
  { voice_id: 'Serena',      provider_key: 'qwen_serena',      display_name: '苏瑶 · 温柔女声',        gender: 'female', language_hint: 'zh', description: '温柔舒缓，适合课程、陪伴和情感内容。', sample_url: '' },
  { voice_id: 'Elias',       provider_key: 'qwen_elias',       display_name: '墨讲师 · 知识女声',      gender: 'female', language_hint: 'zh', description: '严谨又有叙事感，适合课程、科普和教程。', sample_url: '' },
  { voice_id: 'Neil',        provider_key: 'qwen_neil',        display_name: '阿闻 · 主持男声',        gender: 'male',   language_hint: 'zh', description: '字正腔圆的专业男主持，适合新闻和商业解说。', sample_url: '' },
  { voice_id: 'Ethan',       provider_key: 'qwen_ethan',       display_name: '晨煦 · 阳光男声',        gender: 'male',   language_hint: 'zh', description: '阳光温暖有活力，适合科技和知识讲解。', sample_url: '' },
  { voice_id: 'Kai',         provider_key: 'qwen_kai',         display_name: '凯 · 松弛男声',          gender: 'male',   language_hint: 'zh', description: '舒服松弛，适合长视频和有声内容。', sample_url: '' },
  { voice_id: 'Moon',        provider_key: 'qwen_moon',        display_name: '月白 · 帅气男声',        gender: 'male',   language_hint: 'zh', description: '自信帅气、年轻，适合科技和年轻化产品。', sample_url: '' },
  { voice_id: 'Vincent',     provider_key: 'qwen_vincent',     display_name: '田叔 · 烟嗓男声',        gender: 'male',   language_hint: 'zh', description: '沙哑烟嗓、有故事感，适合纪录片和历史。', sample_url: '' },
  { voice_id: 'Eldric Sage', provider_key: 'qwen_eldric_sage', display_name: '沧明子 · 睿智老者',      gender: 'male',   language_hint: 'zh', description: '沉稳睿智的老者，适合历史和传统文化。', sample_url: '' },
  { voice_id: 'Arthur',      provider_key: 'qwen_arthur',      display_name: '徐大爷 · 质朴老者',      gender: 'male',   language_hint: 'zh', description: '质朴沧桑的说书嗓，适合故事和怀旧。', sample_url: '' },
  { voice_id: 'Bellona',     provider_key: 'qwen_bellona',     display_name: '燕铮莺 · 戏剧女声',      gender: 'female', language_hint: 'zh', description: '洪亮清楚、戏剧张力强，适合广告和预告片。', sample_url: '' },
  { voice_id: 'Nofish',      provider_key: 'qwen_nofish',      display_name: '不吃鱼 · 设计师男声',    gender: 'male',   language_hint: 'zh', description: '不卷舌的松弛男声，适合口播和播客。', sample_url: '' },
  { voice_id: 'Seren',       provider_key: 'qwen_seren',       display_name: '小婉 · 舒缓女声',        gender: 'female', language_hint: 'zh', description: '温和舒缓，适合助眠、情感和有声书。', sample_url: '' },
  { voice_id: 'Nini',        provider_key: 'qwen_nini',        display_name: '邻家妹妹 · 甜糯女声',    gender: 'female', language_hint: 'zh', description: '甜糯亲切，适合陪伴和生活方式内容。', sample_url: '' },
  { voice_id: 'Mia',         provider_key: 'qwen_mia',         display_name: '乖小妹 · 乖巧女声',      gender: 'female', language_hint: 'zh', description: '乖巧温柔如清泉，适合温情内容。', sample_url: '' },
  { voice_id: 'Chelsie',     provider_key: 'qwen_chelsie',     display_name: '千雪 · 软萌女声',        gender: 'female', language_hint: 'zh', description: '二次元软萌，适合娱乐和角色化内容。', sample_url: '' },
  { voice_id: 'Momo',        provider_key: 'qwen_momo',        display_name: '茉兔 · 俏皮女声',        gender: 'female', language_hint: 'zh', description: '俏皮撒娇，适合娱乐和轻松内容。', sample_url: '' },
  { voice_id: 'Vivian',      provider_key: 'qwen_vivian',      display_name: '十三 · 可爱女声',        gender: 'female', language_hint: 'zh', description: '可爱、略带小暴躁，适合角色化短视频。', sample_url: '' },
  { voice_id: 'Bella',       provider_key: 'qwen_bella',       display_name: '萌宝 · 可爱女声',        gender: 'female', language_hint: 'zh', description: '可爱年轻，适合活泼轻松内容。', sample_url: '' },
  { voice_id: 'Bunny',       provider_key: 'qwen_bunny',       display_name: '萌小姬 · 甜萌女声',      gender: 'female', language_hint: 'zh', description: '极致可爱、年轻，适合娱乐内容。', sample_url: '' },
  { voice_id: 'Stella',      provider_key: 'qwen_stella',      display_name: '少女阿月 · 少女声',      gender: 'female', language_hint: 'zh', description: '迷糊少女，需要时充满激情，适合角色和剧情。', sample_url: '' },
  { voice_id: 'Mochi',       provider_key: 'qwen_mochi',       display_name: '沙小弥 · 童声男',        gender: 'male',   language_hint: 'zh', description: '早慧又天真的童声，适合角色和故事。', sample_url: '' },
  { voice_id: 'Pip',         provider_key: 'qwen_pip',         display_name: '顽屁小孩 · 顽皮童声',    gender: 'male',   language_hint: 'zh', description: '顽皮又天真的孩子声，适合角色化内容。', sample_url: '' },
]

export function isQwenVoiceProvider(provider?: string): boolean {
  return !!provider && provider.startsWith('qwen_')
}
