"""Release-channel resolution for Dev Fleet's per-lane worktrees.

The defect these tests exist to prevent is a SILENT one: a lane that resolves to
the wrong ref still produces a worktree that builds, boots as a pod and serves a
dashboard, so "stable" showing a prerelease looks exactly like success. Every
assertion below is therefore about the resolver's *answer*, not about whether it
ran.

Tag fixtures use this repository's real tag vocabulary (``v0.5.0``,
``v0.6.0-insider.6``) so a rename of the release workflow's tag shape shows up
here rather than in production.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.apps.builtins.dev_fleet import release_channel_pin as rcp
from kiro_crew.apps.builtins.dev_fleet import repository, runtime

# Newest first, which is what `--sort=-creatordate` gives the resolver. The
# interleaving is the point: insider's tip is NEWER than stable's tip, so a
# resolver that ignored the lane filter and simply took the first line would
# return an insider tag for stable — and that is the real shape of this repo's
# tag history, not a contrived case.
_TAGS_NEWEST_FIRST = [
    "v0.6.0-insider.6",
    "v0.6.0-insider.5",
    "v0.5.0",
    "v0.5.0-insider.11",
    "v0.4.1",
]


def _fake_git(tags: list[str] | None = None, *, oids: dict[str, str] | None = None):
    """A git stand-in answering only what the resolver asks."""
    tags = _TAGS_NEWEST_FIRST if tags is None else tags
    oids = oids or {}

    async def fake_run(cmd, **kw):
        if "tag" in cmd and "--list" in cmd:
            return 0, "\n".join(tags) + "\n", ""
        if "rev-parse" in cmd:
            target = cmd[-1]
            if target in oids:
                return 0, oids[target] + "\n", ""
            # Deterministic stand-in oid derived from the ref, so assertions can
            # tie a returned oid back to the ref it was resolved from.
            return 0, f"oid-{target}\n", ""
        return 1, "", f"unexpected argv: {cmd}"

    return fake_run


@pytest.fixture(autouse=True)
def _pinned_repo(monkeypatch):
    monkeypatch.setattr(repository, "_repo", lambda: "/fake/repo")
    monkeypatch.setattr(repository, "_UPSTREAM_REMOTE", "origin")


# --------------------------------------------------------------------------
# naming
# --------------------------------------------------------------------------
@pytest.mark.parametrize("lane", ["stable", "insider"])
def test_worktree_name_carries_the_lane_under_the_shared_prefix(lane):
    """One naming rule, on the backend only.

    The fleet payload publishes this string per lane, so the frontend never
    rebuilds it — a second copy of the prefix rule is what would let a change to
    ``WORKTREE_PREFIX`` desync a row's label from the directory it names.
    """
    assert rcp.worktree_name(lane) == f"{rcp.WORKTREE_PREFIX}{lane}"
    assert rcp.worktree_name(lane).endswith(lane)


def test_worktree_name_is_a_valid_pod_identity():
    """The basename becomes ``kirocrew-pod@<name>.service``.

    A name that fails the pod name rule would surface as a pod that cannot be
    brought up — long after the worktree was created and built.
    """
    from kiro_crew.pod.runtime import _NAME_RE

    for lane in rcp.LANES:
        assert _NAME_RE.match(rcp.worktree_name(lane)), lane


def test_worktree_path_is_a_sibling_of_the_primary_checkout():
    # Compared as PATHS, not as strings. ``worktree_path`` returns a native path,
    # so a POSIX string literal here asserted the separator rather than the
    # placement and failed on Windows for a correct return value. What the name
    # of this test actually claims is sibling-ness, so pin that instead.
    repo = Path("/Users/me/Projects/KiroCrew")
    got = Path(rcp.worktree_path(str(repo), "stable"))
    assert got == repo.parent / "release-channel-stable"
    assert got.parent == repo.parent
    assert got.name == rcp.worktree_name("stable")


# --------------------------------------------------------------------------
# tag -> lane classification
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("tag", "lane"),
    [
        ("v0.5.0", "stable"),
        ("v0.6.0-insider.6", "insider"),
        ("v0.6.0-rc.2", "insider"),
        ("v1.2.3-nightly.20260910", "nightly"),
    ],
)
def test_tag_lane_defers_to_the_shared_classifier(tag, lane):
    assert rcp.tag_lane(tag) == lane


@pytest.mark.parametrize("tag", ["main", "v1.2", "release-0.5.0", "v0.5.0.1", ""])
def test_tag_lane_rejects_non_release_tags(tag):
    assert rcp.tag_lane(tag) is None


def test_tag_lane_agrees_with_release_channel_module():
    """No second copy of the lane rule.

    A private rule here would drift from ``release_channel.channel`` the first
    time the release workflow adds a prerelease spelling, and the drift would be
    invisible: both answers are plausible strings.
    """
    from kiro_crew import release_channel

    for tag in _TAGS_NEWEST_FIRST:
        assert rcp.tag_lane(tag) == release_channel.channel(tag[1:])


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_resolve_stable_skips_newer_prerelease_tags(monkeypatch):
    """The core discriminator: insider's tip is newer, stable must not take it."""
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git())
    got = await rcp.resolve("stable")
    assert got["ok"] is True
    assert got["tag"] == "v0.5.0"
    assert got["ref"] == "refs/tags/v0.5.0"
    assert got["version"] == "0.5.0"


@pytest.mark.asyncio
async def test_resolve_insider_takes_newest_prerelease(monkeypatch):
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git())
    got = await rcp.resolve("insider")
    assert got["tag"] == "v0.6.0-insider.6"
    assert got["version"] == "0.6.0-insider.6"


@pytest.mark.asyncio
async def test_resolve_stable_orders_by_version_not_by_tag_date(monkeypatch):
    """A backport cut AFTER a newer line must not become the stable tip.

    ``v0.4.2`` tagged after ``v0.5.0`` is ordinary release practice (a patch on an
    older line), and it is what breaks date ordering: it is the newest tag by date
    and an older release by version. A re-pushed or re-created tag does the same
    thing — it carries today's date for last year's release. Stable users are on
    ``v0.5.0``, so that is what the stable row must pin.
    """
    monkeypatch.setattr(
        runtime,
        "_run_cmd",
        _fake_git(["v0.4.2", "v0.5.0", "v0.4.1"]),  # newest-first BY DATE
    )
    got = await rcp.resolve("stable")
    assert got["tag"] == "v0.5.0"


@pytest.mark.asyncio
async def test_resolve_stable_compares_version_parts_numerically(monkeypatch):
    """``v0.10.0`` beats ``v0.9.0`` — a string sort would get this backwards."""
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git(["v0.9.0", "v0.10.0"]))
    got = await rcp.resolve("stable")
    assert got["tag"] == "v0.10.0"


@pytest.mark.asyncio
async def test_resolve_insider_keeps_creation_order(monkeypatch):
    """Insider must NOT be version-sorted: the two prerelease spellings the
    release workflow publishes (``-insider.N`` and ``-rc.N``) have no precedence
    stated anywhere in this repo, so publication order is the only fact available
    — and for a prerelease feed it is also the right one. ``-rc.1`` listed first
    is the last thing the lane pushed out, even though ``-insider.9`` would sort
    above it under any suffix rule this module invented.
    """
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git(["v0.7.0-rc.1", "v0.7.0-insider.9"]))
    got = await rcp.resolve("insider")
    assert got["tag"] == "v0.7.0-rc.1"


def test_lane_candidates_is_deterministic_under_a_reordered_listing():
    """Same tags, different listing order → same stable answer.

    The version sort has to be total for the row to stop flickering between two
    tags as git's date ordering shifts under a re-fetch.
    """
    tags = ["v0.4.2", "v0.5.0", "v0.4.1"]
    first = rcp._lane_candidates("stable", tags)
    assert first == rcp._lane_candidates("stable", list(reversed(tags)))
    assert first[0] == "v0.5.0"


def test_lanes_are_the_tagged_subset_and_exclude_nightly():
    """A lane must name a specific PUBLISHED build, which needs a tag.

    ``nightly.yml`` builds from ``main`` HEAD and tags nothing, so the newest ref
    a nightly lane could name is ``<remote>/main`` — main *now*, not what nightly
    shipped, and where the primary checkout already sits after Sync. A row
    promising the former while showing the latter is worse than no row.
    Derived from ``RELEASE_CHANNELS`` rather than re-listed, so the two cannot
    drift apart.
    """
    assert "nightly" in rcp.RELEASE_CHANNELS
    assert "nightly" not in rcp.LANES
    assert set(rcp.LANES) == set(rcp.RELEASE_CHANNELS) - {"nightly"}


@pytest.mark.asyncio
async def test_resolve_refuses_nightly_as_unresolvable(monkeypatch):
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git())
    got = await rcp.resolve("nightly")
    assert got["ok"] is False
    assert "nightly" in got["error"]


@pytest.mark.asyncio
async def test_resolve_reports_oid_of_the_tagged_commit(monkeypatch):
    """Resolution must peel to a commit.

    An annotated tag's own object is not a commit, and handing a tag object to
    ``git worktree add`` / ``checkout --detach`` puts the worktree somewhere the
    behind-count cannot be computed from.
    """
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git())
    got = await rcp.resolve("stable")
    assert got["oid"] == "oid-refs/tags/v0.5.0^{commit}"


@pytest.mark.asyncio
async def test_resolve_rejects_an_unknown_lane(monkeypatch):
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git())
    got = await rcp.resolve("beta")
    assert got["ok"] is False
    assert "beta" in got["error"]


@pytest.mark.asyncio
async def test_resolve_reports_an_empty_lane_rather_than_guessing(monkeypatch):
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git(tags=["v0.6.0-insider.6"]))
    got = await rcp.resolve("stable")
    assert got["ok"] is False
    assert "no stable release tag" in got["error"]


@pytest.mark.asyncio
async def test_resolve_skips_a_tag_that_will_not_resolve(monkeypatch):
    """One broken local ref must not report the whole lane as unpublished."""

    async def fake_run(cmd, **kw):
        if "tag" in cmd and "--list" in cmd:
            return 0, "v0.6.0\nv0.5.0\n", ""
        if "rev-parse" in cmd:
            if "v0.6.0^{commit}" in cmd[-1]:
                return 1, "", "bad object"
            return 0, f"oid-{cmd[-1]}\n", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.resolve("stable")
    assert got["ok"] is True
    assert got["tag"] == "v0.5.0"


@pytest.mark.asyncio
async def test_resolve_classifies_each_tag_exactly_once(monkeypatch):
    """There is no second classification to re-check, and that is deliberate.

    An earlier revision carried a ``lane_check`` field claiming the resolver
    verified its own answer. It could not: ``tag_lane(tag)`` IS
    ``channel(tag[1:])``, selection already filters on it, and ``version`` is that
    same ``tag[1:]`` — so the comparison was a tautology that only ever agreed,
    and the test which "proved" it fired had to stub BOTH sides to produce a
    disagreement. A guard asserted against a stub of itself is not a guard.
    This test pins the honest property instead: one classifier call per candidate
    tag, and no ``lane_check`` in the result.
    """
    calls: list[str] = []
    real = rcp._release_channel.channel
    monkeypatch.setattr(rcp._release_channel, "channel", lambda v: (calls.append(v), real(v))[1])
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git(tags=["v0.5.0"]))
    got = await rcp.resolve("stable")
    assert got["ok"] is True
    assert "lane_check" not in got
    assert calls == ["0.5.0"]


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fetch_refs_is_additive_and_never_prunes_tags(monkeypatch):
    """The fetch must not be able to delete a tag the operator authored.

    Measured on git 2.54: the only fetch forms that drop a remotely-deleted tag
    (``--prune --prune-tags`` with no ``--tags``, or an explicit
    ``+refs/tags/*:refs/tags/*`` under ``--prune``) delete every local-only tag
    with it, and a pruned tag ref is in no reflog. So this asserts the ABSENCE of
    both pruning flags: the cost of retraction coverage by this route is the
    operator's own tags, which is the worse trade.
    """
    seen: list[list[str]] = []

    async def fake_run(cmd, **kw):
        seen.append(cmd)
        return 0, "", ""

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    assert await rcp.fetch_refs("/fake/repo") is None
    assert seen and "--tags" in seen[0]
    assert "--prune-tags" not in seen[0]
    assert "--prune" not in seen[0]


@pytest.mark.asyncio
async def test_fetch_refs_returns_a_redacted_error(monkeypatch):
    async def fake_run(cmd, **kw):
        return 1, "", "fatal: could not read Username for 'https://github.com'"

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    err = await rcp.fetch_refs("/fake/repo")
    assert err and "fatal" in err


# --------------------------------------------------------------------------
# worktree position relative to the lane tip
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_worktree_state_counts_behind_the_channel_tip_not_main(monkeypatch):
    """``behind`` on a channel row means distance from the lane tip.

    The fleet's usual behind-count is against ``BASE_BRANCH``; on a release
    worktree that number is large and meaningless, because the worktree is not
    trying to track main.

    Also pins the defect that made the row's badge dishonest: ``version`` is the
    release the tree IS on (``v0.5.0``), never the lane's resolved tip
    (``v0.6.0``). A row fed the resolved version renames itself to every new
    release as it ships while the checkout stays put.
    """
    calls: list[list[str]] = []

    async def fake_run(cmd, **kw):
        calls.append(cmd)
        if "symbolic-ref" in cmd:
            return 1, "", "not a symbolic ref"
        if "rev-parse" in cmd:
            return 0, "head-oid\n", ""
        if "rev-list" in cmd:
            return 0, "3\n", ""
        if "tag" in cmd:
            return 0, "v0.5.0\n", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.worktree_state("/wt", {"oid": "tip-oid", "lane": "stable", "version": "0.6.0"})
    assert got["behind"] == 3
    assert got["at_tip"] is False
    assert got["detached"] is True
    assert got["version"] == "0.5.0"
    ranges = [c[-1] for c in calls if "rev-list" in c]
    assert ranges == ["head-oid..tip-oid"]


@pytest.mark.asyncio
async def test_behind_worktree_on_a_foreign_tag_reports_no_release(monkeypatch):
    """A tag from ANOTHER lane at the same commit must not rename the row.

    A commit can carry several tags. Taking the first would let an insider tag
    that happens to share the commit label the stable row, so the lookup filters
    on the lane — and when nothing matches, ``None`` is the honest answer rather
    than falling back to the tip the tree does not contain.
    """

    async def fake_run(cmd, **kw):
        if "symbolic-ref" in cmd:
            return 1, "", ""
        if "rev-parse" in cmd:
            return 0, "head-oid\n", ""
        if "rev-list" in cmd:
            return 0, "2\n", ""
        if "tag" in cmd:
            return 0, "v0.6.0-insider.4\nsome-local-marker\n", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.worktree_state("/wt", {"oid": "tip-oid", "lane": "stable", "version": "0.6.0"})
    assert got["behind"] == 2
    assert got["version"] is None


@pytest.mark.asyncio
async def test_worktree_state_reports_at_tip_without_counting(monkeypatch):
    async def fake_run(cmd, **kw):
        if "symbolic-ref" in cmd:
            return 1, "", ""
        if "rev-parse" in cmd:
            return 0, "same-oid\n", ""
        raise AssertionError(f"should not have run: {cmd}")

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.worktree_state("/wt", {"oid": "same-oid", "lane": "stable", "version": "0.6.0"})
    assert got["at_tip"] is True
    assert got["behind"] == 0
    # At the tip the tree is ON the tip's release by definition, so the version
    # comes from the already-resolved answer. The `raise` above is the assertion
    # that matters: no `git tag` call is spent re-deriving what we know.
    assert got["version"] == "0.6.0"


@pytest.mark.asyncio
async def test_worktree_state_reports_an_attached_head_as_not_detached(monkeypatch):
    """The guard against adopting a coincidentally-named worktree.

    A user's own ``release-channel-stable`` branch checkout must not gain lane
    controls on the strength of its name; only a detached checkout at a resolved
    ref is a channel worktree.
    """

    async def fake_run(cmd, **kw):
        if "symbolic-ref" in cmd:
            return 0, "refs/heads/release-channel-stable\n", ""
        if "rev-parse" in cmd:
            return 0, "head-oid\n", ""
        if "rev-list" in cmd:
            return 0, "1\n", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.worktree_state("/wt", {"oid": "tip-oid"})
    assert got["detached"] is False
