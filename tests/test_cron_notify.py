"""Cron completion → owner notification gating.

System/infrastructure cron tasks (``__synthesis_continuous__``,
``__synthesis__``, ``__coach_daily__``, ``__trajectory_prune__``,
``__heartbeat__``) run on their own cadence and must NOT DM the Telegram
owner — a 15-minute "⏰ __synthesis_continuous__ No pending items" stream is
pure chat noise. Only user-created routines notify.
"""
from __future__ import annotations

import server
import telegram_bot


def test_is_system_task_classifies_dunder_names():
    assert server._is_system_task("__synthesis_continuous__") is True
    assert server._is_system_task("__synthesis__") is True
    assert server._is_system_task("__coach_daily__") is True
    assert server._is_system_task("__trajectory_prune__") is True
    assert server._is_system_task("__heartbeat__") is True


def test_is_system_task_false_for_user_routines():
    assert server._is_system_task("Morning digest") is False
    assert server._is_system_task("check the deploy") is False
    # leading-but-not-trailing dunder is not a system task
    assert server._is_system_task("__weird") is False


def _wire_verified_bot(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_bot, "is_verified", lambda: True)
    monkeypatch.setattr(telegram_bot, "_running", True, raising=False)
    monkeypatch.setattr(telegram_bot, "get_owner_id", lambda: 12345)
    monkeypatch.setattr(telegram_bot, "send_message", lambda *a, **k: sent.append((a, k)))
    # No WS clients so the callback only exercises the telegram branch.
    monkeypatch.setattr(server, "_ws_loop", None, raising=False)
    monkeypatch.setattr(server, "_ws_clients", set(), raising=False)
    return sent


def test_cron_callback_suppresses_system_task_telegram(monkeypatch):
    sent = _wire_verified_bot(monkeypatch)
    server._cron_callback("__synthesis_continuous__", "__synthesis_continuous__", "No pending items")
    assert sent == []  # no DM for a system task


def test_cron_callback_notifies_user_routine(monkeypatch):
    sent = _wire_verified_bot(monkeypatch)
    server._cron_callback("Morning digest", "summarize inbox", "Here is your digest")
    assert len(sent) == 1
    (args, kwargs) = sent[0]
    # owner id + a message mentioning the routine name
    assert args[0] == 12345
    assert "Morning digest" in args[1]
