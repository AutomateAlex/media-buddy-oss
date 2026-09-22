import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import type { TFunction } from 'i18next'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api/client'
import { VoicePreviewList } from '../components/VoicePreviewList'
import { alertDialog } from '../components/Dialog'
import { ChannelSubTabs } from '../components/ChannelSubTabs'
import { LanguageMenu } from '../components/LanguageMenu'
import { SubtitleSizeSlider, SUBTITLE_SCALE_DEFAULT } from '../components/SubtitleSizeSlider'
import { QWEN_VOICES, QWEN_VOICE_DEFAULT, isQwenVoiceProvider } from '../data/qwenVoices'
import { voiceDisplayName } from '../lib/voiceLabels'
import { outputLanguageFromUi, effectiveSubtitleLanguage, subtitleLanguageForOutput, ttsPreviewText } from '../i18n'
import { projectDisplayName } from '../lib/projectDisplay'
import { batchDownloadProjects } from '../lib/batchDownload'
import type { BatchRun, ChannelDesignResult, ChannelMemory, ChannelRuleJson, Project, Series, SeriesCreate, TtsVoice } from '../types'

type ChannelTemplate = {
  nameKey: string
  directionKey: string
  formatKey: string
  industry: string
  prompt: string
  group?: string
}

// 知识故事型(真实资料 + 反常识问题 + 故事推进 + 解释价值)最像优秀 YouTuber,优先露出。
const CHANNEL_TEMPLATES: ChannelTemplate[] = [
  {
    nameKey: 'channels.wildlifeName',
    directionKey: 'channels.wildlifeDirection',
    formatKey: 'channels.formatLongShort',
    industry: 'wildlife_science',
    prompt: '频道方向：野生动物科普。覆盖动物冷知识、生态故事、攻击防御和生存策略；选题要兼顾真实知识、故事张力和自然敬畏感，避免血腥猎奇、拟人化过度和未经证实的夸张说法。',
  },
  {
    nameKey: 'channels.historyName',
    directionKey: 'channels.historyDirection',
    formatKey: 'channels.formatLong',
    industry: 'history_trivia',
    prompt: '频道方向：历史文明与神话。覆盖历史人物、古代战争、文明细节、遗迹考古、神话民俗和反常识故事；内容要有时间线、因果关系和证据边界，必须区分史实、考古证据、传说、文学和猜测，避免把戏剧化演绎当结论。',
  },
  {
    nameKey: 'channels.cosmosName',
    directionKey: 'channels.cosmosDirection',
    formatKey: 'channels.formatLongShort',
    industry: 'space_science',
    prompt: '频道方向：宇宙与自然科学。覆盖宇宙、物理、天文、地貌、气候、地球灾难和自然奇观；讲解要通俗、准确、有想象力，画面可使用宇宙、实验、地图、自然空镜和科学可视化，避免制造恐慌或编造伤亡细节。',
  },
  {
    nameKey: 'channels.techName',
    directionKey: 'channels.techDirection',
    formatKey: 'channels.formatLongShort',
    industry: 'technology_explainer',
    prompt: '频道方向：科技解释。覆盖 AI、芯片、软件、互联网产品、机器人、自动驾驶和未来技术趋势；脚本要把复杂技术拆成普通人能理解的逻辑，画面优先产品、设备、数据流和工作场景，避免夸大效果、硬编参数和未经证实的未来预言。',
  },
  // ===== 第二梯队 — 结构成熟,真实公司/数据/人物需联网资料包把关 =====
  {
    nameKey: 'channels.businessName',
    directionKey: 'channels.businessDirection',
    formatKey: 'channels.formatLongShort',
    industry: 'business_story',
    prompt: '频道方向：商业与金钱。覆盖品牌崛起、公司失败、人物决策、行业变化、消费陷阱、财富习惯和普通人的金钱决策；内容要用故事结构解释商业常识和行为误区，不提供具体投资建议，不承诺收益，避免空泛鸡汤。',
  },
  {
    nameKey: 'channels.biographyName',
    directionKey: 'channels.biographyDirection',
    formatKey: 'channels.formatLong',
    industry: 'biography',
    prompt: '频道方向：人物传记。脚本要围绕人物目标、冲突、关键事件和结果展开，避免流水账。',
  },
  {
    nameKey: 'channels.psychologyName',
    directionKey: 'channels.psychologyDirection',
    formatKey: 'channels.formatLongShort',
    industry: 'psychology_human_nature',
    prompt: '频道方向：心理学与人生思考。覆盖认知偏差、情绪、人际关系、决策心理、哲学观点、人生选择、自由意志和意义感；内容要通俗、克制、有现实例子和观点推进，避免医疗诊断、绝对化心理标签和空泛鸡汤。',
  },
  {
    nameKey: 'channels.mysteryName',
    directionKey: 'channels.mysteryDirection',
    formatKey: 'channels.formatLong',
    industry: 'mystery_explainer',
    prompt: '频道方向：悬疑未解。保持克制和证据意识，用问题推进叙事，不把猜测说成事实。',
  },
  // ===== 第三梯队 — 新加入,需多跑几轮打磨(反鸡汤 / 书籍出处机制) =====
  {
    nameKey: 'channels.careerName',
    directionKey: 'channels.careerDirection',
    formatKey: 'channels.formatLongShort',
    industry: 'career_personal_growth',
    prompt: '频道方向：职场与个人成长。覆盖职场沟通、职业选择、能力成长、真实励志故事、成功失败复盘、行动方法和自我管理；内容必须落到真实案例、具体场景和可执行经验，避免喊口号、玄学成功学和空泛鸡汤。',
  },
  {
    nameKey: 'channels.readingName',
    directionKey: 'channels.readingDirection',
    formatKey: 'channels.formatLongShort',
    industry: 'reading_ideas',
    prompt: '频道方向：读书与思想。覆盖书籍精读、观点拆解、作家思想、经典名著、商业书和现实生活启发；内容要讲清一本书的核心问题、关键观点、现实案例和适用边界，避免照搬书摘、堆名言和无证据的鸡汤化解读。',
  },
  {
    nameKey: 'channels.wealthName',
    directionKey: 'channels.wealthDirection',
    formatKey: 'channels.formatLongShort',
    industry: 'wealth_wisdom',
    prompt: '频道方向：财商觉醒与财富智慧。讲顶级投资家和财富作家（巴菲特、芒格、清崎、纳瓦尔、达里奥等）的故事、理念、思维方式，经典财商书精读，以及经济如何运行；用小白能懂的话讲透“看懂钱、提升财商”。绝不荐股、不预测涨跌、不给具体买卖点、不承诺收益，讲的是思维和原理而非操作。',
  },
  {
    nameKey: 'channels.parentingName',
    directionKey: 'channels.parentingDirection',
    formatKey: 'channels.formatShortLong',
    industry: 'parenting_kids',
    prompt: '频道方向：科学育儿（家长向）。一条视频解决一个真实带娃痛点：辅食、睡眠夜醒、大运动、如厕、戒奶、入园、挑食和常见护理常识；过来人/闺蜜口吻、循证实用、不制造焦虑。不做医疗诊断、不推荐具体药物、不宣称疗效，涉及明确症状提示就医。',
  },
  {
    nameKey: 'channels.mineralName',
    directionKey: 'channels.mineralDirection',
    formatKey: 'channels.formatLongShort',
    industry: 'mineral_gem',
    prompt: '频道方向：矿石与宝石。一条视频讲一种真实矿物/宝石/晶体的成因、独特性质和冷知识，画面用真实标本、晶体生长延时、切割打磨；讲地质和科学，绝不吹能量疗愈/招财转运这类伪科学功效。',
  },
  {
    nameKey: 'channels.plantName',
    directionKey: 'channels.plantDirection',
    formatKey: 'channels.formatLongShort',
    industry: 'plant_science',
    prompt: '频道方向：植物科普。一条视频讲一种真实植物的生存策略/行为/冷知识，它怎么在竞争里活下来、有什么绝招；严禁编造不存在的植物或习性、严禁夸张。',
  },
  {
    nameKey: 'channels.fengshuiName',
    directionKey: 'channels.fengshuiDirection',
    formatKey: 'channels.formatLongShort',
    industry: 'fengshui_wisdom',
    prompt: '频道方向：风水常识与名案。只讲道理、文化和建筑智慧，绝不算命看运预测吉凶。三条线：居家布局的道理（玄关/藏风纳气/动线采光）、办公室商铺的空间讲究、建筑与城市的著名风水故事（中银大厦风水大战等）。不做坟墓阴宅、不做山川龙脉，不碰玄乎神秘词。',
  },
  {
    nameKey: 'channels.ichingName',
    directionKey: 'channels.ichingDirection',
    formatKey: 'channels.formatLongShort',
    industry: 'iching_wisdom',
    prompt: '频道方向：易经奇门·处世智慧。用大白话讲易经和奇门里的处世智慧和决策思维，当古人的思维模型落到职场/选择/为人处世（潜龙勿用、亢龙有悔、阴阳双面视角）；绝不算命、不预测运势、不教化解，讲的是思维和道理。',
  },
  {
    nameKey: 'channels.healthName',
    directionKey: 'channels.healthDirection',
    formatKey: 'channels.formatShortLong',
    industry: 'health_wellness',
    prompt: '频道方向：脆皮青年·快节奏健康。给年轻人的生活方式健康：睡眠/熬夜/久坐/颈椎/脱发/咖啡/精力管理，轻松有梗、科学不焦虑。只讲生活方式和预防，绝不做疾病诊断、不推荐药物、不宣称疗效、不带货保健品，涉及症状提示就医。',
  },
  {
    nameKey: 'channels.ufoName',
    directionKey: 'channels.ufoDirection',
    formatKey: 'channels.formatLong',
    industry: 'ufo_cases',
    prompt: '频道方向：UFO悬案档案。讲全球真实、有公开记录的UFO目击与调查悬案，基于解密文件、军方证词、雷达数据；用悬念钩子，但结尾回到证据本身、说清为何至今无定论，绝不渲染超自然、不下“外星人已确认”结论、不编造。',
  },
  // ===== 其它 — 暂未纳入「知识故事型」优先级(用户未列入梯队) =====
  {
    nameKey: 'channels.foodName',
    directionKey: 'channels.foodDirection',
    formatKey: 'channels.formatShortLong',
    industry: 'food_tutorial',
    prompt: '频道方向：美食教程。脚本要清楚呈现食材、步骤、火候和成品卖点，画面优先食材和烹饪动作。',
  },
  {
    nameKey: 'channels.cityName',
    directionKey: 'channels.cityDirection',
    formatKey: 'channels.formatLongShort',
    industry: 'city_architecture',
    prompt: '频道方向：城市与建筑冷知识。内容要把空间、工程和历史背景讲清楚，画面优先城市、建筑、地图和工程素材。',
  },
  // ===== 全球系列 — 实体类型频道(欧美为主·全球化),一频道一实体类型,每条视频讲一个实例 =====
  {
    nameKey: 'channels.worldCitiesName',
    directionKey: 'channels.worldCitiesDirection',
    formatKey: 'channels.formatLongShort',
    industry: 'world_cities',
    group: 'global',
    prompt: '频道方向：世界城市系列。每条视频聚焦全世界的一座城市（以欧洲、北美为主，兼顾各大洲），讲它的历史、性格、地标和故事；画面优先该城市的天际线、街区、地标空镜，主体选海外素材库能配到画面的国际名城。',
  },
  {
    nameKey: 'channels.worldTownsName',
    directionKey: 'channels.worldTownsDirection',
    formatKey: 'channels.formatLongShort',
    industry: 'world_towns',
    group: 'global',
    prompt: '频道方向：世界小镇与古镇系列。每条视频聚焦全世界的一个小镇或古镇（欧美为主），讲它的风貌、传统和故事；画面优先小镇街景、自然环境与生活细节。',
  },
  {
    nameKey: 'channels.worldLandmarksName',
    directionKey: 'channels.worldLandmarksDirection',
    formatKey: 'channels.formatLongShort',
    industry: 'world_landmarks',
    group: 'global',
    prompt: '频道方向：世界地标与奇观系列。每条视频聚焦一个世界知名地标/建筑奇观（欧美为主），讲它的建造、秘密与象征意义；画面优先该地标的实拍空镜。',
  },
  {
    nameKey: 'channels.worldBrandsName',
    directionKey: 'channels.worldBrandsDirection',
    formatKey: 'channels.formatLongShort',
    industry: 'world_brands',
    group: 'global',
    prompt: '频道方向：世界品牌故事系列。每条视频聚焦一个全球知名品牌（欧美为主），用故事结构讲它的崛起、危机与翻身；不提供投资建议，画面优先产品、门店、广告与品牌符号。',
  },
  {
    nameKey: 'channels.worldUniversitiesName',
    directionKey: 'channels.worldUniversitiesDirection',
    formatKey: 'channels.formatLongShort',
    industry: 'world_universities',
    group: 'global',
    prompt: '频道方向：世界名校系列。每条视频聚焦一所世界顶尖大学（欧美为主），讲它的历史、传统、名人与学术传奇；画面优先校园、建筑与典礼空镜。',
  },
  {
    nameKey: 'channels.worldMuseumsName',
    directionKey: 'channels.worldMuseumsDirection',
    formatKey: 'channels.formatLongShort',
    industry: 'world_museums',
    group: 'global',
    prompt: '频道方向：世界博物馆系列。每条视频聚焦一座世界知名博物馆/美术馆（欧美为主），讲它的镇馆之宝与故事；画面优先博物馆建筑、展厅与名作空镜，注意版权安全。',
  },
  {
    nameKey: 'channels.worldCuisinesName',
    directionKey: 'channels.worldCuisinesDirection',
    formatKey: 'channels.formatShortLong',
    industry: 'world_cuisines',
    group: 'global',
    prompt: '频道方向：各国美食与饮食文化系列。每条视频聚焦一道/一国的美食（欧美为主，文化故事取向而非教程），讲它的起源、传统与走向世界的故事；画面优先美食与饮食场景空镜。',
  },
  {
    nameKey: 'channels.worldGeographyName',
    directionKey: 'channels.worldGeographyDirection',
    formatKey: 'channels.formatLongShort',
    industry: 'world_geography',
    group: 'global',
    prompt: '频道方向：世界地理冷知识系列。每条视频聚焦一个地理对象（沙漠/峡湾/山脉/湖泊等，全球分布），讲它的成因、变化与反常识冷知识；画面优先自然地貌空镜，避免编造伤亡或制造恐慌。',
  },
  {
    nameKey: 'channels.notableChineseName',
    directionKey: 'channels.notableChineseDirection',
    formatKey: 'channels.formatLong',
    industry: 'notable_chinese',
    group: 'global',
    prompt: '频道方向：全球知名华人系列。每条视频聚焦一位分布在世界各地的知名华人/华裔（美国、加拿大、欧洲、东南亚、澳洲…），覆盖科学、科技、商业、艺术、影视、体育等不同领域，讲他/她在异国的奋斗与成就；不局限于中国大陆本地题材。',
  },
  {
    nameKey: 'channels.worldFestivalsName',
    directionKey: 'channels.worldFestivalsDirection',
    formatKey: 'channels.formatLongShort',
    industry: 'world_festivals',
    group: 'global',
    prompt: '频道方向：世界节日与民俗系列。每条视频聚焦一个世界知名节日/民俗庆典（欧美为主），讲它的起源、习俗与背后的历史；画面优先庆典、游行与传统场景空镜。',
  },
  {
    nameKey: 'channels.worldNaturalWondersName',
    directionKey: 'channels.worldNaturalWondersDirection',
    formatKey: 'channels.formatLongShort',
    industry: 'world_natural_wonders',
    group: 'global',
    prompt: '频道方向：世界自然奇观系列。每条视频聚焦一个世界自然奇观（极光、峡谷、山脉、瀑布、湖泊等，全球分布），讲它的成因、震撼与面临的威胁；画面优先壮丽自然空镜。',
  },
]

// 每个频道的展示元数据(按 industry 键):分类 / emoji 图标 / 是否推荐 / 诚实标签。
// 一句话简介走 i18n `channels.blurb.<industry>`。新版频道选择器用它做分类 Tab + 小卡。
type ChannelMeta = { category: string; icon: string; recommended?: boolean; tags?: string[] }
const CHANNEL_META: Record<string, ChannelMeta> = {
  wildlife_science:        { category: 'animal',   icon: '🦁', recommended: true, tags: ['tagBeginner', 'tagStockEasy'] },
  history_trivia:          { category: 'history',  icon: '🏛️', recommended: true, tags: ['tagBeginner'] },
  space_science:           { category: 'tech',     icon: '🌌', recommended: true, tags: ['tagStockEasy'] },
  technology_explainer:    { category: 'tech',     icon: '💡', recommended: true, tags: ['tagBeginner'] },
  business_story:          { category: 'business', icon: '💰', recommended: true, tags: ['tagBizValue'] },
  biography:               { category: 'people',   icon: '👤' },
  psychology_human_nature: { category: 'people',   icon: '🧠' },
  mystery_explainer:       { category: 'history',  icon: '🕵️' },
  career_personal_growth:  { category: 'people',   icon: '📈' },
  reading_ideas:           { category: 'people',   icon: '📖' },
  food_tutorial:           { category: 'food',     icon: '🍳', tags: ['tagStockEasy'] },
  city_architecture:       { category: 'city',     icon: '🏙️', tags: ['tagStockEasy'] },
  world_cities:            { category: 'global',   icon: '🌆', tags: ['tagStockEasy'] },
  world_towns:             { category: 'global',   icon: '🏘️', tags: ['tagStockEasy'] },
  world_landmarks:         { category: 'global',   icon: '🗼', tags: ['tagStockEasy'] },
  world_brands:            { category: 'global',   icon: '🛍️', tags: ['tagBizValue'] },
  world_universities:      { category: 'global',   icon: '🎓' },
  world_museums:           { category: 'global',   icon: '🖼️', tags: ['tagStockEasy'] },
  world_cuisines:          { category: 'global',   icon: '🍜', tags: ['tagStockEasy'] },
  world_geography:         { category: 'global',   icon: '🗺️', tags: ['tagStockEasy'] },
  notable_chinese:         { category: 'global',   icon: '🌏' },
  world_festivals:         { category: 'global',   icon: '🎉', tags: ['tagStockEasy'] },
  world_natural_wonders:   { category: 'global',   icon: '🏔️', tags: ['tagStockEasy'] },
  wealth_wisdom:           { category: 'business', icon: '💹', recommended: true, tags: ['tagBizValue'] },
  parenting_kids:          { category: 'life',     icon: '🍼', recommended: true },
  mineral_gem:             { category: 'nature',   icon: '💎', tags: ['tagStockEasy'] },
  plant_science:           { category: 'nature',   icon: '🌿', tags: ['tagStockEasy'] },
  fengshui_wisdom:         { category: 'life',     icon: '🧭' },
  iching_wisdom:           { category: 'people',   icon: '☯️' },
  health_wellness:         { category: 'life',     icon: '⚡' },
  ufo_cases:               { category: 'history',  icon: '🛸' },
  custom_channel:          { category: 'custom',   icon: '✨' },
}
// 「全部」放第一位(默认选中)—— 之前在最后要横向滚动才看得到。
const CHANNEL_TABS = ['all', 'recommended', 'animal', 'nature', 'history', 'tech', 'business', 'life', 'people', 'city', 'food', 'global'] as const

// 还是老值(如 earth_disasters/money_literacy),显示缩略图/图标/简介时先归一到合并后的 tag,
// 否则按老 tag 找不到图会掉回 📺。
const CHANNEL_TAG_ALIASES: Record<string, string> = {
  animal_survival_strategy: 'wildlife_science',
  ancient_war_strategy: 'history_trivia',
  ancient_civilization: 'history_trivia',
  mythology_folklore: 'history_trivia',
  money_literacy: 'business_story',
  nature_geography: 'space_science',
  earth_disasters: 'space_science',
  ai_future_tech: 'technology_explainer',
  philosophy_life: 'psychology_human_nature',
}
function resolveChannelTag(tag?: string | null): string {
  const t = tag || ''
  return CHANNEL_TAG_ALIASES[t] || t
}

// 频道配图(src/assets/channels/<industry>.jpg),按 industry 取打包后的 URL;缺图回退 emoji。
const CHANNEL_IMAGES = import.meta.glob('../assets/channels/*.jpg', { eager: true, import: 'default' }) as Record<string, string>
function channelImage(industry?: string): string | undefined {
  if (!industry) return undefined
  // 先找【原始 tag】的专属图(老 tag 也能有自己的图,如 money_literacy 用自己的图);
  // 找不到再退【合并后 tag】的图(如 earth_disasters → space_science)。
  for (const tag of [String(industry), resolveChannelTag(industry)]) {
    if (!tag) continue
    const hit = Object.entries(CHANNEL_IMAGES).find(([p]) => p.endsWith(`/${tag}.jpg`))
    if (hit) return hit[1]
  }
  return undefined
}

function formatLabel(t: TFunction, value: string) {
  if (value === 'youtube_landscape' || value === 'youtube_long') return t('channels.longVideo')
  if (value === 'youtube_shorts') return t('channels.shortVideo')
  return value.replaceAll('_', ' ')
}

function channelPayload(template: ChannelTemplate, t: TFunction, customName?: string): SeriesCreate {
  return {
    name: customName?.trim() || t(template.nameKey),
    description: t(template.directionKey),
    industry_tag: template.industry,
    director_prompt: template.prompt,
    output_format: 'youtube_landscape',
    duration_target_seconds: 600,
    tts_provider: QWEN_VOICE_DEFAULT,
    pipeline_mode: 'stock_first',
    bgm_enabled: true,
    daily_video_cap: 10,
    daily_cost_cap_usd: 8,
  }
}

// 自定义频道(AI 生成)的建频道 payload:把 AI 规则写进 channel_rule_json,industry_tag
// 固定 custom_channel。后端 _rule_for 见到 channel_rule_json 就用这套动态定位,选题/契合/
// 收口全线达到 ≈ 预设频道质量(而非退化成通用兜底)。
function customChannelPayload(name: string, directorPrompt: string, rule: ChannelRuleJson): SeriesCreate {
  return {
    name: name.trim(),
    description: rule.positioning || '',
    industry_tag: 'custom_channel',
    director_prompt: directorPrompt || '频道方向：自定义。围绕频道定位生产内容，保持频道内部风格一致。',
    channel_rule_json: rule,
    output_format: 'youtube_landscape',
    duration_target_seconds: 600,
    tts_provider: QWEN_VOICE_DEFAULT,
    pipeline_mode: 'stock_first',
    bgm_enabled: true,
    daily_video_cap: 10,
    daily_cost_cap_usd: 8,
  }
}

// 预设频道(industry_tag 命中已知预设 + 名字仍是预设默认名)的卡片名/描述跟随 UI
// 语言切换;用户自定义命名的频道(名字非预设默认)按数据库存储显示,绝不被覆盖。
const TAG_TO_TEMPLATE: Record<string, ChannelTemplate> = Object.fromEntries(
  CHANNEL_TEMPLATES.map((tpl) => [tpl.industry, tpl]),
)
function channelDisplay(t: TFunction, channel: Series): { name: string; desc: string } {
  const tpl = TAG_TO_TEMPLATE[channel.industry_tag || '']
  const isPreset = !!tpl && !!channel.name
    && (channel.name === t(tpl.nameKey, { lng: 'en' }) || channel.name === t(tpl.nameKey, { lng: 'zh-CN' }))
  return {
    name: isPreset ? t(tpl.nameKey) : (channel.name || ''),
    desc: isPreset ? t(tpl.directionKey) : (channel.description || t('channels.noDirectionYet')),
  }
}

export function ChannelsHome() {
  const { t } = useTranslation()
  const [channels, setChannels] = useState<Series[]>([])
  const [loading, setLoading] = useState(true)
  const [deleteTarget, setDeleteTarget] = useState<Series | null>(null)
  const [deleting, setDeleting] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      const seriesList = await api.series.list()
      setChannels(seriesList)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  const confirmDeleteChannel = async () => {
    if (!deleteTarget) return
    setDeleting(true)
    try {
      await api.series.delete(deleteTarget.id)
      setChannels((prev) => prev.filter((channel) => channel.id !== deleteTarget.id))
      setDeleteTarget(null)
    } catch (e: any) {
      void alertDialog(e?.response?.data?.detail || e.message || t('channels.deleteChannelFailed'))
    } finally {
      setDeleting(false)
    }
  }

  return (
    <>
      <header className="page-header">
        <div className="crumb">
          <span className="here">{t('channels.channelsHome')}</span>
        </div>
        <LanguageMenu />
      </header>

      <div className="workspace-page channel-page">
        <div className="channel-home-head">
          <div>
            <h1>{t('channels.heroTitle')}</h1>
            <p>{t('channels.heroSubtitle')}</p>
          </div>
          <Link to="/channels/new" data-tour="create-channel" className="channel-recommend-btn channel-home-cta">
            ＋ {t('channels.createMyChannel')}
          </Link>
        </div>


        <section className="channel-section">
          <div className="rail-title-row">
            <h2>{t('channels.existingChannels')}</h2>
            <Link to="/channels/new">{t('channels.newChannelArrow')}</Link>
          </div>
          {loading ? (
            <p className="hint">{t('channels.loadingChannels')}</p>
          ) : channels.length === 0 ? (
            <div className="channel-empty">
              <h3>{t('channels.noChannelsYet')}</h3>
              <p>{t('channels.noChannelsHint')}</p>
            </div>
          ) : (
            <div className="channel-card-grid">
              {channels.map((channel) => {
                const tag = resolveChannelTag(channel.industry_tag || '')  // 归一后的 tag:给 blurb / 图标兜底用
                const blurb = CHANNEL_META[tag] ? t(`channels.blurb.${tag}`) : channelDisplay(t, channel).desc
                return (
                <div key={channel.id} className="channel-card-sm channel-managed">
                  <Link to={`/channels/${channel.id}`} className="ch-managed-body">
                    <div className="ch-card-head">
                      <span className="ch-card-icon">
                        {channelImage(channel.industry_tag ?? undefined)
                          ? <img src={channelImage(channel.industry_tag ?? undefined)} alt="" loading="lazy" />
                          : (CHANNEL_META[tag]?.icon || '📺')}
                      </span>
                      <strong className="ch-card-name">{channelDisplay(t, channel).name}</strong>
                    </div>
                    <p className="ch-card-blurb">{blurb}</p>
                  </Link>
                  <div className="ch-managed-foot">
                    <span className="ch-managed-meta">
                      {formatLabel(t, channel.output_format)} · {t('channels.defaultMinutes', { minutes: Math.round(channel.duration_target_seconds / 60) })}
                    </span>
                    <button
                      type="button"
                      className="ch-managed-del"
                      aria-label={t('channels.deleteChannelAria', { name: channelDisplay(t, channel).name })}
                      title={t('channels.deleteChannel')}
                      onClick={() => setDeleteTarget(channel)}
                    >
                      {t('channels.delete')}
                    </button>
                  </div>
                </div>
              )})}
            </div>
          )}
        </section>

        {deleteTarget && (
          <div className="modal-backdrop channel-delete-backdrop" onClick={() => !deleting && setDeleteTarget(null)}>
            <div
              className="modal-content channel-delete-modal"
              role="dialog"
              aria-modal="true"
              aria-labelledby="channel-delete-title"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="modal-body">
                <span className="channel-danger-badge">{t('channels.dangerousAction')}</span>
                <h2 id="channel-delete-title">{t('channels.deleteConfirmTitle')}</h2>
                <p>{t('channels.deleteConfirmBody', { name: channelDisplay(t, deleteTarget).name })}</p>
                <p className="channel-delete-warning">
                  {t('channels.deleteConfirmWarning')}
                </p>
                <div className="modal-actions">
                  <button type="button" className="btn-secondary" onClick={() => setDeleteTarget(null)} disabled={deleting}>
                    {t('channels.cancel')}
                  </button>
                  <button type="button" className="btn-danger" onClick={confirmDeleteChannel} disabled={deleting}>
                    {deleting ? t('channels.deleting') : t('channels.confirmDeleteIrreversible')}
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}

      </div>
    </>
  )
}

export function ChannelNew() {
  const { t } = useTranslation()
  const nav = useNavigate()
  const [creating, setCreating] = useState(false)
  const [query, setQuery] = useState('')
  const [activeTab, setActiveTab] = useState<string>('all')
  // 自定义频道 · 和 AI 一起创建的弹窗:描述 → AI 生成规则 → 预览微调 → 建。
  const [customModalOpen, setCustomModalOpen] = useState(false)
  const [customDesc, setCustomDesc] = useState('')
  const [designing, setDesigning] = useState(false)
  const [designErr, setDesignErr] = useState<string | null>(null)
  const [designed, setDesigned] = useState<ChannelDesignResult | null>(null)
  const [editName, setEditName] = useState('')
  const [editPositioning, setEditPositioning] = useState('')
  // 已有频道 → 我的频道置顶 + 标记"已创建"避免重复建。
  const [existingChannels, setExistingChannels] = useState<Series[]>([])
  useEffect(() => {
    api.series.list().then(setExistingChannels).catch(() => setExistingChannels([]))
  }, [])
  const existingIdByName = useMemo(() => {
    const m = new Map<string, string>()
    for (const s of existingChannels) {
      const k = (s.name || '').trim().toLowerCase()
      if (k && !m.has(k)) m.set(k, s.id)
    }
    return m
  }, [existingChannels])
  const existingIdFor = (name?: string) => existingIdByName.get((name || '').trim().toLowerCase())
  // 按频道类型(industry_tag)匹配已建频道 —— 比纯名字匹配更稳(名字漂移也认得出)。
  const existingIdByIndustry = useMemo(() => {
    const m = new Map<string, string>()
    for (const s of existingChannels) {
      const k = s.industry_tag || ''
      if (k && k !== 'custom_channel' && !m.has(k)) m.set(k, s.id)
    }
    return m
  }, [existingChannels])
  const customTemplate = useMemo<ChannelTemplate>(() => ({
    nameKey: 'channels.customName',
    directionKey: 'channels.customDirection',
    formatKey: 'channels.formatLongShort',
    industry: 'custom_channel',
    prompt: '频道方向：自定义。围绕用户给定的频道定位生产内容，保持频道内部风格一致。',
    group: 'custom',
  }), [])
  // 「自定义频道」卡放最前面,方便用户第一眼就看到"建自己的频道"。
  const allTemplates = useMemo(() => [customTemplate, ...CHANNEL_TEMPLATES], [customTemplate])
  // 该模板对应的已建频道 id(预设按类型认);自定义频道可建多个,不在网格里标"已创建"。
  const createdIdFor = (tpl: ChannelTemplate): string | undefined =>
    tpl === customTemplate ? undefined : (existingIdByIndustry.get(tpl.industry) || existingIdFor(t(tpl.nameKey)))

  const create = async (tpl: ChannelTemplate) => {
    setCreating(true)
    try {
      const channel = await api.series.create(channelPayload(tpl, t))
      nav(`/channels/${channel.id}`)
    } catch (e: any) {
      const detail = e?.response?.data?.detail
      if (detail && typeof detail === 'object' && detail.error === 'duplicate_channel') {
        void alertDialog(detail.message || t('channels.duplicateChannel'))
        if (detail.existing_id) nav(`/channels/${detail.existing_id}`)
        return
      }
      void alertDialog((typeof detail === 'string' ? detail : null) || e.message || t('channels.createChannelFailed'))
    } finally {
      setCreating(false)
    }
  }

  // 自定义频道弹窗:① 用户描述 → AI 生成规则(命中红线返回 ok:false)。
  const runDesign = async () => {
    const desc = customDesc.trim()
    if (!desc) { setDesignErr(t('channels.customDescRequired')); return }
    setDesigning(true); setDesignErr(null); setDesigned(null)
    try {
      const res = await api.series.design(desc, outputLanguageFromUi())
      if (!res.ok) { setDesignErr(res.reason || t('channels.customBlockedHint')); return }
      setDesigned(res)
      setEditName(res.name || res.rule?.label || '')
      setEditPositioning(res.rule?.positioning || '')
    } catch {
      setDesignErr(t('channels.customDesignFailed'))
    } finally {
      setDesigning(false)
    }
  }

  // ② 预览微调后创建:把(可能改过的)name/positioning 写回 rule,建到 custom_channel。
  const createCustom = async () => {
    if (!designed?.rule) return
    const name = editName.trim()
    if (!name) { setDesignErr(t('channels.customNameRequired')); return }
    setCreating(true); setDesignErr(null)
    try {
      const rule: ChannelRuleJson = { ...designed.rule, positioning: editPositioning.trim() || designed.rule.positioning }
      const channel = await api.series.create(customChannelPayload(name, designed.director_prompt || '', rule))
      nav(`/channels/${channel.id}`)
    } catch (e: any) {
      const detail = e?.response?.data?.detail
      if (detail && typeof detail === 'object' && detail.error === 'duplicate_channel') {
        setDesignErr(detail.message || t('channels.duplicateChannel'))
        if (detail.existing_id) nav(`/channels/${detail.existing_id}`)
        return
      }
      setDesignErr((typeof detail === 'string' ? detail : null) || e.message || t('channels.createChannelFailed'))
    } finally {
      setCreating(false)
    }
  }

  const closeCustomModal = () => {
    if (designing || creating) return
    setCustomModalOpen(false)
    setCustomDesc(''); setDesigned(null); setDesignErr(null); setEditName(''); setEditPositioning('')
  }

  // 过滤:有搜索词 → 全部模板里模糊匹配(忽略 Tab);否则按 Tab 分类。
  const q = query.trim().toLowerCase()
  const matchesQuery = (tpl: ChannelTemplate) => {
    if (!q) return true
    return `${t(tpl.nameKey)} ${t(`channels.blurb.${tpl.industry}`)} ${t(tpl.directionKey)}`.toLowerCase().includes(q)
  }
  const visible = allTemplates
    .filter((tpl) => {
      if (tpl === customTemplate) return q ? matchesQuery(tpl) : activeTab === 'all'  // 自定义卡只在「全部」或搜索命中时出现
      if (!matchesQuery(tpl)) return false
      if (q) return true  // 搜索时跨全部分类
      const meta = CHANNEL_META[tpl.industry]
      if (activeTab === 'all') return true
      if (activeTab === 'recommended') return !!meta?.recommended
      return meta?.category === activeTab
    })
    // 已创建的排到最下面(稳定排序,保留组内原顺序),让可创建的排在前面好挑。
    .sort((a, b) => (createdIdFor(a) ? 1 : 0) - (createdIdFor(b) ? 1 : 0))

  return (
    <>
      <header className="page-header">
        <div className="crumb">
          <Link to="/channels" className="seg">{t('channels.channels')}</Link>
          <span className="sep">/</span>
          <span className="here">{t('channels.createChannel')}</span>
        </div>
        <Link to="/channels" className="page-back-link">{t('channels.backToChannelsHome')}</Link>
      </header>
      <div className="workspace-page channel-page">
        <div className="channel-picker-head">
          <h1>{t('channels.chooseDirectionTitle')}</h1>
          <p>{t('channels.chooseDirectionSubtitle')}</p>
        </div>

        <div className="channel-search-row">
          <div className="channel-search-box">
            <span className="channel-search-icon">🔍</span>
            <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t('channels.searchPlaceholder')} />
          </div>
          <button type="button" className="channel-recommend-btn" onClick={() => { setQuery(''); setActiveTab('recommended') }}>
            ✨ {t('channels.helpMeRecommend')}
          </button>
        </div>

        {existingChannels.length > 0 && (
          <div className="channel-mine">
            <h3>{t('channels.myChannels')}</h3>
            <div className="channel-mine-strip">
              {existingChannels.map((ch) => (
                <Link key={ch.id} to={`/channels/${ch.id}`} className="channel-mine-card">
                  <span className="ch-mine-icon">
                    {channelImage(ch.industry_tag ?? undefined)
                      ? <img src={channelImage(ch.industry_tag ?? undefined)} alt="" />
                      : (CHANNEL_META[resolveChannelTag(ch.industry_tag)] || { icon: '📺' }).icon}
                  </span>
                  <span className="ch-mine-name">{channelDisplay(t, ch).name}</span>
                  <span className="ch-mine-badge">✓ {t('channels.alreadyCreatedBadge')}</span>
                </Link>
              ))}
            </div>
          </div>
        )}

        <div className="channel-tabs">
          {CHANNEL_TABS.map((tab) => (
            <button
              key={tab}
              type="button"
              className={`channel-tab${!q && activeTab === tab ? ' active' : ''}`}
              onClick={() => { setQuery(''); setActiveTab(tab) }}
            >
              {t(`channels.tab.${tab}`)}
            </button>
          ))}
        </div>

        {visible.length === 0 ? (
          <p className="hint">{t('channels.noResults')}</p>
        ) : (
          <div className="channel-card-grid">
            {visible.map((tpl, cardIdx) => {
              const meta = CHANNEL_META[tpl.industry] || { icon: '📺', tags: [] }
              const isCustom = tpl === customTemplate
              const dupId = createdIdFor(tpl)
              // 引导锚点:第一张非自定义的预设卡挂 data-tour="preset-card"。
              const isFirstPreset = !isCustom && cardIdx === visible.findIndex((x) => x !== customTemplate)
              return (
                <div
                  key={tpl.nameKey}
                  data-tour={isFirstPreset ? 'preset-card' : undefined}
                  className={`channel-card-sm${dupId ? ' created' : ''}`}
                >
                  <div className="ch-card-head">
                    <span className="ch-card-icon">
                      {channelImage(tpl.industry)
                        ? <img src={channelImage(tpl.industry)} alt="" loading="lazy" />
                        : meta.icon}
                    </span>
                    <strong className="ch-card-name">{t(tpl.nameKey)}</strong>
                    {dupId && <span className="template-created-badge">✓ {t('channels.alreadyCreatedBadge')}</span>}
                  </div>
                  <p className="ch-card-blurb">{t(`channels.blurb.${tpl.industry}`)}</p>
                  <div className="ch-card-tags">
                    <span className="ch-format">{t(tpl.formatKey)}</span>
                    {(meta.tags || []).map((tg) => (
                      <span key={tg} className="ch-tag">{t(`channels.${tg}`)}</span>
                    ))}
                  </div>
                  {isCustom ? (
                    <button type="button" data-tour="custom-channel" className="ch-card-btn primary" onClick={() => setCustomModalOpen(true)}>
                      {t('channels.customCreateWithAi')}
                    </button>
                  ) : dupId ? (
                    <button type="button" className="ch-card-btn" onClick={() => nav(`/channels/${dupId}`)}>
                      {t('channels.goToChannel')}
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="ch-card-btn primary"
                      onClick={() => create(tpl)}
                      disabled={creating}
                    >
                      {creating ? t('channels.creating') : t('channels.confirmCreateChannel')}
                    </button>
                  )}
                </div>
              )
            })}
          </div>
        )}

        {customModalOpen && (
          <div className="modal-backdrop" onClick={closeCustomModal}>
            <div
              className="modal-content channel-custom-modal"
              role="dialog"
              aria-modal="true"
              aria-labelledby="channel-custom-title"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="modal-body">
                <h2 id="channel-custom-title">{t('channels.customModalTitle')}</h2>
                {!designed ? (
                  <>
                    <p className="channel-custom-sub">{t('channels.customModalSubtitle')}</p>
                    <label className="channel-custom-label" htmlFor="channel-custom-desc">
                      {t('channels.customDescLabel')}
                    </label>
                    <textarea
                      id="channel-custom-desc"
                      className="channel-custom-textarea"
                      rows={5}
                      value={customDesc}
                      onChange={(e) => setCustomDesc(e.target.value)}
                      placeholder={t('channels.customDescPlaceholder')}
                      disabled={designing}
                    />
                    {designErr && <div className="billing-error">{designErr}</div>}
                    <div className="modal-actions">
                      <button type="button" className="btn-secondary" onClick={closeCustomModal} disabled={designing}>
                        {t('channels.cancel')}
                      </button>
                      <button type="button" className="btn-primary" onClick={runDesign} disabled={designing || !customDesc.trim()}>
                        {designing ? t('channels.customGenerating') : t('channels.customGenerate')}
                      </button>
                    </div>
                  </>
                ) : (
                  <>
                    <p className="channel-custom-sub">{t('channels.customPreviewHint')}</p>
                    <label className="channel-custom-label" htmlFor="channel-custom-name">
                      {t('channels.customPreviewNameLabel')}
                    </label>
                    <input
                      id="channel-custom-name"
                      className="ch-custom-input"
                      value={editName}
                      onChange={(e) => setEditName(e.target.value)}
                      placeholder={t('channels.channelNamePlaceholder')}
                    />
                    <label className="channel-custom-label" htmlFor="channel-custom-pos">
                      {t('channels.customPreviewPositioningLabel')}
                    </label>
                    <textarea
                      id="channel-custom-pos"
                      className="channel-custom-textarea"
                      rows={6}
                      value={editPositioning}
                      onChange={(e) => setEditPositioning(e.target.value)}
                    />
                    {(designed.rule?.subjects?.length ?? 0) > 0 && (
                      <div className="channel-custom-preview">
                        <span className="channel-custom-label">{t('channels.customPreviewSubjectsLabel')}</span>
                        <div className="channel-custom-chips">
                          {(designed.rule?.subjects ?? []).slice(0, 12).map((s, i) => (
                            <span key={i} className="channel-custom-chip">{s}</span>
                          ))}
                        </div>
                      </div>
                    )}
                    {designed.rule?.closure_rule && (
                      <p className="channel-custom-closure">
                        <strong>{t('channels.customPreviewClosureLabel')}：</strong>{designed.rule.closure_rule}
                      </p>
                    )}
                    {designErr && <div className="billing-error">{designErr}</div>}
                    <div className="modal-actions">
                      <button type="button" className="btn-secondary" onClick={() => { setDesigned(null); setDesignErr(null) }} disabled={creating}>
                        {t('channels.customRegenerate')}
                      </button>
                      <button type="button" className="btn-primary" onClick={createCustom} disabled={creating || !editName.trim()}>
                        {creating ? t('channels.creating') : t('channels.confirmCreateChannel')}
                      </button>
                    </div>
                  </>
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </>
  )
}

// 三档 约6/约8/约10 分钟,默认约6(甜区)。字数硬下限≈6.3分,故最短≈6分钟——
// 再短会为凑时长牺牲故事完整性(铁律:完整性 > 时长)。内容量随时长缩放见后端 script_gen。
export const BATCH_LONG_DURATIONS = ['约 6 分钟', '约 8 分钟', '约 10 分钟']
// 短视频上限 2 分钟(去掉 3 分钟 / 3-5 分钟:短视频没必要那么长)。
// 短视频下限提到 45 秒:去掉 15 秒以内 / 15-30 秒 / 30-45 秒(产品决定不再要更短的)。
export const BATCH_SHORT_DURATIONS = ['45-60 秒', '1 分钟', '2 分钟']
// 时长选项的值(存库/解析)保持中文串不变;仅在英文界面把【显示 label】换成英文。
const BATCH_DURATION_LABEL_EN: Record<string, string> = {
  '约 6 分钟': 'About 6 min', '约 8 分钟': 'About 8 min', '约 10 分钟': 'About 10 min',
  // 历史值(旧存档/旧批次)仍给英文显示,避免回落成中文串。
  '3-5 分钟': '3-5 min', '5-8 分钟': '5-8 min',
  '15 秒以内': 'Under 15s', '15-30 秒': '15-30s', '30-45 秒': '30-45s',
  '45-60 秒': '45-60s', '1 分钟': '1 min', '2 分钟': '2 min', '3 分钟': '3 min',
}
export function batchDurationLabel(value: string, lang: 'zh' | 'en'): string {
  return lang === 'en' ? (BATCH_DURATION_LABEL_EN[value] || value) : value
}
const BATCH_SETTINGS_DEFAULTS_KEY = 'media-buddy.channel.batch-settings.defaults'
export const DEFAULT_BATCH_LONG_DURATION = BATCH_LONG_DURATIONS[0] as string
export const DEFAULT_BATCH_SHORT_DURATION = BATCH_SHORT_DURATIONS[0] as string

export type BatchSettingsDefaults = {
  outputLanguage?: 'zh' | 'en'
  longOutputLanguage?: 'zh' | 'en'
  shortOutputLanguage?: 'zh' | 'en'
  ttsProvider?: string
  longTtsProvider?: string
  shortTtsProvider?: string
  longDuration?: string
  shortDuration?: string
  shortFramingStyle?: 'fill' | 'blur'
  framingStyle?: 'fill' | 'blur'
  // 背景音乐开关(长短共用一个开关)。默认关 —— 躲 YouTube 版权投诉。
  includeMusic?: boolean
  // 语速挡位(长短共用):正常1.0/稍快1.2/快1.4/极快1.6。默认【快】(1.4)。
  ttsSpeed?: number
  // 字幕字号缩放系数(客户拖拉杆自选):1.0=标准。默认标准。
  subtitleFontScale?: number
  // 关掉是少数情况(比如要拿去二次剪辑)。⚠️ `undefined` 也当成开,
  // 别把「没保存过」误当成「关掉了」。
  includeSubtitles?: boolean
  // 高级英语配音档（'normal'=千问 / 'advanced'=ElevenLabs 英美音）。
  //
  //    弹框每次打开都硬写成 'normal'。后果不只是「丢一个开关」：
  //    档位回到 normal → 音色目录拉的是普通目录 → 刚恢复出来的
  //    ElevenLabs 音色**不在这份目录里** → 被 `preferredBatchVoice`
  //    顶掉换成千问。客户选的高级英语配音就这么无声无息没了。
  //
  // ⚠️ 恢复时要**先定档位、再拉目录**，顺序反了同样会被顶掉。
  voiceTier?: 'normal' | 'advanced'
  // 字幕语言。⚠️ **以前这个值根本没存** —— 弹框里改完关掉就没了,
  subtitleLanguage?: 'zh-Hans' | 'zh-Hant' | 'en' | 'zh-Hans+en' | 'zh-Hant+en'
}

// 语速挡位 → 倍速(批量 + 工作室共用同一套值)。
export const BATCH_SPEEDS: { value: number; key: string }[] = [
  { value: 1.0, key: 'workspace.speedNormal' },
  { value: 1.2, key: 'workspace.speedFaster' },
  { value: 1.4, key: 'workspace.speedFast' },
  { value: 1.6, key: 'workspace.speedVeryFast' },
]
export function defaultBatchSpeed(defaults: BatchSettingsDefaults): number {
  const s = defaults.ttsSpeed
  return typeof s === 'number' && s >= 0.7 && s <= 2.0 ? s : 1.4 // 默认【快】档
}
export function defaultBatchSubtitleScale(defaults: BatchSettingsDefaults): number {
  const s = defaults.subtitleFontScale
  return typeof s === 'number' && s >= 0.5 && s <= 2.0 ? s : SUBTITLE_SCALE_DEFAULT // 默认标准
}

// 老项目里存的 azure_* 仍可被后端 _qwen_voice_for 兜底映射到千问,不影响历史。
const BATCH_BUILTIN_QWEN_VOICES: TtsVoice[] = QWEN_VOICES

export function batchDurationToSeconds(label: string): number {
  // 按【具体数字】判定,从大到小,避免"含 5 就先命中 480"这类子串误配
  // (旧 bug:'3-5 分钟' 里的 '5' 先命中 /5-8|5/ → 480,300 分支成死代码)。
  if (/20/.test(label)) return 1800
  if (/12|15/.test(label)) return 1200
  if (/10/.test(label)) return 600   // 约 10 分钟
  if (/8/.test(label)) return 480    // 约 8 分钟(含历史 5-8)
  if (/6/.test(label)) return 360    // 约 6 分钟
  if (/5/.test(label)) return 300    // 历史 5/3-5(后端字数下限会兜到 ~6.3 分)
  if (/3/.test(label)) return 300
  return 600
}

export function batchShortDurationToSeconds(label: string): number {
  if (/3-5/.test(label)) return 300
  if (/3\s*分钟/.test(label)) return 180
  if (/2\s*分钟/.test(label)) return 120
  if (/1\s*分钟/.test(label)) return 60
  if (/45-60|45\s*[-~到至]\s*60/.test(label)) return 60
  if (/30-45|30\s*[-~到至]\s*45/.test(label)) return 45
  if (/15-30|15\s*[-~到至]\s*30/.test(label)) return 30
  return 15
}

// "长视频/内置目录能用的音色" —— 现在是千问(qwen_);历史 azure_ 仍保留兼容旧存档。
export function isAzureVoiceProvider(provider?: string): boolean {
  return !!provider && (provider.startsWith('qwen_') || provider.startsWith('azure_'))
}

/**
 * 这是不是一个我们认识的音色标识。**读取保存的默认值时用它把关。**
 *
 *    而那个函数只认 `qwen_` / `azure_` —— **`elevenlabs_` 一律不认**。
 *    于是客户在设置里选了「高级英语配音」（ElevenLabs 的 Rachel/Adam 这些），
 *    存是存进去了，**一重新打开就被这道校验当垃圾丢掉**，退回中文音色。
 *    客户看到的是「我明明选了英文配音，出来还是中文」，而且**不报任何错**。
 *
 * ⚠️ 高级英语配音在出片管线里是**放行**的（`pipeline_service` 那道
 *    「长视频不用 ElevenLabs」的闸对英文 + 高级音色开了例外）。
 *    所以前端这里丢掉它，纯粹是白白截断了一条本来走得通的路。
 */
export function isKnownVoiceProvider(provider?: string): boolean {
  return isAzureVoiceProvider(provider) || !!provider?.startsWith('elevenlabs_')
}

export function batchVoiceLanguage(voice: Pick<TtsVoice, 'provider_key' | 'language_hint'>): string {
  if (voice.language_hint) return voice.language_hint
  if (voice.provider_key.startsWith('azure_') && /^azure_(ava|andrew|aria|brian|guy|jenny|davis|steffan)/.test(voice.provider_key)) return 'en'
  return 'zh'
}

export function batchVoiceMatchesLanguage(voice: TtsVoice, language: 'zh' | 'en'): boolean {
  // 千问音色多语种(中英都能说),不按语言过滤 —— 中英批量都展示全部 24 个。
  if (isQwenVoiceProvider(voice.provider_key)) return true
  const hint = batchVoiceLanguage(voice)
  return language === 'en' ? hint === 'en' : hint !== 'en'
}

export function cleanBatchVoice(voice: TtsVoice): TtsVoice {
  return {
    ...voice,
    voice_id: voice.voice_id || voice.provider_key,
    language_hint: batchVoiceLanguage(voice),
    description: voice.description || '',
    sample_url: voice.sample_url || '',
  }
}

export function mergeBatchVoices(remote: TtsVoice[], format: 'youtube_long' | 'youtube_shorts'): TtsVoice[] {
  const cleaned = remote.map(cleanBatchVoice)
  if (format === 'youtube_shorts') return cleaned

  const byKey = new Map<string, TtsVoice>()
  BATCH_BUILTIN_QWEN_VOICES.forEach((voice) => byKey.set(voice.provider_key, cleanBatchVoice(voice)))
  cleaned.filter((voice) => isAzureVoiceProvider(voice.provider_key)).forEach((voice) => {
    const existing = byKey.get(voice.provider_key)
    byKey.set(voice.provider_key, cleanBatchVoice(existing ? { ...existing, ...voice } : voice))
  })
  return Array.from(byKey.values())
}

export function preferredBatchVoice(
  voices: TtsVoice[],
  language: 'zh' | 'en',
  currentProvider?: string,
): TtsVoice | undefined {
  const current = voices.find((voice) => voice.provider_key === currentProvider)
  if (current && batchVoiceMatchesLanguage(current, language)) return current
  return voices.find((voice) => batchVoiceMatchesLanguage(voice, language)) || voices[0]
}

export function defaultBatchOutputLanguage(
  defaults: BatchSettingsDefaults,
  format: 'youtube_long' | 'youtube_shorts',
): 'zh' | 'en' {
  // 客户明确保存的频道输出语言优先；页面 UI 语言只用于首次没有客户选择时的兜底。
  const saved = format === 'youtube_long'
    ? defaults.longOutputLanguage
    : defaults.shortOutputLanguage
  return saved || defaults.outputLanguage || outputLanguageFromUi()
}

export function defaultBatchTtsProvider(
  defaults: BatchSettingsDefaults,
  format: 'youtube_long' | 'youtube_shorts',
  channelProvider?: string,
): string {
  const legacy = defaults.ttsProvider
  if (format === 'youtube_long') {
    return defaults.longTtsProvider
      || (isAzureVoiceProvider(legacy) ? legacy : undefined)
      || (isAzureVoiceProvider(channelProvider) ? channelProvider : undefined)
      || QWEN_VOICE_DEFAULT
  }
  return defaults.shortTtsProvider
    || (isQwenVoiceProvider(legacy) ? legacy : undefined)
    || QWEN_VOICE_DEFAULT
}

export function defaultBatchFramingStyle(defaults: BatchSettingsDefaults): 'fill' | 'blur' {
  return defaults.shortFramingStyle || defaults.framingStyle || 'fill'
}

export function defaultBatchIncludeSubtitles(defaults: BatchSettingsDefaults): boolean {
  // 默认**开**。⚠️ `undefined`(没保存过)也算开 —— 别把「没设置」当成「关掉了」,
  //    否则老频道一升级就全部没字幕。只有显式存过 false 才是关。
  return defaults.includeSubtitles !== false
}
export function defaultBatchIncludeMusic(defaults: BatchSettingsDefaults): boolean {
  // 默认关:躲版权投诉。只有用户保存过 includeMusic=true 才默认开。
  return defaults.includeMusic === true
}

export type BatchSubtitleLanguage = 'zh-Hans' | 'zh-Hant' | 'en' | 'zh-Hans+en' | 'zh-Hant+en'

/**
 * 打开设置弹框时，字幕语言该显示成什么。
 *
 *    字幕依然没有保存为英文。」根因是两个弹框的初始值都直接取**界面语言**，
 *    **压根不看客户存过什么** —— 存进去的值读都没读。
 *
 * ⚠️ 客户存过的优先；没存过才退回界面语言。
 *    反过来（界面语言优先）等于每次都把客户的选择盖掉。
 */
export function defaultBatchSubtitleLanguage(
  defaults: BatchSettingsDefaults,
  outputLanguage: 'zh' | 'en',
): BatchSubtitleLanguage {
  //    旧版在中文界面 + 英文出片时默认给简体中文字幕 —— 客户忘了手动改，
  //    整条英文片配中文字幕报废。这类客户还不会触发任何 onChange，
  //    所以必须在【默认值这一层】就按出片语言来。
  return (defaults.subtitleLanguage as BatchSubtitleLanguage)
    || (effectiveSubtitleLanguage(undefined, outputLanguage) as BatchSubtitleLanguage)
}

/**
 * 打开设置弹框时，配音档位该停在哪一档。
 *
 * ⚠️ 存过的档位优先；没存过就**从音色本身反推** —— 老客户的浏览器里
 *    他们的高级配音会在下一次打开弹框时被打回普通档。
 */
export function defaultBatchVoiceTier(
  defaults: BatchSettingsDefaults,
  format: 'youtube_long' | 'youtube_shorts',
): 'normal' | 'advanced' {
  if (defaults.voiceTier) return defaults.voiceTier
  const provider = format === 'youtube_long' ? defaults.longTtsProvider : defaults.shortTtsProvider
  return provider?.startsWith('elevenlabs_') ? 'advanced' : 'normal'
}

export function normalizeBatchSettingsDefaults(defaults: BatchSettingsDefaults): BatchSettingsDefaults {
  const next: BatchSettingsDefaults = { ...defaults }
  if (defaults.outputLanguage) {
    next.longOutputLanguage = next.longOutputLanguage || defaults.outputLanguage
    next.shortOutputLanguage = next.shortOutputLanguage || defaults.outputLanguage
  }
  if (defaults.ttsProvider) {
    if (isAzureVoiceProvider(defaults.ttsProvider)) {
      next.longTtsProvider = next.longTtsProvider || defaults.ttsProvider
      next.shortTtsProvider = next.shortTtsProvider || defaults.ttsProvider
    } else {
      next.shortTtsProvider = next.shortTtsProvider || defaults.ttsProvider
    }
  }
  if (defaults.framingStyle) next.shortFramingStyle = next.shortFramingStyle || defaults.framingStyle
  return next
}

function batchSettingsDefaultsKey(channelId: string): string {
  return `${BATCH_SETTINGS_DEFAULTS_KEY}:${channelId}`
}

export function readBatchSettingsDefaults(channelId?: string): BatchSettingsDefaults {
  if (typeof window === 'undefined' || !channelId) return {}
  try {
    const parsed = JSON.parse(window.localStorage.getItem(batchSettingsDefaultsKey(channelId)) || '{}') as BatchSettingsDefaults
    const outputLanguage = parsed.outputLanguage === 'en' || parsed.outputLanguage === 'zh' ? parsed.outputLanguage : undefined
    const longOutputLanguage = parsed.longOutputLanguage === 'en' || parsed.longOutputLanguage === 'zh' ? parsed.longOutputLanguage : undefined
    const shortOutputLanguage = parsed.shortOutputLanguage === 'en' || parsed.shortOutputLanguage === 'zh' ? parsed.shortOutputLanguage : undefined
    const framingStyle = parsed.framingStyle === 'blur' || parsed.framingStyle === 'fill' ? parsed.framingStyle : undefined
    const shortFramingStyle = parsed.shortFramingStyle === 'blur' || parsed.shortFramingStyle === 'fill' ? parsed.shortFramingStyle : undefined
    return normalizeBatchSettingsDefaults({
      outputLanguage,
      longOutputLanguage,
      shortOutputLanguage,
      ttsProvider: typeof parsed.ttsProvider === 'string' && parsed.ttsProvider.trim() ? parsed.ttsProvider : undefined,
      longTtsProvider: isKnownVoiceProvider(parsed.longTtsProvider) ? parsed.longTtsProvider : undefined,
      shortTtsProvider: typeof parsed.shortTtsProvider === 'string' && parsed.shortTtsProvider.trim()
        ? parsed.shortTtsProvider
        : undefined,
      longDuration: BATCH_LONG_DURATIONS.includes(parsed.longDuration || '') ? parsed.longDuration : undefined,
      shortDuration: BATCH_SHORT_DURATIONS.includes(parsed.shortDuration || '') ? parsed.shortDuration : undefined,
      shortFramingStyle,
      framingStyle,
      includeMusic: typeof parsed.includeMusic === 'boolean' ? parsed.includeMusic : undefined,
      ttsSpeed: typeof parsed.ttsSpeed === 'number' && parsed.ttsSpeed >= 0.7 && parsed.ttsSpeed <= 2.0 ? parsed.ttsSpeed : undefined,
      includeSubtitles: typeof parsed.includeSubtitles === 'boolean' ? parsed.includeSubtitles : undefined,
      voiceTier: parsed.voiceTier === 'advanced' || parsed.voiceTier === 'normal' ? parsed.voiceTier : undefined,
      subtitleLanguage: ['zh-Hans', 'zh-Hant', 'en', 'zh-Hans+en', 'zh-Hant+en'].includes(parsed.subtitleLanguage || '')
        ? parsed.subtitleLanguage
        : undefined,
    })
  } catch {
    return {}
  }
}

export function saveBatchSettingsDefaults(channelId: string, defaults: BatchSettingsDefaults): boolean {
  if (typeof window === 'undefined' || !channelId) return false
  try {
    window.localStorage.setItem(batchSettingsDefaultsKey(channelId), JSON.stringify(defaults))
    return true
  } catch {
    // 存储失败(Safari 隐私模式 / 配额满)—— 返回 false 让调用方明确提示用户,
    // 不再静默吞掉(用户会以为"存了默认"其实没存)。
    return false
  }
}

// ── 频道「上次用的长/短」记忆 ──────────────────────────────
// 按频道存;频道工作台的排队执行长/短开关据此预选,记住客户上次用的类型。
const CHANNEL_DEFAULT_ENTRY_KEY = 'media-buddy.channel.default-entry'
export function readChannelDefaultEntry(channelId?: string): 'long' | 'short' | null {
  if (typeof window === 'undefined' || !channelId) return null
  const v = window.localStorage.getItem(`${CHANNEL_DEFAULT_ENTRY_KEY}:${channelId}`)
  return v === 'long' || v === 'short' ? v : null
}
export function saveChannelDefaultEntry(channelId: string, entry: 'long' | 'short'): boolean {
  if (typeof window === 'undefined' || !channelId) return false
  try {
    window.localStorage.setItem(`${CHANNEL_DEFAULT_ENTRY_KEY}:${channelId}`, entry)
    return true
  } catch {
    return false
  }
}

export function ChannelDetail() {
  const { t, i18n } = useTranslation()
  const { id } = useParams()
  const nav = useNavigate()
  const [channel, setChannel] = useState<Series | null>(null)
  const [projects, setProjects] = useState<Project[]>([])
  // P1-4 页面白屏异常恢复:频道加载失败时用。channelLoadFailed=真则显示手动重试而非永远转圈。
  const [channelLoadFailed, setChannelLoadFailed] = useState(false)
  const channelSilentRetried = useRef(false)
  const [memory, setMemory] = useState<ChannelMemory | null>(null)
  // Source-available build: no AI topic engine. Titles are typed by hand, one per line.
  const [manualTitles, setManualTitles] = useState('')
  const [savedBatchDefaults, setSavedBatchDefaults] = useState<BatchSettingsDefaults>(() => readBatchSettingsDefaults(id))
  // 按频道记住上次用的长/短,预选到排队执行的长/短开关(不再要求用户显式设「默认」)。
  const [bulkFormat, setBulkFormat] = useState<'youtube_long' | 'youtube_shorts'>(
    () => (readChannelDefaultEntry(id) === 'short' ? 'youtube_shorts' : 'youtube_long'),
  )
  // 短视频画面风格:fill 普通竖版裁剪铺满 / blur 背景虚化+正方形前景。仅短视频可选。
  const [bulkFraming, setBulkFraming] = useState<'fill' | 'blur'>(defaultBatchFramingStyle(savedBatchDefaults))
  // 背景音乐开关(长短共用)。默认关 —— 躲 YouTube 版权投诉,客户想要才打开。
  const [bulkMusic, setBulkMusic] = useState<boolean>(defaultBatchIncludeMusic(savedBatchDefaults))
  const [bulkSubtitles, setBulkSubtitles] = useState<boolean>(defaultBatchIncludeSubtitles(savedBatchDefaults))
  // 语速挡位(长短共用)+ 按挡试听。默认稍快(1.2)。
  const [bulkSpeed, setBulkSpeed] = useState<number>(defaultBatchSpeed(savedBatchDefaults))
  const [bulkSubtitleScale, setBulkSubtitleScale] = useState<number>(defaultBatchSubtitleScale(savedBatchDefaults))
  const [playingSpeed, setPlayingSpeed] = useState<number | null>(null)
  const speedAudioRef = useRef<HTMLAudioElement | null>(null)
  const [batchRun, setBatchRun] = useState<BatchRun | null>(null)
  // Last seen completed count — so we refresh the wallet (sidebar balance) only
  // when a video actually finishes, not on every poll tick.
  const lastCompletedRef = useRef(0)
  const [batchBusy, setBatchBusy] = useState(false)
  const [batchMessage, setBatchMessage] = useState<string | null>(null)
  const [batchError, setBatchError] = useState<string | null>(null)
  // 最近项目里直接多选下载(手机用户不用逐条跳进详情页下载)。
  const [dlSel, setDlSel] = useState<Set<string>>(new Set())
  const [downloading, setDownloading] = useState(false)
  // 选题撞了频道已有内容时,显示"仍然继续"让用户确认绕过重复闸。
  const [duplicateConfirm, setDuplicateConfirm] = useState(false)
  // 语言默认跟界面语言走(中文界面→中文,英文界面→英文),弹窗里仍可手动覆盖。
  const [batchLanguage, setBatchLanguage] = useState<'zh' | 'en'>(defaultBatchOutputLanguage(savedBatchDefaults, 'youtube_long'))
  // 字幕语言(可独立于配音):简/繁/英 + 中英双语。默认跟随界面语言。
  // ⚠️ 客户存过的优先 —— 以前直接取界面语言，等于每次打开都盖掉他的选择。
  const [batchSubtitleLanguage, setBatchSubtitleLanguage] = useState<BatchSubtitleLanguage>(
    defaultBatchSubtitleLanguage(savedBatchDefaults, defaultBatchOutputLanguage(savedBatchDefaults, 'youtube_long')),
  )
  // 界面/系统语言:驱动展示 label(音色名、时长选项)跟随页面语言(中文页中文/英文页英文)。
  const uiLang = outputLanguageFromUi()
  const [batchTtsProvider, setBatchTtsProvider] = useState<string>(defaultBatchTtsProvider(savedBatchDefaults, 'youtube_long', channel?.tts_provider))
  const [batchDuration, setBatchDuration] = useState<string>(savedBatchDefaults.longDuration || DEFAULT_BATCH_LONG_DURATION)
  const [batchShortDuration, setBatchShortDuration] = useState<string>(savedBatchDefaults.shortDuration || DEFAULT_BATCH_SHORT_DURATION)
  const [batchVoices, setBatchVoices] = useState<TtsVoice[]>([])
  // 高级英语配音档:'normal'=千问 / 'advanced'=ElevenLabs 英/美音。仅英文输出可用(照搬工作台)。
  const [batchVoiceTier, setBatchVoiceTier] = useState<'normal' | 'advanced'>(
    defaultBatchVoiceTier(savedBatchDefaults, 'youtube_long'),
  )
  const [showBatchSettings, setShowBatchSettings] = useState(false)
  const [batchDefaultMessage, setBatchDefaultMessage] = useState<string | null>(null)
  // 点"保存为默认"弹二次确认框(取代原来点了只出一行弱文字的交互)。手机端同一组件、同一弹窗。
  const [showBatchDefaultConfirm, setShowBatchDefaultConfirm] = useState(false)

  // 排队执行长/短开关变化时,按频道记住上次用的类型(下次进来预选)。
  useEffect(() => {
    if (id) saveChannelDefaultEntry(id, bulkFormat === 'youtube_long' ? 'long' : 'short')
  }, [id, bulkFormat])

  useEffect(() => {
    if (!id) return
    const defaults = readBatchSettingsDefaults(id)
    setSavedBatchDefaults(defaults)
    setBatchLanguage(defaultBatchOutputLanguage(defaults, bulkFormat))
    setBatchTtsProvider(defaultBatchTtsProvider(defaults, bulkFormat))
    setBatchDuration(defaults.longDuration || DEFAULT_BATCH_LONG_DURATION)
    setBatchShortDuration(defaults.shortDuration || DEFAULT_BATCH_SHORT_DURATION)
    setBulkFraming(defaultBatchFramingStyle(defaults))
    setBulkMusic(defaultBatchIncludeMusic(defaults))
    setBulkSpeed(defaultBatchSpeed(defaults))
    setBulkSubtitleScale(defaultBatchSubtitleScale(defaults))
    // ⚠️ 这两项以前漏了 —— 存进去了却从来不恢复，等于没存。
    //    档位必须在音色目录 effect 之前就位，否则目录换掉会把音色顶飞。
    setBatchVoiceTier(defaultBatchVoiceTier(defaults, bulkFormat))
    setBatchSubtitleLanguage(defaultBatchSubtitleLanguage(defaults, defaultBatchOutputLanguage(defaults, bulkFormat)))
    setBatchDefaultMessage(null)
  }, [id])

  // 按语速挡位试听:用当前批量选的音色,现场合成该速度的预览。
  const previewBatchSpeed = (speed: number) => {
    if (!batchTtsProvider) return
    if (playingSpeed === speed) {
      speedAudioRef.current?.pause(); speedAudioRef.current = null; setPlayingSpeed(null); return
    }
    speedAudioRef.current?.pause()
    const audio = new Audio(api.tts.previewUrl(batchTtsProvider, speed, ttsPreviewText(batchLanguage)))
    audio.onended = () => setPlayingSpeed(null)
    audio.onerror = () => setPlayingSpeed(null)
    speedAudioRef.current = audio
    setPlayingSpeed(speed)
    audio.play().catch(() => setPlayingSpeed(null))
  }

  useEffect(() => {
    setBatchLanguage(defaultBatchOutputLanguage(savedBatchDefaults, bulkFormat))
    setBatchTtsProvider(defaultBatchTtsProvider(savedBatchDefaults, bulkFormat, channel?.tts_provider))
    if (bulkFormat === 'youtube_long') {
      setBatchDuration(savedBatchDefaults.longDuration || DEFAULT_BATCH_LONG_DURATION)
    } else {
      setBatchShortDuration(savedBatchDefaults.shortDuration || DEFAULT_BATCH_SHORT_DURATION)
      setBulkFraming(defaultBatchFramingStyle(savedBatchDefaults))
    }
    setBatchDefaultMessage(null)
  }, [bulkFormat])

  useEffect(() => {
    if (!id) return
    const applyChannel = (c: Series) => {
      setChannel(c)
      setChannelLoadFailed(false)
      channelSilentRetried.current = false
      try { sessionStorage.removeItem(`mb.reloaded.channel.${id}`) } catch {/* noop */}
      if (bulkFormat === 'youtube_long' && !savedBatchDefaults.longTtsProvider && !savedBatchDefaults.ttsProvider && c?.tts_provider) {
        setBatchTtsProvider(defaultBatchTtsProvider(savedBatchDefaults, 'youtube_long', c.tts_provider))
      }
    }
    // P1-4 异常驱动恢复:先静默重拉一次 → 仍失败、本会话没整页刷过、且用户没在输入 →
    // 整页刷新一次(sessionStorage 加锁,同页面会话最多一次,**绝不无限刷新**)→ 再不行显示手动重试。
    const onChannelError = () => {
      if (!channelSilentRetried.current) {
        channelSilentRetried.current = true
        window.setTimeout(() => { if (id) api.series.get(id).then(applyChannel).catch(onChannelError) }, 1200)
        return
      }
      const active = document.activeElement as HTMLElement | null
      const typing = !!active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA')
      let reloadedOnce = false
      try { reloadedOnce = !!sessionStorage.getItem(`mb.reloaded.channel.${id}`) } catch {/* noop */}
      if (!typing && !reloadedOnce) {
        try { sessionStorage.setItem(`mb.reloaded.channel.${id}`, '1'); window.location.reload(); return } catch {/* noop */}
      }
      setChannelLoadFailed(true)
    }
    api.series.get(id).then(applyChannel).catch(onChannelError)
    api.series.listProjects(id).then(setProjects).catch(() => setProjects([]))
    api.series.memory(id).then(setMemory).catch(() => setMemory(null))
  }, [id, bulkFormat, savedBatchDefaults])

  // 配音列表跟着批量的长/短切换走:短视频 → ElevenLabs 普通话(flag 开时);
  // 每个 voice 含 provider_key / display_name / language_hint。
  useEffect(() => {
    const fmt = bulkFormat === 'youtube_long' ? 'youtube_landscape' : 'youtube_shorts'
    // 高级英语档:拉 ElevenLabs 英文目录(英/美音)。直接用 remote,跳过 mergeBatchVoices ——
    // 否则长视频分支只保留千问/Azure,会把 ElevenLabs 音色过滤成空。
    const wantAdvanced = batchLanguage === 'en' && batchVoiceTier === 'advanced'
    let cancelled = false
    const req = wantAdvanced
      ? api.tts.listVoices(fmt, { tier: 'advanced', outputLanguage: 'en' })
      : api.tts.listVoices(fmt)
    req
      .then((res) => {
        if (cancelled) return
        setBatchVoices(wantAdvanced
          ? (res.voices || []).map(cleanBatchVoice)
          : mergeBatchVoices(res.voices || [], bulkFormat))
      })
      .catch((err) => {
        console.warn('batch tts voice catalog failed', err)
        if (cancelled) return
        if (wantAdvanced) {
          // 高级英语目录加载失败 → 提示 + 回退普通,不静默按高级处理。
          void alertDialog(t('workspace.voiceTierLoadFailed'))
          setBatchVoiceTier('normal')
        } else {
          setBatchVoices(mergeBatchVoices([], bulkFormat))
        }
      })
    return () => {
      cancelled = true
    }
  }, [bulkFormat, batchLanguage, batchVoiceTier])

  // 配音音色跟"输出语言"走:en → 优先 ElevenLabs(多语种、音质最好,原生说英文),
  // 否则退英文 Azure;zh → 频道默认中文音色。覆盖"界面=英文时初值就该是英文音色"以及
  // 切格式后旧音色不在目录里的情况。已是合适语言的音色则保留(尊重用户手选)。
  useEffect(() => {
    if (!batchVoices.length) return
    setBatchTtsProvider((prev) => {
      const next = preferredBatchVoice(batchVoices, batchLanguage, prev)
      return next?.provider_key || prev
    })
  }, [batchLanguage, batchVoices])

  // 预取缓存:长/短视频各一个池,「换一批」先从当前类型的池秒切 5 条,后台续满。
  // 长短来回切不重新生成(显示各自已出的批);只有点「换一批」才推进下一批。
  const filteredBatchVoices = useMemo(
    () => batchVoices.filter((voice) => batchVoiceMatchesLanguage(voice, batchLanguage)),
    [batchVoices, batchLanguage],
  )
  const selectedBatchVoice = useMemo(
    () => batchVoices.find((voice) => voice.provider_key === batchTtsProvider) || null,
    [batchVoices, batchTtsProvider],
  )

  useEffect(() => {
    if (!batchRun || !['pending', 'running'].includes(batchRun.status)) return
    const timer = window.setInterval(() => {
      api.batch.get(batchRun.id)
        .then((next) => {
          setBatchRun(next)
          // A finished video deducted points → refresh just the wallet balance
          // (sidebar) via a global event. No full-page reload.
          if (next.completed_count !== lastCompletedRef.current || !['pending', 'running'].includes(next.status)) {
            lastCompletedRef.current = next.completed_count
            window.dispatchEvent(new Event('mb:wallet-refresh'))
          }
          if (!['pending', 'running'].includes(next.status) && id) {
            api.series.listProjects(id).then(setProjects).catch(() => {})
          }
        })
        .catch(() => {})
    }, 5000)
    return () => window.clearInterval(timer)
  }, [batchRun?.id, batchRun?.status, id])

  if (!id) return null
  if (!channel) {
    if (channelLoadFailed) {
      return (
        <div className="channel-load-error">
          <p>{t('channels.loadFailed')}</p>
          <button
            className="btn-primary"
            onClick={() => {
              try { sessionStorage.removeItem(`mb.reloaded.channel.${id}`) } catch {/* noop */}
              window.location.reload()
            }}
          >
            {t('channels.retryReload')}
          </button>
        </div>
      )
    }
    return <p className="hint">{t('channels.loadingChannel')}</p>
  }

  const recent = projects.slice(0, 12)
  const completed = projects.filter((p) => p.status === 'completed').length

  const toggleDlSel = (pid: string) => {
    setDlSel((prev) => {
      const next = new Set(prev)
      if (next.has(pid)) next.delete(pid)
      else next.add(pid)
      return next
    })
  }
  // 单条直接下载:点卡片上的下载图标,立即下这一条(不进多选/打包流程)。
  const downloadOne = (e: React.MouseEvent, pid: string) => {
    e.preventDefault()
    e.stopPropagation()
    const a = document.createElement('a')
    a.href = api.projects.downloadUrl(pid)
    a.rel = 'noopener'
    document.body.appendChild(a)
    a.click()
    a.remove()
  }
  const handleBatchDownload = async () => {
    const ids = Array.from(dlSel)
    if (ids.length === 0 || downloading) return
    setDownloading(true)
    try {
      await batchDownloadProjects(ids)
      setDlSel(new Set())
    } finally {
      setDownloading(false)
    }
  }
  const titleList = manualTitles.split('\n').map((line) => line.trim()).filter(Boolean)
  const batchIsRunning = batchRun ? ['pending', 'running'].includes(batchRun.status) : false

  const createBulkBatch = async (allowDuplicate = false) => {
    if (!id || titleList.length === 0 || batchBusy) return
    setBatchBusy(true)
    setBatchError(null)
    setBatchMessage(null)
    setDuplicateConfirm(false)
    try {
      const prompts = titleList.map((title) => {
        const typeName = bulkFormat === 'youtube_long' ? t('channels.longVideo') : t('channels.shortVideo')
        return [
          t('channels.batchPromptMake', { name: channel.name, type: typeName }),
          t('channels.batchPromptTopic', { title }),
          t('channels.batchPromptPositioning', { positioning: channel.description || memory?.positioning || t('channels.batchPromptKeepPositioning') }),
          t('channels.batchPromptRequirement'),
        ].join('\n')
      })
      const batch = await api.series.createBatch(id, {
        prompts,
        output_format: bulkFormat === 'youtube_long' ? 'youtube_landscape' : 'youtube_shorts',
        framing_style: bulkFormat === 'youtube_shorts' ? bulkFraming : 'fill',
        include_music: bulkMusic,
        tts_speed: bulkSpeed,
        subtitle_font_scale: bulkSubtitleScale,
        concurrency: bulkFormat === 'youtube_long' ? 2 : 4,
        daily_cost_cap_usd: channel.daily_cost_cap_usd || 200,
        tts_provider: batchTtsProvider,
        output_language: batchLanguage,
        subtitle_language: batchSubtitleLanguage,
        include_subtitles: bulkSubtitles,
        duration_seconds: bulkFormat === 'youtube_long' ? batchDurationToSeconds(batchDuration) : batchShortDurationToSeconds(batchShortDuration),
        allow_duplicate: allowDuplicate || undefined,
      })
      setBatchRun(batch)
      setBatchMessage(t('channels.batchQueued', { count: batch.requested_count, type: bulkFormat === 'youtube_long' ? t('channels.longVideo') : t('channels.shortVideo') }))
      api.series.listProjects(id).then(setProjects).catch(() => {})
      setManualTitles('')
    } catch (err) {
      // FastAPI 把 HTTPException 的内容包在 { detail: {...} } 里,先解包一层再判 error,
      const raw = (err as { response?: { data?: unknown }; message?: string }).response?.data
      const detail = (raw && typeof raw === 'object' && 'detail' in raw)
        ? (raw as { detail?: unknown }).detail
        : raw
      if (typeof detail === 'object' && detail && 'error' in detail) {
        const code = String((detail as { error?: unknown }).error || '')
        if (code === 'channel_duplicate_topic') {
          setBatchError(t('channels.errorDuplicateTopic'))
          setDuplicateConfirm(true)  // 允许用户确认后绕过重复闸继续
        } else if (code === 'channel_position_mismatch') {
          setBatchError(t('channels.errorPositionMismatch'))
        } else if (code === 'insufficient_points') {
          const bal = Number((detail as { balance?: unknown }).balance ?? 0)
          const req = Number((detail as { required?: unknown }).required ?? 0)
          setBatchError(t('channels.errorInsufficientPoints', { balance: bal, required: req }))
        } else if (code === 'queue_full') {
          // 排队上限(MB_USER_QUEUE_CAP):允许继续排但有上限,给友好提示不是裸错误码。
          const inflight = Number((detail as { inflight?: unknown }).inflight ?? 0)
          const cap = Number((detail as { cap?: unknown }).cap ?? 0)
          setBatchError(t('channels.errorQueueFull', { inflight, cap }))
        } else {
          setBatchError(t('channels.errorBatchGeneric', { code }))
        }
      } else {
        setBatchError((err as Error).message || t('channels.batchProductionFailed'))
      }
    } finally {
      setBatchBusy(false)
    }
  }

  const stopBulkBatch = async () => {
    if (!batchRun || batchBusy) return
    setBatchBusy(true)
    setBatchError(null)
    try {
      const stopped = await api.batch.stop(batchRun.id)
      setBatchRun(stopped)
      setBatchMessage(t('channels.batchStopped'))
      api.series.listProjects(id).then(setProjects).catch(() => {})
    } catch (err) {
      setBatchError((err as Error).message || t('channels.stopBatchFailed'))
    } finally {
      setBatchBusy(false)
    }
  }

  const persistBatchSettingsAsDefault = () => {
    if (!id) return
    const currentDefaults = readBatchSettingsDefaults(id)
    const nextDefaults: BatchSettingsDefaults = {
      ...currentDefaults,
      outputLanguage: undefined,
      ttsProvider: undefined,
      framingStyle: undefined,
    }
    if (bulkFormat === 'youtube_long') {
      nextDefaults.longOutputLanguage = batchLanguage
      nextDefaults.longTtsProvider = batchTtsProvider
      nextDefaults.longDuration = batchDuration
    } else {
      nextDefaults.shortOutputLanguage = batchLanguage
      nextDefaults.shortTtsProvider = batchTtsProvider
      nextDefaults.shortDuration = batchShortDuration
      nextDefaults.shortFramingStyle = bulkFraming
    }
    // 背景音乐开关 + 语速挡位长短共用,任意格式保存都记住。
    nextDefaults.includeMusic = bulkMusic
    nextDefaults.ttsSpeed = bulkSpeed
    nextDefaults.subtitleFontScale = bulkSubtitleScale
    // ⚠️ 字幕语言 + 配音档以前**根本没写进来** —— 客户改完关掉就没了。
    nextDefaults.subtitleLanguage = batchSubtitleLanguage
    nextDefaults.includeSubtitles = bulkSubtitles
    nextDefaults.voiceTier = batchVoiceTier
    if (!saveBatchSettingsDefaults(id, nextDefaults)) {
      // 存储写入失败 → 明确告诉用户没存上,别让他以为存好了。
      setBatchDefaultMessage(t('channels.batchDefaultsSaveFailed'))
      return false
    }
    setSavedBatchDefaults(nextDefaults)
    setBatchDefaultMessage(t('channels.batchDefaultsSaved'))
    return true
  }

  return (
    <>
      <header className="page-header">
        <div className="crumb">
          <Link to="/channels" className="seg">{t('channels.channels')}</Link>
          <span className="sep">/</span>
          <span className="here">{channelDisplay(t, channel).name}</span>
        </div>
        <button type="button" className="page-back-link" onClick={() => nav('/channels')}>
          {t('channels.backToChannelsHome')}
        </button>
      </header>
      <div className="workspace-page channel-page">
        <section className="workspace-hero workspace-purple channel-workbench-hero">
          <div>
            <p className="workspace-eyebrow">{t('channels.channelWorkbench')}</p>
            <h1>{channelDisplay(t, channel).name}</h1>
            <p>{channelDisplay(t, channel).desc}</p>
          </div>
          <div className="channel-stats">
            <span><strong>{projects.length}</strong>{t('channels.statProjects')}</span>
            <span><strong>{completed}</strong>{t('channels.statCompleted')}</span>
            <span><strong>{formatLabel(t, channel.output_format)}</strong>{t('channels.statDefaultFormat')}</span>
          </div>
        </section>

        {/* 频道级子导航(仅手机端 sticky 吸顶,桌面隐藏)。频道智能=当前页高亮。 */}
        <ChannelSubTabs channelId={id} active="intel" />

        <section className="channel-actions">
          <Link to={`/channels/${id}/youtube`} className="channel-action-card long">
            <span>L</span>
            <strong>{t('channels.createLongVideo')}</strong>
            <p>{t('channels.createLongVideoHint')}</p>
          </Link>
          <Link to={`/channels/${id}/shorts`} className="channel-action-card short">
            <span>S</span>
            <strong>{t('channels.createShortVideo')}</strong>
            <p>{t('channels.createShortVideoHint')}</p>
          </Link>
          <Link to={`/channels/${id}/projects`} className="channel-action-card">
            <span>J</span>
            <strong>{t('channels.channelProjects')}</strong>
            <p>{t('channels.channelProjectsHint')}</p>
          </Link>
        </section>

        <section className="channel-section channel-intel-section">
          <div className="rail-title-row">
            <div>
              <h2>{t('channels.channelIntelligence')}</h2>
              <p className="hint">{t('channels.channelIntelligenceHint')}</p>
            </div>
          </div>
          <div className="channel-smart-console">
            <article className="channel-recommendation-panel">
              <div className="channel-panel-heading">
                <div>
                  <span className="channel-badge">{t('channels.manualTitlesBadge')}</span>
                  <h3>{t('channels.manualTitlesTitle')}</h3>
                </div>
                <span className="channel-selection-count">{t('channels.selectedCount', { count: titleList.length })}</span>
              </div>
              <textarea
                className="account-input"
                style={{ width: '100%', minHeight: 180, resize: 'vertical', fontFamily: 'inherit' }}
                value={manualTitles}
                onChange={(e) => setManualTitles(e.target.value)}
                placeholder={t('channels.manualTitlesPlaceholder')}
              />
              <p className="hint">{t('channels.manualTitlesHint')}</p>
            </article>

            <aside className="channel-bulk-panel">
              <div className="channel-panel-heading">
                <div>
                  <span className="channel-badge">{t('channels.oneClickBatch')}</span>
                  <h3>{t('channels.queueExecution')}</h3>
                </div>
                {batchRun ? <span className={`channel-batch-status status-${batchRun.status}`}>{batchRun.status}</span> : null}
              </div>

              <div className="channel-format-toggle" role="group" aria-label={t('channels.selectBatchFormatAria')}>
                {/* 不再因"有批次在跑"而锁死 —— 用户要能一边出片一边继续排队下一批。 */}
                <button
                  type="button"
                  className={bulkFormat === 'youtube_long' ? 'active long' : ''}
                  onClick={() => setBulkFormat('youtube_long')}
                >
                  {t('channels.longVideo')}
                </button>
                <button
                  type="button"
                  className={bulkFormat === 'youtube_shorts' ? 'active short' : ''}
                  onClick={() => setBulkFormat('youtube_shorts')}
                >
                  {t('channels.shortVideo')}
                </button>
              </div>

              {/* 出片设置常驻可点(就算有批次在跑)—— 调的是"下一批"的设置,不影响在跑的批次。 */}
              {(
                <button
                  type="button"
                  className="channel-batch-settings-btn"
                  onClick={() => {
                    setBatchDefaultMessage(null)
                    setShowBatchSettings(true)
                  }}
                >
                  <span className="channel-batch-settings-gear" aria-hidden="true">⚙</span>
                  <span className="channel-batch-settings-text">
                    <strong>{t('channels.batchSettings')}</strong>
                    <small>
                      {(batchLanguage === 'en' ? t('channels.langEnglish') : t('channels.langChinese'))}
                      {' · '}
                      {selectedBatchVoice ? voiceDisplayName(t, selectedBatchVoice) : batchTtsProvider}
                      {bulkFormat === 'youtube_long' ? ` · ${batchDurationLabel(batchDuration, uiLang)}` : ''}
                      {bulkFormat === 'youtube_shorts' ? ` · ${batchDurationLabel(batchShortDuration, uiLang)} · ${bulkFraming === 'blur' ? t('channels.framingBlur') : t('channels.framingFill')}` : ''}
                    </small>
                  </span>
                  <span className="channel-batch-settings-arrow" aria-hidden="true">›</span>
                </button>
              )}

              <div className="channel-script-preview">
                <label>{t('channels.scriptPreview')}</label>
                <div>
                  <strong>{titleList[0] || t('channels.selectTopicToPreview')}</strong>
                  <p>
                    {titleList[0]
                      ? t('channels.previewWithFocus', { name: channelDisplay(t, channel).name, type: bulkFormat === 'youtube_long' ? t('channels.about10MinLong') : t('channels.shortVideo'), focus: titleList[0] })
                      : t('channels.previewEmptyHint')}
                  </p>
                  {titleList.length > 0 ? (
                    <ol>
                      {titleList.slice(0, 6).map((title) => <li key={title}>{title}</li>)}
                    </ol>
                  ) : null}
                </div>
              </div>

              {batchRun ? (
                <div className="channel-batch-progress">
                  <span>{t('channels.progress')}</span>
                  <strong>{batchRun.completed_count}/{batchRun.requested_count}</strong>
                  <small>{t('channels.failedCount', { count: batchRun.failed_count })}</small>
                </div>
              ) : null}

              {batchMessage ? <p className="channel-batch-message">{batchMessage}</p> : null}
              {batchError ? <p className="channel-batch-error">{batchError}</p> : null}
              {duplicateConfirm ? (
                <button
                  type="button"
                  className="btn-secondary channel-batch-confirm"
                  onClick={() => createBulkBatch(true)}
                  disabled={batchBusy}
                >
                  {t('channels.confirmDuplicateProceed')}
                </button>
              ) : null}

              <div className="channel-bulk-actions">
                <button
                  type="button"
                  className="btn-primary channel-bulk-primary"
                  onClick={() => createBulkBatch(false)}
                  disabled={batchBusy || titleList.length === 0}
                >
                  {batchBusy ? t('channels.queuing') : t('channels.oneClickBatchWithCount', { count: titleList.length || '' })}
                </button>
                <button
                  type="button"
                  className="btn-danger channel-bulk-stop"
                  onClick={stopBulkBatch}
                  disabled={batchBusy || !batchIsRunning}
                >
                  STOP
                </button>
              </div>
            </aside>
          </div>
        </section>

        <section className="channel-section">
          <div className="rail-title-row">
            <h2>{t('channels.recentProjects')}</h2>
            <Link to={`/channels/${id}/projects`}>{t('channels.viewAll')}</Link>
          </div>
          {recent.length === 0 ? (
            <div className="channel-empty compact">
              <p>{t('channels.noProjectsYet')}</p>
            </div>
          ) : (
            <div className="channel-project-strip">
              {recent.map((project) => {
                const canSelect = project.status === 'completed' && !!project.output_path
                const picked = dlSel.has(project.id)
                return (
                <Link key={project.id} to={`/project/${project.id}`} className={`channel-project-card${picked ? ' dl-picked' : ''}`}>
                  {canSelect && (
                    <button
                      type="button"
                      className={`card-dl-check${picked ? ' checked' : ''}`}
                      onClick={(e) => { e.preventDefault(); e.stopPropagation(); toggleDlSel(project.id) }}
                      aria-label={t('channels.selectForDownload')}
                    >
                      {picked ? '✓' : ''}
                    </button>
                  )}
                  {canSelect && (
                    <button
                      type="button"
                      className="card-dl-one"
                      onClick={(e) => downloadOne(e, project.id)}
                      aria-label={t('channels.downloadThis')}
                      title={t('channels.downloadThis')}
                    >
                      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M12 3v12" /><path d="m7 12 5 5 5-5" /><path d="M5 21h14" />
                      </svg>
                    </button>
                  )}
                  {project.output_path ? (
                    <video src={api.projects.outputUrl(project.id)} preload="metadata" muted />
                  ) : (
                    <div className="channel-project-placeholder">{formatLabel(t, project.output_format)}</div>
                  )}
                  <strong>{projectDisplayName(project, { full: true })}</strong>
                  <span>
                    {project.status} · {formatLabel(t, project.output_format)}
                    {project.created_at ? ` · ${new Date(project.created_at).toLocaleDateString()}` : ''}
                  </span>
                  {project.downloaded_at && (
                    <span className="card-downloaded-badge">✓ {t('projectsPage.downloaded')}</span>
                  )}
                </Link>
              )})}
            </div>
          )}
        </section>

        {dlSel.size > 0 && (
          <div className="batch-download-bar">
            <span className="batch-download-count">{t('channels.selectedForDownload', { count: dlSel.size })}</span>
            <div className="batch-download-actions">
              <button type="button" className="btn-row-action" onClick={() => setDlSel(new Set())} disabled={downloading}>
                {t('channels.cancel')}
              </button>
              <button type="button" className="btn-row-action btn-row-action-primary" onClick={handleBatchDownload} disabled={downloading}>
                {downloading ? t('channels.downloading') : t('channels.downloadSelected', { count: dlSel.size })}
              </button>
            </div>
          </div>
        )}

        {showBatchSettings && channel && (
          <div className="modal-backdrop channel-batch-backdrop" onClick={() => setShowBatchSettings(false)}>
            <div
              className="modal-content channel-batch-modal"
              role="dialog"
              aria-modal="true"
              aria-labelledby="channel-batch-title"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="modal-body">
                <span className="channel-badge">{t('channels.oneClickBatch')}</span>
                <h2 id="channel-batch-title">{t('channels.batchSettingsTitle')}</h2>
                <p className="channel-batch-modal-hint">{t('channels.batchSettingsHint')}</p>

                <div className="channel-batch-config">
                  <label className="config-row">
                    <span>{t('channels.outputLanguage')}</span>
                    <select
                      value={batchLanguage}
                      onChange={(e) => {
                        const lang = e.target.value as 'zh' | 'en'
                        if (lang !== 'en') setBatchVoiceTier('normal')  // 高级仅英文
                        setBatchLanguage(lang)
                        // 忘了改字幕语言等于整批报废 —— 这里代价最大。
                        const nextSub = subtitleLanguageForOutput(lang, batchSubtitleLanguage)
                        if (nextSub) setBatchSubtitleLanguage(nextSub as BatchSubtitleLanguage)
                      }}
                    >
                      <option value="zh">{t('channels.langChinese')}</option>
                      <option value="en">{t('channels.langEnglish')}</option>
                    </select>
                  </label>

                  <label className="config-row">
                    <span>{t('workspace.subtitleSwitch')}</span>
                    <button
                      type="button"
                      role="switch"
                      aria-checked={bulkSubtitles}
                      className={`mb-switch${bulkSubtitles ? ' is-on' : ''}`}
                      onClick={() => setBulkSubtitles((v) => !v)}
                      title={bulkSubtitles ? t('workspace.subtitlesOn') : t('workspace.subtitlesOff')}
                    >
                      <span className="mb-switch-knob" />
                    </button>
                  </label>

                  {bulkSubtitles && (
                    <>
                  <label className="config-row">
                    <span>{t('workspace.subtitleLanguage')}</span>
                    <select
                      value={batchSubtitleLanguage}
                      onChange={(e) => setBatchSubtitleLanguage(e.target.value as typeof batchSubtitleLanguage)}
                    >
                      {([['zh-Hans', 'workspace.subHans'], ['zh-Hant', 'workspace.subHant'], ['en', 'workspace.subEn'], ['zh-Hans+en', 'workspace.subHansEn'], ['zh-Hant+en', 'workspace.subHantEn']] as const).map(([v, k]) => (
                        <option key={v} value={v}>{t(k)}</option>
                      ))}
                    </select>
                  </label>

                  <div className="config-row config-row-subsize">
                    <SubtitleSizeSlider
                      value={bulkSubtitleScale}
                      onChange={setBulkSubtitleScale}
                      subtitleLanguage={batchSubtitleLanguage}
                      portrait={bulkFormat === 'youtube_shorts'}
                      labels={{
                        title: t('workspace.subtitleFontSize'),
                        hint: t('workspace.subSizeHint'),
                        small: t('workspace.subSizeSmall'),
                        standard: t('workspace.subSizeStandard'),
                        large: t('workspace.subSizeLarge'),
                        xlarge: t('workspace.subSizeXlarge'),
                      }}
                    />
                  </div>
                    </>
                  )}

                  {/* 配音区(仿工作台):标题 → 语速挡位 → 普通/高级英语切换 → 音色列表铺满整宽。
                      原来用 .config-row 两列栅格,音色列表被挤进 88px 标签列 → 竖成一条,故改块级布局。 */}
                  <div className="config-fold batch-voice-config">
                    <div className="batch-voice-head">
                      <span>{t('channels.outputVoice')}</span>
                    </div>
                    {/* 语速挡位(长短共用)+ 按挡试听:点标签选挡,点 ▶ 用当前音色听该速度。 */}
                    <div className="voice-speed-row">
                      <span>{t('workspace.speechSpeed')}</span>
                      <div className="voice-speed-options" role="group" aria-label={t('workspace.speechSpeed')}>
                        {BATCH_SPEEDS.map((it) => {
                          const active = Math.abs(bulkSpeed - it.value) < 0.001
                          return (
                            <div key={it.value} className={`voice-speed-opt${active ? ' active' : ''}`}>
                              <button type="button" className="voice-speed-pick" onClick={() => setBulkSpeed(it.value)}>
                                {t(it.key)}
                              </button>
                              <button
                                type="button"
                                className="voice-speed-play"
                                title={t('workspace.previewThisSpeed')}
                                aria-label={t('workspace.previewThisSpeed')}
                                onClick={() => previewBatchSpeed(it.value)}
                              >
                                {playingSpeed === it.value ? '⏸' : '▶'}
                              </button>
                            </div>
                          )
                        })}
                      </div>
                    </div>
                    {batchLanguage === 'en' && (
                      <div className="voice-tier-toggle" role="group" aria-label={t('workspace.voiceTier')}>
                        <button
                          type="button"
                          className={`voice-tier-btn${batchVoiceTier === 'normal' ? ' active' : ''}`}
                          onClick={() => setBatchVoiceTier('normal')}
                        >
                          {t('workspace.voiceTierNormal')}
                        </button>
                        <button
                          type="button"
                          className={`voice-tier-btn${batchVoiceTier === 'advanced' ? ' active' : ''}`}
                          onClick={() => setBatchVoiceTier('advanced')}
                        >
                          {t('workspace.voiceTierAdvanced')}
                        </button>
                      </div>
                    )}
                    <VoicePreviewList
                      voices={filteredBatchVoices}
                      value={batchTtsProvider}
                      onChange={setBatchTtsProvider}
                      speed={bulkSpeed}
                      lang={batchLanguage}
                    />
                  </div>

                  {bulkFormat === 'youtube_long' ? (
                    <label className="config-row">
                      <span>{t('channels.outputDuration')}</span>
                      <select value={batchDuration} onChange={(e) => setBatchDuration(e.target.value)}>
                        {BATCH_LONG_DURATIONS.map((item) => (
                          <option key={item} value={item}>{batchDurationLabel(item, uiLang)}</option>
                        ))}
                      </select>
                    </label>
                  ) : null}

                  {bulkFormat === 'youtube_shorts' ? (
                    <>
                      <label className="config-row">
                        <span>{t('channels.outputDuration')}</span>
                        <select value={batchShortDuration} onChange={(e) => setBatchShortDuration(e.target.value)}>
                          {BATCH_SHORT_DURATIONS.map((item) => (
                            <option key={item} value={item}>{batchDurationLabel(item, uiLang)}</option>
                          ))}
                        </select>
                      </label>

                      <label className="config-row">
                        <span>{t('channels.videoFraming')}</span>
                        <select value={bulkFraming} onChange={(e) => setBulkFraming(e.target.value as 'fill' | 'blur')}>
                          <option value="fill">{t('channels.framingFill')}</option>
                          <option value="blur">{t('channels.framingBlur')}</option>
                        </select>
                      </label>
                    </>
                  ) : null}

                  {/* 背景音乐开关(长短共用)。默认关 —— 躲 YouTube 版权投诉,客户想要才打开。 */}
                  <label className="config-row">
                    <span>{t('channels.backgroundMusic')}</span>
                    <button
                      type="button"
                      role="switch"
                      aria-checked={bulkMusic}
                      className={`mb-switch${bulkMusic ? ' is-on' : ''}`}
                      onClick={() => setBulkMusic((v) => !v)}
                      title={bulkMusic ? t('channels.musicOn') : t('channels.musicOff')}
                    >
                      <span className="mb-switch-knob" />
                    </button>
                  </label>
                </div>

                <div className="modal-actions">
                  <button type="button" className="btn-secondary" onClick={() => setShowBatchSettings(false)}>
                    {t('channels.saveDefaultCancel')}
                  </button>
                  <button
                    type="button"
                    className="btn-primary"
                    onClick={() => {
                      // 主按钮"保存为默认"不再直接静默保存 —— 弹二次确认框,客户点确认才存。
                      setBatchDefaultMessage(null)
                      setShowBatchDefaultConfirm(true)
                    }}
                  >
                    {t('channels.saveBatchDefaults')}
                  </button>
                </div>
                {batchDefaultMessage ? <p className="channel-batch-default-message">{batchDefaultMessage}</p> : null}

                {showBatchDefaultConfirm && (
                  <div
                    className="modal-backdrop"
                    style={{ zIndex: 1000 }}
                    onClick={() => setShowBatchDefaultConfirm(false)}
                  >
                    <div
                      className="modal-content"
                      role="dialog"
                      aria-modal="true"
                      aria-labelledby="batch-default-confirm-title"
                      style={{ maxWidth: 440 }}
                      onClick={(e) => e.stopPropagation()}
                    >
                      <div className="modal-body">
                        <h3 id="batch-default-confirm-title">{t('channels.saveDefaultConfirmTitle')}</h3>
                        <p className="channel-batch-modal-hint">{t('channels.saveDefaultConfirmBody')}</p>
                        <div className="modal-actions">
                          <button
                            type="button"
                            className="btn-secondary"
                            onClick={() => setShowBatchDefaultConfirm(false)}
                          >
                            {t('channels.saveDefaultCancel')}
                          </button>
                          <button
                            type="button"
                            className="btn-primary"
                            onClick={() => {
                              // 确认后真正保存;成功则连设置弹窗一起关闭,存储失败则留在设置弹窗显示错误。
                              const ok = persistBatchSettingsAsDefault()
                              setShowBatchDefaultConfirm(false)
                              if (ok) setShowBatchSettings(false)
                            }}
                          >
                            {t('channels.saveDefaultConfirmOk')}
                          </button>
                        </div>
                      </div>
                    </div>
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </>
  )
}
