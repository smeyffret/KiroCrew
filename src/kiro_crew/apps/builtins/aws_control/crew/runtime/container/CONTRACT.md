# Container contract

Three processes run in one ECS task and share a filesystem. This file is the
only place their boundaries are defined. Read it before writing code, and if you
need something here to change, say so rather than changing it: all three tracks
import from the same base and a local fix becomes the next disagreement.

Design authority: `../share-my-crew/CHORUS-DOC.md` section 4 and 9.1.

| Process | Owns | Exposure |
|---|---|---|
| Kiro Crew backend | sessions, conversations, transcripts, MCP, subagents, skills, memory | loopback only, no interface served |
| Front process | authenticating the remote caller, stripping the crew prefix, forwarding a turn | the only listener the network reaches |

Nothing here serves a user interface. The owner's control plane runs on the
owner's own machine and is out of scope for the container.

**No backup subsystem ships in this container yet.** The sidecar that copied session state
to S3 and the restore that brought it back were extracted from this PR; durability across
task replacement is a capability the container does not yet have (tracked as its own design),
not a regression -- there is no crew container on main. The front process still fetches a
slot's transcript from S3 on demand, so `SMC_BACKUP_BUCKET`/`SMC_BACKUP_PREFIX` name where it
reads; until a writer exists it reads an empty bucket.

## Shared base, owned centrally

`container/common/` is written and owned outside the three tracks:

- `common.load()` returns a frozen `Settings`. Read the environment through it,
  never through `os.environ` directly, or the processes will disagree.
- **`config_dir` equals `data_home`.** Verified against a running gateway:
  Kiro Crew's `config_dir()` and `data_home()` resolve to the same directory, so
  `session_map.json` and `open_slots.json` sit at the home ROOT, not under a
  `config/` subdirectory. The default was `data_home/config` at first; the
  supervisor refuses to start when the two disagree, because a backend writing to
  one path while the deployment points at another loses the record of which
  conversations existed.
- `common.read_boot_secret(run_dir, port)` reads the backend's per-boot secret.
  **Never cache the return value.** It is `os.urandom(16)` per boot and is not
  persisted, so a cached copy works until the backend restarts and then fails
  every request with a 403 that looks like a client fault.
- `common.CONTROL_SECRET_HEADER` is the one control header name. Three systems
  have to agree on it and two of them are not Python.
- `common.BACKEND_HOST` is `127.0.0.1` and is not configurable.

## Track ownership, no file is shared

| Track | Owns | Public seam other tracks may call |
|---|---|---|
| S1 front | `container/front/**`, `tests/test_front_*.py` | `container.front.app:build_app(settings)`, `container.front.__main__:main()` |
| S3 supervisor | `container/supervisor/**`, `tests/test_supervisor_*.py` | `container.supervisor.backend:start_backend(settings)`, `:wait_until_ready(settings, timeout)` |

Held centrally: `container/common/**`, `Dockerfile`, `requirements*.txt`,
`app.json`, `deploy/**`, this file.

Do not import another track's internals. Only the seams above are stable.

## Startup order, owned by S3

The order is a correctness requirement, not a preference:

1. Gate the environment (layout, credential, sandbox) and install the crew bundle. Nothing
   has started.
2. The backend starts, and `wait_until_ready` returns only when the port answers
   **and** the boot secret file exists. Process-alive is not ready.
3. The front process starts.

There is no restore phase and no sidecar: the backup subsystem was extracted from this PR.
When durability returns it must reinstate the ordering rule that made it correct -- a
restore must finish before the backend starts, because the backend's periodic flush writes
its in-memory slot table and a flush landing before restore completes persists an empty set
and destroys the record of which conversations existed (`dashboard_persistence.py`).

## Backend facts the front process must respect

Established by reading the Kiro Crew source. Do not rediscover, do not contradict.

- Turn call: `POST /v1/chat/completions` with `{model, messages, id, stream}`
  (`openai_compat.py`, routed `routes/sessions.py`). Returns one JSON
  completion (`:566`) or an SSE stream (`:470`). `id` is the slot id and is what
  continues a conversation. No WebSocket is required.
- **The stream is OpenAI-format, not ACP.** Measured live: completion chunks
  carrying assistant text, keepalive comments, a terminal sentinel, and an error
  object on failure, with **no event names at all**. The ACP `sessionUpdate`
  vocabulary and the `tool_result` frame that carries tool output verbatim live
  on the owner control stream (`GET /sessions/{id}/stream`), which the front
  process does not serve. An allowlist keyed on event names drops every frame
  and hands the customer an empty turn. Key the projection on frame shape, and
  keep it fail-closed so a kind added later is dropped rather than relayed.
- Auth: send `X-Internal-Secret`. Both `/v1/chat/completions` and `/api/chat`
  are in `_MIXED_INTERNAL_API_PATHS` (`server.py`), so the grant
  reaches the handler directly (`token_auth.py`).
- **Do not forward the client's `Origin` or any `X-Forwarded-*` header.**
  Loopback with no Origin is trusted by the CSRF check; a forwarded foreign
  Origin trips it (`origin.py`). Confirmed live: a foreign Origin with a valid
  secret is refused, so the strip is load-bearing rather than precautionary.
- **A busy slot returns 409.** Confirmed live. Requests for one slot id must be
  serialized in the front process rather than surfacing 409 to the caller.
- `/v1` has no per-client API key concept and its `usage` block is hardcoded to
  zero. Per-caller identity and accounting belong to the front process.
- The backend runs in dashboard mode. There is no `--no-dashboard` flag;
  dashboard-off is `--slack-only` (`cli.py`), and it starts a different,
  much smaller server (`_init_api_server`) that has neither the chat endpoints
  nor the slot registry, and it quiets nothing.

## Launching the backend, verified names only

These were all wrong once in a way that produced no error, so use these and not
the plausible ones.

- **`KIROCREW_BIND=127.0.0.1`.** The published image sets `0.0.0.0`. A
  deployment that does not override it puts the backend on the network and
  removes the only trust boundary this design has, while every local test still
  passes. `KIROCREW_HOST` is read by nothing. Verify the resulting listener
  rather than assuming it.
- **`KIROCREW_TELEMETRY_DISABLED=1`** to silence the beacon (`sections.py`),
  or the config key `telemetry.beacon_enabled=false` (`sections.py`). Either
  gate works. An environment variable named after the config key is read by
  nothing, which is the trap: it looks like it worked.
- **`KIROCREW_HOME` must equal `SMC_DATA_HOME`.** The boot secret is written to
  `config_dir()/run/`, and `config_dir()` IS `KIROCREW_HOME`, so this is what
  makes `common.secret_path()` resolve.
- `--no-crons`: arming the scheduler fires any overdue job immediately
  (`gateway.py:~10262`).
- Serve no messaging channel. See "No messaging channel" below: the container
  disables every transport by name in a config file it owns, and passes no channel
  credential into the launch environment.
- The boot update check cannot be disabled by config. Leave it and record it as
  an outbound request the deployment makes.
- **`--approval yolo`**, and the credential. Two things the deployment cannot run
  without:
  - Approval mode is `yolo` (`cli.py`, choices reads|yolo|interactive), and
    it is **refused unless `KIROCREW_HOME` is explicitly non-default**. Nothing
    in the container is there to click Approve, so an interactive prompt is an
    indefinite stall rather than a question. The cost is stated plainly: every
    tool the crew carries is auto-approved for whatever a customer's message
    causes it to do. The narrowing path is `reads`, which auto-approves only
    read-only tools, once a real turn is working.
  - `KIRO_API_KEY` comes from AWS Secrets Manager through the task definition.
    The container never signs in. The environment is inherited wholesale by the
    backend, so the key reaches the kiro-cli child (`loader.py`,
    `runtime.py`) and the sandbox env filter deliberately does not deny it
    (`runtime.py`). Refuse to start when it is absent. **Presence is not
    validity:** an invalid key yields a container that answers its health check
    while every turn fails, so only a real turn establishes the credential works.

**Readiness proves less than it looks like.** Port answering plus the secret file
existing does not mean a turn will succeed: a backend with no model credential
answers and then returns `503 kiro_prerequisite_required` on every turn. Say in
code what the check proves and what it does not.

**The sandbox: this container runs sandboxed-only.**
`agent.sandbox_allow_unsandboxed_exec` (`sections.py`) is the config key kiro-cli
reads. kiro-cli spawns the model subprocess inside an unprivileged user namespace,
and Fargate does not permit one: the supervisor probes it by forking and calling
`os.unshare(CLONE_NEWUSER)`, and the kernel refuses.

This container runs SANDBOXED-ONLY. There is deliberately no config key or env var
that opts into unsandboxed execution, so on a host without a user namespace (Fargate
today) the supervisor refuses to start rather than run the model subprocess exposed.

**An undetermined probe refuses too.** The supervisor proceeds only on a positive
verdict: a probe that cannot run (no `os.unshare`), cannot fork, or whose child
neither succeeds nor reports a kernel refusal is a refusal, and the message repeats
which of those it was. Reading "could not determine" as "probably fine" is the same
shape of defect as reading the backend environment through a denylist -- it holds for
the hosts already thought of and fails open on the next one, and failing open here
means an auto-approving worker holding the model credential with no sandbox.

Why there is no opt-in, and why the ECS boundary is not a substitute for one. The
model subprocess auto-approves every tool (`--approval yolo` -- nothing in the
container is there to click Approve) and its environment carries the model
credential `KIRO_API_KEY`, which kiro-cli re-injects into the worker and which is the
whole model-auth mechanism, so the worker must hold it. An unsandboxed worker would
therefore run an auto-approved shell, driven by untrusted customer prompt content,
with a live credential readable in its own environment -- a
prompt-injection-to-credential-exfiltration path. The ECS task boundary (single
tenant, one data home, private subnet, load-balancer-only ingress) does NOT close
that path: it defends against a third party reaching the task, but the attacker here
is the customer's own prompt content, which is untrusted by definition and already
inside the boundary. The two peer secrets `AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`
and `SMC_CONTROL_SECRET` are stripped from the backend env for this reason;
`KIRO_API_KEY` cannot be stripped without breaking every turn, so the only way to
keep the exposure unreachable is to keep the worker sandboxed. The sandbox and the
live credential are thus mutually exclusive with the exposure: the worker is
sandboxed, or the container does not start.

Offering an unsandboxed posture safely would require brokering the model credential
out of the worker's environment (so an unsandboxed worker never holds it). That is
not part of this change, so the unsandboxed posture is not offered here -- the
container ships sandboxed-only, and on a host without a user namespace it refuses to
start rather than run exposed.

**No messaging channel.** A crew in a container has nobody to reach on Slack or
Telegram: the only caller it serves is the customer's HTTP turn through the front
process. A transport that comes up hands that crew an outbound channel into an
owner's workspace that nobody chose to grant it.

Before the backend starts, the supervisor writes `<config dir>/config.json` with every
channel section's `enabled` set to false, merged over anything already there so a
shipped file cannot outvote it. The launch environment also carries no channel
credential. Both halves are required and neither covers the other's case: `imessage`
and `whatsapp` carry no credential in the gateway's channel registry, so they start on
their config flag alone, and `slack` has no `enabled` key at all, so it starts on its
tokens alone.

The names in both lists come from that registry and are ratcheted against it by
`test/test_crew_container_channel_isolation.py`, in both directions -- a channel the
gateway can start and the container does not disable fails, and so does a name the
gateway does not have, because that reads as coverage while doing nothing.

**Shutdown: a worker is not in the backend's process group.** Workers spawn with
`start_new_session` (`runtime.py`), so each `setsid`s into its own group and
**escapes a `killpg` on the backend**. They are reaped by the backend's own
SIGTERM handling, which makes the drain window load-bearing rather than a
courtesy. Killing the group and skipping the drain leaves a worker that goes on
to finish its turn.

## What every track owes

Unit tests that run with no AWS and no real backend: fake the backend's HTTP
surface, fake S3, use real temporary files for anything touching the filesystem.
Run only your own test files.

State plainly what you could not verify. None of this can be proven from a
laptop, and a claim that outruns the evidence is worse than a gap that is named.
