"""Mattermost bridge token lifecycle: revoke-on-stop and cleanup-on-start.

Each time the bridge starts it creates a personal-access token for every bot.
Without revocation those tokens pile up indefinitely. Two properties are tested:

1. stop() revokes every token minted by this process.
2. After minting a new token the bridge revokes every other Switch token on the
   bot, left by an earlier process (e.g. after a crash that skipped stop()).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from switch_core.bridges.collaboration.adapter import AgentIdentity
from switch_core.bridges.collaboration.mattermost.adapter import (
    MattermostAdapter,
    MattermostConnectionConfig,
)
from tests.switch_core.bridges.collaboration.test_bridge_agent_display_names import (
    _agent,
    _bridge,
    _run,
)

_SWITCHDEV = AgentIdentity(
    agent_id="agent-1",
    name="switchdev",
    description="a Switch agent",
    display_name=None,
)

_WORKER = AgentIdentity(
    agent_id="agent-2",
    name="worker",
    description="another agent",
    display_name=None,
)


class _TokenAwareAdmin:
    """Fake Mattermost admin driver that tracks token creates and revocations
    and lets the test seed pre-existing stale tokens for a bot."""

    def __init__(self, stale_tokens_by_bot: dict[str, list[dict[str, Any]]]) -> None:
        self._tokens = stale_tokens_by_bot
        self.revoked: list[str] = []
        self.token_pages: list[str] = []
        self._token_seq = 0
        self.client = SimpleNamespace(make_request=self._make_request)
        self.teams = SimpleNamespace(add_user_to_team=lambda team_id, body: None)

    def login(self) -> None:
        return None

    def _make_request(
        self, method: str, endpoint: str, options: dict[str, Any] | None = None
    ) -> Any:
        payload = self._respond(method, endpoint, options)
        return SimpleNamespace(json=lambda: payload)

    def _respond(
        self, method: str, endpoint: str, options: dict[str, Any] | None
    ) -> Any:
        if method == "get" and endpoint == "/config":
            return {"TeamSettings": {"TeammateNameDisplay": "full_name"}}
        if method == "get" and endpoint.startswith("/bots?"):
            return []
        if method == "post" and endpoint == "/bots":
            assert options is not None
            return {"user_id": f"bot-{options['username']}"}
        if method == "put" and endpoint.startswith("/bots/"):
            return {}
        if (
            method == "post"
            and "/tokens" in endpoint
            and endpoint != "/users/tokens/revoke"
        ):
            assert options is not None
            self._token_seq += 1
            bot_id = endpoint.split("/users/")[1].split("/tokens")[0]
            token_id = f"tok-{self._token_seq}"
            self._tokens.setdefault(bot_id, []).append(
                {"id": token_id, "description": options["description"]}
            )
            return {"token": f"secret-{self._token_seq}", "id": token_id}
        if method == "get" and "/tokens?" in endpoint:
            bot_id = endpoint.split("/users/")[1].split("/tokens")[0]
            query = dict(
                part.split("=") for part in endpoint.split("?", 1)[1].split("&")
            )
            page, per_page = int(query["page"]), int(query["per_page"])
            self.token_pages.append(endpoint)
            tokens = self._tokens.get(bot_id, [])
            return tokens[page * per_page : (page + 1) * per_page]
        if method == "post" and endpoint == "/users/tokens/revoke":
            assert options is not None
            self.revoked.append(str(options["token_id"]))
            return {}
        raise AssertionError(f"unexpected Mattermost call: {method} {endpoint}")


class _SilentBot:
    websocket: object | None = None

    def login(self) -> None:
        return None

    async def init_websocket(self, handler: Any) -> None:
        await asyncio.Event().wait()


def _adapter(
    stale_tokens_by_bot: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[MattermostAdapter, _TokenAwareAdmin]:
    bridge = _bridge(_agent("switchdev", "Switch Dev"), _agent("worker", "Worker"))
    adapter = MattermostAdapter(
        config=MattermostConnectionConfig(
            url="http://mattermost.invalid",
            admin_user="admin",
            admin_password="pw",
            team_name="team",
        )
    )
    admin = _TokenAwareAdmin(stale_tokens_by_bot or {})
    adapter._admin_driver = admin  # type: ignore[assignment]
    adapter._team_id = "team-1"
    adapter.set_agent_presentation_resolver(bridge._agent_presentation)

    async def set_bot_icon(bot_id: str, agent_name: str) -> None:
        return None

    adapter._set_bot_icon = set_bot_icon  # type: ignore[method-assign]
    adapter._create_driver = lambda **kwargs: _SilentBot()  # type: ignore[assignment,method-assign,return-value]
    return adapter, admin


# ── revoke on stop ────────────────────────────────────────────────────────────


def test_stop_revokes_agent_bot_token() -> None:
    adapter, admin = _adapter()

    async def go() -> None:
        adapter._main_loop = asyncio.get_running_loop()
        await adapter.create_agent_identity(_SWITCHDEV)
        await adapter.stop()

    _run(go())

    assert admin.revoked == ["tok-1"]


def test_stop_revokes_tokens_for_all_agent_bots() -> None:
    adapter, admin = _adapter()

    async def go() -> None:
        adapter._main_loop = asyncio.get_running_loop()
        await adapter.create_agent_identity(_SWITCHDEV)
        await adapter.create_agent_identity(_WORKER)
        await adapter.stop()

    _run(go())

    assert sorted(admin.revoked) == ["tok-1", "tok-2"]


def test_remove_agent_identity_revokes_its_token() -> None:
    adapter, admin = _adapter()

    async def go() -> None:
        adapter._main_loop = asyncio.get_running_loop()
        await adapter.create_agent_identity(_SWITCHDEV)
        await adapter.create_agent_identity(_WORKER)
        await adapter.remove_agent_identity(_SWITCHDEV)

    _run(go())

    assert admin.revoked == ["tok-1"]


# ── revoke stale tokens on start ─────────────────────────────────────────────


def test_stale_switch_tokens_are_revoked_but_the_new_one_kept() -> None:
    stale = [
        {"id": "old-tok-1", "description": "Switch bridge token for switchdev"},
        {"id": "old-tok-2", "description": "Switch bridge token for switchdev"},
    ]
    adapter, admin = _adapter(stale_tokens_by_bot={"bot-switchdev": stale})

    async def go() -> None:
        adapter._main_loop = asyncio.get_running_loop()
        await adapter.create_agent_identity(_SWITCHDEV)

    _run(go())

    assert sorted(admin.revoked) == ["old-tok-1", "old-tok-2"]
    assert adapter._agent_bots["switchdev"]["token_id"] == "tok-1"


def test_non_switch_tokens_are_not_revoked_on_start() -> None:
    stale = [
        {"id": "user-tok-1", "description": "My personal token"},
        {"id": "old-tok-1", "description": "Switch bridge token for switchdev"},
    ]
    adapter, admin = _adapter(stale_tokens_by_bot={"bot-switchdev": stale})

    async def go() -> None:
        adapter._main_loop = asyncio.get_running_loop()
        await adapter.create_agent_identity(_SWITCHDEV)

    _run(go())

    assert "user-tok-1" not in admin.revoked
    assert "old-tok-1" in admin.revoked


def test_no_stale_tokens_means_no_revocations() -> None:
    adapter, admin = _adapter()

    async def go() -> None:
        adapter._main_loop = asyncio.get_running_loop()
        await adapter.create_agent_identity(_SWITCHDEV)

    _run(go())

    assert admin.revoked == []


def test_stale_tokens_are_found_past_the_first_page() -> None:
    stale = [
        {"id": f"old-tok-{n}", "description": "Switch bridge token for switchdev"}
        for n in range(250)
    ]
    adapter, admin = _adapter(stale_tokens_by_bot={"bot-switchdev": stale})

    async def go() -> None:
        adapter._main_loop = asyncio.get_running_loop()
        await adapter.create_agent_identity(_SWITCHDEV)

    _run(go())

    assert len(admin.token_pages) == 2
    assert sorted(admin.revoked) == sorted(f"old-tok-{n}" for n in range(250))


def test_admin_bot_keeps_only_its_new_token() -> None:
    stale = [{"id": "old-admin-tok", "description": "Switch admin bot token"}]
    adapter, admin = _adapter(stale_tokens_by_bot={"bot-switch-admin": stale})

    async def go() -> None:
        adapter._main_loop = asyncio.get_running_loop()
        await adapter._ensure_admin_bot()
        assert admin.revoked == ["old-admin-tok"]
        await adapter.stop()

    _run(go())

    assert admin.revoked == ["old-admin-tok", "tok-1"]


def test_a_renamed_agent_still_has_its_token_revoked_on_removal() -> None:
    adapter, admin = _adapter()
    renamed = AgentIdentity(
        agent_id="agent-1",
        name="switchdev2",
        description="a Switch agent",
        display_name=None,
    )

    async def go() -> None:
        adapter._main_loop = asyncio.get_running_loop()
        await adapter.create_agent_identity(_SWITCHDEV)
        await adapter.rename_agent_identity("switchdev", renamed)
        await adapter.remove_agent_identity(renamed)

    _run(go())

    assert admin.revoked == ["tok-1"]
