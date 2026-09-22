# Media Buddy (source-available, noncommercial)

**Give it a channel and a few titles → get finished short or long videos.** Script by an AI director, stock footage matched shot by shot, voice-over, subtitles, music — rendered locally with ffmpeg, using **your own API keys**.

> 中文说明在下方 → [中文](#中文)

This is the source-available edition of [Media Buddy](https://media-buddy.com), released under the **PolyForm Noncommercial 1.0.0** license: free for personal, educational, research and nonprofit use; **any commercial use needs a separate license** (hello@media-buddy.com).

| | This repo (noncommercial) | Hosted version ([media-buddy.com](https://media-buddy.com)) |
|---|---|---|
| Setup | Paste your own keys (Settings → API Keys) | Zero setup, sign in and render |
| Stock footage | Pexels, Pixabay, Coverr | HD stock library included |
| AI topic engine / trend radar | — (you type the titles) | Endless topic suggestions + YouTube trend radar |
| Footage import & AI editing | — | Included |
| Cost | Pay your providers directly | Credits |

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

| | 本仓库（非商用） | 托管版 media-buddy.com |
|---|---|---|
| 配置 | 自己填 key | 零配置 |
| 素材 | Pexels / Pixabay / Coverr | 自带高清素材库 |
| 智能选题 / 热点雷达 | 无（标题自己写） | 有 |
| 素材导入 / 智能剪辑 | 无 | 有 |
