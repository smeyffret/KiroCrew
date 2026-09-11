"""New-user friction: what confused the tester, reported beside (not inside) the verdict.

The harness gives the model one extra tool, ``report_friction``. Whenever the
persona (``scenarios.PERSONAS``) stalls -- cannot find a control, clicks the
wrong thing, does not understand a label or an icon, does not know what just
happened -- it files one structured entry and carries on with the task. A
scenario can PASS with a dozen friction entries and FAIL with none: the two
channels never touch each other.

This module owns everything about those entries once the model has spoken:

* :data:`FRICTION_TOOL` -- the tool schema the harness advertises;
* :func:`validate_entry` -- the only way an entry gets into ``summary.json``
  (every field typed, bounded and stripped; the model cannot smuggle Markdown,
  fences or mentions into a bot-authored comment);
* :func:`entry_key` -- the cross-night identity ``(feature, element,
  what_confused)`` after normalization, so the same confusion seen on
  consecutive nights is one row with a count, not a new row each night;
* :func:`merge_ledger` -- folds a run into the ledger the workflow carries
  from night to night as an artifact;
* :func:`render_section` -- the "New-user friction" block for ``verdict.md``,
  the run summary and the nightly issue, grouped by feature, ordered by
  severity;
* :func:`plan_issues` / :func:`file_issues` -- the GitHub issues: at most
  ``ISSUE_CAP`` new ones per night for non-cosmetic rows that have no issue yet
  (new tonight, or held over by an earlier night's cap),
  one "again on <date>" comment for a recurrence that already has an issue.

CLI (what the workflow calls)::

    python test/gui_user/friction.py merge  --summary results/summary.json \
        --ledger prev/friction_ledger.json --out results/friction.json \
        --out-ledger results/friction_ledger.json --date YYYY-MM-DD \
        --run-url ... --artifact-url ... --head-sha ...
    python test/gui_user/friction.py issues --ledger results/friction_ledger.json \
        --repo owner/name --date YYYY-MM-DD [--dry-run]
    python test/gui_user/friction.py snapshot --ledger results/friction_ledger.json \
        --out results/friction.json --date YYYY-MM-DD     # after issues: rows now carry numbers
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

if __package__ in (None, ""):  # ``python test/gui_user/friction.py``
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui_user.scenarios import FEATURES  # noqa: E402

SEVERITIES: tuple[str, ...] = ("blocker", "slows-down", "cosmetic")
#: Longest text the model may put in one field; longer is cut, never rejected,
#: so a verbose tester does not lose the entry.
FIELD_MAX = 240
#: Entries one attempt may file; past this the tool answers "log full" and the
#: task continues. Bounds the cost of a tester who narrates every pixel.
ATTEMPT_CAP = 12
#: New issues one night may open; the rest stay in the run summary.
ISSUE_CAP = 5
LEDGER_VERSION = 1
LEDGER_FILE = "friction_ledger.json"
FRICTION_FILE = "friction.json"
ISSUE_MARKER = "<!-- gui-user-friction "
LABEL_UX = "ux"
LABEL_CHANNEL = "channel: gui-user-test"
#: ``feature`` slug -> the repo's ``area:`` label, for the slugs whose surface is
#: not the dashboard shell. Every key must be a ``FEATURES`` slug (a unit test
#: holds it); anything unlisted is the dashboard.
AREA_LABELS: dict[str, str] = {
    "apps": "area: apps",
    "schedule": "area: cron",
}
DEFAULT_AREA_LABEL = "area: dashboard"

FRICTION_TOOL: dict[str, Any] = {
    "name": "report_friction",
    "description": (
        "Record ONE moment where the app confused you as a first-time user: you paused for more "
        "than a glance to find something, could not find a control, clicked the wrong thing, did "
        "not understand a label, icon or message, did not know what was happening, or the layout "
        "hid the main action. Call it the moment it happens, then continue the task. Say it in "
        "your own words. This never changes the task or its verdict."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "surface": {
                "type": "string",
                "maxLength": FIELD_MAX,
                "description": "Page or panel you were on (as a user would name it).",
            },
            "element": {
                "type": "string",
                "maxLength": FIELD_MAX,
                "description": "The control, text or area involved, described so someone else could find it.",
            },
            "what_confused": {
                "type": "string",
                "maxLength": FIELD_MAX,
                "description": "One sentence, first person: what confused you.",
            },
            "expected": {
                "type": "string",
                "maxLength": FIELD_MAX,
                "description": "What you expected to see or happen.",
            },
            "actual": {
                "type": "string",
                "maxLength": FIELD_MAX,
                "description": "What actually was there or happened.",
            },
            "severity": {
                "type": "string",
                "enum": list(SEVERITIES),
                "description": (
                    "blocker = you could not continue without guessing; slows-down = you got there "
                    "but lost time; cosmetic = it looked wrong but did not slow you down."
                ),
            },
        },
        "required": ["surface", "element", "what_confused", "expected", "actual", "severity"],
        "additionalProperties": False,
    },
}

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^a-z0-9 ]+")


class FrictionError(ValueError):
    """A friction entry or ledger is malformed."""


# --------------------------------------------------------------------------
# Entries
# --------------------------------------------------------------------------


def _clean(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise FrictionError(f"{what} must be a string")
    kept = "".join(ch for ch in value if ch.isprintable() or ch in "\n\t")
    text = _WS.sub(" ", kept).strip()
    if not text:
        raise FrictionError(f"{what} must not be empty")
    return text if len(text) <= FIELD_MAX else text[: FIELD_MAX - 1] + "…"


def normalize(text: str) -> str:
    """Case-, whitespace- and punctuation-insensitive form used for the key."""
    return _WS.sub(" ", _PUNCT.sub(" ", text.lower())).strip()


def entry_key(feature: str, element: str, what_confused: str) -> str:
    raw = "\n".join([feature, normalize(element), normalize(what_confused)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def validate_entry(
    raw: Any, *, feature: str, scenario: str, screenshot: str, step: int
) -> dict[str, Any]:
    """Turn a ``report_friction`` tool input into a stored entry, or raise."""
    if not isinstance(raw, dict):
        raise FrictionError("friction input must be a mapping")
    unknown = set(raw) - set(FRICTION_TOOL["input_schema"]["properties"])
    if unknown:
        raise FrictionError(f"unknown friction fields {sorted(unknown)}")
    severity = raw.get("severity")
    if severity not in SEVERITIES:
        raise FrictionError(f"severity must be one of {SEVERITIES}")
    if feature not in FEATURES:
        raise FrictionError(f"feature {feature!r} is not a registry slug")
    entry = {
        "feature": feature,
        "scenario": scenario,
        "surface": _clean(raw.get("surface"), "surface"),
        "element": _clean(raw.get("element"), "element"),
        "what_confused": _clean(raw.get("what_confused"), "what_confused"),
        "expected": _clean(raw.get("expected"), "expected"),
        "actual": _clean(raw.get("actual"), "actual"),
        "severity": severity,
        "screenshot": str(screenshot),
        "step": int(step),
    }
    entry["key"] = entry_key(feature, entry["element"], entry["what_confused"])
    return entry


def collect(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Every entry of a run, deduplicated by key (first sighting wins)."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for sc in summary.get("scenarios") or []:
        if not isinstance(sc, dict):
            continue
        for att in sc.get("attempts") or []:
            if not isinstance(att, dict):
                continue
            for e in att.get("friction") or []:
                if not isinstance(e, dict) or e.get("severity") not in SEVERITIES:
                    continue
                key = str(e.get("key") or "")
                if not key or key in seen:
                    continue
                seen.add(key)
                out.append(dict(e))
    return out


def severity_rank(severity: str) -> int:
    return SEVERITIES.index(severity) if severity in SEVERITIES else len(SEVERITIES)


def sort_entries(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    slugs = list(FEATURES)

    def rank(e: dict[str, Any]) -> tuple[int, int, str]:
        f = str(e.get("feature", ""))
        return (
            slugs.index(f) if f in slugs else len(slugs),
            severity_rank(e["severity"]),
            e["key"],
        )

    return sorted(entries, key=rank)


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------


def empty_ledger() -> dict[str, Any]:
    return {"version": LEDGER_VERSION, "entries": {}}


def load_ledger(path: Optional[Path]) -> dict[str, Any]:
    if path is None or not path.exists():
        return empty_ledger()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FrictionError(f"ledger {path}: {exc}") from exc
    if (
        not isinstance(doc, dict)
        or doc.get("version") != LEDGER_VERSION
        or not isinstance(doc.get("entries"), dict)
    ):
        raise FrictionError(f"ledger {path}: unexpected shape")
    for key, row in doc["entries"].items():
        if not isinstance(row, dict) or row.get("severity") not in SEVERITIES:
            raise FrictionError(f"ledger {path}: entry {key} is malformed")
    return doc


def merge_ledger(
    ledger: dict[str, Any],
    entries: Iterable[dict[str, Any]],
    *,
    date: str,
    run_url: str = "",
    artifact_url: str = "",
    head_sha: str = "",
) -> tuple[dict[str, Any], list[str]]:
    """Fold one run into the ledger; return ``(ledger, keys that are new tonight)``.

    A recurrence bumps ``count`` and ``last_seen`` and takes the newest sighting
    whole (surface, wording, expected/actual, screenshot) plus the worst severity
    of old and new, so a blocker never downgrades to cosmetic because one
    night's tester shrugged. Runs are told apart by ``run_url``: the same run
    merged twice is a no-op, a different run on the same day refreshes the
    evidence but does not count the day twice.
    """
    rows: dict[str, Any] = ledger["entries"]
    new_keys: list[str] = []
    for e in entries:
        key = e["key"]
        row = rows.get(key)
        if row is None:
            rows[key] = {
                "feature": e["feature"],
                "scenario": e["scenario"],
                "surface": e["surface"],
                "element": e["element"],
                "what_confused": e["what_confused"],
                "expected": e["expected"],
                "actual": e["actual"],
                "severity": e["severity"],
                "screenshot": e["screenshot"],
                "first_seen": date,
                "last_seen": date,
                "count": 1,
                "issue": None,
                "run_url": run_url,
                "artifact_url": artifact_url,
                "head_sha": head_sha,
            }
            new_keys.append(key)
            continue
        same_day = row.get("last_seen") == date
        if same_day and row.get("run_url") == run_url:
            continue  # the same run merged twice
        if not same_day:
            # A night counts once, however many runs saw the row that day.
            row["count"] = int(row.get("count", 1)) + 1
            row["last_seen"] = date
        # Take the newest sighting whole -- surface, wording and evidence together --
        # so a row never pairs an old location with a new screenshot, and a
        # same-day run other than the one recorded (a dispatch after the nightly)
        # reports ITS evidence under ITS artifact, not the earlier run's. The key
        # is normalized, so ``element`` / ``what_confused`` may differ in spelling.
        for field in ("scenario", "surface", "element", "what_confused", "expected", "actual"):
            row[field] = e[field]
        row["screenshot"] = e["screenshot"]
        row["run_url"] = run_url
        row["artifact_url"] = artifact_url
        row["head_sha"] = head_sha
        if severity_rank(e["severity"]) < severity_rank(row["severity"]):
            row["severity"] = e["severity"]
    return ledger, new_keys


def tonight(ledger: dict[str, Any], date: str) -> list[dict[str, Any]]:
    """Ledger rows seen on ``date``, as entries carrying their key, count and issue."""
    out = []
    for key, row in ledger["entries"].items():
        if row.get("last_seen") == date:
            out.append({**row, "key": key})
    return sort_entries(out)


def rows_for(ledger: dict[str, Any], keys: Iterable[str]) -> list[dict[str, Any]]:
    """Ledger rows for exactly these keys (one run's own sightings), in catalog order.

    ``friction.json`` is scoped this way rather than by date: a dispatch on the
    same day as the nightly must report its own rows, not the nightly's, or its
    screenshot links would point into the wrong artifact.
    """
    wanted = set(keys)
    return sort_entries(
        [{**row, "key": key} for key, row in ledger["entries"].items() if key in wanted]
    )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


_SCHEME = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*):(//)")
_WWW = re.compile(r"(?i)\bwww\.")


def _cell(text: Any, *, max_chars: int = FIELD_MAX) -> str:
    """Model-authored text rendered inert in a Markdown table cell / issue body.

    Pipes, backticks, angle and square brackets lose their Markdown meaning, an
    ``@`` cannot mention, and a URL is defanged (zero-width space after the
    scheme colon and inside ``www.``) so GitHub never autolinks a URL the tester
    merely read off the screen -- an injected page must not become a live link
    in a bot-authored issue.
    """
    s = str(text or "")
    kept = "".join(ch for ch in s if ch.isprintable())
    kept = kept.replace("|", "/").replace("`", "'").replace("@", "@\u200b")
    kept = kept.replace("<", "‹").replace(">", "›").replace("[", "(").replace("]", ")")
    kept = _SCHEME.sub("\\1:\u200b\\2", kept)
    kept = _WWW.sub("www\u200b.", kept)
    kept = _WS.sub(" ", kept).strip()
    return kept if len(kept) <= max_chars else kept[: max_chars - 1] + "…"


def _shot_link(entry: dict[str, Any], artifact_url: Optional[str]) -> str:
    shot = _cell(entry.get("screenshot"), max_chars=120)
    if not shot:
        return "—"
    url = artifact_url or entry.get("artifact_url") or ""
    return f"[`{shot}`]({url})" if url else f"`{shot}`"


def render_section(entries: Iterable[dict[str, Any]], *, artifact_url: Optional[str] = None) -> str:
    """The "New-user friction" block: one table per feature, worst severity first."""
    rows = sort_entries(entries)
    lines = ["## New-user friction", ""]
    if not rows:
        lines.append("_The tester reported nothing confusing in this run._")
        return "\n".join(lines) + "\n"
    counts = {s: sum(1 for r in rows if r["severity"] == s) for s in SEVERITIES}
    lines.append(
        f"_{len(rows)} moment(s) where a first-time user stalled: "
        + " · ".join(f"{counts[s]} {s}" for s in SEVERITIES)
        + ". Reported by the tester persona beside the verdict; a scenario can PASS and still list friction._"
    )
    current = None
    for r in rows:
        if r["feature"] != current:
            current = r["feature"]
            lines += [
                "",
                f"### {FEATURES.get(current, current)} (`{current}`)",
                "",
                "| Severity | What confused me | Where | Expected → actual | Seen | Screenshot |",
                "|---|---|---|---|---|---|",
            ]
        seen = f"{int(r.get('count', 1))}× · last {r.get('last_seen', '')}".strip(" ·")
        if int(r.get("count", 1)) == 1:
            seen = "new"
        issue = r.get("issue")
        if issue:
            seen += f" · #{int(issue)}"
        lines.append(
            f"| {r['severity']} | {_cell(r['what_confused'])} | {_cell(r['surface'])} → {_cell(r['element'])} "
            f"| {_cell(r['expected'])} → {_cell(r['actual'])} | {seen} | {_shot_link(r, artifact_url)} |"
        )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Issues
# --------------------------------------------------------------------------


def issue_title(row: dict[str, Any]) -> str:
    text = _cell(row["what_confused"], max_chars=90)
    return f"ux({row['feature']}): {text}"


def issue_body(key: str, row: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"{ISSUE_MARKER}{key} -->",
            f"A first-time user drove `{row.get('scenario', '')}` (feature `{row['feature']}`) in the nightly "
            f"GUI user test and stalled here. Severity **{row['severity']}**. Opened by the lane; the wording "
            "below is the tester's, in the first person.",
            "",
            f"- **Where:** {_cell(row['surface'])} → {_cell(row['element'])}",
            f"- **What confused me:** {_cell(row['what_confused'])}",
            f"- **Expected:** {_cell(row['expected'])}",
            f"- **Actual:** {_cell(row['actual'])}",
            f"- **First seen:** {row.get('first_seen', '')} · **seen** {int(row.get('count', 1))}×"
            + (
                " · earlier issue(s): " + ", ".join(f"#{int(n)}" for n in row["closed_issues"])
                if row.get("closed_issues")
                else ""
            ),
            f"- **Screenshot:** {_shot_link(row, None)} · [run]({row.get('run_url', '')}) · `{row.get('head_sha', '')}`",
            "",
            "Recurrences are appended as comments by the lane; close when the confusion is gone (the lane "
            "reopens nothing -- a sighting after the close opens a new issue that links back here).",
            "",
            "Lane: `.github/workflows/gui-user-test.yml` · docs: `docs/build/gui-user-test.md` · tracking: #9578",
        ]
    )


def plan_issues(ledger: dict[str, Any], *, date: str, cap: int = ISSUE_CAP) -> dict[str, list[str]]:
    """Which ledger keys get a new issue tonight, which get a recurrence comment.

    Cosmetic rows never leave the summary. Every non-cosmetic row in the ledger
    that has no issue yet is a candidate -- tonight's sightings first, then rows
    an earlier night's cap held over, each group in registry-then-severity
    order -- and the first ``cap`` are opened. A row seen tonight that already
    has an issue and a count above one gets an "again on <date>" comment, once
    per date (``last_commented`` makes a same-day rerun idempotent).
    """
    seen_tonight = {r["key"] for r in tonight(ledger, date)}
    unissued = [
        {**row, "key": key}
        for key, row in ledger["entries"].items()
        if row["severity"] != "cosmetic" and not row.get("issue")
    ]
    ordered = sort_entries([r for r in unissued if r["key"] in seen_tonight]) + sort_entries(
        [r for r in unissued if r["key"] not in seen_tonight]
    )
    create = [r["key"] for r in ordered[:cap]]
    comment = [
        r["key"]
        for r in tonight(ledger, date)
        if r["severity"] != "cosmetic"
        and r.get("issue")
        and int(r.get("count", 1)) > 1
        and r.get("last_commented") != date  # a same-day rerun must not post the comment twice
    ]
    return {"create": create, "comment": comment}


Runner = Callable[[list[str]], str]


def _gh(args: list[str]) -> str:
    proc = subprocess.run(
        ["gh", *args], check=True, capture_output=True, text=True, encoding="utf-8"
    )
    return proc.stdout


Persist = Callable[[dict[str, Any]], None]


def file_issues(
    ledger: dict[str, Any],
    *,
    repo: str,
    date: str,
    run: Runner = _gh,
    cap: int = ISSUE_CAP,
    persist: Optional[Persist] = None,
) -> dict[str, Any]:
    """Open / comment the planned issues via ``gh``; record issue numbers in the ledger.

    ``persist`` (the ledger writer) is called after EVERY successful ``gh``
    mutation, so a failure partway through a batch never loses the numbers of
    the issues already opened -- a rerun would otherwise open them twice. A
    failed create or comment is recorded under ``failed`` and the batch goes on.
    """
    rows = ledger["entries"]
    failed: list[dict[str, str]] = []

    def save() -> None:
        if persist is not None:
            persist(ledger)

    # A recurrence whose issue a human has since CLOSED is a new sighting, not a
    # comment on a closed thread: forget the mapping (kept under ``closed_issues``)
    # so the plan below opens a fresh issue. Unknown state (gh error) keeps the
    # mapping and the comment path -- never lose a number on a transient failure.
    for row in tonight(ledger, date):
        issue = row.get("issue")
        if not issue or int(row.get("count", 1)) < 2:
            continue
        try:
            state = json.loads(
                run(["issue", "view", str(issue), "--repo", repo, "--json", "state"])
            ).get("state")
        except (subprocess.CalledProcessError, ValueError):
            continue
        if state == "CLOSED":
            live = rows[row["key"]]
            live.setdefault("closed_issues", []).append(int(issue))
            live["issue"] = None
            live.pop("last_commented", None)
            save()

    plan = plan_issues(ledger, date=date, cap=cap)

    for label, color, desc in (
        (LABEL_UX, "D4C5F9", "New-user confusion reported by the GUI user-test persona"),
        (LABEL_CHANNEL, "0E8A16", "Filed by the nightly GUI user-test lane"),
    ):
        try:
            run(
                [
                    "label",
                    "create",
                    label,
                    "--repo",
                    repo,
                    "--color",
                    color,
                    "--description",
                    desc,
                    "--force",
                ]
            )
        except subprocess.CalledProcessError:
            pass  # the label exists or labels are not ours to make; create still works
    opened: list[int] = []
    for key in plan["create"]:
        row = rows[key]
        try:
            out = run(
                [
                    "issue",
                    "create",
                    "--repo",
                    repo,
                    "--title",
                    issue_title(row),
                    "--body",
                    issue_body(key, row),
                    "--label",
                    LABEL_UX,
                    "--label",
                    LABEL_CHANNEL,
                    "--label",
                    AREA_LABELS.get(row["feature"], DEFAULT_AREA_LABEL),
                ]
            )
        except subprocess.CalledProcessError as exc:
            failed.append({"key": key, "op": "create", "error": str(exc)})
            continue
        m = re.search(r"/issues/(\d+)", out)
        if not m:
            failed.append({"key": key, "op": "create", "error": "no issue URL in gh output"})
            continue
        row["issue"] = int(m.group(1))
        opened.append(row["issue"])
        save()
    commented: list[int] = []
    for key in plan["comment"]:
        row = rows[key]
        body = (
            f"Again on {date} ({int(row.get('count', 1))}× so far, severity {row['severity']}): "
            f"{_cell(row['what_confused'])} — {_shot_link(row, None)} · [run]({row.get('run_url', '')})"
        )
        try:
            run(["issue", "comment", str(row["issue"]), "--repo", repo, "--body", body])
        except subprocess.CalledProcessError as exc:
            failed.append({"key": key, "op": "comment", "error": str(exc)})
            continue
        row["last_commented"] = date
        commented.append(int(row["issue"]))
        save()
    return {"opened": opened, "commented": commented, "failed": failed}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _write(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="new-user friction: merge, render, file issues")
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("merge", help="fold summary.json into the ledger; write friction.json")
    m.add_argument("--summary", type=Path, required=True)
    m.add_argument("--ledger", type=Path, help="previous night's ledger (optional)")
    m.add_argument("--out", type=Path, required=True, help="friction.json for this run")
    m.add_argument("--out-ledger", type=Path, required=True)
    m.add_argument("--date", required=True, help="YYYY-MM-DD of this run")
    m.add_argument("--run-url", default="")
    m.add_argument("--artifact-url", default="")
    m.add_argument("--head-sha", default="")

    r = sub.add_parser(
        "snapshot", help="rewrite friction.json (tonight's rows) from the ledger, e.g. after issues"
    )
    r.add_argument("--ledger", type=Path, required=True)
    r.add_argument("--out", type=Path, required=True, help="the friction.json `merge` wrote")
    r.add_argument("--date", required=True)

    i = sub.add_parser("issues", help="open / comment GitHub issues for tonight's ledger rows")
    i.add_argument("--ledger", type=Path, required=True)
    i.add_argument("--repo", required=True)
    i.add_argument("--date", required=True)
    i.add_argument(
        "--artifact-url",
        default="",
        help="this run's artifact URL, stamped on tonight's rows so issue screenshot links resolve",
    )
    i.add_argument("--dry-run", action="store_true")

    args = p.parse_args(argv)
    try:
        if args.cmd == "merge":
            summary = json.loads(args.summary.read_text(encoding="utf-8"))
            ledger = load_ledger(args.ledger)
            entries = collect(summary)
            ledger, new_keys = merge_ledger(
                ledger,
                entries,
                date=args.date,
                run_url=args.run_url,
                artifact_url=args.artifact_url,
                head_sha=args.head_sha,
            )
            run_keys = [e["key"] for e in entries]
            rows = rows_for(ledger, run_keys)
            _write(
                args.out,
                {
                    "date": args.date,
                    "head_sha": args.head_sha,
                    "run_url": args.run_url,
                    "artifact_url": args.artifact_url,
                    "run_keys": run_keys,
                    "new_keys": new_keys,
                    "entries": rows,
                },
            )
            _write(args.out_ledger, ledger)
            print(
                f"{len(rows)} friction entr{'y' if len(rows) == 1 else 'ies'}, {len(new_keys)} new"
            )
        elif args.cmd == "snapshot":
            ledger = load_ledger(args.ledger)
            if not args.out.exists():
                raise FrictionError(f"{args.out}: snapshot needs the friction.json `merge` wrote")
            prior = json.loads(args.out.read_text(encoding="utf-8"))
            run_keys = [str(k) for k in prior.get("run_keys") or []]
            rows = rows_for(ledger, run_keys)
            _write(
                args.out,
                {
                    "date": args.date,
                    "head_sha": prior.get("head_sha", ""),
                    "run_url": prior.get("run_url", ""),
                    "artifact_url": prior.get("artifact_url", ""),
                    "run_keys": run_keys,
                    "new_keys": prior.get("new_keys", []),
                    "entries": rows,
                },
            )
            print(f"{len(rows)} friction entr{'y' if len(rows) == 1 else 'ies'} snapshotted")
        else:
            ledger = load_ledger(args.ledger)
            if args.artifact_url:
                for row in ledger["entries"].values():
                    if row.get("last_seen") == args.date:
                        row["artifact_url"] = args.artifact_url
            if args.dry_run:
                print(json.dumps(plan_issues(ledger, date=args.date), indent=2))
                return 0
            _write(args.ledger, ledger)  # the artifact-url stamp, before any gh call
            result = file_issues(
                ledger,
                repo=args.repo,
                date=args.date,
                persist=lambda led: _write(args.ledger, led),
            )
            print(json.dumps(result))
            if result["failed"]:
                print(f"friction: {len(result['failed'])} gh call(s) failed", file=sys.stderr)
                return 2
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"friction: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
