# Kids Haven — Telegram + YouTube

A bilingual Arabic/English children's story-video MVP operated from a private Telegram bot, with Google OAuth to upload reviewed videos and retrieve YouTube Analytics.

## What the bot does

- Creates one Arabic and one English original sea-adventure episode per day at configured UTC times (defaults 12:00 and 14:00).
- Generates short scripts with FastLLM, creates narrated vertical MP4 scene-card videos, and sends each to the configured Telegram admin for review. With `FASTLLM_MODEL=auto`, it prefers an available Grok text model, then uses the existing model preference/fallback behavior.
- Uploads to YouTube **only after the admin presses “Approve & publish”**. Published uploads are public and marked as made for kids (`selfDeclaredMadeForKids: true`).
- `/today` manually generates both language versions; `/status` lists recent episodes; `/stats` queries YouTube Analytics.
- Stores jobs, generated files, and OAuth token in SQLite/files under `DATA_DIR`.

## Important limitations

This MVP renders narrated text/illustration cards, **not full AI animation or generated scene artwork**. Nano Banana image generation is not integrated: it requires an image-generation API supported by the configured provider, which the current text-completion integration does not call. Edge TTS is used for voice. Daily generation can incur provider costs and may be affected by Railway uptime, API limits, and outbound network access. The scheduler runs inside the web service; use one Railway replica and keep it running. Failed daily runs can be retried with `/today` after reviewing the error.

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
DAILY_TOPIC=Original gentle sea adventures teaching kindness and teamwork
```

Optional: `BACKGROUND_MUSIC_PATH=/app/data/music/licensed-track.mp3` for music you have rights to use. Without this variable, the video has narration only.

Add a Railway Volume mounted at `/app/data` so the SQLite database, media, and Google token survive redeployments. The included `nixpacks.toml` installs FFmpeg. Use exactly one running replica because Telegram polling and the in-process scheduler must not run twice. `PUBLIC_BASE_URL` must be the full HTTPS Railway domain.

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

## Schedule and publishing

Schedule values use UTC in `HH:MM` format. Defaults are noon and 14:00 UTC. Set times appropriate for your audience. The scheduler generates a draft/video at those times, then sends it to Telegram. No video is automatically published without the explicit approval button.

## Web endpoints

- `/` service info
- `/health` non-secret service readiness
- `/docs` API documentation
- `/youtube/oauth/callback` Google OAuth redirect
- `/studio/episodes` and `/studio/stats` require `x-api-key: APP_API_KEY`

`/studio/stats` reports YouTube Analytics for the last 28 complete days. The Telegram `/stats` command shows the top videos for that period.
