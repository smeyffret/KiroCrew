"""The crew container's channel isolation must stay in step with the gateway.

The container that a remote crew runs as serves exactly one caller: the customer's
HTTP turn, through its front process. It has nobody to reach on a messaging channel,
so it disables every transport before the backend starts -- by name in a config file
it owns, and by refusing to pass a channel credential into the launch environment.

Both of those are lists of names, and a list of names is only as good as its last
update. This file is what keeps them updated: it compares them against
``kiro_crew.channels.builtin_channel_descriptors()``, the registry the gateway itself
derives its governed channel roster from. A channel added there reds this test until
the container turns it off, which is the difference between an isolation claim and an
isolation that holds.

The container's source is READ, never imported. ``crew/runtime/`` is a docker build
context whose modules import each other as top-level ``container.*``, and
``test_spawn_audit.py::test_container_image_assets_are_not_imported`` pins that the
gateway never imports the tree. So the two constants are pulled out of the file's AST.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC = (
    REPO_ROOT
    / "src"
    / "kiro_crew"
    / "apps"
    / "builtins"
    / "aws_control"
    / "crew"
    / "runtime"
    / "container"
    / "supervisor"
    / "backend.py"
)


def _literal(name: str) -> object:
    """The value assigned to a module-level constant, read from the source.

    ``ast.literal_eval`` on the assigned node, so only a literal is accepted -- a
    constant computed at import time would raise here rather than be silently read as
    empty, which is the failure mode that would make this whole file vacuous.
    """
    tree = ast.parse(BACKEND_SRC.read_text(encoding="utf-8"))
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        value = node.value
        assert value is not None, f"{name} has no assigned value"
        if isinstance(value, ast.Call):  # frozenset({...}) and friends
            assert len(value.args) == 1, f"{name} is a call this reader cannot evaluate"
            return ast.literal_eval(value.args[0])
        return ast.literal_eval(value)
    raise AssertionError(f"{name} is not assigned at module level in {BACKEND_SRC}")


@pytest.fixture(scope="module")
def registry():
    """The gateway's own channel roster, with each channel's credential variables."""
    from kiro_crew.channels import builtin_channel_descriptors
    from kiro_crew.messaging.registry import governed_members

    descriptors = tuple(builtin_channel_descriptors())
    assert descriptors, "the channel registry is empty; every assertion here would be vacuous"
    return {
        "members": set(governed_members(descriptors)),
        "credentials": {name for d in descriptors for name in d.credentials},
    }


def test_the_container_disables_every_channel_the_gateway_can_start(registry) -> None:
    sections = set(_literal("CHANNEL_SECTIONS"))
    missing = sorted(registry["members"] - sections)
    assert not missing, (
        "the crew container does not disable these transports, so a crew in a container "
        f"can come up on them: {missing}. Add them to CHANNEL_SECTIONS in {BACKEND_SRC}."
    )


def test_the_container_names_no_channel_the_gateway_does_not_have(registry) -> None:
    """The other direction, because a stale name is a false claim of coverage.

    A section the gateway does not have is written into the config and ignored, which
    makes the list read as broader than its reach.
    """
    sections = set(_literal("CHANNEL_SECTIONS"))
    unknown = sorted(sections - registry["members"])
    assert not unknown, f"these are not channels the gateway can start: {unknown}"


def test_the_container_strips_every_channel_credential_the_gateway_reads(registry) -> None:
    stripped = set(_literal("CHANNEL_CRED_ENV"))
    missing = sorted(registry["credentials"] - stripped)
    assert not missing, (
        "these channel credentials would reach the container's backend, and through it "
        f"the auto-approving model worker: {missing}. Add them to CHANNEL_CRED_ENV in "
        f"{BACKEND_SRC}."
    )


def test_the_container_strips_no_variable_the_gateway_never_reads(registry) -> None:
    """A name nothing reads strips nothing while making the list look complete."""
    stripped = set(_literal("CHANNEL_CRED_ENV"))
    unknown = sorted(stripped - registry["credentials"])
    assert not unknown, (
        f"these variables are in no channel's credential set, so removing them protects "
        f"nothing: {unknown}"
    )
