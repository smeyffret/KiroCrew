"""Install the crew bundle into the paths Kiro Crew actually reads, or refuse.

The crew rides in the image at ``/app/crew-bundle`` (PACKAGING-CONTRACT.md, T3).
The previous design uploaded it to S3 and nothing in the container ever read it,
so ten gates went green while the deployment served a default agent. This module
closes that by construction: the supervisor installs the bundle BEFORE the
backend starts, and refuses to boot unless the named crew is the one installed.

WHERE EACH ENTRY GOES -- verified against the Kiro Crew source at
a Kiro Crew source checkout (0.6.0), not inferred from a plausible
name. ``config_dir()`` and ``data_home()`` resolve to the SAME directory, so a
``<home>/config/`` guess would land two of these where nothing reads them:

* ``agent.json`` -> ``<kiro home>/agents/<crew_name>.json``.
  ``agent_discovery.list_agents`` is THE reader of installed agent specs, keyed by
  the spec's ``name``, and it reads and JSON-parses every ``~/.kiro/agents/*.json``
  on each call. The gateway resolves that directory as ``kiro_home() / "agents"``,
  where ``kiro_home()`` is ``$KIRO_HOME`` or ``~/.kiro``. It is NOT under the data
  home and NOT governed by ``KIROCREW_HOME``: the backend is launched with
  ``KIROCREW_HOME=data_home`` but no ``KIRO_HOME`` (``supervisor/backend.py``),
  so the spec lands under the process HOME. Resolved here the same way rather
  than imported, so this module needs no ``kiro_crew`` install (matching the
  supervisor's other minimal, import-free config reads).

  The gateway's chain reaches that path through a private override-BLIND helper,
  which ``test_host_isolation_floor.py::test_the_ambient_resolver_has_exactly_one_caller``
  keeps to a fixed set of callers. This module is not one of them and must not
  become one, so it is named here by what it computes rather than by its symbol:
  a source-text guard cannot tell a docstring from a call, and it is right not to
  try.

* ``mcp.json`` -> ``<data home>/mcp.json``. The Kiro Crew-scope MCP config is
  ``data_home()/"mcp.json"`` -- the highest-priority source in
  ``mcp_discovery._mcp_sources`` (``mcp_discovery.py:297``,
  ``SCOPE_KIROCREW``) and what ``dashboard/handlers/mcp.py:1620``
  (``_kirocrew_mcp_json`` -> ``data_home()/"mcp.json"``) reads for its
  ``mcpServers`` (``:704``).

* ``skills/`` -> ``<data home>/skills/``. ``skills.py:1422`` ``skills_dir()`` is
  ``config_dir() / SKILLS_DIR_NAME`` and ``SKILLS_DIR_NAME == "skills"``
  (``skills.py:49``); ``config_dir()`` (``config/paths.py:265``) honours
  ``KIROCREW_HOME``, so in the container this is ``<data home>/skills``.

THE DIGEST is recomputed here byte-identically to the producer
(``share-my-crew/build/export/crew_export/bundle.py:78`` ``_bundle_digest``) and
its independent verifier (``build/export/tools/verify_bundle.py:50``
``digest_of``), which agree exactly. A different serialisation would fail every
valid bundle, so ``_content_digest`` is copied verbatim rather than reinvented.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .. import common
from ..common import Settings

log = logging.getLogger("container.supervisor")

#: The four entries PACKAGING-CONTRACT.md freezes, with the on-disk kind each
#: must be. ``skills`` is a directory (may be empty but MUST exist); the rest are
#: files (``mcp.json`` may be ``{}`` but MUST exist).
BUNDLE_ENTRIES: tuple[tuple[str, str], ...] = (
    ("manifest.json", "file"),
    ("agent.json", "file"),
    ("mcp.json", "file"),
    ("skills", "dir"),
)

#: Written at the data-home root on a successful install. For a human reading the
#: logs; T4's gate proves the crew from the image digest, not from this file.
INSTALLED_MARKER = ".smc-crew-installed.json"


def default_kiro_agents_dir() -> Path:
    """Where kiro-cli reads agent specs: ``<kiro home>/agents``.

    Mirrors ``kiro_crew.config.paths.kiro_home`` (``config/paths.py:510``):
    ``$KIRO_HOME`` if set, else ``~/.kiro``, then ``/agents``. Deliberately NOT
    under the data home -- see the module docstring. The one behaviour not
    mirrored is ``kiro_home``'s rejection of a system-directory ``$KIRO_HOME``;
    that guards a pathological override the container never sets, and copying it
    would only widen this module's surface.
    """
    override = os.environ.get("KIRO_HOME")
    home = Path(override).expanduser() if override else Path.home() / ".kiro"
    return home / "agents"


def _content_digest(root: Path) -> str:
    """Recompute the bundle content digest, byte-identical to the producer.

    Copied verbatim from ``crew_export/bundle.py:78`` and ``verify_bundle.py:50``
    (which agree): sha256 over sorted ``[rel_posix, sha256(bytes)]`` rows for
    every file except the top-level ``manifest.json`` (it carries the digest),
    then sha256 of the compact-JSON payload, prefixed ``"sha256:"``. The
    ``sorted(root.rglob("*"))`` over ``Path`` objects and the
    ``separators=(",", ":")`` compaction are both load-bearing: change either and
    the digest of a valid bundle stops matching.
    """
    rows: list[list[str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel == "manifest.json":
            continue
        rows.append([rel, hashlib.sha256(path.read_bytes()).hexdigest()])
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_json_object(path: Path, label: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise common.ConfigError(
            f"bundle check failed [{label} is readable JSON]: {path} could not be "
            f"read as JSON ({exc})."
        ) from exc
    if not isinstance(data, dict):
        raise common.ConfigError(
            f"bundle check failed [{label} is a JSON object]: {path} parsed to a "
            f"{type(data).__name__}, not an object."
        )
    return data


# Bundle install writes into the data home, which under a mounted persistent volume
# (this image's Dockerfile provisions ``/var/lib/kirocrew`` for exactly that) carries over
# from a prior task. A sandboxed agent in that prior task can write ``mcp.json`` and the
# other destinations, so it can plant a SYMLINK there pointing outside the data home.
# ``install_bundle`` then runs at the NEXT boot, before the backend and any sandbox, as the
# image's own user, and a plain ``shutil.copyfile``/``copytree`` FOLLOWS that symlink and
# overwrites the target outside any confinement. So every destination write below refuses a
# symlink AT the destination, the same way the (extracted) backup reader refused one at its
# source: check and write are the same ``O_NOFOLLOW`` open, not a stat-then-write pair a
# swap can slip between.


def _write_nofollow(dst: Path, data: bytes) -> None:
    """Write *data* to *dst*, refusing to follow a symlink planted at *dst*.

    ``O_NOFOLLOW`` fails with ``ELOOP`` when the final component is a symlink, so a
    pre-planted link cannot redirect this write outside the data home. ``O_TRUNC`` gives the
    same whole-file-replace semantics ``copyfile`` had for the ordinary (non-symlink) case.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(dst), flags, 0o644)
    except OSError as exc:
        raise common.ConfigError(
            f"bundle check failed [destination is not a symlink]: {dst} could not be opened "
            f"for writing without following a link ({exc}). A pre-planted symlink there would "
            f"redirect the unsandboxed install to overwrite a file outside the data home, so "
            f"the install refuses rather than follow it."
        ) from exc
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except OSError as exc:
        raise common.ConfigError(
            f"bundle check failed [destination write]: {dst} could not be written ({exc})."
        ) from exc


def _prepare_dir_nofollow(dst: Path) -> None:
    """Ensure *dst* is a real directory to copy into, refusing a symlink at that path.

    ``copytree(dirs_exist_ok=True)`` into a path that is a symlink to a directory would write
    through the link. ``os.path.islink`` is checked BEFORE ``is_dir`` because a symlink to a
    directory answers True to ``is_dir`` -- the link is what must be refused, not resolved.
    """
    if os.path.islink(str(dst)):
        raise common.ConfigError(
            f"bundle check failed [destination is not a symlink]: {dst} is a symlink. A "
            f"pre-planted link there would redirect the unsandboxed skills install outside "
            f"the data home, so the install refuses rather than copy through it."
        )
    dst.mkdir(parents=True, exist_ok=True)


def _copytree_nofollow(src: Path, dst: Path) -> None:
    """Copy the trusted *src* tree into *dst*, refusing a symlink at any destination path.

    ``copytree`` alone would follow a symlink pre-planted at a NESTED destination the same way
    it would at the top level. This walks the trusted source and writes each file through
    ``_write_nofollow`` and each subdirectory through ``_prepare_dir_nofollow``, so no
    destination component -- top-level or nested -- can redirect a write outside the tree.
    The source is in-image and digest-verified, so it is trusted; only the destinations,
    which live on the possibly-persistent data home, are guarded.
    """
    for entry in sorted(src.iterdir()):
        target = dst / entry.name
        if entry.is_dir() and not entry.is_symlink():
            _prepare_dir_nofollow(target)
            _copytree_nofollow(entry, target)
        elif entry.is_file() and not entry.is_symlink():
            _write_nofollow(target, entry.read_bytes())
        # A symlink IN THE SOURCE is skipped: the bundle is laid out as plain files and
        # dirs, so a link there is not something to reproduce into the read path.


def _install_skills_tree(src: Path, dst: Path) -> None:
    """Replace *dst* with a fresh copy of the trusted *src* skills tree, atomically.

    Two properties, both load-bearing:

    * **Replace, not merge.** ``install_bundle`` runs at every boot and the data home may be
      a persistent volume, so a plain copy-into-place leaves a skill that a later bundle
      REMOVED or renamed still sitting in the read path -- a stale, possibly
      governance-relevant skill silently active. So the whole tree is staged fresh and swapped
      in: whatever the destination held before, including a dropped skill, is gone after.
    * **No symlink is followed.** ``dst`` is refused if it is a symlink (a pre-planted link
      would redirect the unsandboxed install outside the data home), the staging copy is
      written through the same ``O_NOFOLLOW`` guard as every other destination, and the swap
      is an ``os.replace`` of one directory onto another on the same filesystem -- atomic, no
      partially-installed window a reader could observe.
    """
    if os.path.islink(str(dst)):
        raise common.ConfigError(
            f"bundle check failed [destination is not a symlink]: {dst} is a symlink. A "
            f"pre-planted link there would redirect the unsandboxed skills install outside "
            f"the data home, so the install refuses rather than copy through it."
        )
    dst.parent.mkdir(parents=True, exist_ok=True)
    # Stage in a sibling temp dir on the SAME filesystem so the replace is atomic. Prefixed
    # so a crashed prior install leaves an obvious orphan rather than a plausible skill dir.
    staging = Path(tempfile.mkdtemp(dir=str(dst.parent), prefix=".skills-staging-"))
    try:
        _copytree_nofollow(src, staging)
        # Clear the old tree, then swap. rmtree only after the confirmed-not-a-symlink check
        # above, so this never deletes through a link. os.replace onto a nonexistent target
        # is the atomic rename; onto an existing dir it would need the target empty, hence the
        # rmtree first.
        if dst.exists():
            shutil.rmtree(dst)
        os.replace(staging, dst)
    except BaseException:
        # A failed install leaves no half-written staging dir behind.
        shutil.rmtree(staging, ignore_errors=True)
        raise


def install_bundle(settings: Settings, *, agents_dir: Path | None = None) -> dict:
    """Verify the bundle, then lay it out where Kiro Crew reads it. Fail CLOSED.

    Called from ``run()`` before the backend starts, alongside ``verify_layout``
    / ``require_api_key`` / ``verify_sandbox``. Every refusal names the
    check that failed and both values, because a container that boots with the
    wrong crew is the exact failure this change exists to prevent.

    ``agents_dir`` is injected only by tests, so the agent spec never lands in the
    real ``~/.kiro/agents`` during a test run; production resolves it via
    :func:`default_kiro_agents_dir`. Returns the marker payload on success.
    """
    bundle_dir = settings.bundle_dir

    # 1. The bundle dir and each of its four entries must exist, with the right
    #    kind. The crew rides in an image layer, which cannot be absent -- so a
    #    missing one means the image was built wrong, not a runtime blip.
    if not bundle_dir.is_dir():
        raise common.ConfigError(
            f"bundle check failed [bundle dir present]: SMC_BUNDLE_DIR="
            f"{bundle_dir} is not a directory (exists={bundle_dir.exists()}). The "
            f"crew rides in the image at this path; booting without it would "
            f"serve a default agent."
        )
    for entry, kind in BUNDLE_ENTRIES:
        p = bundle_dir / entry
        ok = p.is_dir() if kind == "dir" else p.is_file()
        if not ok:
            raise common.ConfigError(
                f"bundle check failed [entry present]: expected a {kind} at {p}, "
                f"but exists={p.exists()} is_file={p.is_file()} "
                f"is_dir={p.is_dir()}."
            )

    manifest = _read_json_object(bundle_dir / "manifest.json", "manifest.json")
    agent_spec = _read_json_object(bundle_dir / "agent.json", "agent.json")

    # 2. manifest crew_name must equal SMC_CREW_NAME. An empty SMC_CREW_NAME is
    #    refused too: "it started" must mean "the NAMED crew is installed", and
    #    an unnamed crew cannot satisfy that even if the manifest also omits it.
    manifest_crew = str(manifest.get("crew_name") or "")
    if not settings.crew_name:
        raise common.ConfigError(
            "bundle check failed [manifest crew_name == SMC_CREW_NAME]: "
            f"SMC_CREW_NAME is empty (manifest crew_name={manifest_crew!r}). "
            "The deployment must name the crew it intends to serve."
        )
    if manifest_crew != settings.crew_name:
        raise common.ConfigError(
            "bundle check failed [manifest crew_name == SMC_CREW_NAME]: "
            f"manifest crew_name={manifest_crew!r} != SMC_CREW_NAME="
            f"{settings.crew_name!r}. The image does not carry the crew this "
            "task was configured to serve."
        )

    # 3. agent.json name must equal crew_name, or kiro-cli resolves a different
    #    agent (or none) -- surfacing later as a mode error, not the naming bug.
    agent_name = str(agent_spec.get("name") or "")
    if agent_name != manifest_crew:
        raise common.ConfigError(
            "bundle check failed [agent.json name == crew_name]: agent.json "
            f"name={agent_name!r} != manifest crew_name={manifest_crew!r}. The "
            f"spec is installed at <agents>/{manifest_crew}.json and read back by "
            "its own name, so a mismatch serves nothing."
        )

    # 4. The recomputed content digest must equal the manifest's. This is what
    #    proves the bytes in the image are the bytes that were reviewed.
    manifest_digest = str(manifest.get("digest") or "")
    recomputed = _content_digest(bundle_dir)
    if recomputed != manifest_digest:
        raise common.ConfigError(
            "bundle check failed [content digest == manifest digest]: recomputed="
            f"{recomputed!r} != manifest digest={manifest_digest!r}. The bundle "
            "content does not match what the manifest was signed over."
        )

    # All checks passed -- install into the read paths verified above.
    agents = agents_dir if agents_dir is not None else default_kiro_agents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    # A crew name is a NAME, checked before it becomes a path segment. Every check above
    # is an EQUALITY or a digest: they prove the manifest agrees with the spec and with the
    # bytes, and none of them constrains the SHAPE of the agreed name. So a manifest and a
    # spec that both say ``../../etc/whatever`` pass all four and then
    # ``agents / f"{manifest_crew}.json"`` resolves outside ``agents``, because
    # ``Path.__truediv__`` treats an absolute segment as a new root and ``..`` as a parent
    # step -- and ``shutil.copyfile`` writes there, before the backend starts, as root in
    # the image.
    #
    # Whether the name can be attacker-chosen depends on how the deploy tooling populates
    # it, and that tooling is in another track. That is the reason to check here rather
    # than the reason not to: this is the process that does the write, so this is where the
    # answer holds regardless of what set the value.
    #
    # The builder has the same guard for the same reason (``_validated_crew_name`` in
    # ``packaging/build.py``). Duplicated rather than shared, like the no-follow opener:
    # this tree is image source the gateway must not import.
    if (
        not manifest_crew
        or manifest_crew in {".", ".."}
        or "/" in manifest_crew
        or "\\" in manifest_crew
        or "\x00" in manifest_crew
        or os.path.isabs(manifest_crew)
    ):
        raise common.ConfigError(
            f"bundle check failed [crew name is a name]: crew_name={manifest_crew!r} "
            "contains a path separator, is absolute, or is a directory reference. The "
            "spec is installed at <agents>/<crew_name>.json, so a name that can leave "
            "that directory would overwrite a file outside it."
        )
    # One guard, not two. A containment assertion on the resolved destination was here as
    # defence in depth and it is unreachable: with the shape check above in place, no name
    # gets far enough to land outside ``agents``, so no test could redden it. A guard no
    # test can fail is a comment claiming a property nobody verifies, so it is gone rather
    # than shipped. If the join ever changes shape, the check to add back is the one that
    # can be tested against the new shape.
    agent_dst = agents / f"{manifest_crew}.json"
    # Copy the validated bytes rather than re-serialising, so what kiro-cli reads
    # is exactly what the digest covered -- through a no-follow open so a symlink
    # pre-planted at the destination cannot redirect the write.
    _write_nofollow(agent_dst, (bundle_dir / "agent.json").read_bytes())

    settings.data_home.mkdir(parents=True, exist_ok=True)
    mcp_dst = settings.data_home / "mcp.json"
    _write_nofollow(mcp_dst, (bundle_dir / "mcp.json").read_bytes())

    skills_dst = settings.data_home / "skills"
    _install_skills_tree(bundle_dir / "skills", skills_dst)

    payload = {
        "crew_name": manifest_crew,
        "bundle_digest": manifest_digest,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }
    marker = settings.data_home / INSTALLED_MARKER
    _write_nofollow(marker, (json.dumps(payload, indent=2) + "\n").encode("utf-8"))
    log.info(
        "crew %r installed: agent=%s mcp=%s skills=%s digest=%s",
        manifest_crew,
        agent_dst,
        mcp_dst,
        skills_dst,
        manifest_digest,
    )
    return payload
