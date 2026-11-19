# Kids Haven — Telegram + YouTube

A bilingual Arabic/English children's story-video MVP operated from a private Telegram bot, with Google OAuth to upload reviewed videos and retrieve YouTube Analytics.

## What the bot does

- Creates one Arabic and one English original episode per day at configured UTC times (defaults 12:00 and 14:00). Each script targets 300-360 spoken words; actual duration depends on narration speed. If the story is outside that range, the model is asked to revise it, up to three attempts.
- Generates scripts with FastLLM, creates narrated vertical MP4 videos, and sends each to the configured Telegram admin for review. Optionally, Cloudflare Workers AI generates scene illustrations with a slow camera zoom. With `FASTLLM_MODEL=auto`, it prefers an available Grok text model.
- Uploads to YouTube **only after the admin presses “Approve & publish”**. Published uploads are public and marked as made for kids (`selfDeclaredMadeForKids: true`).
- The button shown by `/start`, or `/today`, manually generates both language versions; `/status` lists recent episodes; `/stats` queries YouTube Analytics.
- Telegram review uploads larger than 49 MB are compressed into a temporary copy; the original video is retained for YouTube. Use `/review EPISODE_ID` to retry sending an existing video awaiting review.
- `/status` includes today's generation state (even before a script is saved), render progress and the latest error. `/today` runs without blocking other Telegram commands, and retries failed renders and episodes stuck in `rendering` for over 10 minutes, reusing the saved script.
- Scene artwork and narration are prepared concurrently, with one scene prepared ahead while FFmpeg renders the current scene. Rendering stays sequential to limit CPU use.
- Stores jobs, generated files, and OAuth token in SQLite/files under `DATA_DIR`.

## Important limitations

By default the renderer uses text cards. Cloudflare mode generates still images with a slow zoom, **not fully animated characters or songs**. Images can be inconsistent or unsuitable, so review every video before approval. Edge TTS is used for voice. Daily generation can incur provider costs and may be affected by Railway uptime, API limits, and outbound network access. The scheduler runs inside the web service; use one Railway replica and keep it running. Failed daily runs can be retried with `/today` after reviewing the error.

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

Optional: `BACKGROUND_MUSIC_PATH=/app/data/music/licensed-track.mp3` for music you have rights to use. Without this variable, the video has narration only.

### AI scene illustrations

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

## Schedule and publishing

Schedule values use UTC in `HH:MM` format. Defaults are noon and 14:00 UTC. Set times appropriate for your audience. The scheduler generates a draft/video at those times, then sends it to Telegram. No video is automatically published without the explicit approval button.

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
