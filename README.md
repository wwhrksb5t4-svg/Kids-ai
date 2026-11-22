# Kids Haven — Telegram + YouTube

A bilingual Arabic/English children's story-video MVP operated from a private Telegram bot, with Google OAuth to upload reviewed videos and retrieve YouTube Analytics.

## What the bot does

- Creates one Arabic and one English original episode per day at configured UTC times (defaults 12:00 and 14:00). Each script targets 300-360 spoken words; actual duration depends on narration speed. If the story is outside that range, the model is asked to revise it, up to three attempts.
- Generates scripts with FastLLM, creates narrated vertical MP4 videos, and sends each to the configured Telegram admin for review. Optionally, Cloudflare Workers AI generates scene illustrations with a slow camera zoom. With `FASTLLM_MODEL=auto`, it prefers an available Grok text model.
- The existing `/new` and `/today` flows upload to YouTube **only after admin approval**. The separate automatic nursery-rhyme flow below publishes when explicitly started via `/create` or enabled with `/auto_on`.
- `/new` or the `/start` button accepts a user idea and offers story, educational, and adventure scripts; short or 2-3 minute duration; Arabic or English; and the configured primary or alternate voice. The bot sends a script and scene-outline preview first. Only after approving production does it render and send the video for a separate YouTube publish approval.
- `/settings` switches among the configured content, image, video and song providers in Telegram; the dashboard offers the same persistent settings. Values saved there override environment defaults. Credentials are validated but never shown.
- `/today` manually generates scheduled episodes; `/status` lists recent episodes; `/queue` shows persisted production jobs; `/pause ID`, `/resume ID` and `/stop ID` control approved idea-video jobs; `/cancel` cancels an unfinished `/new` setup; `/cost ID USD` records actual provider charges manually; `/stats` queries YouTube Analytics.
- Telegram review uploads larger than 49 MB are compressed into a temporary copy; the original video is retained for YouTube. Use `/review EPISODE_ID` to retry sending an existing video awaiting review.
- `/status` includes today's generation state (even before a script is saved), render progress and the latest error. `/today` runs without blocking other Telegram commands, and retries failed renders and episodes stuck in `rendering` for over 10 minutes, reusing the saved script.
- Scene artwork and narration are prepared concurrently, with one scene prepared ahead while FFmpeg renders the current scene. Rendering stays sequential to limit CPU use.
- Set `DAILY_LANGUAGES=ar` for one Arabic episode per day, or `ar,en` for both. `/today` uses the same setting.
- Stores jobs, generated files, and OAuth token in SQLite/files under `DATA_DIR`.

## Important limitations

By default the renderer uses text cards. Cloudflare mode generates still images with a slow camera move; actual character animation requires a configured image-to-video provider. fal.ai and Hugging Face may fail, queue, or incur cost; the experimental Blender renderer only supports preset song loops, not arbitrary story scenes. Edge TTS is used for narration; music generation is song-mode only, and scene sound-effect descriptions are not yet mixed as audio effects. The short and long duration options target narration word counts, not guaranteed final runtime. The current story renderer exports vertical 720p; it does not upscale source art to 1080p. YouTube thumbnails are generated from the finished video frame and title. Job state survives restarts on the Railway Volume and an interrupted render is requeued; resume after a restart may repeat an external API call already in flight when the service stopped. Provider costs are not available through a common billing API; `/cost` and the dashboard store amounts entered from provider invoices, not automatic estimates. Review every video before approval. Use one Railway replica for Telegram polling, the scheduler, and the production worker.

YouTube OAuth needs Google Cloud setup and may require OAuth consent-screen configuration/testing. The Google account must own or manage the target channel. Analytics data availability can lag. Before publishing, the admin must review every video; children's content requires careful manual checks. This software does not guarantee YouTube policy compliance or channel growth.

## Railway setup

Deploy this repository as a Railway service. Configure these Variables (never commit actual secrets):

```text
FASTLLM_API_KEY=your_active_fastllm_key
FASTLLM_BASE_URL=https://fastllm.top/v1
FASTLLM_MODEL=auto
TELEGRAM_BOT_TOKEN=token_from_BotFather
ADMIN_TELEGRAM_ID=your_numeric_telegram_user_id
APP_API_KEY=long_random_secret_for_web_admin
PUBLIC_BASE_URL=https://your-railway-domain
GOOGLE_CLIENT_ID=your_google_oauth_client_id
GOOGLE_CLIENT_SECRET=your_google_oauth_client_secret
DATA_DIR=/app/data
DAILY_AR_TIME=12:00
DAILY_EN_TIME=14:00
# Optional: leave unset to rotate through 12 built-in themes
# DAILY_TOPIC=
```

## Automatic nursery-rhyme pipeline

This is a separate, opt-in workflow; existing `/new`, `/today`, OAuth, dashboard, and review flows remain available. `/create TOPIC` queues an immediate job **that publishes automatically**. `/auto_on HOURS` enables periodic automatic publication; `/auto_off` stops future scheduled jobs and cancels queued automatic jobs. An upload already in flight may finish. The bot must be an administrator of the Telegram channel.

1. Copy `.env.example` to `.env` for local use, and set `DATA_DIR=./data`. Set `BOT_TOKEN` (or the existing `TELEGRAM_BOT_TOKEN`), `ADMIN_IDS` as comma-separated numeric user IDs, `CHANNEL_ID`, `GEMINI_API_KEY`, and `MAX_UPLOADS_PER_DAY`. Cloudflare credentials are recommended; Pollinations.ai is used if Cloudflare is unavailable or fails. `GROQ_API_KEY` is an optional text fallback. Gemini, Groq, Cloudflare and Pollinations availability and policies can change. Never send keys to the bot.
2. Set `PUBLIC_BASE_URL` to a reachable HTTPS URL and use the existing Google Web OAuth setup and `/connect`. Either configure `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`, or set `YOUTUBE_CLIENT_SECRET` to the path of a Google **web client** JSON file. The OAuth token and generated character library are stored under `DATA_DIR`. Protect the directory and mount it persistently on Railway.
3. Put music you have the right to use in `DATA_DIR/music`, or set `MUSIC_LIBRARY_PATH` to an existing folder such as `assets/music`. Supported formats include MP3 and WAV. Check each track's commercial-use and attribution requirements yourself.
4. Install the dependencies with `pip install -r requirements.txt` and install FFmpeg/ffprobe. The first 2.5D render uses `rembg` CPU segmentation and may download its model. Run `uvicorn main:app --host 0.0.0.0 --port 8000`, or run `docker compose up --build` after preparing `.env` and `data/`. Scene images are generated by Cloudflare Workers AI with Pollinations.ai fallback; PyTorch is not needed on Railway or on the iPhone used to control Telegram.
5. In Railway, select `VIDEO_PROVIDER` and add `FAL_KEY` for Kling/Hailuo/PixVerse/Vidu/Seedance/Veo, or `RUNWAY_API_KEY` for Runway. `VIDEO_API_MAX_SCENES_PER_DAY=8` and `VIDEO_API_DAILY_BUDGET_USD=10` are the default daily guards. The budget guard uses conservative per-scene estimates (customizable with `VIDEO_ESTIMATED_COST_<PROVIDER>_USD`) and is not a guaranteed invoice cap; actual prices, models, durations, and rounding differ. In the private admin chat, use `/setlang ar`, `/setmusic library`, `/addtopic TOPIC`, and `/auto_on 24`; or use `/create TOPIC` for an immediate publication. `/queue` includes waiting topics; `/status` includes automatic jobs and errors; `/video_usage` reports today's request count and estimated spend. `/resume ID` retries a failed job when its publication state is safe to retry.
6. `/characters`, `/newchar IDEA`, `/preview ID`, and `/delchar ID` manage the original character library. Each entry includes a fixed `gender` and appearance bible; changes persist in `DATA_DIR/characters.json`.

Scene artwork is generated remotely with Cloudflare Workers AI (`CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN`) and falls back to Pollinations.ai. The fixed character bible supplies the appearance prompt and gender; image models can still vary faces between scenes, so visual consistency is not guaranteed.

The default requirements intentionally omit PyTorch and local image/video models. The optional `requirements-local-musicgen.txt` is only for local MusicGen under the noncommercial-license restrictions described below. It is not needed to run the bot or control it from an iPhone. For Oracle Cloud Always Free, install Docker and Compose, copy the repository and `.env` securely, and mount persistent storage; verify VM and provider limits before relying on any free tier.

`MUSIC_MODE=library` is the default. `generate` uses local `facebook/musicgen-small`, and `auto` tries it before the library. To run MusicGen, separately install `pip install -r requirements-local-musicgen.txt`; this optional install includes PyTorch and is not required for the bot, Cloudflare image generation, or video APIs. MusicGen model weights are [CC-BY-NC-4.0](https://huggingface.co/facebook/musicgen-small), **not licensed for commercial use**. Generation is disabled unless `MUSICGEN_NONCOMMERCIAL_CONFIRMED=1`; only set it after checking that your intended use complies. `auto` uses the library when confirmation is absent. This system cannot establish copyright clearance for generated content or external music.

The automatic renderer outputs 1920×1080 with burned-in white, outlined subtitles. With `VIDEO_PROVIDER=none`, `rembg` separates the AI-generated character and FFmpeg adds 2.5D parallax and zoom; this is not a 3D rig. Explicitly selected hosted image-to-video providers remain available and override the local parallax mode. Edge TTS speaks rather than sings the lyrics. Character names, gender, appearance, location and camera-angle requirements are validated, but generated content can still be imperfect. Review every video before publishing.

Automatic jobs, settings, per-destination publication states, and quota reservations are saved in SQLite. On restart interrupted jobs are requeued and completed scene files reused. If an upload or Telegram send has an uncertain outcome, the job is stopped rather than automatically repeated: inspect the channel and YouTube first to avoid duplicate publication. Only files within a completed automatic job's work directory are removed.

Optional: `BACKGROUND_MUSIC_PATH=/app/data/music/licensed-track.mp3` for music you have rights to use. Without this variable, the video has narration only.

### AI scene illustrations

Every new scene uses a separately generated AI image, its own location and camera angle, then a 2.5D character-layer animation when `VIDEO_PROVIDER=none`. Character appearance is taken from `assets/characters/characters.json`; no procedural character artwork is generated. Existing published videos are not changed.

1. Create a Cloudflare account and set up Workers AI. In its [REST API setup](https://developers.cloudflare.com/workers-ai/get-started/rest-api/), find the Account ID and create a Workers AI API token with the required permission.
2. In the Railway service's Variables, set `IMAGE_PROVIDER=cloudflare`, `CLOUDFLARE_ACCOUNT_ID` to the Account ID, and `CLOUDFLARE_API_TOKEN` to the token. If Cloudflare is unconfigured or unavailable, image generation tries Pollinations.ai. Never share or commit the token.
3. Deploy the changes, then use `/today`. Each scene requests one image with `@cf/black-forest-labs/flux-1-schnell`; completed images are reused when a render is retried. Existing completed videos are not regenerated.
4. Review the images, narration and music rights in Telegram before publishing. The model generates independent images, so characters may look different across scenes. Video generation does not create music or singing.

Cloudflare's [pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/) describes a shared daily free allocation; usage and limits can change. On the Free plan, exceeding it triggers the Pollinations.ai fallback; paid usage can incur charges. FastLLM and Railway costs are separate. If both image providers fail, the render fails visibly rather than drawing a character or publishing a text card.

Add a Railway Volume mounted at `/app/data` so the SQLite database, media, and Google token survive redeployments. The included Dockerfile installs FFmpeg. Use exactly one running replica because Telegram polling and the in-process scheduler must not run twice. `PUBLIC_BASE_URL` must be the full HTTPS Railway domain.

### Telegram bot

1. Create a bot with Telegram `@BotFather`; set its token as `TELEGRAM_BOT_TOKEN`.
2. Obtain your numeric Telegram user ID and set `ADMIN_TELEGRAM_ID` to it. Only this ID can control the bot.
3. Set Railway variables and redeploy. Open your bot, send `/start`.
4. Send `/new`, enter an idea, choose the script type, length, language, and configured voice. Review the script and scene outline. Approve production only when ready; after the rendered video arrives, use its separate button to publish now or choose schedule and send `/schedule EPISODE_ID YYYY-MM-DD HH:MM` in UTC. The dashboard and `/settings` switch among providers whose required Railway credentials are already set.

### Google / YouTube OAuth

1. In Google Cloud Console, create/select a project and enable **YouTube Data API v3** and **YouTube Analytics API**.
2. Configure the OAuth consent screen. Add your Google account as a test user while the app is in testing mode, if required.
3. Create an OAuth Client ID of type **Web application**.
4. Add this exact Authorized redirect URI: `https://YOUR-RAILWAY-DOMAIN/youtube/oauth/callback`.
5. Put the client ID and secret in Railway variables. Set `PUBLIC_BASE_URL` to that same deployed domain.
6. In the private Telegram admin chat, use `/connect`, open the Google link, and authorize the channel-owning Google account. The refresh token is stored in the mounted Railway Volume.
7. Use `/today` to create two videos and test. Review each video in Telegram and press the publish button only when satisfied.

Do not send Google passwords, OAuth tokens, or provider keys to anyone. Revoke OAuth access from your Google Account if you need to disconnect the integration; delete `google-token.json` from the Railway Volume to remove the saved token.

### Animated scenes (fal.ai image-to-video)

Choose an optional hosted image-to-video provider from Telegram `/settings` or the dashboard. Kling, Hailuo, PixVerse, Vidu, Dreamina/Seedance, and Google Veo use their documented image-to-video models hosted by fal.ai and require `FAL_KEY`. Runway uses its official developer API and requires `RUNWAY_API_KEY`. These override the local 2.5D animation. With `VIDEO_PROVIDER=none`, every AI scene image is segmented and animated locally using parallax and zoom.

Set `VIDEO_API_MAX_SCENES_PER_DAY` (default `8`) and `VIDEO_API_DAILY_BUDGET_USD` (default `$10`) to limit paid scene-generation usage across existing and automatic workflows. Failed requests count toward both guards. Budget reservation uses per-scene estimates, and is **not a guaranteed dollar-spend cap**: actual model rates, output duration, provider-side rounding, or price changes can result in different charges. Check the linked provider pricing before enabling a model. Existing `/new` and `/today` still require review and explicit YouTube approval; `/create` and `/auto_on` keep their separate automatic-publishing behavior.

Provider reference pages: [Kling](https://fal.ai/models/fal-ai/kling-video/v3/pro/image-to-video), [Hailuo](https://fal.ai/models/fal-ai/minimax/hailuo-2.3-fast/standard/image-to-video), [PixVerse](https://fal.ai/models/fal-ai/pixverse/v6/image-to-video), [Vidu](https://fal.ai/models/fal-ai/vidu/q4/image-to-video), [Dreamina/Seedance](https://fal.ai/models/bytedance/seedance-2.0/fast/image-to-video), [Veo](https://fal.ai/models/fal-ai/veo3.1/image-to-video), [Runway API](https://docs.dev.runwayml.com/).

Optional experiment: `VIDEO_PROVIDER=hf` uses a public Hugging Face Space (`HF_SPACE`, `HF_API_NAME`, optional `HF_TOKEN`). It is slow and unreliable, and Space interfaces can change; it is not the recommended production adapter.

## Song mode (default): original kids-song music videos

`CONTENT_MODE=song` (default) makes 16:9 sing-along videos of about 1-2 minutes using the original character bible, with a distinct AI-generated scene image for every section. Songs are original; no existing franchise or song is copied. Set `CONTENT_MODE=story` for narrated stories.

- Recommended settings: `IMAGE_PROVIDER=cloudflare`, `VIDEO_PROVIDER=none` for local 2.5D parallax, and `SONG_PROVIDER=tts` or `fal` (AI-sung track with `FAL_KEY`).
- `tts` is a child-like voice reading each line on a beat over a synthesized backing: it is not real singing. For a sung vocal use `SONG_PROVIDER=fal` (ACE-Step on fal.ai; unverified request schema, set `FAL_MUSIC_MODEL` if needed). With `fal`, lyric timing across scenes is approximate.
- YouTube upload adds lyrics, tags and kids-song hashtags. Review every video first; AI images and voices can be flawed.
- The animation is a 2.5D effect from AI-generated images and separated character layers, not a full 3D rig.

## Schedule and publishing

Schedule values use UTC in `HH:MM` format. Defaults are noon and 14:00 UTC. The existing daily scheduler sends drafts to Telegram and requires approval before publishing. The separate opt-in automatic workflow publishes without per-video approval.

## Web endpoints

- `/` service info
- `/health` non-secret service readiness
- `/docs` API documentation
- `/youtube/oauth/callback` Google OAuth redirect
- `/studio/episodes` and `/studio/stats` require `x-api-key: APP_API_KEY`

`/studio/stats` reports YouTube Analytics for the last 28 complete days. The Telegram `/stats` command shows the top videos for that period.

## Dashboard

Open `/dashboard` on your Railway domain and enter `APP_API_KEY` to see settings, episode progress/errors, and daily runs (auto-refreshes every 10s). The key is kept in the browser session only.

## Studio features

- **Varied topics:** without `DAILY_TOPIC`, themes rotate daily (sea, forest, space, farm, ...) and the prompt avoids recent titles. **Remove `DAILY_TOPIC` from Railway** if you want rotation.
- **Consistent characters:** the AI writes a character sheet and art style that is prepended to every image prompt. Images may still vary between scenes.
- **Animated subtitles** (Cloudflare mode) use a single image playlist to reduce FFmpeg memory use. An **original synthesized lullaby** plays under the narration (override with `BACKGROUND_MUSIC_PATH`).
- **Voice:** `VOICE_AR`, `VOICE_EN`, `VOICE_RATE` (default `-5%`), `VOICE_PITCH` (default `+0Hz`).
- **Dashboard** at `/dashboard` has a "generate now" button (`POST /studio/generate`, `x-api-key` header).
