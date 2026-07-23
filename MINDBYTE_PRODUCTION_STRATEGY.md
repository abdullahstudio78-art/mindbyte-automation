# MindByte Production Strategy — Complete Architecture
Last updated: 2026-07-24

This is the answer to "step back and design the complete production
strategy" — how MindByte becomes a scalable psychology storytelling
channel (Lucifer-Talk-style visuals, 8-character roster, daily Shorts +
weekly long-form) without building something too complex to maintain.

Priority order honored throughout: storytelling > retention > emotional
connection > visual identity > scalability > automation efficiency.

---

## 1. Complete Production Architecture

```
Topic research (existing idea-scoring system, unchanged)
    v
Script creation (existing generate_script/generate_longform_script)
    v
Story breakdown - NEW: script tagged per-beat with:
    - which character (if any) is present
    - what they're doing/feeling
    - what environment they're in
    - camera intent (push-in, static, pan)
    v
Scene planning - NEW: for each beat, resolve to one of three routes:
    A) Existing asset match (character+pose+environment already in library)
    B) Recombination (existing character pose + existing environment,
       composited fresh - only possible once compositing is real, see
       Phase 2 below)
    C) New asset needed - flag for generation queue
    v
Check existing assets (a real lookup against the asset manifest, not a
    keyword guess - this is the step that makes "create only what's
    needed" real instead of aspirational)
    v
Identify missing assets - the delta from step above becomes this week's
    generation queue (see Section 4 for sizing)
    v
Create only required new assets (batched, not per-video - see Section 3)
    v
Assemble video (existing ffmpeg pipeline, extended with the
    illustrated-scene render path once Phase 2 ships)
    v
Publish (existing upload/schedule flow, unchanged)
    v
Collect performance data (NEW - Section 5)
    v
Improve system (NEW - Section 6, feeds back into "Topic research" and
    "Identify missing assets")
```

This is a loop, not a line: the "improve system" step feeds both the next
week's topics AND the next week's asset priorities. That second feedback
path (data -> asset queue) is the piece that doesn't exist yet anywhere
in the current pipeline and is the main new system this document adds.

**What ships in phases, not all at once:**
- Phase 0 (now): plain stock footage, daily cadence unbroken (already live
  on main as of today).
- Phase 1: 2-3 character asset libraries at "essential tier" (see Section
  2/3) + real background-removal so characters composite into scenes
  without the grounding problems seen in this week's prototypes.
- Phase 2: scene-planning step wired into the real pipeline, character
  system re-enabled behind its existing flag, weekly asset queue running.
- Phase 3: performance-data pipeline + self-improving asset planning
  (Sections 5/6).

Do not attempt to ship all four phases at once - each is independently
useful and independently testable, which is what keeps this maintainable.

---

## 2. Character Development Strategy

**Start with 3 characters, not 8.** Byte, Maya, and Alex already have
manifest entries and (for Byte) an essential asset tier built. This isn't
just convenience - these three were chosen (per the existing character
bible) to cover distinct, broad psychology territory:

| Character | Role | Topic coverage |
|---|---|---|
| **Byte** | Narrator/host, direct-to-camera | Confidence, social dynamics, "here's why" explainer beats, curiosity hooks - the connective tissue of almost every video regardless of topic |
| **Maya** | Emotional/relational | Heartbreak, attachment, empathy, relationship psychology, healing/growth |
| **Alex** | Internal/anxious | Overthinking, anxiety, rumination, self-doubt, insomnia/racing thoughts |

Between them, these three cover the large majority of the psychology
content space MindByte already publishes in (social behavior, relationships,
anxiety/self-perception). A rough estimate: 70-80% of typical script beats
map cleanly onto one of these three roles. That's the real test for
"which characters cover the maximum topics" - not personality variety for
its own sake, but coverage of the emotional registers the scripts actually
hit.

**Characters must do more than narrate** - this is a scene-planning
requirement, not just an asset requirement. Every character-beat in a
script should be tagged with an action/emotion/environment triple (e.g.
"Alex, anxious, lying in bed at night, phone lighting his face") rather
than just "Alex talking." That triple is exactly what selects which pose
+ expression + environment combination to use, and it's what makes a shot
read as a character experiencing something instead of a mascot narrating
facts.

**When to add characters 4-8**: only when the data says so (Section 6),
triggered by one of two signals, not a fixed calendar:
1. A recurring topic cluster keeps forcing an awkward fit onto Byte/Maya/
   Alex (e.g. workplace/career psychology doesn't fit any of the three
   well - that's a signal for a 4th character built specifically for it).
2. Byte/Maya/Alex are being reused so often within the SAME video that it
   reads as repetitive (a real risk once daily Shorts + weekly long-form
   both lean on 3 characters) - this is a volume signal, not a topic
   signal, and shows up in retention data as a dip on videos with 3+
   reused character beats.

Do not add character 4 preemptively "to be safe" - an unused 4th
character is pure sunk cost (a full pose/expression/environment library
for zero marginal topic coverage), which is exactly the "impressive but
unmaintainable" trap being avoided here.

---

## 3. Visual/Asset Library Strategy

**Reuse-by-combination, not reuse-by-duplication.** The expensive mistake
would be generating one flat "Byte sad in bedroom" image, one "Byte happy
in bedroom" image, one "Byte sad in cafe" image, etc. - that's N characters
x M emotions x K environments as separate generations. Instead:

- **Character layer**: each character gets ~8-10 core expressions and
  ~5-6 core poses/gestures (walking, sitting, thinking, holding-phone,
  arms-crossed, pointing) - independent of environment.
- **Environment layer**: a shared library of ~8-10 environments (bedroom,
  city street, cafe, office, home, nature, night scene, rain scene, social/
  group scene) - independent of character.
- **Composition happens at render time** (once Phase 2's real compositing
  work lands, replacing this week's flawed cutout prototype) - a
  character + expression + environment is assembled on demand, not
  pre-generated as a fixed pairing.

This is combinatorial, not additive: 3 characters x 9 expressions x 9
environments is theoretically 243 combinations from just 3x9 + 9 = 36
actual generated assets. That ratio (generate the pieces, combine at
render time) is the entire reason this stays maintainable at "hundreds of
videos" scale instead of requiring hundreds of bespoke generations.

**Motion is a separate, even-more-reusable layer.** Camera push-in, pan,
parallax, rain/fog/particle overlays, and lighting/vignette treatments are
pure ffmpeg filter graphs, not images - build each ONCE as a named,
parameterized treatment (we already have this pattern working:
`render_environment_motion_clip`, `apply_atmosphere_overlay`) and apply it
to any character+environment composite. These don't multiply the asset
count at all - they're infrastructure, built once, reused forever.

**What "done" looks like for Phase 1** (per character): ~8 expressions +
~5 poses = ~13 generated images. For 3 characters: ~39 character images
total, plus ~9 shared environments = 48 generated assets to have a
functioning combinatorial library. That is a finite, achievable one-time
build, not an open-ended commitment - consistent with "build like an
animation studio's asset library, not per-video assets," while staying far
short of "thousands of assets."

---

## 4. Weekly Production Requirements

Assumptions stated explicitly (these are planning estimates, not
measurements - they should be corrected once real data exists):
- Daily Short: ~12-16 sentences/visual-beats each -> ~7 shorts/week = ~91-112 beats/week.
- Weekly long-form: ~15-18 minutes, roughly 40-60 visual beats (longer
  average shot length than Shorts) -> ~50 beats/week.
- **Total: ~140-160 visual beats/week** to cover.

Once the Phase 1 library (48 assets) exists, the overwhelming majority of
weekly beats are **served by existing combinations** (a beat needing
"Alex, worried, bedroom at night" is already covered by existing pose +
expression + environment, composited fresh each time - zero new asset
cost). New-asset need per week should be small and targeted:

- **Estimated new assets/week once Phase 1 is live: 5-10.** These come
  from beats that don't fit any existing expression/pose/environment
  combination - e.g. a script beat needing "Maya laughing at a party" when
  no "social/group" environment or "laughing" expression exists yet for
  her. This is the "identify missing assets" step from Section 1's
  pipeline - it should produce a short, specific list, not a vague
  "more assets" request.
- Long-form gets priority on new "hero" assets (2-4/week) since it carries
  more narrative weight per shot and viewers spend more time on each
  scene - Shorts should default to recombining existing assets unless a
  beat genuinely can't be covered.
- If the missing-asset queue is consistently longer than ~10-12/week,
  that's a signal the Phase 1 library was undersized for the topic mix
  actually being produced - expand the environment or expression set,
  not the character count (per Section 2's "don't add characters
  preemptively" rule).

This keeps the system need-driven: the queue is generated FROM actual
scripts each week, not from a pre-built content calendar guessing what
might be needed.

---

## 5. Automated Data Analysis System

**Weekly, not per-video.** Reviewing every video individually doesn't
scale and invites noisy, small-sample decisions. A weekly rollup is the
right cadence to match the existing weekly-trend-research task already
scheduled in this project.

**Data pulled (YouTube Analytics API - free, part of the same Google
Cloud project already used for upload)**:
- Views, average view duration, average percentage viewed (retention)
- Click-through rate on impressions (thumbnail/title effectiveness,
  long-form specifically since Shorts don't get meaningful CTR data)
- Likes/comments/shares (engagement rate, not raw counts - normalize by
  views so a 5k-view video isn't penalized against a 50k-view one)
- Subscriber gain attributable to the video (YouTube Analytics exposes
  this per-video)

**Tagging requirement for analysis to be possible at all**: every
published video needs structured metadata logged at publish time (this
extends the existing Google Sheet dashboard, doesn't replace it) -
which character(s) appeared, which environment(s), which pillar/topic,
which story structure was used (e.g. "decision-beat," "direct address,"
"scenario dramatization"). Without this tagging, "which character
performs best" is not an answerable question no matter how much view data
exists - this is the one new piece of data-collection work required, and
it's cheap (a few extra columns filled in during the existing publish
step).

**Weekly report contents** (auto-generated, written to the project the
same way `weekly-trend-reports.md` already works):
- Top/bottom 3 videos by retention, with their character/environment/
  topic tags surfaced alongside
- Simple correlation view: average retention broken down by character
  used, by environment used, by pillar, by story structure - not
  sophisticated statistics, just grouped averages, since the sample size
  (7 shorts + 1 long-form/week) doesn't support anything fancier without
  months of accumulated data
- Explicit call-out when a pattern is based on too little data to trust
  yet (e.g. "Alex has only appeared in 2 videos - too early to conclude
  anything") - this matters so the self-improvement loop in Section 6
  doesn't overfit to noise in week 1.

---

## 6. Self-Improving Content Workflow

The loop: **Publish -> Collect data -> Analyze -> Find winning patterns ->
Next week's asset/topic priorities -> Produce better videos.**

Concretely, the weekly report from Section 5 feeds two decisions, made by
whoever reviews the report (this should stay a human-approved step, not
fully autonomous, at least until the pattern-detection has proven
reliable over several months):

1. **Topic priority for next week's idea-scoring** - if "Byte + night-city
   + emotional-psychology" is outperforming, the existing idea-scoring
   prompt gets a nudge toward more of that combination (this is a small,
   safe change: adjust scoring weights/prompt bias, not a hard rule).
2. **Asset queue priority** - if a character/environment/expression
   combination is proving popular, generate MORE variations within that
   combination (more night-city environments, more of Byte's emotional
   expressions) before spending budget on less-proven territory. If
   something underperforms consistently (multiple weeks, not one data
   point), stop generating new assets for that combination - existing
   assets aren't deleted, just deprioritized for new production.

**Guardrails, stated explicitly**: don't let this loop collapse the
channel onto one lucky combination too fast - psychology content needs
topic breadth to keep growing subscribers, and character variety is part
of what makes the channel feel alive rather than repetitive (this was
flagged already in the existing character-bible doc: "do not build toward
reliance on ONE character or ONE fixed visual style"). The weekly report
should always include a small "exploration" allocation (roughly 1 short/
week) on an underused character/topic combination specifically to keep
generating fresh data rather than only ever confirming existing winners.

---

## What this document deliberately does NOT do

It doesn't commit to a start date for Phase 2 (character system
re-enable) - that depends on the background-removal/compositing work
actually being solved properly (this week's prototype attempts showed
real gaps: grounding, no walk-cycle, no purpose-built environments - see
`PROTOTYPE_CINEMATIC_SHOT_NOTES.md`). This document is the strategy for
what to build and in what order; it is not a claim that all of it is
ready today.
