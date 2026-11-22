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

1. Copy `.env.example` to `.env` for local use, and set `DATA_DIR=./data`. Set `BOT_TOKEN` (or the existing `TELEGRAM_BOT_TOKEN`), `ADMIN_IDS` as comma-separated numeric user IDs, `CHANNEL_ID`, `GEMINI_API_KEY`, and `MAX_UPLOADS_PER_DAY`. `GROQ_API_KEY` is an optional text fallback. Gemini and Groq account quotas and billing policies can change; free usage is not guaranteed. Never send keys to the bot.
2. Set `PUBLIC_BASE_URL` to a reachable HTTPS URL and use the existing Google Web OAuth setup and `/connect`. Either configure `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`, or set `YOUTUBE_CLIENT_SECRET` to the path of a Google **web client** JSON file. The OAuth token and generated character library are stored under `DATA_DIR`. Protect the directory and mount it persistently on Railway.
3. Put music you have the right to use in `DATA_DIR/music`, or set `MUSIC_LIBRARY_PATH` to an existing folder such as `assets/music`. Supported formats include MP3 and WAV. Check each track's commercial-use and attribution requirements yourself.
4. Install the dependencies with `pip install -r requirements.txt` and install FFmpeg/ffprobe. Run `uvicorn main:app --host 0.0.0.0 --port 8000`. Or run `docker compose up --build` after preparing `.env` and `data/`. The container uses CPU PyTorch unless configured with a compatible GPU runtime and drivers.
5. In the private admin chat, use `/setlang ar`, `/setmusic library`, `/addtopic TOPIC`, and `/auto_on 24`; or use `/create TOPIC` for an immediate publication. `/queue` includes waiting topics; `/status` includes automatic jobs, errors, and today's automatic upload count. `/resume ID` retries a failed job when its publication state is safe to retry.
6. `/characters`, `/newchar IDEA`, `/preview ID`, and `/delchar ID` manage the original character library. Changes persist in `DATA_DIR/characters.json`.

The new workflow uses the locally downloaded `black-forest-labs/FLUX.1-schnell` image model, **not** Pollinations or paid image APIs. The 12-billion-parameter model requires a large download and substantial RAM/VRAM; CPU-only generation may be impractically slow. Access can require accepting model terms and an `HF_TOKEN`. Railway or Oracle Always Free may not have enough resources, and neither hosting nor provider free tiers are guaranteed. For Oracle Cloud Always Free, provision an eligible VM, open only required network ports, install Docker and Compose, copy the repository and `.env` securely, mount persistent storage, then run `docker compose up --build`; model and storage requirements must be checked against the actual VM limits.

`MUSIC_MODE=library` is the default. `generate` uses local `facebook/musicgen-small`, and `auto` tries it before the library. MusicGen model weights are [CC-BY-NC-4.0](https://huggingface.co/facebook/musicgen-small), **not licensed for commercial use**. Generation is disabled unless `MUSICGEN_NONCOMMERCIAL_CONFIRMED=1`; only set it after checking that your intended use complies. `auto` uses the library when confirmation is absent. This system cannot establish copyright clearance for generated content or external music.

The output is 1920×1080 with burned-in captions, but 1280×720 generated images are upscaled; Ken Burns is camera movement over still images, not character animation. Edge TTS speaks rather than sings the lyrics. Character descriptions and seeds improve repeatability but do not guarantee visual identity or original melodies. The originality guard checks known names and close spellings; it cannot detect all copyrighted similarities. Review the content and rights before using automatic publishing. The upload limit applies to this new automatic workflow; it does not count uploads made through the pre-existing manual workflow.

Automatic jobs, settings, per-destination publication states, and quota reservations are saved in SQLite. On restart interrupted jobs are requeued and completed scene files reused. If an upload or Telegram send has an uncertain outcome, the job is stopped rather than automatically repeated: inspect the channel and YouTube first to avoid duplicate publication. Only files within a completed automatic job's work directory are removed.

Optional: `BACKGROUND_MUSIC_PATH=/app/data/music/licensed-track.mp3` for music you have rights to use. Without this variable, the video has narration only.

### AI scene illustrations

For a simple original 3D character instead of generated illustrations, set `VIDEO_PROVIDER=blender`, `CONTENT_MODE=song`, `SONG_PROVIDER=tts`, and `DAILY_LANGUAGES=ar`. The song writer can choose a garden dance, playhouse wave, rainbow clap or pointing at colored blocks for each section. Blender renders and caches only the selected short loops; older songs or invalid action choices use a rotating selection. The scenes and gestures are preset and repeat: they cannot depict arbitrary story events or match each lyric, and TTS is not singing. The first render of a new scene version can take several minutes per loop; later episodes reuse the cached clips. The Docker image includes Blender; rendering and hosting still incur Railway costs. Check actual usage before relying on a monthly budget. Existing videos do not change automatically. Older story episodes cannot be rendered in Blender mode.

1. Create a Cloudflare account and set up Workers AI. In its [REST API setup](https://developers.cloudflare.com/workers-ai/get-started/rest-api/), find the Account ID and create a Workers AI API token with the required permission.
2. In the Railway service's Variables, set `IMAGE_PROVIDER=cloudflare`, `CLOUDFLARE_ACCOUNT_ID` to the Account ID, and `CLOUDFLARE_API_TOKEN` to the token. Never share or commit the token.
3. Deploy the changes, then use `/today`. Each scene requests one image with `@cf/black-forest-labs/flux-1-schnell`; completed images are reused when a render is retried. Existing completed videos are not regenerated.
4. Review the images, narration and music rights in Telegram before publishing. The model generates independent images, so characters may look different across scenes. Video generation does not create music or singing.

Cloudflare's [pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/) describes a shared daily free allocation; usage and limits can change. On the Free plan, exceeding it stops requests; Paid plan usage above the free allocation can incur charges. FastLLM and Railway costs are separate. A missing credential, exhausted quota, or image-generation error fails the render visibly instead of silently publishing text cards. Use `IMAGE_PROVIDER=cards` to retain the original renderer.

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

Set `IMAGE_PROVIDER=cloudflare`, `VIDEO_PROVIDER=fal` and `FAL_KEY` (from fal.ai dashboard). Each scene image is animated into a 5-second clip (`FAL_VIDEO_MODEL`, default Kling 2.1 standard) that plays forward and backward under the narration. This adds fal.ai cost per scene (about 6 clips per episode); check their pricing. Failures fail the render visibly; completed clips are reused on retry.

Free option: `VIDEO_PROVIDER=hf` animates scenes through a public Hugging Face Space (`HF_SPACE`, `HF_API_NAME`, optional `HF_TOKEN` for a better free quota). It is slow, queued and unreliable; any scene that fails falls back to the zoom animation. The default Space's API arguments are unverified and may change, so test with `/today`.

## Song mode (default): original kids-song music videos

`CONTENT_MODE=song` (default) makes 16:9 sing-along videos of about 1-2 minutes, modelled on what top nursery-rhyme channels do: one recurring cute character, simple AABB rhymes, a chorus repeated 3 times, bright glossy 3D-style scenes and big on-screen lyrics. Songs are original; no existing franchise or song is copied. Set `CONTENT_MODE=story` for the previous narrated stories.

- Recommended settings: `IMAGE_PROVIDER=cloudflare`, `VIDEO_PROVIDER=hf` (free) or `fal` (paid, better), `SONG_PROVIDER=tts` (free) or `fal` (real AI-sung track with `FAL_KEY`).
- `tts` is a child-like voice reading each line on a beat over a synthesized backing: it is not real singing. For a sung vocal use `SONG_PROVIDER=fal` (ACE-Step on fal.ai; unverified request schema, set `FAL_MUSIC_MODEL` if needed). With `fal`, lyric timing across scenes is approximate.
- YouTube upload adds lyrics, tags and kids-song hashtags. Review every video first; AI images and voices can be flawed.
- Honest limits: 3D look comes from AI images plus short image-to-video clips, not a studio animation pipeline; matching CoComelon quality is not realistic.

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
