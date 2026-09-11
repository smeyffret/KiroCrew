/**
 * A crew's face when the crew wears an APPEARANCE PACK — art somebody else drew,
 * served per state from the crew appearance library.
 *
 * `CrewAvatar` composes the seeded ghost itself and draws an uploaded picture as
 * an `<img>`. A pack is neither: its art may be an SVG, a Lottie document or a
 * row of a sprite sheet, and the format is a property of the SLOT rather than of
 * the pack, so which player draws it can only be known after reading the pack.
 * That read is what this component owns, and it is why the pack tier is a
 * component rather than another `src` in `CrewAvatar`.
 *
 * Three tiers, one per format:
 *   svg     — an `<img>` pointed at the per-slot route, exactly as before.
 *   lottie  — `LottieRenderer`, looping.
 *   sprite  — `SpriteRenderer`, stepping the row this slot occupies.
 *
 * ANIMATION IS BOUNDED BY VISIBILITY. A roster draws one avatar per crew at
 * 18-38px, so a page can hold dozens; a Lottie timeline or a sprite loop per row
 * would spend real per-frame work on faces scrolled far out of view. Each avatar
 * therefore observes its own box and animates only while it intersects the
 * viewport — off screen it holds frame 0. The rule is visibility rather than a
 * size threshold on purpose: the dense roster's own avatars are 38px, so any
 * threshold low enough to animate the crew card would animate every row in the
 * list at once, which is the cost this bound exists to remove.
 *
 * A failed read (after React Query's one retry) reports through `onError` and
 * renders nothing, so `CrewAvatar` answers with the seeded ghost — the same thing
 * a deleted pack already shows. A query left in error state has no data, so it
 * is refetched on the next mount, focus or reconnect: a gateway blip is not a
 * permanent ghost.
 *
 * `prefers-reduced-motion` holds every avatar on its first frame regardless of
 * visibility. Both players are JS-driven timelines, so the stylesheet's global
 * reduced-motion rule cannot reach them; the preference has to be read here.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { LottieRenderer } from './LottieRenderer'
import { SpriteRenderer } from './SpriteRenderer'
import { packSlotUrl } from '../../lib/appearancePacks/library'
import { resolveSlot, spriteRowFor } from '../../lib/appearancePacks/detail'
import { usePackDetail } from '../../hooks/usePackDetail'

/** The user's motion preference, read live: it is a system setting that can flip
 *  while the page is open, and a paused timeline must follow it. */
function useReducedMotion(): boolean {
  const query = () =>
    typeof window !== 'undefined' && typeof window.matchMedia === 'function'
      ? window.matchMedia('(prefers-reduced-motion: reduce)')
      : null
  const [reduced, setReduced] = useState(() => query()?.matches ?? false)
  useEffect(() => {
    const mq = query()
    if (!mq) return
    const onChange = () => setReduced(mq.matches)
    onChange()
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])
  return reduced
}

export interface PackAvatarProps {
  /** The pack id from the crew's `avatar` record — already validated by
   *  `packAvatarFrom`, and never the built-in `kiro-ghost` (whose art ships in
   *  this bundle and is composed locally). */
  id: string
  /** Which reaction to draw. Resolved through the pack's own fallback chain, so
   *  a pack that draws only `idle` still answers every state. */
  state: string
  /** Rendered edge length in px. The art fits this box. */
  size: number
  className?: string
  /** Fired when the pack cannot be read or draws nothing for any state, BEFORE
   *  the caller's fallback renders — so a blank face is never reported as saved
   *  fine. */
  onError?: () => void
}

export default function PackAvatar({ id, state, size, className = '', onError }: PackAvatarProps) {
  // One read per pack per session, shared across every avatar wearing it, retried
  // once, and re-read for every mounted subscriber when the Library invalidates
  // the key. React Query owns all of that; this component only asks.
  const { data: detail, isError: readFailed } = usePackDetail(id)
  /** The RENDERER could not draw the art it was handed (broken image, malformed
   *  clip, sheet without the row). Distinct from the read failing. */
  const [failed, setFailed] = useState(false)
  // The box is mounted even while the art is not, so the observer has something
  // to watch from the first commit rather than only once the read lands.
  const boxRef = useRef<HTMLSpanElement>(null)
  const [visible, setVisible] = useState(false)
  const reducedMotion = useReducedMotion()
  // Animate only when on screen, when the user has not asked for less motion,
  // AND when the state is a reaction. `idle` holds its first frame: a roster
  // holds dozens of these faces, and a dozen looping idles make motion the
  // wallpaper of the page, so motion is reserved for something happening —
  // a turn running, a turn done, an error — which is what a reaction is.
  const playing = visible && !reducedMotion && state !== 'idle'

  // A renderer failure is about ONE pack's art: a different id gets a fresh
  // chance rather than inheriting the previous pack's verdict.
  useEffect(() => {
    setFailed(false)
  }, [id])

  useEffect(() => {
    const node = boxRef.current
    // No IntersectionObserver (an old engine, a test env that removed it) means
    // no visibility signal, so animate: a still avatar everywhere is a worse
    // regression than an unbounded one on an engine nobody ships.
    if (!node || typeof IntersectionObserver === 'undefined') {
      setVisible(true)
      return
    }
    const observer = new IntersectionObserver((entries) => {
      // Latest entry only. A burst of records for one target is a scroll, and
      // the last one is where it came to rest.
      const last = entries[entries.length - 1]
      if (last) setVisible(last.isIntersecting)
    })
    observer.observe(node)
    return () => observer.disconnect()
  }, [])

  // A pack that draws NOTHING for this state even after fallback is as broken as
  // one that could not be read — both leave the crew faceless, so both take the
  // ghost. Resolved before the error report below so one effect covers both.
  const slot = useMemo(() => (detail ? resolveSlot(detail, state) : null), [detail, state])
  const broken = failed || readFailed || (detail !== undefined && slot === null)

  // Reported from an effect rather than during render: `onError` is a caller's
  // state write (`CrewAvatar` remembers the failure, the crew editor shows a
  // warning), and doing that in a render body updates another component
  // mid-render.
  useEffect(() => {
    if (broken) onError?.()
  }, [broken, onError])

  // Stable, because `LottieRenderer` takes it as an effect dependency: a fresh
  // identity per render would destroy and reload the animation every render.
  const reportFailed = useCallback(() => setFailed(true), [])

  const box = `shrink-0 overflow-hidden rounded-md border border-border bg-bg-elevated ${className}`

  if (broken) return null

  // Before the read lands there is nothing to draw and no way to know which
  // player will draw it, so the box holds its space rather than flashing a ghost
  // that the art then replaces. (A read that FAILED is `broken` above, so this is
  // only ever the pending state.)
  if (!detail || !slot) {
    return (
      <span
        ref={boxRef}
        aria-hidden="true"
        // `.skeleton` shimmers, so a slow or dead read reads as loading rather
        // than as a blank face for the length of the retry window.
        className={`${box} skeleton`}
        style={{ width: size, height: size, display: 'inline-block' }}
        data-testid="pack-avatar-pending"
      />
    )
  }

  const art = detail.animations[slot]

  return (
    <span
      ref={boxRef}
      aria-hidden="true"
      className={box}
      style={{ width: size, height: size, display: 'inline-block', lineHeight: 0 }}
      data-testid={`pack-avatar-${art.format}`}
      data-pack-slot={slot}
      data-pack-playing={playing ? 'true' : 'false'}
    >
      {art.format === 'lottie' ? (
        <LottieRenderer
          animationData={art.content}
          width={size}
          height={size}
          loop
          autoplay={playing}
          // Malformed art is a load failure like a missing file: the importer only
          // checks that a pack's `.json` is non-empty, so a clip this player
          // cannot parse reaches here and must fall back rather than draw nothing.
          onError={reportFailed}
        />
      ) : art.format === 'sprite' ? (
        <SpriteRenderer
          // The sheet as an IMAGE, from the slot route — which base64-decodes it
          // to `image/png`. The inlined `content` is base64 text, and a canvas
          // cannot draw text.
          src={packSlotUrl(id, slot)}
          // Each of these is a positive finite number or absent — `packDetailFrom`
          // drops anything else — so a served `"0"` cannot reach the renderer's
          // frame count as `Infinity`. Absent falls to the avatar's own box for a
          // dimension and to SpriteRenderer's default for the rate.
          frameWidth={detail.sprite?.frameWidth ?? size}
          frameHeight={detail.sprite?.frameHeight ?? size}
          fps={detail.sprite?.fps}
          displaySize={size}
          row={spriteRowFor(detail, slot)}
          playing={playing}
          // A sheet that will not decode is a load failure like a missing file:
          // report it so the crew gets the ghost rather than a transparent tile.
          onError={reportFailed}
        />
      ) : (
        <img
          // The per-slot route, not the inlined `content`: it carries the
          // inert-SVG content policy the detail route's JSON body does not, and
          // it is what the picker's thumbnails already request, so the browser
          // cache is shared with them.
          src={packSlotUrl(id, slot)}
          alt=""
          aria-hidden="true"
          width={size}
          height={size}
          // object-contain, not cover: a pack's art is drawn to its own frame and
          // cropping it would cut the character's head off. The picture tier
          // crops because the client squares an upload before sending it.
          style={{ width: size, height: size, objectFit: 'contain' }}
          onError={reportFailed}
        />
      )}
    </span>
  )
}
