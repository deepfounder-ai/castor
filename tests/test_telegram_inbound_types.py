"""Inbound message types — non-text Telegram updates the agent should see.

Before this, ``_handle_update`` parsed only text, caption, photo, document,
and voice/audio. Every other message type (location, contact, poll, dice,
sticker, video, video_note, venue, animation) hit the
``if not text and not image_b64: return`` gate and was silently dropped —
the user got no reply at all.

``_describe_nontext_message`` maps each of those to a short bracketed text
injection so the agent receives a meaningful prompt instead of nothing.
Pure function — no network, no DB.
"""
from __future__ import annotations

import telegram_bot as tb


def test_location():
    out = tb._describe_nontext_message({"location": {"latitude": 51.5, "longitude": -0.12}})
    assert out is not None
    assert "51.5" in out and "-0.12" in out
    assert "location" in out.lower()


def test_venue():
    out = tb._describe_nontext_message({
        "venue": {
            "title": "British Museum",
            "address": "Great Russell St",
            "location": {"latitude": 51.51, "longitude": -0.12},
        }
    })
    assert "British Museum" in out
    assert "Great Russell St" in out


def test_contact():
    out = tb._describe_nontext_message({
        "contact": {"first_name": "Ada", "last_name": "Lovelace", "phone_number": "+15551234"}
    })
    assert "Ada" in out
    assert "Lovelace" in out
    assert "+15551234" in out


def test_contact_minimal():
    # last_name is optional.
    out = tb._describe_nontext_message({"contact": {"first_name": "Bob", "phone_number": "+1"}})
    assert "Bob" in out
    assert "+1" in out


def test_poll():
    out = tb._describe_nontext_message({
        "poll": {
            "question": "Best language?",
            "options": [{"text": "Python"}, {"text": "Rust"}, {"text": "Go"}],
        }
    })
    assert "Best language?" in out
    assert "Python" in out and "Rust" in out and "Go" in out


def test_dice():
    out = tb._describe_nontext_message({"dice": {"emoji": "🎲", "value": 4}})
    assert "🎲" in out
    assert "4" in out


def test_sticker():
    out = tb._describe_nontext_message({"sticker": {"emoji": "😀", "file_id": "x"}})
    assert "😀" in out
    assert "sticker" in out.lower()


def test_sticker_no_emoji():
    out = tb._describe_nontext_message({"sticker": {"file_id": "x"}})
    assert out is not None
    assert "sticker" in out.lower()


def test_video():
    out = tb._describe_nontext_message({"video": {"duration": 12, "width": 640, "height": 480}})
    assert "video" in out.lower()
    assert "12" in out


def test_video_note():
    out = tb._describe_nontext_message({"video_note": {"duration": 5}})
    assert "video" in out.lower()


def test_animation_only_when_no_document():
    # Telegram sends a GIF as `animation` AND usually a `document`; the
    # document handler already saves the file, so the describer must defer
    # to avoid a double note.
    assert tb._describe_nontext_message(
        {"animation": {"duration": 2}, "document": {"file_id": "x", "file_name": "g.gif"}}
    ) is None
    out = tb._describe_nontext_message({"animation": {"duration": 2}})
    assert out is not None
    assert "animation" in out.lower() or "gif" in out.lower()


def test_plain_text_message_returns_none():
    # A normal text message is handled by the text path; describer stays out.
    assert tb._describe_nontext_message({"text": "hello"}) is None


def test_photo_returns_none():
    # Photo handled by the image path; describer must not double-handle.
    assert tb._describe_nontext_message({"photo": [{"file_id": "x"}]}) is None


def test_document_returns_none():
    assert tb._describe_nontext_message({"document": {"file_id": "x", "file_name": "a.txt"}}) is None


def test_voice_returns_none():
    assert tb._describe_nontext_message({"voice": {"file_id": "x"}}) is None


def test_empty_message_returns_none():
    assert tb._describe_nontext_message({}) is None
