"""Unit tests for GoogleChatAdapter and the Pub/Sub listener's event decode.

Imports the real app modules when the full stack is available (the normal
case — this file sorts before other unit tests, so unlike
test_whatsapp_adapter it must NOT leave stub modules in sys.modules where
later-collected tests would import them). Only when the app stack can't be
imported (standalone runs without deps) does it fall back to the whatsapp
test's stub-and-load-from-path pattern.
"""
import base64
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

try:
    from app.services.platform_adapters.google_chat_adapter import (  # noqa: E402
        GoogleChatAdapter,
        MAX_TEXT_LEN,
    )
    from app.services.google_chat_listener_service import (  # noqa: E402
        GoogleChatPubSubListener,
    )
except Exception:  # pragma: no cover — standalone fallback without app deps
    def _install_stubs():
        models_pkg = types.ModuleType("app.models")
        ep_mod = types.ModuleType("app.models.external_platform")

        class _EP:  # noqa: N801
            pass

        ep_mod.ExternalPlatform = _EP
        eum_mod = types.ModuleType("app.models.external_user_mapping")

        class _EUM:  # noqa: N801
            pass

        eum_mod.ExternalUserMapping = _EUM

        settings_pkg = types.ModuleType("app.settings")
        config_mod = types.ModuleType("app.settings.config")
        bow_cfg = types.SimpleNamespace(base_url="http://localhost:3000")
        config_mod.settings = types.SimpleNamespace(bow_config=bow_cfg)

        sys.modules.setdefault("app", types.ModuleType("app"))
        sys.modules.setdefault("app.models", models_pkg)
        sys.modules.setdefault("app.models.external_platform", ep_mod)
        sys.modules.setdefault("app.models.external_user_mapping", eum_mod)
        sys.modules.setdefault("app.settings", settings_pkg)
        sys.modules.setdefault("app.settings.config", config_mod)

    _install_stubs()

    def _load(name: str, path: Path):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    _base = _load(
        "app.services.platform_adapters.base_adapter",
        BACKEND_ROOT / "app" / "services" / "platform_adapters" / "base_adapter.py",
    )
    pa_pkg = types.ModuleType("app.services")
    pa_pkg2 = types.ModuleType("app.services.platform_adapters")
    pa_pkg2.__path__ = [str(BACKEND_ROOT / "app" / "services" / "platform_adapters")]
    sys.modules.setdefault("app.services", pa_pkg)
    sys.modules.setdefault("app.services.platform_adapters", pa_pkg2)
    sys.modules.setdefault("app.services.platform_adapters.base_adapter", _base)

    gc_mod = _load(
        "app.services.platform_adapters.google_chat_adapter",
        BACKEND_ROOT / "app" / "services" / "platform_adapters" / "google_chat_adapter.py",
    )
    GoogleChatAdapter = gc_mod.GoogleChatAdapter
    MAX_TEXT_LEN = gc_mod.MAX_TEXT_LEN

    listener_mod = _load(
        "app.services.google_chat_listener_service",
        BACKEND_ROOT / "app" / "services" / "google_chat_listener_service.py",
    )
    GoogleChatPubSubListener = listener_mod.GoogleChatPubSubListener


# ---------- Fakes ----------


class FakePlatform:
    def __init__(self, credentials=None, config=None):
        self._credentials = credentials or {}
        self.platform_config = config or {}

    def decrypt_credentials(self):
        return dict(self._credentials)


def make_adapter():
    creds = {"service_account_json": {"type": "service_account", "client_email": "sa@p.iam"}}
    cfg = {"pubsub_subscription": "projects/p/subscriptions/s", "auto_link_by_email": True}
    return GoogleChatAdapter(FakePlatform(credentials=creds, config=cfg))


def _message_event(
    text="hi",
    argument_text=None,
    space_type="DIRECT_MESSAGE",
    thread="spaces/AAA/threads/T1",
    thread_reply=False,
    sender_type="HUMAN",
):
    """Shape captured from a live Pub/Sub delivery (2026-07 verification)."""
    msg = {
        "name": "spaces/AAA/messages/M1.M1",
        "sender": {
            "name": "users/117300060449094535067",
            "displayName": "Yochay Ettun",
            "email": "yochay@bagofwords.com",
            "type": sender_type,
        },
        "createTime": "2026-07-25T14:46:55.237167Z",
        "text": text,
        "thread": {"name": thread},
        "space": {"name": "spaces/AAA", "spaceType": space_type},
    }
    if argument_text is not None:
        msg["argumentText"] = argument_text
    if thread_reply:
        msg["threadReply"] = True
    return {
        "type": "MESSAGE",
        "eventTime": "2026-07-25T14:46:55.237167Z",
        "message": msg,
        "user": msg["sender"],
        "space": msg["space"],
    }


# ---------- process_incoming_message ----------


@pytest.mark.asyncio
async def test_dm_message_normalization():
    adapter = make_adapter()
    out = await adapter.process_incoming_message(_message_event())
    assert out["platform_type"] == "google_chat"
    assert out["external_user_id"] == "users/117300060449094535067"
    assert out["channel_id"] == "spaces/AAA"
    assert out["channel_type"] == "personal"
    assert out["message_text"] == "hi"
    assert out["thread_ts"] == "spaces/AAA/threads/T1"
    assert out["message_ts"] == "spaces/AAA/messages/M1.M1"
    assert out["is_thread_reply"] is False


@pytest.mark.asyncio
async def test_space_mention_uses_argument_text_and_channel_type():
    adapter = make_adapter()
    event = _message_event(
        text="@bow show sales", argument_text="show sales", space_type="SPACE"
    )
    out = await adapter.process_incoming_message(event)
    assert out["channel_type"] == "channel"
    # argumentText arrives with the @mention already stripped.
    assert out["message_text"] == "show sales"


@pytest.mark.asyncio
async def test_thread_reply_flag():
    adapter = make_adapter()
    out = await adapter.process_incoming_message(_message_event(thread_reply=True))
    assert out["is_thread_reply"] is True


@pytest.mark.asyncio
async def test_bot_own_message_is_skipped():
    adapter = make_adapter()
    assert await adapter.process_incoming_message(_message_event(sender_type="BOT")) is None


@pytest.mark.asyncio
async def test_non_message_event_is_skipped():
    adapter = make_adapter()
    assert await adapter.process_incoming_message({"type": "ADDED_TO_SPACE"}) is None


@pytest.mark.asyncio
async def test_sender_identity_served_by_get_user_info():
    adapter = make_adapter()
    await adapter.process_incoming_message(_message_event())
    info = await adapter.get_user_info("users/117300060449094535067")
    assert info["email"] == "yochay@bagofwords.com"
    assert info["real_name"] == "Yochay Ettun"


# ---------- outbound text splitting ----------


def test_split_short_text_is_single_chunk():
    assert GoogleChatAdapter._split_text("hello") == ["hello"]


def test_split_long_text_respects_limit_and_newlines():
    text = ("line one\n" * 800).strip()  # ~7200 chars
    chunks = GoogleChatAdapter._split_text(text)
    assert len(chunks) > 1
    assert all(len(c) <= MAX_TEXT_LEN for c in chunks)
    # Nothing lost (modulo the newline consumed at each split point).
    assert "".join(c.replace("\n", "") for c in chunks) == text.replace("\n", "")


def test_split_unbreakable_text_hard_cuts():
    text = "x" * (MAX_TEXT_LEN * 2 + 10)
    chunks = GoogleChatAdapter._split_text(text)
    assert all(len(c) <= MAX_TEXT_LEN for c in chunks)
    assert "".join(chunks) == text


# ---------- listener event decode ----------


def test_listener_decodes_chat_event():
    event = _message_event()
    pm = {"data": base64.b64encode(json.dumps(event).encode()).decode(), "messageId": "1"}
    assert GoogleChatPubSubListener._decode_event(pm)["type"] == "MESSAGE"


def test_listener_skips_non_json_payload():
    # e.g. a manual console test publish
    pm = {"data": base64.b64encode(b"hi").decode(), "messageId": "2"}
    assert GoogleChatPubSubListener._decode_event(pm) is None


def test_listener_skips_empty_payload():
    assert GoogleChatPubSubListener._decode_event({"messageId": "3"}) is None
