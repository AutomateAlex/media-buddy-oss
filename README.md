# Media Buddy (source-available, noncommercial)

**Give it a channel and a few titles → get finished short or long videos.** Script by an AI director, stock footage matched shot by shot, voice-over, subtitles, music — rendered locally with ffmpeg, using **your own API keys**.

> 中文说明在下方 → [中文](#中文)

This is the source-available edition of [Media Buddy](https://media-buddy.com), released under the **PolyForm Noncommercial 1.0.0** license: free for personal, educational, research and nonprofit use; **any commercial use needs a separate license** (hello@media-buddy.com).

> **Want better footage, endless topics, or zero setup?** The same engine runs hosted at **[media-buddy.com](https://media-buddy.com/?utm_source=github&utm_medium=readme&utm_campaign=oss)** with a licensed HD stock library, an AI topic engine, a YouTube trend radar, footage import + AI editing, and an MCP server for Claude Code / Codex. No keys to manage, pay per video. → [What you get on the hosted version](#when-to-move-to-the-hosted-version)

| | This repo (noncommercial) | Hosted version ([media-buddy.com](https://media-buddy.com/?utm_source=github&utm_medium=readme&utm_campaign=oss)) |
|---|---|---|
| Setup | Paste your own keys (Settings → API Keys) | Zero setup, sign in and render |
| Stock footage | Free libraries (Pexels, Pixabay, Coverr) — coverage varies by topic | Licensed HD stock library, director-picked per shot |
| Voices | Free Edge voices; Qwen / ElevenLabs with your own keys | Premium voices included |
| Topics | You type the titles | AI topic engine + YouTube trend radar |
| Your own footage | — | Import + AI editing |
| Agents | — | Media Buddy MCP (stock search & shot review from Claude Code / Codex) |
| Cost | Pay your providers directly | Pay per video, no key management |
| Support & updates | Community, this repo | Product support, continuous updates |

## What is in the box

- **Channels** — create a channel (AI helps design its positioning), keep its voice / language / format defaults, and queue a batch: one title per line → one video each.
- **Short video studio** and **Long video studio** — chat with the AI director, approve the script, render. Long form = chaptered 5–20 minute videos.
- **Projects** — every render with its stages, output preview, download, favourites.
- **Settings → API Keys** — paste keys once; they take effect immediately.

## Quick start

Requirements: **Python 3.11+**, **Node 20+** (only to build the UI once), **ffmpeg** on your `PATH` (Windows: `scripts/fetch-ffmpeg.ps1`).

```bash
git clone <this repo> media-buddy && cd media-buddy

# 1. backend
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"

# 2. build the web UI once (served by the backend afterwards)
cd src/frontend && npm install && npm run build && cd ../..

# 3. run
cd src && uvicorn backend.main:app --port 8000
```

Open **http://127.0.0.1:8000**, go to **Settings → API Keys**, paste at least `OPENROUTER_API_KEY` and `PEXELS_API_KEY`, and start a channel. Add `MEDIA_BUDDY_QWEN_API_KEY` for the Qwen Mandarin voices. (Or copy `.env.example` to `.env` in the repo root before starting.)

### Keys

| Key | Required | What for |
|---|---|---|
| `OPENROUTER_API_KEY` | **yes** | every LLM call (script, shot planning, verifier, vision, research) — Qwen, Claude, GPT, Gemini all through one key |
| `PEXELS_API_KEY` | **yes** | free stock footage |
| `MEDIA_BUDDY_QWEN_API_KEY` | recommended | Qwen voice-over only (24 Mandarin voices, the default narrator); without it the free Microsoft Edge voices are used |
| `PIXABAY_API_KEY`, `COVERR_API_KEY`, `FREESOUND_API_KEY` | optional | more free stock / music |
| `MEDIA_BUDDY_ELEVENLABS_API_KEY` | optional | premium English voices (also set `MEDIA_BUDDY_ENABLE_ELEVENLABS=1`) |

Default models: text `anthropic/claude-haiku-4-5`, vision `google/gemini-2.5-flash`, both via OpenRouter. Override with `MEDIA_BUDDY_DEFAULT_MODEL`, `OMNI_MODEL`, or per purpose `MEDIA_BUDDY_<PURPOSE>_MODEL` (see `src/backend/lib/llm_client.py`).

### Using the HTTP API directly

The backend trusts requests from its own origin. From another client, pass the per-install token stored at `~/.media-buddy-oss/.app_token`:

```bash
TOKEN=$(cat ~/.media-buddy-oss/.app_token)
curl -H "X-MediaBuddy-Token: $TOKEN" http://127.0.0.1:8000/api/system/status
```

Interactive docs: `http://127.0.0.1:8000/docs`.

## Development

```bash
pytest                                   # offline
cd src/frontend && npm run typecheck && npm run build
cd src/frontend && npm run dev           # hot-reload UI on :5173 (backend on :8000)
```

Tests never call the network (`MEDIA_BUDDY_ALLOW_OFFLINE=1` is set by `tests/conftest.py`).

## When to move to the hosted version

You can run this build forever for noncommercial use. People usually move to [media-buddy.com](https://media-buddy.com/?utm_source=github&utm_medium=readme&utm_campaign=oss) when one of these bites:

1. **Footage.** Free libraries run thin on specific subjects (a named animal, a historic place, a niche craft). The hosted engine picks from a licensed HD library shot by shot, so fewer chunks fall back to generic B-roll.
2. **Topics.** Here you type every title. Hosted adds an AI topic engine per channel and a YouTube trend radar, so a channel can publish daily without a writer's room.
3. **Your own material.** Import your footage and let the AI editor cut it; not available in this build.
4. **Agents.** The Media Buddy MCP server lets Claude Code / Codex search stock, build a review pack and lock picks for a shot list, then render through Media Buddy.
5. **Commercial use.** This repo is noncommercial. Any business use goes through the hosted product or a commercial license (hello@media-buddy.com).

Channels, scripts and voice settings work the same way on both, so what you learn here carries over.

## Not included (hosted version only)

AI topic engine, YouTube trend radar, footage import / AI editing, cloud accounts and billing. Hooks for them were removed, not stubbed: this build runs single-user on SQLite with an in-process render worker.

## License

**PolyForm Noncommercial 1.0.0** — see [LICENSE](./LICENSE) and [NOTICE](./NOTICE).

- ✅ Personal projects, learning, research, nonprofits, government: use, modify, share freely.
- ❌ Any commercial use (inside a company, as a service, in a paid product): not permitted without a commercial license — write to hello@media-buddy.com.

Third-party components keep their own licenses: [THIRD_PARTY_LICENSES.md](./THIRD_PARTY_LICENSES.md). "Media Buddy" is a trademark; the hosted service is separate.

---

## 中文

**给一个频道、几行标题 → 直接出短视频或长视频。** AI 编导写剧本，逐句匹配素材，配音、字幕、配乐，本机 ffmpeg 合成，用**你自己的 API key**。

这是 [Media Buddy](https://media-buddy.com) 的源码公开版（**PolyForm Noncommercial 1.0.0**：个人、学习、科研、非营利可自由使用和修改；**任何商业用途需另行授权**，联系 hello@media-buddy.com）。

> **想要更好的素材、不用自己想选题、也不想配 key？** 同一套引擎的托管版在 **[media-buddy.com](https://media-buddy.com/?utm_source=github&utm_medium=readme&utm_campaign=oss)**：自带授权高清素材库、AI 智能选题、YouTube 热点雷达、素材导入 + AI 剪辑，还有给 Claude Code / Codex 用的 MCP。按条付费，不用管 key。→ [什么时候该换托管版](#什么时候该换托管版)

### 里面有什么

- **频道** —— 建频道（AI 帮你设计定位），记住配音 / 语言 / 格式默认值，一行一个标题批量出片。
- **短视频工作室** 和 **长视频工作室** —— 跟 AI 编导聊，确认剧本，出片。长视频 = 5–20 分钟带章节。
- **项目管理** —— 每条片的阶段、预览、下载、收藏。
- **设置 → API 密钥** —— 粘贴一次，立即生效。

### 三步跑起来

前置：Python 3.11+、Node 20+（只用来 build 一次界面）、ffmpeg 在 `PATH` 里。

1. `pip install -e ".[dev]"`
2. `cd src/frontend && npm install && npm run build && cd ../..`
3. `cd src && uvicorn backend.main:app --port 8000`，浏览器打开 `http://127.0.0.1:8000`

进「设置 → API 密钥」填 `OPENROUTER_API_KEY` 和 `PEXELS_API_KEY`，就能出片（所有大模型调用都走 OpenRouter 一把 key）。想要千问配音再填 `MEDIA_BUDDY_QWEN_API_KEY`，不填用免费的 Edge 音色。

### 本仓库 vs 托管版

| | 本仓库（非商用） | 托管版 [media-buddy.com](https://media-buddy.com/?utm_source=github&utm_medium=readme&utm_campaign=oss) |
|---|---|---|
| 配置 | 自己填 key | 零配置，登录即出片 |
| 素材 | 免费素材库（Pexels / Pixabay / Coverr），冷门题材经常搜不到 | 授权高清素材库，导演逐镜头挑 |
| 配音 | 免费 Edge 音色；千问 / ElevenLabs 要自己的 key | 高级音色内置 |
| 选题 | 标题自己写 | AI 智能选题 + YouTube 热点雷达 |
| 自己的素材 | 无 | 素材导入 + AI 剪辑 |
| AI 助手 | 无 | Media Buddy MCP（在 Claude Code / Codex 里搜素材、审镜头） |
| 费用 | 直接付给各家供应商 | 按条付费，不用管 key |
| 支持与更新 | 社区、本仓库 | 产品支持、持续更新 |

### 什么时候该换托管版

非商用可以一直用这个版本。大家一般在这几种情况下换到 [media-buddy.com](https://media-buddy.com/?utm_source=github&utm_medium=readme&utm_campaign=oss)：

1. **素材不够用。** 免费库在具体题材上很薄（某种动物、某个古迹、某门手艺），出片会退到泛泛的空镜。托管版从授权高清库里逐镜头挑，兜底少得多。
2. **选题跟不上。** 这里每个标题都要自己写；托管版每个频道都有 AI 选题和 YouTube 热点雷达，能做到日更。
3. **想用自己的素材。** 导入自己的片子让 AI 剪，这个版本没有。
4. **想让 AI 助手干活。** Media Buddy MCP 让 Claude Code / Codex 直接搜素材、出复审包、锁定镜头，再交给 Media Buddy 出片。
5. **要商用。** 本仓库不可商用；商业使用走托管版或另签授权（hello@media-buddy.com）。

频道、剧本、配音设置两边逻辑一样，在这里学会的用法过去照样能用。
