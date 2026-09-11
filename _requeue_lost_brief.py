"""One-off (2026-09-11): re-queues the NextWeekQueue brief lost in run #158
(2026-09-09) - 'Why Men Overreact When They Feel Their Partner Pulling
Away' scored idea 7.2/quality 8 and passed compliance, but was never
published: it was marked Used=Y at topic-selection time (the bug just fixed
in pipeline.py/pipeline_longform.py/facebook_pipeline.py - see
select_topic_for_run()'s 2026-09-11 comment), then the run crashed on a
3-tier ffmpeg render timeout before ever reaching upload. Tagged Confidence
High since the idea already proved itself; nothing wrong with it, only with
the infra that dropped it. Safe to run once; delete alongside its temp
workflow afterward, same pattern as every other one-off verification script
this session."""

from datetime import datetime, timezone
import pipeline as p

token = p.get_access_token()
row = [
    datetime.now(timezone.utc).date().isoformat(),  # WeekOf
    "short",                                         # Format
    "Relationship Psychology",                       # Pillar
    "Why Men Overreact When They Feel Their Partner Pulling Away",  # Title
    "", "", "", "", "", "", "",                       # Hook..TargetLengthSec (unknown, regenerated fresh)
    "",                                                # Used
    datetime.now(timezone.utc).isoformat(),           # CreatedAt
    "", "", "", "", "",                                # HookType..LoyaltyAngle
    "High",                                            # Confidence - already proved itself before the crash
    "",                                                # TrendSource
]
try:
    p.sheet_append(token, f"{p.NEXT_QUEUE_SHEET_TAB}!A:T", row)
except Exception as e:
    healed = p.ensure_sheet_tab(token, p.NEXT_QUEUE_SHEET_TAB, p.NEXT_QUEUE_HEADER)
    if healed:
        p.sheet_append(token, f"{p.NEXT_QUEUE_SHEET_TAB}!A:T", row)
    else:
        raise
print("[requeue] re-added lost brief to NextWeekQueue: "
      "'Why Men Overreact When They Feel Their Partner Pulling Away' (Confidence=High)")
