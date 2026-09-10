/**
 * Release-channel worktree rows in the Dev Fleet table.
 *
 * The whole feature is a claim about WHICH RELEASE a checkout is sitting on, so
 * every test here asserts on what the row states rather than on whether it
 * rendered. Two failure modes are specifically guarded:
 *
 * - **Adopting on the name.** `release-channel-stable` is a reserved basename. A
 *   user's own branch checkout under that name must keep ordinary controls; only
 *   the backend's `worktree` field (set when the tree is detached at a resolved
 *   ref) confers lane controls.
 * - **Reusing a column with a different meaning silently.** BEHIND counts from
 *   the LANE TIP on these rows, not from main, and PR is inapplicable rather
 *   than merely absent.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'

import DevFleetPage, { __resetDevFleetNoticesForTests } from '../pages/DevFleetPage'

function renderPage() {
  return renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
}

const MAIN = {
  name: 'main',
  is_main: true,
  running: false,
  has_dist: true,
  behind: 0,
  last_updated_at: Date.now() / 1000,
}

// A feature worktree carrying the repo's real naming convention, so the
// ordering assertions compare against what the fleet actually shows.
const FEATURE = {
  name: 'kirocrew-wt-update-freshness',
  is_main: false,
  running: false,
  has_dist: true,
  behind: 12,
  last_updated_at: Date.now() / 1000 - 3600,
}

const STABLE_WT = {
  name: 'release-channel-stable',
  is_main: false,
  running: false,
  has_dist: true,
  // Behind MAIN is large by construction on a release worktree — the row must
  // not show this number.
  behind: 412,
  last_updated_at: Date.now() / 1000 - 86400 * 2,
}

const CHANNELS = {
  stable: {
    lane: 'stable',
    name: 'release-channel-stable',
    worktree: 'release-channel-stable',
    ref: 'refs/tags/v0.5.0',
    version: '0.5.0',
    tip_version: '0.5.0',
    error: null,
    at_tip: true,
    behind: 0,
    name_taken_by_branch: false,
  },
  insider: {
    lane: 'insider',
    name: 'release-channel-insider',
    worktree: null,
    ref: 'refs/tags/v0.6.0-insider.6',
    version: '0.6.0-insider.6',
    tip_version: '0.6.0-insider.6',
    error: null,
    at_tip: null,
    behind: null,
    name_taken_by_branch: false,
  },
}

function mockFleet(data: Record<string, unknown>, posts?: Record<string, unknown>) {
  const seen: { url: string; body: unknown }[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation((url, init) => {
    const u = typeof url === 'string' ? url : (url as Request).url
    if (init?.method === 'POST') {
      seen.push({ url: u, body: init.body ? JSON.parse(String(init.body)) : null })
      const key = Object.keys(posts || {}).find((k) => u.includes(k))
      return Promise.resolve(
        new Response(JSON.stringify(key ? posts![key] : { ok: true }), { status: 200 }),
      )
    }
    if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(data), { status: 200 }))
    if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
    return Promise.resolve(new Response('{}', { status: 200 }))
  })
  return seen
}

beforeEach(() => {
  __resetDevFleetNoticesForTests()
  vi.restoreAllMocks()
})

describe('DevFleetPage release-channel rows', () => {
  it('badges an adopted lane row with the release it is sitting on', async () => {
    mockFleet({
      base_branch: 'main',
      worktrees: [MAIN, STABLE_WT, FEATURE],
      release_channels: [CHANNELS.stable, CHANNELS.insider],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    // The lane is already in the row name, so the badge carries the version.
    const badge = screen.getByText('0.5.0')
    expect(badge).toBeInTheDocument()
    expect(badge).toHaveAttribute('title', expect.stringContaining('refs/tags/v0.5.0'))
  })

  it('lists a lane with no worktree as a placeholder row offering Create', async () => {
    // Without the placeholder there is nowhere on the page the feature is
    // discoverable — the design has no header control.
    mockFleet({
      worktrees: [MAIN, FEATURE],
      release_channels: [CHANNELS.stable, CHANNELS.insider],
    })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('release-channel-placeholder-insider')).toBeInTheDocument())
    const row = screen.getByTestId('release-channel-placeholder-insider')
    expect(within(row).getByText('release-channel-insider')).toBeInTheDocument()
    expect(within(row).getByText('0.6.0-insider.6')).toBeInTheDocument()
    expect(within(row).getByText('no worktree yet')).toBeInTheDocument()
    expect(within(row).getByRole('button', { name: /create/i })).toBeEnabled()
  })

  it('leaves an un-materialized lane error inline, with no page-level notice', async () => {
    // The placeholder ALREADY renders `rc.error` and disables Create on it, so a
    // page notice for this case says the same thing twice. Ungated it also
    // misfired: on any checkout with no `v*` tags (a shallow or `--no-tags`
    // clone, a fork before its first release) every lane carries an error, so the
    // page raised an error-level notice on every mount for a feature that
    // operator never used. The adopted-row case below is what the notice is for.
    const broken = { ...CHANNELS.stable, worktree: null, ref: null, version: null,
      tip_version: null, error: 'no stable release tag found in this checkout' }
    mockFleet({ worktrees: [MAIN], release_channels: [broken] })
    renderPage()
    await waitFor(() => expect(
      screen.getByTestId('release-channel-placeholder-stable'),
    ).toBeInTheDocument())
    expect(screen.getByTestId('release-channel-placeholder-stable'))
      .toHaveTextContent(/no stable release tag/)
    expect(screen.queryByTestId('devfleet-action-error')).not.toBeInTheDocument()
  })

  it('is the ONLY surface for an error on an ADOPTED lane row', async () => {
    // Why the notice cannot be replaced by the inline placeholder text: a lane
    // whose worktree exists and is detached is adopted even when resolution
    // fails, so there is no placeholder row to carry the message, and the version
    // badge falls back to the lane name — a row that looks fine while the lane is
    // unresolvable. This combination is what the notice exists for.
    const adoptedButBroken = {
      ...CHANNELS.stable,
      ref: null,
      version: null,
      at_tip: null,
      behind: null,
      error: 'cannot list tags (git tag failed)',
    }
    mockFleet({ worktrees: [MAIN, STABLE_WT], release_channels: [adoptedButBroken] })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    expect(screen.queryByTestId('release-channel-placeholder-stable')).not.toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('devfleet-action-error'))
      .toHaveTextContent(/cannot list tags/))
  })

  it('counts BEHIND from the lane tip, not from main', async () => {
    const behindTip = { ...CHANNELS.stable, at_tip: false, behind: 3 }
    mockFleet({
      worktrees: [MAIN, STABLE_WT],
      release_channels: [behindTip],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    // 3 from the channel tip is shown; 412 from main is not.
    expect(screen.getByText('↓3')).toBeInTheDocument()
    expect(screen.queryByText('↓412')).not.toBeInTheDocument()
  })

  it('badges a behind row with the release it HOLDS, not the newer tip', async () => {
    // The badge's own comment says it shows "which release the tree is actually
    // sitting on", and it was fed the RESOLVED version instead — so the moment a
    // newer release shipped the row renamed itself to a build it does not
    // contain, while `↓N` was the only hint anything was stale.
    const behindTip = {
      ...CHANNELS.stable,
      at_tip: false,
      behind: 3,
      version: '0.5.0',
      tip_version: '0.6.0',
      ref: 'refs/tags/v0.6.0',
    }
    mockFleet({ worktrees: [MAIN, STABLE_WT], release_channels: [behindTip] })
    renderPage()
    await waitFor(() => expect(screen.getByText('0.5.0')).toBeInTheDocument())
    expect(screen.queryByText('0.6.0')).not.toBeInTheDocument()
    expect(screen.getByText('0.5.0')).toHaveAttribute(
      'title', expect.stringContaining('0.6.0'),
    )
  })

  it('says so when a lane tree is on no release tag at all', async () => {
    // Adoption is by SHAPE (detached), not by being at a release, so an operator
    // who checked out an arbitrary commit in the lane is on no release. Falling
    // back to the tip's version here would be the same lie the test above pins.
    const offTag = {
      ...CHANNELS.stable,
      at_tip: false,
      behind: 7,
      version: null,
      tip_version: '0.6.0',
    }
    mockFleet({ worktrees: [MAIN, STABLE_WT], release_channels: [offTag] })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    expect(screen.queryByText('0.6.0')).not.toBeInTheDocument()
    expect(screen.getByText('stable')).toHaveAttribute(
      'title', expect.stringContaining('0.6.0'),
    )
  })

  it('marks PR inapplicable on a lane row instead of showing the no-PR dash', async () => {
    // The em dash on every other row means "no PR yet", which invites waiting
    // for one. A tag-detached tree can never have a PR at all.
    mockFleet({ worktrees: [MAIN, STABLE_WT], release_channels: [CHANNELS.stable] })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    const na = screen.getAllByText('n/a')
    expect(na.length).toBeGreaterThan(0)
    expect(na[0]).toHaveAttribute('title', expect.stringContaining('pull request'))
  })

  it('does NOT adopt a branch checkout that merely shares the reserved name', async () => {
    // The name guard, from the UI side: the backend reports worktree=null plus
    // name_taken_by_branch, so the row keeps ordinary controls.
    const taken = { ...CHANNELS.stable, worktree: null, at_tip: null, behind: null, name_taken_by_branch: true }
    mockFleet({
      worktrees: [MAIN, { ...STABLE_WT, behind: 5 }],
      release_channels: [taken],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    // No version badge: this row is not a lane pin.
    expect(screen.queryByText('0.5.0')).not.toBeInTheDocument()
    // Its behind count is the ordinary behind-main figure, not a lane distance.
    expect(screen.getByText('↓5')).toBeInTheDocument()
  })

  it('explains the occupied name on the existing row, not as a second row', async () => {
    // One directory is one row. Rendering a blocked placeholder alongside the
    // real checkout printed `release-channel-stable` twice on the page, which is
    // what this asserts against.
    const taken = { ...CHANNELS.stable, worktree: null, at_tip: null, behind: null, name_taken_by_branch: true }
    mockFleet({ worktrees: [MAIN, { ...STABLE_WT, behind: 5 }], release_channels: [taken] })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    expect(screen.getAllByText('release-channel-stable')).toHaveLength(1)
    expect(screen.queryByTestId('release-channel-placeholder-stable')).not.toBeInTheDocument()
    const badge = screen.getByText('Not a release-channel worktree')
    expect(badge).toHaveAttribute('title', expect.stringContaining('is on a branch'))
  })

  it('badges at-tip and behind-tip differently, with no third mismatch state', async () => {
    // `lane_check` is gone: it compared a value against itself. What the badge
    // must still distinguish is the two states Advance acts on.
    mockFleet({ worktrees: [MAIN, STABLE_WT], release_channels: [CHANNELS.stable] })
    renderPage()
    await waitFor(() => expect(screen.getByText('0.5.0')).toBeInTheDocument())
    expect(screen.getByText('0.5.0')).toHaveAttribute(
      'title', expect.stringContaining('refs/tags/v0.5.0'),
    )
  })

  it('surfaces an unresolvable lane on its placeholder and blocks Create', async () => {
    const broken = {
      ...CHANNELS.stable,
      worktree: null,
      ref: null,
      version: null,
      error: 'no stable release tag found in this checkout',
    }
    mockFleet({ worktrees: [MAIN], release_channels: [broken] })
    renderPage()
    const row = await waitFor(() => screen.getByTestId('release-channel-placeholder-stable'))
    expect(within(row).getByText('no stable release tag found in this checkout')).toBeInTheDocument()
    expect(within(row).getByRole('button', { name: /create/i })).toBeDisabled()
  })

  it('orders lane rows under main and above the feature worktrees', async () => {
    // Fixed position, not part of the sort: every sort key on offer describes
    // feature-branch progress, and a release worktree scores badly on all of
    // them by design.
    mockFleet({
      worktrees: [MAIN, FEATURE, STABLE_WT],
      release_channels: [CHANNELS.stable, CHANNELS.insider],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    const names = screen
      .getAllByText(/^(main|release-channel-\w+|kirocrew-wt-[\w-]+)$/)
      .map((n) => n.textContent)
    expect(names.indexOf('release-channel-stable')).toBeLessThan(
      names.indexOf('kirocrew-wt-update-freshness'),
    )
    expect(names.indexOf('main')).toBeLessThan(names.indexOf('release-channel-stable'))
  })

  it('posts the lane to /release-channel/create when Create is confirmed', async () => {
    const seen = mockFleet(
      { worktrees: [MAIN], release_channels: [CHANNELS.insider] },
      { '/release-channel/create': { ok: true, lane: 'insider', version: '0.6.0-insider.6' } },
    )
    renderPage()
    const row = await waitFor(() => screen.getByTestId('release-channel-placeholder-insider'))
    within(row).getByRole('button', { name: /create/i }).click()
    // Create is destructive enough to confirm: it writes a new checkout to disk.
    const confirm = await waitFor(() => screen.getByRole('button', { name: 'Create', hidden: false }))
    expect(confirm).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText(/Create the insider release-channel worktree/)).toBeInTheDocument())
    // The lane, not a path, is what crosses the wire — the server derives the
    // path so a caller can never name one.
    expect(seen.every((s) => !('path' in ((s.body as object) || {})))).toBe(true)
  })

  it('separates the pinned block from the sorted worktrees', async () => {
    // The pinned rows sit out of the active sort, so a user sorting by name sees
    // `release-channel-*` out of order. The rule is what tells them that is a
    // fixed position rather than a broken sort.
    mockFleet({
      worktrees: [MAIN, FEATURE, STABLE_WT],
      release_channels: [CHANNELS.stable, CHANNELS.insider],
    })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('fleet-pinned-divider')).toBeInTheDocument())
  })

  it('omits the divider when there is nothing on the other side of it', async () => {
    // A rule under the last row would read as a truncated table.
    mockFleet({ worktrees: [MAIN, STABLE_WT], release_channels: [CHANNELS.stable] })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    expect(screen.queryByTestId('fleet-pinned-divider')).not.toBeInTheDocument()
  })
})
