"""CodexHarness, and the default-off switch that decides which transport codex takes.

Two halves, and they are separate on purpose.

The first half drives :class:`~kiro_crew.acp.harness.codex.CodexHarness` against a
FAKE ACP peer -- a scripted dict-in/dict-out stand-in for the adapter, no process and
no pipe. What it proves is that the harness's own seam answers, sent in the order a
session is built in, produce an exchange the adapter accepts: one handshake, two
sessions on the ONE peer with distinct ids, prompts routed to the right one, a model
switch that goes down the config-option channel, and a teardown that sends the verb
codex actually has. A real codex-acp cannot stand in for this here: ``session/new``
answers ``-32000 Authentication required`` without an OpenAI login, so a live adapter
can only be taken as far as ``initialize``.

The second half pins the switch. ``KIROCREW_CODEX_ACP_RUNTIME`` is OFF by default,
and off it must be as if it did not exist: codex stays on AcpClient, and the
capability sets keep the members they shipped with. On, codex reads as a runtime
backend at both gates. The default-off half is the one that would fail if this change
altered product behaviour, which is the whole claim it makes.

The seam-level contract every harness answers is in
``test_acp_harness_contract.py``; codex is parametrised into it. What is here is what
is true of codex and of nothing else.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.acp.harness import codex as harness_mod
from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.harness.base import SpawnContext, TeardownPolicy
from kiro_crew.acp.harness.codex import CodexHarness
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_ACP_RUNTIME,
    ACP_BACKENDS_SESSION_SHARING,
    METHOD_CANCEL,
    METHOD_SESSION_UPDATE,
)
from kiro_crew.acp_backends import (
    ENV_CODEX_ACP_RUNTIME,
    acp_runtime_backends,
    codex_runs_on_acp_runtime,
)
from kiro_crew.providers.acp import AcpProvider

# ---------------------------------------------------------------------------
# The fake peer
# ---------------------------------------------------------------------------


class FakeCodexPeer:
    """A scripted stand-in for one codex-acp process hosting N sessions.

    Deliberately not a mock: it holds the two behaviours the harness's answers are
    judged against, and a mock would assert only that a call was made rather than
    that the peer could answer it.

    * ``sessions`` is a map, like the adapter's own ``this.sessions``, so "two
      session/new on one process" is observable as two entries rather than two calls.
    * ``mcpCapabilities`` advertises ``http`` only, which is what the real adapter
      advertises, and ``session/new`` REFUSES the whole request with ``-32600`` when
      an ``sse`` element reaches it -- the failure the array narrowing prevents.

    Every frame is round-tripped through ``json.dumps``/``loads`` so a payload that
    is not JSON-serialisable fails here rather than at a real pipe.
    """

    MCP_CAPABILITIES = {"http": True, "sse": False, "acp": False}

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.config_options: list[tuple[str, str, Any]] = []
        self.notifications: list[tuple[str, dict[str, Any]]] = []
        self.initialized = False
        self._next_id = 0
        #: Model values this peer refuses, answered as a bare ``-32602`` with no
        #: detail -- the shape a real codex session draws for a stale pin.
        self.rejected_models: set[str] = set()

    # ── wire ──

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        params = json.loads(json.dumps(params))
        self.requests.append((method, params))
        handler = {
            "initialize": self._initialize,
            "session/new": self._session_new,
            "session/prompt": self._session_prompt,
            "session/set_config_option": self._set_config_option,
        }.get(method)
        if handler is None:
            raise AssertionError(f"codex-acp has no {method!r}: it would answer -32601")
        return handler(params)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        params = json.loads(json.dumps(params))
        if method != METHOD_CANCEL:
            raise AssertionError(f"unexpected notification {method!r}")
        self.notifications.append((method, params))

    # ── handlers ──

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        assert params["protocolVersion"] == 1, "codex-acp speaks numeric ACP v1"
        assert params["clientInfo"]["name"], "a flat clientName is not read"
        self.initialized = True
        return {
            "protocolVersion": 1,
            "agentCapabilities": {
                "loadSession": True,
                "mcpCapabilities": dict(self.MCP_CAPABILITIES),
            },
        }

    def _session_new(self, params: dict[str, Any]) -> dict[str, Any]:
        assert self.initialized, "session/new before initialize"
        assert params["cwd"], "codex-acp requires a cwd"
        assert "_meta" not in params, "codex-acp reads none of Crew's _meta"
        for element in params.get("mcpServers") or []:
            if isinstance(element, dict) and element.get("type") == "sse":
                raise CodexRpcError(-32600, "Invalid Request")
        self._next_id += 1
        sid = f"thread-{self._next_id}"
        self.sessions[sid] = {"cwd": params["cwd"], "mcpServers": params.get("mcpServers") or []}
        return {"sessionId": sid, "modes": {"currentModeId": "agent"}}

    def _session_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = params["sessionId"]
        assert sid in self.sessions, f"prompt for unknown session {sid!r}"
        self.sessions[sid].setdefault("prompts", []).append(params["prompt"])
        return {"stopReason": "end_turn"}

    def _set_config_option(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = params["sessionId"]
        assert sid in self.sessions, f"config option for unknown session {sid!r}"
        option, value = params["optionId"], params["value"]
        if option == "model" and value in self.rejected_models:
            raise CodexRpcError(-32602, "Invalid params")
        self.config_options.append((sid, option, value))
        self.sessions[sid][option] = value
        return {}


class CodexRpcError(Exception):
    """A JSON-RPC error frame the peer answered with."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{code} {message}")
        self.code = code


def _initialize_params(adapter: CodexHarness) -> dict[str, Any]:
    """The handshake, built from the harness's own seam answers.

    Assembled here rather than on the harness because the runtime owns
    ``clientInfo``: what a harness decides is the version and the capabilities.
    """
    return {
        "clientInfo": {"name": "kirocrew", "version": "0.1.2"},
        "protocolVersion": adapter.protocol_version,
        "clientCapabilities": adapter.client_capabilities,
    }


def _open_session(
    peer: FakeCodexPeer, *, cwd: str, mcp_servers: list[Any], adapter: CodexHarness | None = None
) -> str:
    """Build one session the way the harness's answers say to, and return its id."""
    adapter = adapter or CodexHarness()
    init = peer.request("initialize", _initialize_params(adapter))
    narrowed = adapter.session_mcp_servers(
        list(mcp_servers), agent_capabilities=init.get("agentCapabilities") or {}
    )
    resp = peer.request("session/new", {"cwd": cwd, "mcpServers": narrowed})
    return resp["sessionId"]


@pytest.fixture()
def adapter() -> CodexHarness:
    return CodexHarness()


def _ctx(**over: Any) -> SpawnContext:
    """A spawn context with every field the harness reads, overridable per test."""
    base: dict[str, Any] = {
        "agent": "kirocrew",
        "work_dir": "/w",
        "model": "gpt-5-codex",
        "environ": {},
        "home": Path("/h"),
        "sandbox_mode": "standard",
    }
    base.update(over)
    return SpawnContext(**base)


# ---------------------------------------------------------------------------
# Half one: the harness against the fake peer
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_harness_for_codex_returns_this_harness(self):
        resolved = harness_for(ACP_BACKEND_CODEX)
        assert isinstance(resolved, CodexHarness)
        assert resolved.backend == ACP_BACKEND_CODEX

    def test_registration_does_not_follow_the_switch(self, monkeypatch):
        """Two different questions, and conflating them breaks both answers.

        The registry answers "can the shared-process runtime drive this host?"; the
        switch answers "does a codex session take that path today?". A registry gated
        on the switch would make the harness unreachable to its own tests and to an
        operator trying the preview.
        """
        monkeypatch.delenv(ENV_CODEX_ACP_RUNTIME, raising=False)
        assert isinstance(harness_for(ACP_BACKEND_CODEX), CodexHarness)
        assert codex_runs_on_acp_runtime() is False


class TestHandshake:
    def test_initialize_is_accepted_and_answers_the_transports(self, adapter):
        peer = FakeCodexPeer()
        init = peer.request("initialize", _initialize_params(adapter))
        assert peer.initialized
        assert init["agentCapabilities"]["mcpCapabilities"] == FakeCodexPeer.MCP_CAPABILITIES

    def test_the_protocol_version_is_this_harness_own_literal(self, adapter):
        """A handshake collapsed to what every harness accepts downgrades one of them.

        Pinned as a value rather than as an import equality, so folding codex's
        version into another harness's constant fails here even when the two integers
        happen to agree.
        """
        assert adapter.protocol_version == 1
        assert harness_mod.PROTOCOL_VERSION_CODEX == 1

    def test_the_version_is_an_integer_not_a_date_string(self, adapter):
        """The TYPE is part of the contract: kiro-cli's date spelling is rejected."""
        assert isinstance(adapter.protocol_version, int)
        assert not isinstance(adapter.protocol_version, str)

    def test_a_junk_handshake_narrows_nothing_rather_than_crashing(self, adapter):
        """A shape the adapter never promised must not cost the session its tools."""
        requested = [{"name": "a", "type": "sse"}, {"name": "b", "url": "http://b"}]
        for junk in (
            {},
            {"mcpCapabilities": None},
            {"mcpCapabilities": {}},
            {"mcpCapabilities": 1},
        ):
            assert adapter.session_mcp_servers(requested, agent_capabilities=junk) is requested


class TestTwoSessionsOnOneProcess:
    """The point of the harness: N sessions, one adapter."""

    def test_two_session_new_yield_distinct_ids_on_one_peer(self):
        peer = FakeCodexPeer()
        first = _open_session(peer, cwd="/w/one", mcp_servers=[])
        second = _open_session(peer, cwd="/w/two", mcp_servers=[])
        assert first != second
        assert set(peer.sessions) == {first, second}
        assert peer.sessions[first]["cwd"] == "/w/one"
        assert peer.sessions[second]["cwd"] == "/w/two"

    def test_a_prompt_reaches_only_its_own_session(self):
        peer = FakeCodexPeer()
        first = _open_session(peer, cwd="/w/one", mcp_servers=[])
        second = _open_session(peer, cwd="/w/two", mcp_servers=[])
        peer.request("session/prompt", {"sessionId": first, "prompt": [{"text": "one"}]})
        peer.request("session/prompt", {"sessionId": second, "prompt": [{"text": "two"}]})
        peer.request("session/prompt", {"sessionId": first, "prompt": [{"text": "three"}]})
        assert [p[0]["text"] for p in peer.sessions[first]["prompts"]] == ["one", "three"]
        assert [p[0]["text"] for p in peer.sessions[second]["prompts"]] == ["two"]

    def test_each_session_carries_its_own_mcp_array(self):
        """codex-acp reads no agent spec, so the array is the session's whole surface."""
        peer = FakeCodexPeer()
        first = _open_session(peer, cwd="/w/one", mcp_servers=[{"name": "a", "url": "http://a"}])
        second = _open_session(peer, cwd="/w/two", mcp_servers=[])
        assert [s["name"] for s in peer.sessions[first]["mcpServers"]] == ["a"]
        assert peer.sessions[second]["mcpServers"] == []


class TestMcpArrayNarrowing:
    def test_an_sse_element_is_dropped_before_it_reaches_the_wire(self):
        peer = FakeCodexPeer()
        sid = _open_session(
            peer,
            cwd="/w",
            mcp_servers=[
                {"name": "keep", "url": "http://keep"},
                {"name": "drop", "type": "sse", "url": "http://drop"},
            ],
        )
        assert [s["name"] for s in peer.sessions[sid]["mcpServers"]] == ["keep"]

    def test_an_unnarrowed_sse_element_would_have_cost_the_whole_session(self, adapter):
        """Why the narrowing is not cosmetic: -32600 is the WHOLE request, not one server.

        Sent deliberately unnarrowed, so this fails if the peer ever stops modelling
        the refusal and the test above starts passing vacuously.
        """
        peer = FakeCodexPeer()
        peer.request("initialize", _initialize_params(adapter))
        with pytest.raises(CodexRpcError) as exc:
            peer.request(
                "session/new",
                {
                    "cwd": "/w",
                    "mcpServers": [
                        {"name": "keep", "url": "http://keep"},
                        {"name": "drop", "type": "sse", "url": "http://drop"},
                    ],
                },
            )
        assert exc.value.code == -32600
        assert peer.sessions == {}

    def test_a_narrowed_list_is_not_aliased(self, adapter):
        """A session must not be able to mutate the caller's list after the fact."""
        source: list[Any] = [{"name": "a", "url": "http://a"}]
        narrowed = adapter.session_mcp_servers(
            source, agent_capabilities={"mcpCapabilities": {"http": True}}
        )
        source.append({"name": "b", "url": "http://b"})
        assert [s["name"] for s in narrowed] == ["a"]


class TestSessionExtras:
    @pytest.mark.asyncio
    async def test_extras_are_empty_and_carry_no_custom_agents(self, adapter):
        """``custom_agents`` is a kiro-family field; codex has no such channel."""
        extras = await adapter.session_extras("kirocrew", work_dir="/w")
        assert extras.custom_agents is None

    @pytest.mark.asyncio
    async def test_extras_stay_empty_whatever_is_passed(self, adapter):
        for kwargs in (
            {"work_dir": None},
            {"work_dir": "/w", "member_dispatch": True},
            {"work_dir": "/w", "mcp_gateway_overlay": object()},
        ):
            assert (await adapter.session_extras("a", **kwargs)).custom_agents is None

    def test_no_session_file_is_named_on_load(self, adapter):
        """The adapter locates the session from its id; a path it cannot read is worse."""
        assert adapter.wants_session_file_on_load is False


class TestModelAndEffortSwitch:
    def test_a_model_write_is_accepted_per_session(self):
        peer = FakeCodexPeer()
        sid = _open_session(peer, cwd="/w", mcp_servers=[])
        peer.request(
            "session/set_config_option",
            {"sessionId": sid, "optionId": "model", "value": "gpt-5-codex"},
        )
        assert peer.config_options == [(sid, "model", "gpt-5-codex")]

    def test_there_is_no_session_set_model_to_send(self):
        """The request the CONFIG_OPTION answer keeps Crew from sending."""
        peer = FakeCodexPeer()
        sid = _open_session(peer, cwd="/w", mcp_servers=[])
        with pytest.raises(AssertionError, match="session/set_model"):
            peer.request("session/set_model", {"sessionId": sid, "modelId": "gpt-5-codex"})

    def test_a_refused_value_is_a_bare_invalid_params(self):
        """The frame that must not be read as a protocol failure.

        Read as one, the session init failed and a stale model pin from another
        backend killed every codex session at startup.
        """
        peer = FakeCodexPeer()
        peer.rejected_models = {"kiro-default"}
        sid = _open_session(peer, cwd="/w", mcp_servers=[])
        with pytest.raises(CodexRpcError) as exc:
            peer.request(
                "session/set_config_option",
                {"sessionId": sid, "optionId": "model", "value": "kiro-default"},
            )
        assert exc.value.code == -32602
        assert str(exc.value).endswith("Invalid params")

    def test_effort_is_its_own_write(self):
        peer = FakeCodexPeer()
        sid = _open_session(peer, cwd="/w", mcp_servers=[])
        peer.request(
            "session/set_config_option", {"sessionId": sid, "optionId": "effort", "value": "high"}
        )
        assert (sid, "effort", "high") in peer.config_options


class TestPermissionRouting:
    """The routing ANSWER lives in ``ACP_BACKEND_ROUTING``, which both drivers read.

    The harness declares no routing member, so what is left to prove here is the
    wire half: the option that table names is accepted on every session of one
    shared process, not just the first.
    """

    def test_the_routing_option_is_accepted_on_every_session_of_the_process(self):
        peer = FakeCodexPeer()
        first = _open_session(peer, cwd="/w/one", mcp_servers=[])
        second = _open_session(peer, cwd="/w/two", mcp_servers=[])
        option, value = ("mode", "read-only")
        for sid in (first, second):
            peer.request(
                "session/set_config_option",
                {"sessionId": sid, "optionId": option, "value": value},
            )
        assert peer.sessions[first]["mode"] == "read-only"
        assert peer.sessions[second]["mode"] == "read-only"


class TestTeardown:
    def test_the_verb_is_plain_session_cancel(self, adapter):
        assert adapter.teardown.method == METHOD_CANCEL
        assert adapter.teardown.method == "session/cancel"

    def test_no_kiro_family_delete_verb_is_sent(self, adapter):
        """Either kiro-family verb would draw -32601 and leave the session on the process."""
        assert adapter.teardown.method != "_kiro.dev/session/terminate"
        assert adapter.teardown.method != "_kiro/session/delete"

    def test_the_policy_carries_the_verb_and_nothing_else(self, adapter):
        """codex neither evicts nor deletes: the adapter keeps the Codex thread.

        A dropped session is unreachable from Crew, not erased from Codex, so no
        retention promise is made or broken here.
        """
        assert adapter.teardown == TeardownPolicy(method=METHOD_CANCEL)

    def test_teardown_cancels_one_session_and_leaves_the_other_running(self, adapter):
        peer = FakeCodexPeer()
        first = _open_session(peer, cwd="/w/one", mcp_servers=[])
        second = _open_session(peer, cwd="/w/two", mcp_servers=[])
        peer.notify(adapter.teardown.method, {"sessionId": first})
        assert peer.notifications == [(METHOD_CANCEL, {"sessionId": first})]
        peer.request("session/prompt", {"sessionId": second, "prompt": [{"text": "still here"}]})


class TestWhatThisHarnessDoesNotHave:
    def test_no_inbound_request_is_claimed(self, adapter):
        assert adapter.host_answered_methods == ()

    @pytest.mark.asyncio
    async def test_answering_one_raises_rather_than_returning_an_empty_result(self, adapter):
        """An empty result would answer a frame this harness never claimed."""
        with pytest.raises(NotImplementedError, match="_kiro/auth/getAccessToken"):
            await adapter.answer_request("_kiro/auth/getAccessToken")

    def test_only_the_standard_session_update_spelling_is_accepted(self, adapter):
        aliases = adapter.notification_aliases
        assert aliases.session_update == (METHOD_SESSION_UPDATE,)
        assert aliases.subagent_list_update == ""
        assert aliases.mcp_init == ()

    def test_the_kiro_family_vocabulary_is_not_inherited(self, adapter):
        """KAS speaks ``_kiro.dev/*`` because it is reached THROUGH kiro-cli. codex is not."""
        from kiro_crew.acp.harness._common import KIRO_FAMILY_ALIASES

        assert adapter.notification_aliases != KIRO_FAMILY_ALIASES

    def test_the_peer_raises_nothing_at_crew_during_a_session(self, adapter):
        peer = FakeCodexPeer()
        sid = _open_session(peer, cwd="/w", mcp_servers=[])
        peer.request("session/prompt", {"sessionId": sid, "prompt": [{"text": "hi"}]})
        peer.notify(adapter.teardown.method, {"sessionId": sid})
        assert [m for m, _ in peer.requests] == ["initialize", "session/new", "session/prompt"]


@pytest.fixture()
def mask_resolved(monkeypatch):
    """Stub the credential mask so an argv test does not touch the real filesystem.

    The mask's own behaviour is asserted in ``TestSpawnMasks``; here it only has to
    not run a sandbox probe.
    """

    async def _preflight(_fn, _backend, _mode):
        return ("/h/.aws",)

    from kiro_crew.acp import client as client_mod

    monkeypatch.setattr(client_mod, "_run_preflight_bounded", _preflight)
    monkeypatch.setattr(
        harness_mod.acp_tool_gate, "adapter_expose_files", lambda b, h: ("/h/.aws/config",)
    )


class TestSpawn:
    @pytest.mark.asyncio
    async def test_the_resolved_entry_is_the_whole_argv(self, adapter, mask_resolved):
        from kiro_crew.acp import client as client_mod

        with patch.object(
            client_mod, "_resolve_codex_acp_bin", return_value=(["/n/node", "/p/index.js"], "/s")
        ):
            plan = await adapter.resolve_spawn(_ctx())
        assert plan.argv == ["/n/node", "/p/index.js"]

    @pytest.mark.asyncio
    async def test_no_agent_and_no_model_flag_is_appended(self, adapter, mask_resolved):
        """codex takes no argv of its own: any invocation blocks on stdin."""
        from kiro_crew.acp import client as client_mod

        with patch.object(
            client_mod, "_resolve_codex_acp_bin", return_value=(["/n/node", "/p/index.js"], "/s")
        ):
            plan = await adapter.resolve_spawn(_ctx())
        assert "--agent" not in plan.argv
        assert "--model" not in plan.argv
        assert "kirocrew" not in plan.argv
        assert "gpt-5-codex" not in plan.argv

    @pytest.mark.asyncio
    async def test_crew_never_owns_this_host_credential(self, adapter, mask_resolved):
        """codex raises nothing at Crew, so a process expecting a callback would wait."""
        from kiro_crew.acp import client as client_mod

        with patch.object(
            client_mod, "_resolve_codex_acp_bin", return_value=(["/n/node", "/p/index.js"], "/s")
        ):
            plan = await adapter.resolve_spawn(_ctx())
        assert plan.host_auth is False

    @pytest.mark.asyncio
    async def test_a_missing_adapter_aborts_the_spawn_with_the_searched_path(self, adapter):
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.session_handle import AcpRuntimeError

        with patch.object(client_mod, "_resolve_codex_acp_bin", return_value=(None, "/one:/two")):
            with pytest.raises(AcpRuntimeError) as exc:
                await adapter.resolve_spawn(_ctx())
        message = str(exc.value)
        assert "codex-acp" in message
        assert "@agentclientprotocol/codex-acp" in message
        assert "CODEX_ACP_BIN" in message
        assert "/one" in message and "/two" in message

    @pytest.mark.asyncio
    async def test_the_resolver_is_delegated_not_duplicated(self, adapter, mask_resolved):
        """One resolution order, so "why did it pick that one?" has one answer."""
        from kiro_crew.acp import client as client_mod

        with patch.object(
            client_mod, "_resolve_codex_acp_bin", return_value=(["/n/node", "/p/i.js"], "/s")
        ) as resolver:
            await adapter.resolve_spawn(_ctx())
        assert resolver.call_count == 1

    def test_the_kiro_api_key_is_taken_out_of_the_child_environment(self, adapter):
        """A foreign adapter must never receive it. Same as the AcpClient path does."""
        env = {"KIRO_API_KEY": "secret", "PATH": "/usr/bin"}
        adapter.apply_spawn_env(env)
        assert "KIRO_API_KEY" not in env
        assert env["PATH"] == "/usr/bin"

    def test_codex_path_is_left_exactly_as_the_operator_set_it(self, adapter):
        env = {"CODEX_PATH": "/opt/my-codex"}
        adapter.apply_spawn_env(env)
        assert env["CODEX_PATH"] == "/opt/my-codex"

    def test_crew_own_sandbox_is_the_only_confinement(self, adapter):
        """A Node adapter carries no OS sandbox for Crew's to nest inside or defer to."""
        assert adapter.internal_sandbox is False

    def test_no_pod_home_remap(self, adapter):
        assert adapter.pod_home_remap is False

    def test_nothing_was_selected_at_spawn_for_a_later_check_to_confirm(self, adapter):
        assert adapter.verifies_agent_activation is False


class TestSpawnMasks:
    """The refusal that must happen BEFORE the process exists."""

    @pytest.mark.asyncio
    async def test_the_mask_is_resolved_and_the_carve_out_is_projected_over_it(self):
        from kiro_crew.acp import client as client_mod

        async def _preflight(_fn, backend, mode):
            assert (backend, mode) == (ACP_BACKEND_CODEX, "standard")
            return ("/h/.aws",)

        with (
            patch.object(client_mod, "_run_preflight_bounded", new=_preflight),
            patch.object(
                harness_mod.acp_tool_gate, "adapter_expose_files", return_value=("/h/.aws/config",)
            ) as expose,
        ):
            hidden, exposed = await harness_mod.resolve_spawn_masks("standard")
        assert hidden == ("/h/.aws",)
        assert exposed == ("/h/.aws/config",)
        # Projected over the mask just resolved, never a re-derived one -- re-deriving
        # puts a filesystem read back on the event loop.
        assert expose.call_args.args == (ACP_BACKEND_CODEX, ("/h/.aws",))

    @pytest.mark.asyncio
    async def test_a_refused_preflight_stops_the_spawn_rather_than_returning_empty(self):
        """An enforced adapter started with its mask dropped has no control at all."""
        from kiro_crew.acp import client as client_mod

        async def _refuse(*_args, **_kwargs):
            raise RuntimeError("sandbox floor refused")

        with patch.object(client_mod, "_run_preflight_bounded", new=_refuse):
            with pytest.raises(RuntimeError, match="sandbox floor refused"):
                await harness_mod.resolve_spawn_masks("off")

    @pytest.mark.asyncio
    async def test_the_spawn_refuses_a_tier_that_would_drop_the_mask(self):
        """The refusal reaches the SPAWN, not just the helper.

        An enforced host that spawned on `off` would run a third-party binary with
        the operator's credential homes readable and nothing compensating for it, so
        the whole spawn has to fail rather than return an empty mask.
        """
        from kiro_crew.acp import client as client_mod

        async def _refuse(*_args, **_kwargs):
            raise RuntimeError("sandbox floor refused")

        with (
            patch.object(
                client_mod, "_resolve_codex_acp_bin", return_value=(["/n/node", "/p/i.js"], "/s")
            ),
            patch.object(client_mod, "_run_preflight_bounded", new=_refuse),
        ):
            with pytest.raises(RuntimeError, match="sandbox floor refused"):
                await CodexHarness().resolve_spawn(_ctx(sandbox_mode="off"))

    @pytest.mark.asyncio
    async def test_the_spawn_hands_the_configured_tier_to_the_refusal(self):
        """The tier comes off the context, never re-read from config.

        Re-reading it would resolve a different tier than the one the argv was built
        for, which is the mismatch carrying the mask on the plan exists to prevent.
        """
        from kiro_crew.acp import client as client_mod

        seen: list[str] = []

        async def _preflight(_fn, backend, mode):
            seen.append(mode)
            return ("/h/.aws",)

        with (
            patch.object(
                client_mod, "_resolve_codex_acp_bin", return_value=(["/n/node", "/p/i.js"], "/s")
            ),
            patch.object(client_mod, "_run_preflight_bounded", new=_preflight),
            patch.object(
                harness_mod.acp_tool_gate, "adapter_expose_files", return_value=("/h/.aws/config",)
            ),
        ):
            plan = await CodexHarness().resolve_spawn(_ctx(sandbox_mode="strict"))
        assert seen == ["strict"]
        assert plan.extra_hidden_dirs == ("/h/.aws",)

    def test_resolve_spawn_resolves_the_mask_with_the_tier(self):
        """No mask-without-a-tier path is left behind for a caller to reach for."""
        import inspect

        source = inspect.getsource(CodexHarness.resolve_spawn)
        assert "resolve_spawn_masks(ctx.sandbox_mode)" in source
        assert "extra_hidden_dirs=" in source
        assert not hasattr(harness_mod, "resolve_mask_only")


class TestReclaimIsInheritedUnchanged:
    def test_the_operator_configured_thresholds_pass_straight_through(self, adapter):
        """No guessed constant: the codex profile has not been measured yet.

        Pinned so adding a number here is a visible edit rather than a quiet one,
        and so a future measurement lands with a test that already says what the
        answer used to be.
        """
        policy = adapter.reclaim_policy(max_age_secs=3600.0, max_rss_mb=500.0)
        assert (policy.max_age_secs, policy.max_rss_mb) == (3600.0, 500.0)


# ---------------------------------------------------------------------------
# Half two: the switch, and what it must not change while it is off
# ---------------------------------------------------------------------------


def _build_provider(backend: str) -> AcpProvider:
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=backend)
    provider._client = MagicMock()
    provider._client.backend = backend
    return provider


class TestTheSwitchIsOffByDefault:
    def test_an_unset_variable_reads_as_off(self, monkeypatch):
        monkeypatch.delenv(ENV_CODEX_ACP_RUNTIME, raising=False)
        assert codex_runs_on_acp_runtime() is False

    def test_off_the_gate_answers_the_shipped_set_verbatim(self, monkeypatch):
        monkeypatch.delenv(ENV_CODEX_ACP_RUNTIME, raising=False)
        assert acp_runtime_backends() == ACP_BACKENDS_ACP_RUNTIME

    def test_off_codex_still_takes_the_acp_client_path(self, monkeypatch):
        monkeypatch.delenv(ENV_CODEX_ACP_RUNTIME, raising=False)
        assert _build_provider(ACP_BACKEND_CODEX).is_acp_runtime_backend is False

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe", " "])
    def test_a_falsey_or_unrecognised_value_reads_as_off(self, monkeypatch, value):
        """An operator exporting ``=0`` to keep a preview off must not get it on.

        The mistake would be silent: the session starts either way, just on the other
        transport.
        """
        monkeypatch.setenv(ENV_CODEX_ACP_RUNTIME, value)
        assert codex_runs_on_acp_runtime() is False
        assert acp_runtime_backends() == ACP_BACKENDS_ACP_RUNTIME

    def test_the_other_harnesses_are_unaffected_either_way(self, monkeypatch):
        for value in ("0", "1"):
            monkeypatch.setenv(ENV_CODEX_ACP_RUNTIME, value)
            assert _build_provider(ACP_BACKEND_KIRO).is_acp_runtime_backend is True
            assert _build_provider(ACP_BACKEND_KAS).is_acp_runtime_backend is True
            assert _build_provider(ACP_BACKEND_CLAUDE).is_acp_runtime_backend is False


class TestTheSwitchOn:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " on "])
    def test_a_truthy_value_reads_as_on(self, monkeypatch, value):
        monkeypatch.setenv(ENV_CODEX_ACP_RUNTIME, value)
        assert codex_runs_on_acp_runtime() is True

    def test_on_codex_joins_the_runtime_answer(self, monkeypatch):
        monkeypatch.setenv(ENV_CODEX_ACP_RUNTIME, "1")
        assert acp_runtime_backends() == ACP_BACKENDS_ACP_RUNTIME | {ACP_BACKEND_CODEX}

    def test_on_the_provider_reads_as_a_runtime_backend(self, monkeypatch):
        monkeypatch.setenv(ENV_CODEX_ACP_RUNTIME, "1")
        assert _build_provider(ACP_BACKEND_CODEX).is_acp_runtime_backend is True

    def test_on_the_background_path_may_reach_codex(self, monkeypatch):
        monkeypatch.setenv(ENV_CODEX_ACP_RUNTIME, "1")
        from kiro_crew.session import _bg_runtime_backends

        assert ACP_BACKEND_CODEX in _bg_runtime_backends()

    def test_off_the_background_path_may_not(self, monkeypatch):
        monkeypatch.delenv(ENV_CODEX_ACP_RUNTIME, raising=False)
        from kiro_crew.session import _bg_runtime_backends

        assert ACP_BACKEND_CODEX not in _bg_runtime_backends()

    def test_the_switch_is_read_per_call_not_cached_at_import(self, monkeypatch):
        """A value frozen at import answers for whichever ran first: gateway or test."""
        monkeypatch.delenv(ENV_CODEX_ACP_RUNTIME, raising=False)
        assert codex_runs_on_acp_runtime() is False
        monkeypatch.setenv(ENV_CODEX_ACP_RUNTIME, "1")
        assert codex_runs_on_acp_runtime() is True
        monkeypatch.delenv(ENV_CODEX_ACP_RUNTIME, raising=False)
        assert codex_runs_on_acp_runtime() is False


class TestTheCapabilitySetsKeepTheirMembers:
    """The switch widens a derived ANSWER; it must not edit the vocabulary."""

    def test_the_shipped_runtime_set_is_unchanged(self, monkeypatch):
        monkeypatch.setenv(ENV_CODEX_ACP_RUNTIME, "1")
        assert ACP_BACKENDS_ACP_RUNTIME == frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS})

    def test_session_sharing_does_not_follow_the_switch(self, monkeypatch):
        """Running on AcpRuntime is necessary for sharing, never sufficient.

        KAS is the precedent: on the runtime, excluded from sharing until keep-aware
        teardown lands. codex is in the same position, and a switch that granted
        sharing as a side effect would hand multiplexed subagent sessions to a host
        whose teardown verb cannot erase a transcript.
        """
        monkeypatch.setenv(ENV_CODEX_ACP_RUNTIME, "1")
        assert ACP_BACKEND_CODEX not in ACP_BACKENDS_SESSION_SHARING
        assert _build_provider(ACP_BACKEND_CODEX).is_session_sharing_eligible is False
