# Cinematic character-in-environment prototype (2026-07-23)

Exploratory work, NOT wired into pipeline.py yet. The user asked for
character+environment shots to feel like one continuous cinematic scene
(character composited into the background, camera movement, parallax,
walking motion) instead of the current slideshow (character image cut to
stock clip cut to another character image). This tests the core technique
on one shot before any pipeline integration.

## What this proves out

1. **Background removal** - none of Byte's existing image assets have
   transparency; they're flat opaque renders on a plain studio backdrop.
   `character_assets/Characters/Byte/Poses/*.png` backgrounds are a uniform
   near-white/beige, so a flood-fill from the four corners + a small
   tolerance (see the one-off script this was built with, not currently
   checked in as a reusable module) cuts out a clean transparent PNG with
   no visible halo. This same approach should work for any character asset
   shot on a similarly plain/flat background - it will NOT work for the
   "Scenes" assets (already-illustrated-into-an-environment shots) since
   those have no flat backdrop to key out.

2. **Layered composite via ffmpeg filter_complex** (see `filter.txt` for
   the exact graph, not checked in as final - written for one specific
   shot):
   - Background layer: an Environments plate, scaled/cropped to
     1080x1920, with a slow zoompan camera push-in (same technique
     `render_environment_motion_clip` in character_assets.py already uses
     for background-only shots).
   - Character layer: the background-removed cutout, overlaid on top with
     x/y position animated over time (linear drift + a small sine bob) to
     fake basic walking/stepping motion, plus a light color grade
     (desaturate + blue color-balance push) so the flat cel-shaded art
     doesn't look pasted-on against the painted environment.
   - Foreground rain layer: a procedurally generated diagonal streak
     pattern (ffmpeg `geq` expression, NOT a real rain sim) rendered to a
     grayscale mask, turned into a white RGBA layer via `alphamerge`, and
     composited on top with the plain `overlay` filter (not `blend=screen`
     - screen-blending directly on YUV planes shifts the whole frame's
       chroma/hue, confirmed by an earlier broken attempt that turned the
       whole frame magenta; `overlay` respects alpha per-pixel instead and
       doesn't touch chroma outside the streak pixels).

## What this does NOT solve yet (real gaps, not polish)

- **No walk-cycle**: Byte has exactly one mid-stride still. The prototype
  fakes motion with a horizontal drift + vertical bob, but his legs don't
  actually move - looks like sliding, not walking. A believable walk needs
  either several stride poses to blend/cut between, or dialing back the
  ambition to a more subtle idle-sway shot instead of full locomotion.
- **Grounding**: this environment plate (`byte_env_bedroom_rain_night.png`)
  has almost no visible floor - the desk fills most of the frame - so the
  character ends up floating in front of furniture rather than standing on
  a floor plane. Real shots need the character's scale/position planned
  against where the environment actually has floor space, which means
  environment art and character blocking need to be designed together per
  shot, not generically reused.
- **No rainy-street (or other) environment plates exist yet** - this reused
  the one existing rain environment (a bedroom) since it was the closest
  match on hand.

## Next steps if this direction is approved

1. Build a reusable background-removal utility (module, not a one-off
   script) covering all characters' Poses/Expressions assets.
2. Get a small set (3-4) of stride/idle frames per character so motion
   reads as real movement, not a single frozen image drifting.
3. Commission/produce Environment plates with specific character blocking
   in mind (where they stand, how big, which part of frame is floor).
4. Only then integrate this as a real alternative path in gather_clips(),
   behind its own flag, so it can be tested against the current
   image-cutout + stock-clip approach before replacing it.
