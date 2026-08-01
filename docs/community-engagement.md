# Community Engagement Pipeline

Automated discovery of relevant psychology videos + AI-drafted comments, built to grow MindByteFacts'
reputation in-niche without ever posting anything the channel owner hasn't personally approved.

## Why the review step isn't optional

YouTube's [API Services Developer Policies](https://developers.google.com/youtube/terms/developer-policies)
state:

> "you must not automate or trigger... comments... without the user's prior specific and express consent"

> "users must have final authority over any actions the [API Client] takes to insert... their data...
> the user must expressly consent to those actions prior to their actual execution."

A fully autonomous find-draft-post loop would violate this. This pipeline is deliberately split into three
independent modes so a real human review always sits between drafting and publishing:

```
generate (drafts ~30 days of candidate comments, writes NOTHING to YouTube)
    -> you review the batch in the CommentQueue sheet tab, set Approved=TRUE per row you want posted
    -> publish (runs daily, only ever posts rows that are already Approved=TRUE)
    -> learn (weekly, checks engagement on posted comments, feeds weekly_review.py)
```

This means you sit down roughly once a month to review a batch (rather than every single day), and the
publish step trickles the approved rows out automatically at the configured daily rate - the "prior express
consent per action" requirement is satisfied because each comment was individually reviewed before ever
being posted, not because the workflow ran on a schedule.

## One-time setup required before this can post anything

1. **New OAuth scope.** The existing `OAUTH_REFRESH_TOKEN` secret almost certainly does not include
   `https://www.googleapis.com/auth/youtube.force-ssl`, which `commentThreads.insert` requires. You need to
   re-run the OAuth consent flow for this app with that scope added (in addition to whatever scopes it
   already has - typically `youtube.upload`, `youtube.readonly`/`youtube`, and `spreadsheets`), and update
   the `OAUTH_REFRESH_TOKEN` GitHub secret if a new token is issued. This is an account-permission change
   only you can make - nothing in this repo can request it on your behalf.
2. **Confirm your channel handle** in `community_engagement_config.py` (`CHANNEL_HANDLE`, `SUBSCRIBE_URL`) -
   defaults are placeholders.
3. **First batch:** trigger `Community Engagement - Generate Batch` manually (Actions tab ->
   `workflow_dispatch`) once, then open the `CommentQueue` tab in your Google Sheet and review the drafted
   rows.

## Reviewing a batch

Each row in `CommentQueue` has: the target video's title/URL/channel, the discovery query that found it,
the drafted comment text, a relevance score and similarity score (both already used to auto-reject weak
drafts before they even reach the sheet), and `Status` / `Approved` / `ScheduledDate` columns.

- Leave `Approved` blank or set it to `FALSE` to never publish a row.
- Set `Approved` to `TRUE` (exact text) to allow it to publish on its `ScheduledDate`.
- The daily publish run only ever looks at rows where `Status=pending_review` AND `Approved=TRUE` AND
  `ScheduledDate` has arrived - nothing else is touched.

## Safety systems (all configurable in `community_engagement_config.py`)

| Control | Default | Purpose |
|---|---|---|
| `DAILY_COMMENT_LIMIT` | 5 | Hard cap on comments posted per day, regardless of how many are approved |
| `MIN_DELAY_SECONDS` / `MAX_DELAY_SECONDS` | 45-240s | Random delay between posts in the same run |
| `SIMILARITY_THRESHOLD` | 0.72 | Rejects a draft that's too textually similar to a recent comment (difflib ratio) |
| `MIN_RELEVANCE_KEYWORD_OVERLAP` | 2 | Rejects a draft that doesn't reference specifics from the target video |
| `BANNED_COMMENT_PATTERNS` + `brand_rules.GENERIC_PHRASES` | see config | Hard-reject self-promotion, subscribe asks, generic filler |
| `FORBID_LINKS_IN_COMMENTS` | always True | No comment may ever contain a URL - not configurable, by design |
| `MIN_CHANNEL_SUBSCRIBERS` / `MAX_CHANNEL_SUBSCRIBERS` | 500 / 3,000,000 | Skip channels too small to matter or too big to be seen in |
| `COMMENTING_ENABLED` | True | Master kill switch - set False to stop `publish` from posting anything at all |

Every discovery/draft/reject/post/failure is logged to the `CommunityEngagementLog` sheet tab
(`log_action()` in `community_engagement.py`), same append-only pattern as the rest of the codebase.

## Why no subscribe link ever appears in a comment

Per the original brief and your own confirmation: the subscribe link (`SUBSCRIBE_URL` in
`community_engagement_config.py`) lives only on the channel's profile/About page. Putting a link inside an
automated comment is one of the fastest ways to trip YouTube's spam filter (URL + a new-ish posting pattern
reads as a classic bot signature) and directly contradicts "feel human, not promotional." The goal is that a
genuinely useful comment makes a curious reader click the commenter's name themselves.

## Learning loop

`community_engagement.py learn` (weekly) re-checks like counts on previously-posted comments via
`comments.list` and appends a snapshot to `CommunityEngagementResults`. `weekly_review.py` now reads both
`CommentQueue` and `CommunityEngagementResults` (via `load_community_engagement_insights()`) and folds a
per-discovery-query engagement summary into its Groq prompt and into the weekly report
(`community_engagement_analysis` field) - purely additive, degrades to "no data yet" if the pipeline hasn't
posted anything, and never affects the existing video-performance analysis.

**Known limitation:** YouTube's public API does not expose "profile visits caused by a specific comment" -
there is no such metric available via the Data API. What IS tracked is like-count on each posted comment,
grouped by which discovery query/topic found the video - a reasonable proxy for "which topics generate
interest," but not a direct subscriber-attribution number. `weekly_review.py`'s prompt is written to say so
explicitly rather than invent an attribution claim.

## What this system will never do

- Post a comment that hasn't been explicitly approved by the channel owner.
- Insert a link, subscribe ask, or channel mention into a comment.
- Scrape YouTube pages directly (all discovery goes through the official `search.list` /
  `videos.list` / `channels.list` endpoints, same as `external_trends.py` already does).
- Offer or imply any incentive for viewers to engage (against YouTube API policy).
- Auto-approve its own drafts, under any configuration.

## Testing performed before delivery

- Full unit-style pass (mocked, no live credentials) against every safety-check function:
  `contains_banned_pattern`, `contains_link`, `relevance_check`, `similarity_against_history`, and the
  combined `run_all_safety_checks()` gate - verified each correctly accepts a good draft and rejects a
  self-promotional draft, a linked draft, and an off-topic draft.
- `passes_channel_filters()` verified against a too-small, a too-large, and a normal-sized channel.
- `python -m py_compile` on all three touched/new files (`community_engagement.py`,
  `community_engagement_config.py`, `weekly_review.py`).
- Could **not** test from this environment: a real GitHub Actions run (no push access to this repo), a real
  Groq/YouTube Data API response (no live credentials here), or an actual `commentThreads.insert` call
  (would require your re-consented OAuth token, which doesn't exist yet). These need a live run on your end
  - see the checklist below.

## Verification checklist for your first live run

- [ ] Re-consent OAuth with `youtube.force-ssl` added; update the `OAUTH_REFRESH_TOKEN` secret if changed.
- [ ] Confirm `CHANNEL_HANDLE`/`SUBSCRIBE_URL` in `community_engagement_config.py`.
- [ ] Manually trigger `Community Engagement - Generate Batch` once; confirm rows appear in `CommentQueue`
      with `Status=pending_review`.
- [ ] Review the batch; set `Approved=TRUE` on a small number of rows first (not all ~150) to watch one
      real publish cycle before trusting the full batch.
- [ ] Manually trigger `Community Engagement - Publish Approved Comments`; confirm it posts only the rows
      you approved, respects `DAILY_COMMENT_LIMIT`, and logs to `CommentQueue`/`CommunityEngagementLog`.
- [ ] Check the posted comments on YouTube directly - confirm they read as intended and aren't flagged/held
      for moderation.
- [ ] After a week, let `Community Engagement - Learning Sync` run once; confirm
      `CommunityEngagementResults` gets rows.
- [ ] Next `weekly_review.yml` Sunday run: confirm the log shows "community engagement data available" and
      the report includes a `community_engagement_analysis` section.
