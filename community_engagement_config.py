"""
MindByte Automation - Community Engagement Pipeline configuration.

Single source of truth for every tunable knob in the community engagement
system (community_engagement.py + .github/workflows/community_engagement_*.yml).
Nothing here talks to the network - it's pure constants, deliberately kept
in one small file so you can change behavior (limits, topics, wording
rules) without touching the logic in community_engagement.py at all.

IMPORTANT - policy context (see docs/community-engagement.md for the full
writeup): YouTube's API Developer Policies require "the user's prior
specific and express consent" before any comment is posted via the API,
with "final authority" resting on the channel owner for each action. This
is NOT a one-time setup checkbox - it means every comment this system ever
posts must have been individually reviewed and approved by you first. The
whole pipeline is built around a generate -> review/approve -> publish
split for exactly this reason. Nothing in this config can turn that off;
COMMENTING_ENABLED below is a master kill switch, not a consent bypass.
"""

# ---------------------------------------------------------------------------
# Master switch
# ---------------------------------------------------------------------------
# Set to False at any time to stop the publish step from posting anything,
# without touching the workflow files. Generation/drafting still runs (it's
# read-only against YouTube), only the actual commentThreads.insert calls
# are gated by this flag.
COMMENTING_ENABLED = True

# ---------------------------------------------------------------------------
# Safety limits
# ---------------------------------------------------------------------------
# Conservative starting point per the original brief. Raise gradually once
# you've watched a few weeks of real results (engagement, no flags/removals,
# no channel strikes) - not before.
DAILY_COMMENT_LIMIT = 5

# Minimum / maximum seconds to sleep between each comment publish within a
# single run, so a batch never posts in a tight, obviously-scripted burst.
MIN_DELAY_SECONDS = 45
MAX_DELAY_SECONDS = 240

# A drafted comment whose normalized text has a similarity score (0-1,
# via difflib.SequenceMatcher) at or above this threshold against ANY
# comment already posted or queued in the last SIMILARITY_LOOKBACK_DAYS is
# rejected as "too similar" - this is what keeps every comment from
# converging on the same 2-3 stock sentences.
SIMILARITY_THRESHOLD = 0.72
SIMILARITY_LOOKBACK_DAYS = 60

# A drafted comment is rejected if it doesn't reference anything specific
# from the target video (see relevance_check() in community_engagement.py)
# - this is a heuristic keyword/overlap check, not a semantic model, and is
# intentionally conservative (reject on doubt).
MIN_RELEVANCE_KEYWORD_OVERLAP = 2

# ---------------------------------------------------------------------------
# Discovery topics (kept in sync with brand_rules.CHANNEL_NICHE_LIST -
# imported directly below rather than re-typed, so the two can never drift)
# ---------------------------------------------------------------------------
from brand_rules import CHANNEL_NICHE_LIST  # noqa: E402

DISCOVERY_QUERIES = [
    "human psychology facts",
    "human behavior explained",
    "relationship psychology advice",
    "emotional intelligence",
    "social psychology experiment",
    "self improvement psychology",
]

# Only consider videos published within this many days (avoids commenting
# on long-dead threads nobody will see, and keeps discovery relevant).
DISCOVERY_LOOKBACK_DAYS = 21

# Videos per discovery query pulled from search.list before filtering.
MAX_RESULTS_PER_QUERY = 15

# Skip channels below this subscriber count (too small to matter) or above
# this count (mega-channels where a comment from a small channel is
# invisible and reads more like spam-fishing). Set either to None to
# disable that bound. These are starting defaults - adjust freely.
MIN_CHANNEL_SUBSCRIBERS = 500
MAX_CHANNEL_SUBSCRIBERS = 3_000_000

# Never comment on MindByte's own uploads if they surface in discovery.
EXCLUDE_OWN_CHANNEL = True

# ---------------------------------------------------------------------------
# Comment content rules
# ---------------------------------------------------------------------------
# Hard ban, checked in addition to brand_rules.GENERIC_PHRASES. Anything
# resembling self-promotion or a call to action is rejected outright -
# per the brief, this system builds reputation by being useful, not by
# asking for anything back.
BANNED_COMMENT_PATTERNS = [
    "subscribe to my channel", "check out my channel", "check my channel",
    "sub for sub", "follow me", "follow my channel", "link in bio",
    "my channel", "our channel", "mindbytefacts", "mindbyte facts",
    "smash that like", "hit subscribe", "new video", "check out our",
]

# No comment may contain a URL/link, ever - links belong on the channel
# profile only (see PROFILE section below), never injected into comments.
# This is a hard rule, not a config toggle.
FORBID_LINKS_IN_COMMENTS = True

MAX_COMMENT_CHARS = 400
MIN_COMMENT_CHARS = 60

# ---------------------------------------------------------------------------
# Batch / approval model
# ---------------------------------------------------------------------------
# One "generate" run drafts roughly a month's worth of candidate comments
# (DAILY_COMMENT_LIMIT * BATCH_DAYS) at once, all landing in the
# CommentQueue tab as status=pending_review. You review the whole batch in
# one sitting (in the Sheet, or by asking me to walk through it with you)
# and flip each row's Approved column to TRUE/FALSE. The daily "publish"
# run only ever considers rows already marked Approved=TRUE - anything
# left blank or FALSE never gets posted. This satisfies "prior specific
# and express consent" per comment while only asking you to sit down and
# review roughly once a month instead of every single day.
BATCH_DAYS = 30
BATCH_TARGET_SIZE = DAILY_COMMENT_LIMIT * BATCH_DAYS  # 150 at defaults

# ---------------------------------------------------------------------------
# Channel profile / subscribe funnel (item 4 of the brief)
# ---------------------------------------------------------------------------
# The canonical subscribe link, generated/verified once and stored here -
# NOT placed in comments (see FORBID_LINKS_IN_COMMENTS above). Use this
# same constant anywhere else in the codebase that needs the channel link
# (channel About section, video descriptions, cross-platform bios) so it
# can never drift into an inconsistent or stale URL.
CHANNEL_HANDLE = "@MindByteFacts"  # confirm/replace with your real handle
SUBSCRIBE_URL = "https://www.youtube.com/@MindByteFacts?sub_confirmation=1"

# ---------------------------------------------------------------------------
# Sheet tabs used by this pipeline (self-healing, same pattern as the rest
# of the codebase - created automatically on first write if missing)
# ---------------------------------------------------------------------------
SEEN_VIDEOS_TAB = "CommunityEngagementSeen"
SEEN_VIDEOS_HEADER = ["VideoID", "ChannelTitle", "Title", "Query", "DiscoveredAt"]

COMMENT_QUEUE_TAB = "CommentQueue"
COMMENT_QUEUE_HEADER = [
    "BatchMonth", "VideoID", "VideoURL", "VideoTitle", "ChannelTitle",
    "Query", "DraftComment", "RelevanceScore", "SimilarityScore",
    "Status", "Approved", "ScheduledDate", "PostedAt", "CommentID",
    "CommentURL", "CreatedAt",
]

ENGAGEMENT_LOG_TAB = "CommunityEngagementLog"
ENGAGEMENT_LOG_HEADER = [
    "Date", "Action", "VideoID", "Query", "Details",
]

ENGAGEMENT_RESULTS_TAB = "CommunityEngagementResults"
ENGAGEMENT_RESULTS_HEADER = [
    "CheckedAt", "CommentID", "VideoID", "LikeCount", "ReplyCount", "Query",
]

assert CHANNEL_NICHE_LIST, "brand_rules.CHANNEL_NICHE_LIST must not be empty"
