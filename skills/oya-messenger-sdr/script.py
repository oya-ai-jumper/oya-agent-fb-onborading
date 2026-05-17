"""SDR state machine — sandbox entry point.

Reads {text, sender_id} from INPUT_JSON env, runs one state transition,
prints a JSON object to stdout. The platform forwards the JSON back to
the agent which relays the `reply` field per its Soul Rule 1.

Output contract:
    {"reply": "<verbatim string>" | null,
     "step":  "<current state>",
     "diag": {...optional...}}

The script drives the browser onboarding form synchronously via
`scripts/playback.py` — the agent's LLM is NOT involved in the form-fill.
`reply` is the only outbound channel; `next_action` no longer exists in
the contract. The blocking playback window (~30-90s) is covered for the
lead by side-channel `__send_preamble__` + `__still_preparing__` reruns
that push acknowledgement messages via skill_invoke in parallel.

Special-purpose `text` values that the agent never sends, but the platform's
debounce/rerun does:
    "__debounce_fire__"        — process the buffered name now
    "__send_preamble__"        — push the preparing_dashboard_template
    "__still_preparing__"      — push the still_preparing_template if mid-playback
    "__idle_ping__"            — nudge a silent lead during data collection
    "__onboarding_slot_retry__" — a queued lead retries the slot claim
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

# Multi-file skills set PYTHONPATH to include the sandbox root + each
# subdirectory; sibling imports work without prefixes.
import oya_runtime  # type: ignore[import-not-found]

import matcher
import messages
import places
import playback
import qualify
import state as state_mod
import xano_check
import debounce as debounce_mod


_DISQUAL_STEP = {
    "hours": "disqualified_hours",
    "website": "disqualified_website",
    "reviews": "disqualified_reviews",
    "rating": "disqualified_rating",
}
_DISQUAL_MSG_KEY = {
    "hours": "disqual_hours",
    "website": "disqual_website",
    "reviews": "disqual_reviews",
    "rating": "disqual_rating",
}


def _emit(reply: str | None, step: str, *, diag: dict | None = None) -> dict:
    out: dict = {"reply": reply, "step": step}
    if diag:
        out["diag"] = diag
    return out


def _config_int(key: str, default: int) -> int:
    try:
        return int(oya_runtime.config().get(key, default))
    except (TypeError, ValueError):
        return default


def _config_float(key: str, default: float) -> float:
    try:
        return float(oya_runtime.config().get(key, default))
    except (TypeError, ValueError):
        return default


def _self_company_name() -> str:
    return (oya_runtime.config().get("self_company_name") or "").strip()


def _gateway_id_hint() -> str:
    """gateway_id is server-resolved by the rerun endpoint when not passed,
    so we leave this empty. Kept as a function so future tightening (e.g.
    skills that target multiple gateways) has a clear hook."""
    return ""


def _matches_candidate_name(text_clean: str, st: dict) -> bool:
    """True if the lead's reply equals the previously-shown candidate's
    name (case-insensitive, both sides stripped). Used in
    awaiting_gmb_confirm and awaiting_address to recognize "the lead
    typed the business name back" as a confirmation. Strict equality
    only — no substring match — to avoid confirming the wrong location
    (e.g. lead types "Eataly" while candidate is "Eataly Flatiron")."""
    if not text_clean:
        return False
    candidate = ((st.get("candidate_gmb") or {}).get("name") or "").strip().lower()
    if not candidate:
        return False
    return text_clean.strip().lower() == candidate


# Common business-suffix tokens used to disambiguate "Joe Coffee" (business)
# from "Joe Smith" (person) in the awaiting_gmb_confirm handler. Any token
# match → treat the input as a business-name re-search, not a person name.
_BUSINESS_TOKENS = {
    "coffee", "cafe", "pizza", "restaurant", "llc", "inc", "corp",
    "co", "co.", "bakery", "shop", "store", "bar", "grill", "kitchen",
    "bistro", "diner", "tavern", "pub", "hotel", "inn", "motel", "lodge",
    "spa", "salon", "gym", "studio", "centre", "center", "club", "hall",
    "house", "park", "plaza", "mall", "office", "bank", "market", "mart",
    "express", "media", "agency", "service", "services", "group",
    "holdings", "ltd", "limited", "boutique", "supply", "supplies",
    "rentals", "auto", "automotive", "dental", "medical", "clinic",
    "salon", "barber", "tattoo", "fitness", "yoga", "studio",
}


_STREET_SUFFIXES = {
    "st", "st.", "street", "rd", "rd.", "road", "ave", "ave.", "avenue",
    "blvd", "blvd.", "boulevard", "dr", "dr.", "drive", "ln", "ln.", "lane",
    "way", "ct", "ct.", "court", "pl", "pl.", "place", "hwy", "highway",
    "pkwy", "parkway", "pike", "trl", "trail", "ter", "terrace", "cir", "circle",
    "sq", "square", "loop", "alley", "row",
}

_US_STATE_CODES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
}


def _looks_like_address(text: str) -> bool:
    """Heuristic: input is a street address, not a business name. Catches
    leads who paste their address into the awaiting_name slot — Places
    returns a literal-address entity which then fails qualification with
    the misleading "your Google Business Profile doesn't meet our
    requirements" message. Better to re-prompt for the business name.

    Two strong signals:
      1. Starts with digits AND contains a street suffix
         (e.g. "11689 Olio Rd Geist", "1051 S Coast Hwy 101").
      2. Contains a US state code preceded by a comma or space, with
         digits anywhere (e.g. "Fishers, IN 46037").
    """
    s = (text or "").strip()
    if not s:
        return False
    parts = s.lower().replace(",", " ").split()
    has_digits = any(any(c.isdigit() for c in p) for p in parts)
    if not has_digits:
        return False
    starts_with_number = parts[0][0].isdigit() if parts and parts[0] else False
    has_suffix = any(p in _STREET_SUFFIXES for p in parts)
    if starts_with_number and has_suffix:
        return True
    has_state_code = any(p in _US_STATE_CODES for p in parts)
    if has_state_code and starts_with_number:
        return True
    return False


def _looks_like_person_name(text: str) -> bool:
    """Heuristic for the awaiting_gmb_confirm "race-with-confirm-prompt"
    case: lead types their full name (expecting the bot to be asking for
    it) while the bot is actually still asking "Is this your business?".
    Without this, "Anna Smith" arriving in awaiting_gmb_confirm would
    fall through to a refined name search and Places would return
    something like "Anna Smith Studio" — completely off track.

    Heuristic: 2-3 alphabetic tokens, none of which is a business suffix
    (`coffee`, `cafe`, `inc`, `llc`, etc.). The conservative side errors
    toward "looks like a business name" so an actual business reply
    (e.g. "Wake N Bakery" — 3 tokens, "Bakery" is a business suffix)
    still goes through the re-search path."""
    s = (text or "").strip()
    if not s or len(s) > 60:
        return False
    parts = s.split()
    if not 2 <= len(parts) <= 3:
        return False
    if not all(p.replace("-", "").replace("'", "").isalpha() for p in parts):
        return False
    if any(p.lower().rstrip(".") in _BUSINESS_TOKENS for p in parts):
        return False
    return True


def _append_oya_utm(url: str) -> str:
    """Append `utm_source=oya.ai` + `utm_medium=ai-employee` to the
    onboarding URL so the customer's downstream analytics (Bubble's
    built-in GA, Xano events, etc.) can attribute the inbound to the
    Oya AI employee. Idempotent — if the params already exist on the
    URL, leave them alone."""
    if not url:
        return url
    try:
        from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
        parsed = urlparse(url)
        params = parse_qsl(parsed.query, keep_blank_values=True)
        existing_keys = {k for k, _ in params}
        if "utm_source" not in existing_keys:
            params.append(("utm_source", "oya.ai"))
        if "utm_medium" not in existing_keys:
            params.append(("utm_medium", "ai-employee"))
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "",
                           urlencode(params), ""))
    except Exception:
        return url


# ── Browser-onboarding slot pool ──────────────────────────────────────────
#
# Single shared slot per agent (concurrency=1). Multiple PSIDs racing the
# Oya Browser session would clobber each other (commands target the active
# tab; no per-tab parameter exists). The slot serializes onboarding form-fills.
#
# Storage: an `agent_memories` row with memory_type='browser_slot', content
# 'oya-messenger-sdr::slot:0', meta={sender_id, claimed_at}, and expires_at
# = now() + onboarding_slot_lease_seconds. The partial unique index from
# migration 20260430120000 (extended below to include 'browser_slot') makes
# INSERT ... ON CONFLICT DO NOTHING atomic — only one claim per agent wins.
#
# We piggyback on the existing skill-state KV via `oya_runtime.state` rather
# than introducing a new endpoint: `state.save` with `if_not_exists=True`
# returns False on collision (that helper is added to oya_runtime in this
# same change).

_SLOT_KEY = "browser_slot:0"


def _try_claim_slot(sender_id: str, lease_seconds: int) -> bool:
    """Atomic claim. Returns True if this PSID now owns the slot, False if
    another PSID is currently holding it."""
    return oya_runtime.state.claim(
        _SLOT_KEY,
        {"sender_id": sender_id,
         "claimed_at": __import__("datetime").datetime.now(
             __import__("datetime").timezone.utc).isoformat()},
        ttl_seconds=int(max(30, lease_seconds)),
    )


def _release_slot() -> None:
    """Idempotent — DELETE on the slot row. Safe to call multiple times and
    safe to call when the slot was never claimed by this PSID; the worst
    case is a tiny race with the reaper which is also idempotent."""
    try:
        oya_runtime.state.delete(_SLOT_KEY)
    except Exception:
        pass


# ── Completion + cooldown ────────────────────────────────────────────────
#
# When state moves to `complete` the lead just received a terminal message
# (Calendly link, onboarding_error, returning_template, etc). Their next
# inbound message used to restart the wizard from welcome — a UX disaster
# when the lead replies with "no email yet" right after a failed onboarding,
# because they'd be re-asked for business name → name → email → phone all
# over again. Now we time-gate the re-engagement: within `re_engage_after_
# seconds` of completion, send a single polite acknowledgement and stay
# silent on subsequent messages. Past the cooldown, treat as a fresh
# conversation (the original behavior — useful when a lead returns days
# later with a new business).

_RE_ENGAGE_DEFAULT_SECONDS = 600  # 10 minutes


def _mark_complete(st: dict) -> None:
    """Transition to `complete` and stamp completion time. Centralizing this
    means every path that ends a conversation (success, failure, timeout,
    active-customer redirect, returning-customer redirect) participates in
    the cooldown logic."""
    st["step"] = "complete"
    st["completed_at"] = datetime.now(timezone.utc).isoformat()
    st.pop("completion_ack_sent", None)
    st.pop("idle_ping_sent_for", None)


def _within_complete_cooldown(st: dict, cooldown_seconds: int) -> bool:
    completed_at = st.get("completed_at")
    if not completed_at:
        return False
    try:
        ts = datetime.fromisoformat(completed_at)
    except ValueError:
        return False
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    return age < cooldown_seconds


# ── Idle-ping during data collection ─────────────────────────────────────
#
# After the bot prompts for full_name / email / phone, schedule a delayed
# rerun. If the lead doesn't reply within `idle_ping_seconds` and is still
# in one of these states, send a single "are you still there?" nudge —
# common SMS-onboarding pattern. We coalesce via dedup_key + replace_pending
# so a quick reply reschedules cleanly. We also gate the ping itself on the
# state's `last_received_at` so a delayed worker pickup doesn't ping a lead
# who already replied. Only one ping per state — no nag loops.

_IDLE_PING_STATES = {"awaiting_full_name", "awaiting_email", "awaiting_phone"}


def _schedule_idle_ping(*, sender_id: str, delay_seconds: int) -> None:
    """Best-effort. The handler at __idle_ping__ no-ops if the state has
    moved on by the time the rerun fires."""
    if delay_seconds <= 0:
        return
    try:
        oya_runtime.schedule_rerun(
            delay_seconds=int(delay_seconds),
            payload={"text": "__idle_ping__", "sender_id": sender_id},
            dedup_key=f"sdr-idle:{sender_id}",
            replace_pending=True,
            channel=sender_id,
        )
    except Exception:
        pass


def _drive_onboarding_sync(*, sender_id: str, st: dict, cfg: dict) -> dict:
    """Run the JM Bubble.io form-fill SYNCHRONOUSLY via `playback.drive_
    onboarding`, then verify against Xano, then return the terminal reply
    (calendly URL on full success, onboarding_error on any failure).

    Replaces the previous `_emit_browser_onboarding` which emitted a
    `next_action.browser_onboarding` payload that the agent's LLM then
    translated into individual browser tool calls. The LLM was unreliable:
    skipped phone fill, auto-corrected `mk1+test@oya.i` \u2192 `mk1+test@oya.io`,
    clicked stale element_ids, hallucinated success when the form was
    half-submitted. Now the script drives the browser itself via direct
    HTTP calls in `playback.py`; the `value` strings the lead typed pass
    through character-for-character.

    Caller (`_start_browser_onboarding`) has already claimed the browser
    slot and set `step='awaiting_onboarding'`. This function is responsible
    for releasing the slot before returning, regardless of outcome.

    BLOCKS for the full ~30-90s playback + Xano-verify window. The lead's
    wait-experience is covered by the side-channel `__send_preamble__`
    (delay=0) and `__still_preparing__` (+30s) reruns scheduled in
    `awaiting_phone` \u2014 they push reassurance via skill_invoke in parallel
    with this blocking call.
    """
    gmb = st.get("candidate_gmb") or {}
    lead = st.get("lead") or {}
    url_tpl = cfg.get("onboarding_url") or ""
    url_fields = {
        "place_id": gmb.get("place_id") or "",
        "gmb_name": gmb.get("name") or "",
        "gmb_address": gmb.get("formatted_address") or "",
    }
    try:
        onboarding_url = url_tpl.format_map(messages._SafeFormat(url_fields))
    except (KeyError, ValueError):
        onboarding_url = url_tpl
    onboarding_url = _append_oya_utm(onboarding_url)

    try:
        result = playback.drive_onboarding(
            onboarding_url=onboarding_url,
            full_name=lead.get("full_name") or "",
            email=lead.get("email") or "",
            phone=lead.get("phone") or "",
            timeout_seconds=90,
        )
    except Exception as exc:
        # Playback module shouldn't raise \u2014 it converts errors to {ok:False}
        # internally \u2014 but defend against import / network blow-ups.
        result = {"ok": False, "error": f"playback raised: {exc}",
                  "diag": {"phase": "raise"}}

    # Slot is released regardless of outcome \u2014 the lease auto-expires too,
    # but releasing eagerly lets queued leads claim it sooner.
    _release_slot()
    # Cancel any pending timeout rerun (best-effort) \u2014 we just terminated.
    try:
        oya_runtime.state.delete(f"sdr-onboard-timeout:{sender_id}")
    except Exception:
        pass

    if not result.get("ok"):
        _mark_complete(st)
        state_mod.save(sender_id, st)
        return _emit(messages.render("onboarding_error"), "complete",
                     diag={"onboarding": "playback_failed",
                           "phase": (result.get("diag") or {}).get("phase"),
                           "reason": result.get("error", "")[:200]})

    # Form submitted successfully. Now verify Xano if configured \u2014 bias
    # toward false-negatives (better silent success than a Calendly invite
    # without a CRM record).
    cid = gmb.get("cid") or ""
    verified = False
    if cid and cfg.get("enable_xano_check"):
        for attempt_delay in (1.0, 2.0, 4.0, 8.0, 15.0):
            time.sleep(attempt_delay)
            status = xano_check.gmb_status(cid)
            if status in {"active", "inactive"}:
                verified = True
                break

    _mark_complete(st)
    state_mod.save(sender_id, st)
    if verified or not cfg.get("enable_xano_check"):
        return _emit(messages.render("calendly_book_template"), "complete",
                     diag={"onboarding": "ok", "xano_verified": verified})
    return _emit(messages.render("onboarding_error"), "complete",
                 diag={"onboarding": "unverified",
                       "reason": "form_submitted_but_xano_record_missing"})


def _enter_or_continue_queue(*, sender_id: str, st: dict, cfg: dict,
                             retry_seconds: int, lease_seconds: int,
                             onboarding_timeout: int) -> dict:
    """Slot is busy. On first hit, send the one-shot 'just running checks'
    notice. On subsequent retries, stay silent (typing dots cover the wait).
    Always reschedules the retry rerun with replace_pending so only one
    pending retry job exists per lead."""
    first_time = not st.get("queued_notice_sent")
    st["step"] = "queued_for_onboarding"
    if first_time:
        st["queued_notice_sent"] = True
    state_mod.save(sender_id, st)
    # The onboarding_timeout fallback also serves as the queue-depth cap.
    # Only schedule it on first entry — replace_pending keeps it pinned to
    # the original arrival time, but to be clear we don't reschedule on
    # retries (would extend the deadline indefinitely).
    if first_time:
        debounce_mod.schedule_onboarding_timeout(
            sender_id=sender_id,
            delay_seconds=onboarding_timeout,
            gateway_id=None,
            channel=sender_id,
        )
    # Schedule the slot-retry rerun. replace_pending=True coalesces multiple
    # retries into a single pending job per lead.
    oya_runtime.schedule_rerun(
        delay_seconds=int(max(2, retry_seconds)),
        payload={"text": "__onboarding_slot_retry__", "sender_id": sender_id},
        dedup_key=f"sdr-onboard-retry:{sender_id}",
        replace_pending=True,
        channel=sender_id,
    )
    if first_time:
        return _emit(messages.render("onboarding_queued_notice"),
                     "queued_for_onboarding",
                     diag={"queued": True, "first_notice": True})
    return _emit(None, "queued_for_onboarding", diag={"queued": True})


def _start_browser_onboarding(*, sender_id: str, st: dict, cfg: dict,
                              onboarding_timeout: int,
                              lease_seconds: int,
                              retry_seconds: int) -> dict:
    """Try to claim the slot, then either drive the playback synchronously
    or queue. Used by both awaiting_phone (after phone collection) and the
    slot-retry handler. Returns the terminal reply (Calendly / fallback /
    queued-notice)."""
    if _try_claim_slot(sender_id, lease_seconds):
        st["step"] = "awaiting_onboarding"
        # Drop the queued flag so a future re-queue would re-send the notice.
        st.pop("queued_notice_sent", None)
        state_mod.save(sender_id, st)
        return _drive_onboarding_sync(sender_id=sender_id, st=st, cfg=cfg)
    return _enter_or_continue_queue(
        sender_id=sender_id, st=st, cfg=cfg,
        retry_seconds=retry_seconds, lease_seconds=lease_seconds,
        onboarding_timeout=onboarding_timeout,
    )


def run(text: str, sender_id: str) -> dict:
    if not sender_id:
        return _emit(None, "error", diag={"error": "missing_sender_id"})

    cfg = oya_runtime.config()
    debounce_seconds = _config_int("debounce_seconds", 4)
    onboarding_timeout = _config_int("onboarding_timeout_seconds", 120)
    min_reviews = _config_int("min_reviews", 10)
    min_rating = _config_float("min_rating", 3.0)

    text_clean = (text or "").strip()
    st = state_mod.load(sender_id)
    state_mod.stamp_received(st, text_clean)
    step = st.get("step") or "new"

    # ── Trigger keyword resets state ──
    # The lead typing the trigger keyword (default "MAPS", case-insensitive,
    # exact match) wipes any in-progress state and starts a brand-new
    # conversation. Useful when a lead got stuck mid-flow (network blip
    # mid-onboarding, gave the wrong GMB and wants to retry, etc.) or
    # arrives long after a previous conversation completed. We also
    # release the browser slot and cancel any pending timeout so the
    # restart is fully clean. Skipped for the script's own internal
    # triggers (those start with `__`, never user-typable).
    trigger_word = (oya_runtime.config().get("trigger_keyword") or "MAPS").strip().upper()
    if (text_clean
            and not text_clean.startswith("__")
            and not text_clean.startswith("onboarding_")
            and trigger_word
            and text_clean.upper() == trigger_word):
        # Only release the browser-onboarding slot if THIS lead was the one
        # holding it. Otherwise we'd kick someone else's onboarding off the
        # slot just because lead B typed MAPS while lead A is mid-form.
        if step in ("awaiting_onboarding", "queued_for_onboarding"):
            _release_slot()
        oya_runtime.state.delete(f"sdr-onboard-timeout:{sender_id}")
        st = {"step": "awaiting_name", "name_buffer": []}
        state_mod.save(sender_id, st)
        return _emit(messages.render("welcome_template"), "awaiting_name",
                     diag={"trigger_reset": True})

    # ── Special internal triggers (debounce / onboarding callback) ──

    if text_clean == "__debounce_fire__":
        return _process_name_buffer(sender_id=sender_id, st=st,
                                    min_reviews=min_reviews, min_rating=min_rating)

    if text_clean == "__onboarding_timeout__":
        if step in ("awaiting_onboarding", "queued_for_onboarding"):
            # Release the slot too — the lead is leaving the queue.
            _release_slot()
            _mark_complete(st)
            state_mod.save(sender_id, st)
            return _emit(messages.render("onboarding_error"), "complete",
                         diag={"reason": "onboarding_timeout"})
        # State already moved on (success or user-driven cancel) — silent.
        return _emit(None, step, diag={"reason": "timeout_noop"})

    if text_clean == "__send_preamble__":
        # Side-channel preamble: scheduled at delay=0 from the
        # awaiting_phone handler. Fires via skill_invoke, which pushes
        # `reply` to the gateway directly (no LLM round-trip), running in
        # parallel with the agent's LLM tool loop that's driving the
        # actual browser playback. Lead sees the acknowledgement within
        # ~1s of sending phone. State must NOT mutate here — the LLM is
        # mid-playback and reading the same row.
        active_states = {"awaiting_onboarding", "queued_for_onboarding"}
        if step not in active_states:
            return _emit(None, step, diag={"preamble": "noop_state"})
        return _emit(messages.render("preparing_dashboard_template"), step,
                     diag={"preamble": "sent"})

    if text_clean == "__still_preparing__":
        # Halfway-through nudge so the lead doesn't feel abandoned during
        # the silent browser playback + Xano verify. Fires ~30s after
        # phone collection. Skip if the conversation already wrapped
        # (success path delivered Calendly, or onboarding_error already
        # sent) so the nudge can never arrive AFTER the final message —
        # that would be confusing ("almost done!" then nothing further).
        active_states = {"awaiting_onboarding", "queued_for_onboarding"}
        if step not in active_states:
            return _emit(None, step, diag={"still_preparing": "noop_state"})
        return _emit(messages.render("still_preparing_template"), step,
                     diag={"still_preparing": "sent"})

    if text_clean == "__idle_ping__":
        # Only nudge a lead who is mid-data-collection AND has actually been
        # silent for at least idle_ping_seconds. Worker queue lag could fire
        # the rerun late, after the lead has already replied — `last_received_
        # at` guards that. One ping per state, tracked via `idle_ping_sent_for`,
        # so we never nag.
        if step not in _IDLE_PING_STATES:
            return _emit(None, step, diag={"idle_ping": "noop_state"})
        idle_seconds = _config_int("idle_ping_seconds", 180)
        last_at = st.get("last_received_at")
        if last_at:
            try:
                silence = (datetime.now(timezone.utc)
                           - datetime.fromisoformat(last_at)).total_seconds()
                if silence < idle_seconds:
                    return _emit(None, step, diag={"idle_ping": "noop_recent_activity",
                                                    "silence_s": int(silence)})
            except ValueError:
                pass
        if st.get("idle_ping_sent_for") == step:
            return _emit(None, step, diag={"idle_ping": "already_sent"})
        st["idle_ping_sent_for"] = step
        state_mod.save(sender_id, st)
        return _emit(messages.render("idle_ping_template"), step,
                     diag={"idle_ping": "sent"})

    if text_clean == "__onboarding_slot_retry__":
        # Re-attempt the slot claim. State should be queued_for_onboarding;
        # if it's drifted (rare: timeout already fired, or a duplicate retry
        # arrived after we already advanced) just no-op silently.
        if step != "queued_for_onboarding":
            return _emit(None, step, diag={"reason": "retry_noop", "step": step})
        retry_seconds = _config_int("onboarding_slot_retry_seconds", 5)
        lease_seconds = _config_int("onboarding_slot_lease_seconds", 200)
        return _start_browser_onboarding(
            sender_id=sender_id, st=st, cfg=cfg,
            onboarding_timeout=onboarding_timeout,
            lease_seconds=lease_seconds, retry_seconds=retry_seconds,
        )

    # `onboarding_complete` / `onboarding_failed` text triggers are no
    # longer used — the LLM doesn't drive the playback anymore, so it
    # never callbacks. The script's `_drive_onboarding_sync` handles
    # success/failure inline. Keeping this comment as a tombstone in case
    # an old scheduled rerun arrives during deploy crossover.

    # ── Normal lead-driven flow ──

    # Re-engagement: a lead whose previous conversation completed (Calendly
    # sent, polite fallback sent, timeout fired) types something new.
    #
    # Time-gated: within `re_engage_after_seconds` of completion, send ONE
    # acknowledgement and stay silent on subsequent turns — restarting the
    # wizard mid-cooldown is the customer-reported "made me re-enter info
    # three times" bug (lead replies "no email yet" right after a failed
    # browser onboarding → was getting asked for business name again).
    # After the cooldown, treat as a fresh conversation (the original
    # behavior — useful when a lead returns days later with a new business).
    if step == "complete" and not text_clean.startswith("__"):
        cooldown = _config_int("re_engage_after_seconds", _RE_ENGAGE_DEFAULT_SECONDS)
        if _within_complete_cooldown(st, cooldown):
            if not st.get("completion_ack_sent"):
                st["completion_ack_sent"] = True
                state_mod.save(sender_id, st)
                return _emit(messages.render("completion_followup_ack"), "complete",
                             diag={"in_cooldown": True})
            return _emit(None, "complete",
                         diag={"in_cooldown": True, "ack_already_sent": True})
        st = {"step": "awaiting_name", "name_buffer": []}
        state_mod.save(sender_id, st)
        return _emit(messages.render("welcome_template"), "awaiting_name",
                     diag={"re_engaged": True})

    # Trigger / fresh conversation.
    if step == "new":
        st["step"] = "awaiting_name"
        st["name_buffer"] = []
        state_mod.save(sender_id, st)
        return _emit(messages.render("welcome_template"), "awaiting_name")

    # Awaiting name: each Messenger Enter is already a discrete message
    # boundary, so there's nothing to debounce — process the inbound name
    # immediately. (The PDF spec's multi-message case ["Jumper" then "Media"]
    # is handled by the awaiting_gmb_confirm branch's catch-all "treat as
    # refined name" path: a second short message after a no-match comes back
    # through this same code as a re-search, not via buffering.)
    if step == "awaiting_name":
        if not text_clean:
            return _emit(None, "awaiting_name")
        if _self_company_name() and matcher.is_self_company(text_clean, _self_company_name()):
            st["step"] = "awaiting_name"
            st["name_buffer"] = []
            state_mod.save(sender_id, st)
            return _emit(messages.render("self_company_response"), "awaiting_name")
        # Lead pasted an address as their business name. The default
        # text_search call would return a literal-address entity ("11689
        # Olio Rd") which fails qualification with a misleading "no
        # business hours" message. Instead, search Places filtered to
        # establishments only so we surface the ACTUAL business at that
        # address. Falls back to a re-prompt only when no business is
        # found (e.g. residential address, vacant building).
        if _looks_like_address(text_clean):
            return _process_address_lookup(
                sender_id=sender_id, st=st, address=text_clean,
                min_reviews=min_reviews, min_rating=min_rating,
            )
        return _process_name(
            sender_id=sender_id, st=st, joined_name=text_clean,
            min_reviews=min_reviews, min_rating=min_rating,
        )

    # Awaiting address (multi-result disambiguation).
    if step == "awaiting_address":
        if not text_clean:
            return _emit(None, "awaiting_address")
        # Defensive: if the lead's reply collided with the bot's "Is this
        # your business?" prompt and they typed the candidate's NAME back
        # (or a plain "yes") instead of an address, treat as confirmation
        # rather than concatenating their reply with the search query.
        if _matches_candidate_name(text_clean, st):
            return _on_gmb_confirmed(sender_id=sender_id, st=st,
                                     min_reviews=min_reviews, min_rating=min_rating)
        if matcher.is_affirmative(text_clean):
            if st.get("candidate_gmb"):
                return _on_gmb_confirmed(sender_id=sender_id, st=st,
                                         min_reviews=min_reviews, min_rating=min_rating)
            # Multi-result with no candidate yet — affirmative is a
            # non-sequitur (we just asked for an address, not a yes/no).
            # Re-emit ask_address rather than concatenating "yes" with
            # `pending_name` and feeding Places a garbage query (which
            # is what produced "Eataly Flatiron NY" in the live thread
            # 4faad157).
            return _emit(messages.render("ask_address"), "awaiting_address",
                         diag={"affirmative_without_candidate": True})
        if matcher.is_negative(text_clean) and not st.get("candidate_gmb"):
            # Same reasoning for "no" / "nope" — we asked for an address.
            return _emit(messages.render("ask_address"), "awaiting_address",
                         diag={"negative_without_candidate": True})
        # If the input clearly isn't an address (no digits — every real
        # street address has a number), don't feed it to Places. The
        # lead is most likely confused (typed a name or email expecting
        # the bot to be asking for those) — re-emit ask_address. This
        # catches "Alex Test", "anna@example.com", "yes please", etc.
        # without doing a junky refined search.
        if not any(c.isdigit() for c in text_clean):
            return _emit(messages.render("ask_address"), "awaiting_address",
                         diag={"non_address_input": True})
        prev_name = st.get("pending_name") or ""
        return _process_name(sender_id=sender_id, st=st,
                             joined_name=f"{prev_name} {text_clean}".strip(),
                             min_reviews=min_reviews, min_rating=min_rating,
                             after_address_round=True)

    # Awaiting yes/no on a candidate GMB.
    if step == "awaiting_gmb_confirm":
        if _self_company_name() and matcher.is_self_company(text_clean, _self_company_name()):
            st["candidate_gmb"] = None
            st["step"] = "awaiting_name"
            st["name_buffer"] = []
            state_mod.save(sender_id, st)
            return _emit(messages.render("self_company_response"), "awaiting_name")
        if matcher.is_affirmative(text_clean):
            return _on_gmb_confirmed(sender_id=sender_id, st=st,
                                     min_reviews=min_reviews, min_rating=min_rating)
        # Lead typed back the candidate's exact name (case-insensitive) —
        # almost always means "yes, this is my business" rather than "I
        # want to search for that name again". Common when the lead's
        # reply collides with the bot's "Is this your business?" prompt.
        if _matches_candidate_name(text_clean, st):
            return _on_gmb_confirmed(sender_id=sender_id, st=st,
                                     min_reviews=min_reviews, min_rating=min_rating)
        # Race-with-confirm-prompt: lead typed their full_name (expecting
        # the bot to ask for it next) before the "Is this your business?"
        # message arrived in their UI. Detect 2-3-token alphabetic input
        # without business-suffix words and treat as implicit confirmation
        # + skip-ahead to awaiting_email with the input pre-filled as
        # full_name. Live transcripts: "Alex Test" → Places returned
        # "Test ALEX" Warsaw; "Dana Test" → Places returned Dana-Farber
        # Cancer Institute. Both nonsensical re-searches that this catches.
        if _looks_like_person_name(text_clean) and st.get("candidate_gmb"):
            return _on_gmb_confirmed_with_name(
                sender_id=sender_id, st=st, full_name=text_clean,
                min_reviews=min_reviews, min_rating=min_rating,
            )
        if matcher.is_negative(text_clean) or text_clean:
            # Treat anything else as a refined name → re-search.
            st["step"] = "awaiting_name"
            st["name_buffer"] = []
            state_mod.save(sender_id, st)
            return _process_name(sender_id=sender_id, st=st, joined_name=text_clean,
                                 min_reviews=min_reviews, min_rating=min_rating)
        return _emit(messages.render("confirm_one"), "awaiting_gmb_confirm")

    # Lead-info collection.
    if step == "awaiting_full_name":
        if not text_clean:
            return _emit(messages.render("ask_full_name"), "awaiting_full_name")
        lead = dict(st.get("lead") or {})
        lead["full_name"] = text_clean
        st["lead"] = lead
        st["step"] = "awaiting_email"
        st.pop("idle_ping_sent_for", None)  # fresh state, fresh ping budget
        state_mod.save(sender_id, st)
        _schedule_idle_ping(sender_id=sender_id,
                            delay_seconds=_config_int("idle_ping_seconds", 180))
        return _emit(messages.render("ask_email"), "awaiting_email")

    if step == "awaiting_email":
        email = matcher.extract_email(text_clean)
        if not email:
            return _emit(messages.render("ask_email"), "awaiting_email")
        status = xano_check.email_status(email)
        if status == "active":
            _mark_complete(st)
            state_mod.save(sender_id, st)
            return _emit(messages.render("active_account_template"), "complete",
                         diag={"customer": "active"})
        if status == "inactive":
            _mark_complete(st)
            state_mod.save(sender_id, st)
            return _emit(messages.returning_with_url(
                cfg.get("returning_calendly_url") or "",
                cfg.get("calendly_url") or "",
            ), "complete", diag={"customer": "returning"})
        lead = dict(st.get("lead") or {})
        lead["email"] = email
        st["lead"] = lead
        st["step"] = "awaiting_phone"
        st.pop("idle_ping_sent_for", None)
        state_mod.save(sender_id, st)
        _schedule_idle_ping(sender_id=sender_id,
                            delay_seconds=_config_int("idle_ping_seconds", 180))
        return _emit(messages.render("ask_phone"), "awaiting_phone")

    if step == "awaiting_phone":
        phone = matcher.extract_phone(text_clean)
        if not phone:
            return _emit(messages.render("ask_phone"), "awaiting_phone")
        lead = dict(st.get("lead") or {})
        lead["phone"] = phone
        st["lead"] = lead
        # Save the lead's contact info before attempting the slot claim, so
        # state reflects what we have even if the lead ends up queued.
        state_mod.save(sender_id, st)
        # Customer feedback: 60s of silence between phone-send and final
        # reply (Calendly link / onboarding_error) feels broken — the lead
        # sees the typing indicator come and go during the silent browser
        # playback + Xano verify window.
        #
        # Side-channel preamble + nudge: the awaiting_phone turn still
        # emits next_action.browser_onboarding immediately so the agent
        # LLM can drive the browser playback in its tool loop (the
        # `skill_invoke` worker that processes scheduled reruns can NOT
        # interpret next_action — it'd silently discard the playback).
        # Separately we schedule:
        #   • `__send_preamble__` at delay=0  — fires via skill_invoke,
        #     emits `preparing_dashboard_template` as a plain reply (no
        #     next_action) and skill_invoke pushes it to the gateway. Lead
        #     sees acknowledgement within ~1s of sending phone, in
        #     parallel with the LLM starting the playback.
        #   • `__still_preparing__` at +30s — same path, sends
        #     `still_preparing_template` if state is still mid-playback,
        #     no-ops if onboarding already completed.
        # Customer-facing copy NEVER mentions "browser" / "automation" /
        # "playback" — leads see a "team onboarding you" framing.
        try:
            oya_runtime.schedule_rerun(
                delay_seconds=0,
                payload={"text": "__send_preamble__", "sender_id": sender_id},
                dedup_key=f"sdr-preamble:{sender_id}",
                replace_pending=True,
                channel=sender_id,
            )
        except Exception:
            pass
        nudge_seconds = _config_int("preparing_nudge_seconds", 30)
        if nudge_seconds > 0:
            try:
                oya_runtime.schedule_rerun(
                    delay_seconds=int(nudge_seconds),
                    payload={"text": "__still_preparing__", "sender_id": sender_id},
                    dedup_key=f"sdr-still-preparing:{sender_id}",
                    replace_pending=True,
                    channel=sender_id,
                )
            except Exception:
                pass
        # Try to claim the browser-onboarding slot. On success, emit the
        # next_action playback. On failure, enter queued_for_onboarding,
        # send the one-shot notice, and schedule the retry rerun.
        retry_seconds = _config_int("onboarding_slot_retry_seconds", 5)
        lease_seconds = _config_int("onboarding_slot_lease_seconds", 200)
        return _start_browser_onboarding(
            sender_id=sender_id, st=st, cfg=cfg,
            onboarding_timeout=onboarding_timeout,
            lease_seconds=lease_seconds, retry_seconds=retry_seconds,
        )

    # Disqualified → either the lead sent a NEW business name (e.g. they
    # mistyped first time and want to retry with a different store), or
    # they're saying "ok I added the website, please recheck". Disambiguate
    # by trying Places against the inbound text first — if it resolves to a
    # business, treat as a fresh search (so MTLPILATES → MTLPILATES, not a
    # recheck of the previously-disqualified Kava). Falls back to the
    # original recheck path when Places returns nothing.
    if step in {"disqualified_hours", "disqualified_website", "disqualified_reviews", "disqualified_rating"}:
        # Conversational closings ("thx", "thanks", "thank you", "got it")
        # are silent — they're a polite acknowledgement, not a recheck
        # signal and not a new search. Without this, "thx" falls through
        # the recheck path and re-emits the disqual message; "thank you"
        # gets fed to Places and surfaces "Thank You Berry Much Farms",
        # an unrelated business in Oregon. Both observed live on
        # 2026-05-08 in Anna's Jumper SDR thread.
        if text_clean and matcher.looks_like_closing(text_clean):
            return _emit(None, step, diag={"closing": True})
        if text_clean and not matcher.is_affirmative(text_clean):
            new_candidates = places.text_search(text_clean)
            if new_candidates:
                st["step"] = "awaiting_name"
                st["name_buffer"] = []
                state_mod.save(sender_id, st)
                return _process_name(
                    sender_id=sender_id, st=st, joined_name=text_clean,
                    min_reviews=min_reviews, min_rating=min_rating,
                )
        gmb = st.get("candidate_gmb") or {}
        place_id = gmb.get("place_id")
        if not place_id:
            st["step"] = "awaiting_name"
            st["name_buffer"] = []
            state_mod.save(sender_id, st)
            return _emit(messages.render("not_found"), "awaiting_name")
        fresh = places.place_details(place_id)
        if not fresh:
            return _emit(messages.render(_DISQUAL_MSG_KEY[step.removeprefix("disqualified_")]),
                         step, diag={"recheck": "failed"})
        st["candidate_gmb"] = fresh
        fail = qualify.check(fresh, min_reviews=min_reviews, min_rating=min_rating)
        if fail is None:
            st["step"] = "awaiting_full_name"
            st.pop("idle_ping_sent_for", None)
            state_mod.save(sender_id, st)
            _schedule_idle_ping(sender_id=sender_id,
                                delay_seconds=_config_int("idle_ping_seconds", 180))
            return _emit(messages.render("ask_full_name"), "awaiting_full_name",
                         diag={"recheck": "passed"})
        st["step"] = _DISQUAL_STEP[fail]
        st["disqual_reason"] = fail
        state_mod.save(sender_id, st)
        return _emit(messages.render(_DISQUAL_MSG_KEY[fail]), _DISQUAL_STEP[fail],
                     diag={"recheck": "still_failing"})

    # Already-onboarding wait state — silent on extra inbound (the lead
    # might be saying "thanks!" while we're navigating their browser).
    if step == "awaiting_onboarding":
        return _emit(None, "awaiting_onboarding")

    # Queued behind another lead's onboarding — silent. The retry rerun
    # eventually moves them into awaiting_onboarding, or the timeout fires.
    if step == "queued_for_onboarding":
        return _emit(None, "queued_for_onboarding")

    # Complete: ignore further messages.
    if step == "complete":
        return _emit(None, "complete")

    # Drift: recover by restarting at name collection.
    st["step"] = "awaiting_name"
    st["name_buffer"] = []
    state_mod.save(sender_id, st)
    return _emit(messages.render("welcome_template"), "awaiting_name", diag={"recovered": True})


def _process_address_lookup(*, sender_id: str, st: dict, address: str,
                            min_reviews: int, min_rating: float) -> dict:
    """Lead pasted an address; resolve to the business at that address.
    Two-step Places lookup:

      1. text_search(address) — returns the geocoded-address entity with
         its lat/lng. The address itself isn't a business so the entity
         types are typically `street_address`/`subpremise`; we just
         harvest the coordinates.
      2. nearby_search(lat, lng, radius=75m) — returns the actual
         business(es) at that physical location, ranked by distance.
         The closest non-address entity wins.

    On no business found at the address (residential, vacant, geocoding
    miss, etc.), re-prompt for the business name with a clear ask."""
    addr_hits = places.text_search(address, max_results=1)
    lat = lng = None
    if addr_hits:
        loc = (addr_hits[0].get("location") or {})
        lat = loc.get("latitude")
        lng = loc.get("longitude")

    candidates: list[dict] = []
    if lat is not None and lng is not None:
        candidates = places.nearby_search(
            latitude=lat, longitude=lng,
            radius_meters=75.0, max_results=5,
            drop_pure_address_results=True,
        )

    if not candidates:
        return _emit(messages.render("ask_business_name_when_address"),
                     "awaiting_name",
                     diag={"input_was_address": True,
                           "geocoded": lat is not None,
                           "places_results": 0})

    gmb = candidates[0]
    cfg_now = oya_runtime.config()
    cid = gmb.get("cid") or ""
    if cid and cfg_now.get("enable_xano_check"):
        gs = xano_check.gmb_status(cid)
        if gs == "active":
            st["candidate_gmb"] = gmb
            _mark_complete(st)
            state_mod.save(sender_id, st)
            return _emit(messages.render("active_account_template"), "complete",
                         diag={"customer": "active",
                               "matched_at": "address_lookup", "cid": cid})
        if gs == "inactive":
            st["candidate_gmb"] = gmb
            _mark_complete(st)
            state_mod.save(sender_id, st)
            return _emit(
                messages.returning_with_url(
                    cfg_now.get("returning_calendly_url") or "",
                    cfg_now.get("calendly_url") or "",
                ),
                "complete",
                diag={"customer": "returning",
                      "matched_at": "address_lookup", "cid": cid},
            )

    st["candidate_gmb"] = gmb
    st["pending_name"] = gmb.get("name") or address
    st["step"] = "awaiting_gmb_confirm"
    st["name_buffer"] = []
    state_mod.save(sender_id, st)
    return _emit(
        messages.confirm_one_with_listing(gmb.get("name", ""),
                                          gmb.get("formatted_address", "")),
        "awaiting_gmb_confirm",
        diag={"input_was_address": True,
              "places_results": len(candidates),
              "gmb_name": gmb.get("name"),
              "gmb_address": gmb.get("formatted_address")},
    )


def _process_name_buffer(*, sender_id: str, st: dict,
                         min_reviews: int, min_rating: float) -> dict:
    buf = list(st.get("name_buffer") or [])
    joined = " ".join(s for s in buf if s).strip()
    if not joined:
        return _emit(None, "awaiting_name")
    return _process_name(sender_id=sender_id, st=st, joined_name=joined,
                         min_reviews=min_reviews, min_rating=min_rating)


def _process_name(*, sender_id: str, st: dict, joined_name: str,
                  min_reviews: int, min_rating: float,
                  after_address_round: bool = False) -> dict:
    if _self_company_name() and matcher.is_self_company(joined_name, _self_company_name()):
        st["step"] = "awaiting_name"
        st["name_buffer"] = []
        state_mod.save(sender_id, st)
        return _emit(messages.render("self_company_response"), "awaiting_name")

    candidates = places.text_search(joined_name)
    if not candidates:
        st["name_buffer"] = []
        st["step"] = "awaiting_name"
        state_mod.save(sender_id, st)
        return _emit(messages.render("not_found"), "awaiting_name")

    if len(candidates) == 1 or after_address_round:
        gmb = candidates[0]
        # Customer-existence short-circuit — runs as soon as we resolve a
        # single GMB from the lead's name (or name + address). If they're
        # already a Jumper customer, they should NEVER be asked to confirm
        # the listing or be qualified — they're identified, route them to
        # the active-account or reactivation message immediately.
        # Xano's `get_gmb` keys on the **numeric Google Maps CID** (e.g.
        # 10979561844225568703), which `places._normalize_place` extracts
        # from `googleMapsUri` into `gmb["cid"]`. The alphanumeric Places
        # `place_id` (ChIJ...) is NOT what Xano indexes on — passing it
        # there silently misses every record.
        cfg_now = oya_runtime.config()
        cid = gmb.get("cid") or ""
        if cid and cfg_now.get("enable_xano_check"):
            gs = xano_check.gmb_status(cid)
            if gs == "active":
                st["candidate_gmb"] = gmb
                _mark_complete(st)
                state_mod.save(sender_id, st)
                return _emit(messages.render("active_account_template"), "complete",
                             diag={"customer": "active", "matched_at": "name_lookup", "cid": cid})
            if gs == "inactive":
                st["candidate_gmb"] = gmb
                _mark_complete(st)
                state_mod.save(sender_id, st)
                return _emit(
                    messages.returning_with_url(
                        cfg_now.get("returning_calendly_url") or "",
                        cfg_now.get("calendly_url") or "",
                    ),
                    "complete",
                    diag={"customer": "returning", "matched_at": "name_lookup", "cid": cid},
                )
        st["candidate_gmb"] = gmb
        st["pending_name"] = joined_name
        st["step"] = "awaiting_gmb_confirm"
        st["name_buffer"] = []
        state_mod.save(sender_id, st)
        return _emit(
            messages.confirm_one_with_listing(gmb.get("name", ""), gmb.get("formatted_address", "")),
            "awaiting_gmb_confirm",
            diag={"gmb_name": gmb.get("name"), "gmb_address": gmb.get("formatted_address")},
        )

    # Multiple candidates → ask for the address to disambiguate.
    st["pending_name"] = joined_name
    st["candidate_gmb"] = None
    st["step"] = "awaiting_address"
    st["name_buffer"] = []
    state_mod.save(sender_id, st)
    return _emit(messages.render("ask_address"), "awaiting_address",
                 diag={"candidates": len(candidates)})


def _on_gmb_confirmed(*, sender_id: str, st: dict,
                      min_reviews: int, min_rating: float) -> dict:
    gmb = st.get("candidate_gmb") or {}
    place_id = gmb.get("place_id")
    fresh = places.place_details(place_id) if place_id else gmb
    if not fresh:
        fresh = gmb
    st["candidate_gmb"] = fresh

    # Existence check already ran in _process_name when the GMB was first
    # resolved. If we're here, Xano either returned None (not a known
    # customer) or the check is disabled — proceed with qualification.
    fail = qualify.check(fresh, min_reviews=min_reviews, min_rating=min_rating)
    if fail is None:
        st["step"] = "awaiting_full_name"
        st.pop("idle_ping_sent_for", None)
        state_mod.save(sender_id, st)
        _schedule_idle_ping(sender_id=sender_id,
                            delay_seconds=_config_int("idle_ping_seconds", 180))
        return _emit(messages.render("ask_full_name"), "awaiting_full_name")

    st["step"] = _DISQUAL_STEP[fail]
    st["disqual_reason"] = fail
    state_mod.save(sender_id, st)
    return _emit(messages.render(_DISQUAL_MSG_KEY[fail]), _DISQUAL_STEP[fail])


def _on_gmb_confirmed_with_name(*, sender_id: str, st: dict, full_name: str,
                                min_reviews: int, min_rating: float) -> dict:
    """Same as `_on_gmb_confirmed` but the lead's full_name was already
    captured (race-with-confirm-prompt heuristic in awaiting_gmb_confirm).
    Sets `lead.full_name` and skips ahead to awaiting_email so the lead
    isn't asked for their name twice."""
    gmb = st.get("candidate_gmb") or {}
    place_id = gmb.get("place_id")
    fresh = places.place_details(place_id) if place_id else gmb
    if not fresh:
        fresh = gmb
    st["candidate_gmb"] = fresh

    fail = qualify.check(fresh, min_reviews=min_reviews, min_rating=min_rating)
    if fail is None:
        lead = dict(st.get("lead") or {})
        lead["full_name"] = full_name
        st["lead"] = lead
        st["step"] = "awaiting_email"
        st.pop("idle_ping_sent_for", None)
        state_mod.save(sender_id, st)
        _schedule_idle_ping(sender_id=sender_id,
                            delay_seconds=_config_int("idle_ping_seconds", 180))
        return _emit(messages.render("ask_email"), "awaiting_email",
                     diag={"name_inferred_from_confirm_race": True})

    # If the GMB doesn't qualify, drop the inferred name (we wouldn't have
    # asked for it on the disqual path anyway) and emit the disqual.
    st["step"] = _DISQUAL_STEP[fail]
    st["disqual_reason"] = fail
    state_mod.save(sender_id, st)
    return _emit(messages.render(_DISQUAL_MSG_KEY[fail]), _DISQUAL_STEP[fail])


def _read_input() -> dict:
    raw = os.environ.get("INPUT_JSON") or "{}"
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


def main() -> int:
    inp = _read_input()
    text = str(inp.get("text") or "")
    sender_id = str(inp.get("sender_id") or "")
    out = run(text=text, sender_id=sender_id)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
