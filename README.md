# mindbyte-automation

Automated YouTube Shorts pipeline for the **MindByte** channel (bite-sized
facts/trivia). Runs entirely on GitHub Actions (free, public-repo runners) -
no paid services, no billing account, no future payments.

## What it does

Once a day (`publish.yml`), the pipeline:

1. Picks an unused topic from the rotation (tracked in the `UsedTopics` sheet tab).
2. Generates an original script, title, description and visual keywords with Gemini.
3. Scores the script's quality (1-10) and runs a basic originality/compliance check.
4. If it passes, sources matching B-roll clips from Pexels.
5. Synthesizes a voiceover with `edge-tts` (free, no API key) and builds timed captions.
6. Assembles the final vertical video with `ffmpeg` (clips + voiceover + burned-in captions).
7. Uploads it to YouTube as **private**, scheduled to go public automatically after
   an 18-hour delay (a safety window rather than an instant zero-review publish).
8. Logs everything to the `Videos` tab of the Google Sheet dashboard.

A second workflow (`analytics.yml`) runs daily and updates views/likes/comments/shares
for every video already logged, so the channel owner never needs to open YouTube Studio.

## Required repository secrets

Set these under **Settings -> Secrets and variables -> Actions**:

| Secret | Purpose |
|---|---|
| `GEMINI_API_KEY` | Script generation + quality scoring |
| `PEXELS_API_KEY` | Stock video B-roll |
| `OAUTH_CLIENT_ID` / `OAUTH_CLIENT_SECRET` / `OAUTH_REFRESH_TOKEN` | YouTube + Sheets access |
| `YOUTUBE_CHANNEL_ID` | Analytics lookups |
| `GOOGLE_SHEET_ID` | Dashboard spreadsheet |

## Why GitHub Actions instead of a paid render API

GitHub Actions minutes are free and unlimited on a public repository, with no
credit card on file and no possibility of a future bill. That constraint
(no future payments, ever) is why this pipeline renders video itself with
`ffmpeg` and voices it with the free `edge-tts` library rather than a paid
video-rendering API or a cloud provider that requires a billing account.

## Local testing

```
pip install -r requirements.txt
export GEMINI_API_KEY=... PEXELS_API_KEY=... OAUTH_CLIENT_ID=... \
       OAUTH_CLIENT_SECRET=... OAUTH_REFRESH_TOKEN=... GOOGLE_SHEET_ID=...
python pipeline.py
```
