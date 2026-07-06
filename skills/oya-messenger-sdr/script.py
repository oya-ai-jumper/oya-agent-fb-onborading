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
import re
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

    Classifier hint wins when present and high-confidence; otherwise the
    regex floor below decides. Two strong regex signals:
      1. Starts with digits AND contains a street suffix
         (e.g. "11689 Olio Rd Geist", "1051 S Coast Hwy 101").
      2. Contains a US state code preceded by a comma or space, with
         digits anywhere (e.g. "Fishers, IN 46037").
    """
    hint = matcher.hint_is_address(text)
    if hint is True:
        return True
    if hint is False:
        return False
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

    Classifier hint wins. Otherwise:
    Heuristic: 2-3 alphabetic tokens, none of which is a business suffix
    (`coffee`, `cafe`, `inc`, `llc`, etc.). The conservative side errors
    toward "looks like a business name" so an actual business reply
    (e.g. "Wake N Bakery" — 3 tokens, "Bakery" is a business suffix)
    still goes through the re-search path."""
    hint = matcher.hint_is_person_name(text)
    if hint is True:
        return True
    if hint is False:
        return False
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


_NON_DATA_INTENTS = {
    "question", "small_talk", "greeting", "closing", "affirmative",
    "negative", "complaint", "off_topic",
}


_NEGATION_PREFIXES = (
    "no its ", "no it's ", "no it is ",
    "no actually ", "no actaully ", "no but actually ",
    "no but ", "no wait ", "no the ", "no my ",
    "nope its ", "nope it's ", "nope actually ",
    "not that one its ", "not that one it's ", "not that one ",
    "wrong its ", "wrong it's ", "wrong one its ",
    "actually its ", "actually it's ", "actually ",
    "no ",
)


_STREET_NUMBER_RE = re.compile(r"^\s*(\d+)")


def _street_number(addr: str) -> str | None:
    """Extract the leading street number from an address string. Returns
    None when no digit-prefixed token exists. Used to detect address-
    snap mismatches: when the lead types '9001 E 116th St' and Places'
    nearby_search returns a business at '8997 E 116th St', the street
    numbers (9001 vs 8997) differ — surface that to the lead instead of
    silently substituting the snapped address as if it were what they typed."""
    if not addr:
        return None
    m = _STREET_NUMBER_RE.match(addr)
    return m.group(1) if m else None


def _addresses_differ_significantly(lead_input: str, candidate_address: str) -> bool:
    """True iff the street numbers in the two address strings differ.
    Conservative: requires BOTH to have a parseable street number; if
    either is missing, treats as 'no mismatch detected' so we don't
    over-surface for inputs that aren't street-number-prefixed (e.g.
    'Sears Tower Chicago' / 'Madison Square Garden').
    Returns False if numbers match exactly. Returns False on near
    matches (±2) since a small offset is often the same building
    indexed differently across Google's data sources."""
    a = _street_number(lead_input)
    b = _street_number(candidate_address)
    if not a or not b:
        return False
    try:
        return abs(int(a) - int(b)) > 2
    except ValueError:
        return a != b


def _siblings_at_same_address(top: dict, candidates: list[dict],
                              max_count: int = 4) -> list[str]:
    """Return up to `max_count` names of OTHER businesses in
    `candidates` that share the same street number as `top`. Used to
    detect strip-mall scenarios where the lead pastes an address and
    multiple businesses share it. Without this, nearby_search's
    auto-pick of the closest candidate hides the alternatives.

    Conservative: only includes siblings with a parseable street
    number that matches the top candidate's. If either doesn't have a
    street number, returns an empty list (no surprise alternatives).
    """
    if not top or not candidates:
        return []
    top_num = _street_number(top.get("formatted_address", ""))
    if not top_num:
        return []
    out: list[str] = []
    seen: set[str] = set()
    top_place_id = top.get("place_id") or ""
    for c in candidates:
        if (c.get("place_id") or "") == top_place_id:
            continue
        c_num = _street_number(c.get("formatted_address", ""))
        if c_num != top_num:
            continue
        name = (c.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
        if len(out) >= max_count:
            break
    return out


def _strip_negation_prefix(text: str) -> str:
    """Strip leading negation/correction phrases from a refined business
    name. Used when the lead rejects a GMB offer AND provides the
    corrected name in the same message, like 'no its Starbucks Coffee'
    or 'actually it's Cafe Noricha'. Without this, the raw text feeds
    Places.text_search → fuzzy matches on the negation tokens too,
    which causes weird results ('not that one Starbucks' could match
    a business with 'not' in the name).

    Conservative: only strips a recognized prefix from the START. If
    the input doesn't begin with one, returns the original text
    unchanged. Quotes / smart-quotes / leading punctuation are stripped
    after the prefix is removed."""
    if not text:
        return ""
    s = text.strip()
    s_lower = s.lower()
    for prefix in _NEGATION_PREFIXES:
        if s_lower.startswith(prefix):
            s = s[len(prefix):]
            break
    # Strip leading/trailing quotes / smart-quotes / punctuation that
    # often arrive with the corrected name (e.g. lead types: "no its
    # 'Starbucks Coffee Company'" → after prefix strip: "'Starbucks
    # Coffee Company'" → strip quotes → "Starbucks Coffee Company").
    return s.strip(" '\"`‘’“”.,;:!?").strip()


_OVERRIDE_PHRASES = (
    "verified",
    "i verified",
    "i've verified",
    "ive verified",
    "override",
    "i confirmed",
    "i've confirmed",
    "i confirm",
    "i added it",
    "i added the",
    "added it already",
    "i already added",
    "it's there",
    "its there",
    "it is there",
    "trust me",
    "skip",
    "skip this",
    "manual review",
    "have a team check",
    "have your team check",
    "let your team verify",
)


def _looks_like_override(text: str) -> bool:
    """True when the lead's input clearly asserts they've fixed the
    disqualification reason and wants to bypass the recheck loop.
    Conservative — matches a short list of explicit phrases so we don't
    accidentally trigger the override on tangential inputs. Used by
    the disqualified-state handler after 2+ recheck attempts have
    already failed."""
    if not text:
        return False
    s = (text or "").strip().lower()
    if not s:
        return False
    return any(p in s for p in _OVERRIDE_PHRASES)


def _qualify_diag(place: dict, fail_reason: str | None) -> dict:
    """Build a structured diagnostic dict for a disqualification so
    anna+team can diagnose 'Google Maps shows a website but the bot says
    no website' false negatives (live prod 2026-05-19: SALWAH JEWELRY had
    a website link visible on Google Maps but the API's `websiteUri` was
    empty because the lead set the link via a non-canonical field).

    Returns the diag as a dict that the caller folds into its `_emit`
    diag, so it lands in the job RESULT (visible in /runs) — NOT printed
    to stdout. Live prod 2026-05-20: this used to `print()` a
    `qualify_disqual ...` line to stdout, but the sandbox merges
    stderr→stdout and `skill_invoke` parses the whole stdout as JSON
    (`json.loads(out_str)`). Any disqualification corrupted that JSON →
    the parser returned `{}` → empty reply → the lead saw NOTHING after
    confirming a business that didn't qualify (Jaffa grills & kebab → no
    website; Hennighausen Olsen & McCrea → <10 reviews). Only the JSON
    line may go to stdout now."""
    if not fail_reason:
        return {}
    return {
        "qualify_disqual": fail_reason,
        "place_id": (place or {}).get("place_id", "?"),
        "name": (place or {}).get("name", "?"),
        "websiteUri": (place or {}).get("websiteUri")
                      or (place or {}).get("website") or "(empty)",
        "rating": (place or {}).get("rating", "?"),
        "userRatingCount": (place or {}).get("userRatingCount")
                           or (place or {}).get("user_ratings_total") or 0,
        "hours_present": bool((place or {}).get("regular_opening_hours")
                              or (place or {}).get("opening_hours")),
        "business_status": (place or {}).get("business_status", "?"),
        "google_maps_uri": (place or {}).get("google_maps_uri", "?"),
    }


def _check_non_data_input(text: str, *, current_step: str,
                          reprompt_key: str) -> dict | None:
    """Shared gate for the data-collection states (awaiting_full_name /
    email / phone): when the lead's input is clearly NOT the data we
    asked for (a question, chitchat, complaint, etc.), don't try to
    extract data from it. Either stay silent so the LLM's own reply is
    the only thing the lead sees (when classifier hint is present),
    or re-emit the relevant prompt (when no hint — internal call,
    scheduled rerun, or LLM tool-call passthrough).

    Returns an `_emit` dict when the input is non-data (caller returns
    it directly), or None when the input might be real data and the
    caller should continue with its normal extraction.

    Live prod 2026-05-19 (Mohamed): the LLM's tool call passed "who's
    the founder?" into awaiting_full_name → the existing validator
    didn't catch it (no question detector) → lead.full_name was
    stamped as "who's the founder?" → state moved to awaiting_email
    and pricing follow-up hung the LLM tool loop for 4+ minutes.
    """
    if not text:
        return None
    _cls = matcher.current_classification() or {}
    _intent = (_cls.get("intent") or "").strip()
    _conf = float(_cls.get("confidence") or 0.0)
    _by_hint = _conf >= 0.55 and _intent in _NON_DATA_INTENTS
    _by_regex = (
        matcher.looks_like_question(text)
        or matcher.looks_like_intent_to_proceed(text)
    )
    if not (_by_hint or _by_regex):
        return None
    # Always re-prompt with the relevant clean template, regardless of
    # whether the classifier hint is present. Why: when called via
    # SDR-direct (hint present), the lead needs visible feedback or the
    # bot looks broken. When called via the LLM gateway's tool loop
    # (no hint passed to tool args), the LLM relays the re-prompt
    # verbatim per behavior_rules — which produces coherent output
    # like "I'm doing great. ⏎ Could you share your business name?"
    # The earlier silent-when-no-hint design risked the LLM saying
    # nothing if it interpreted "no SDR reply" as "no need to respond"
    # — Mohamed's onboarding hung for 4+ min in exactly that mode.
    return _emit(messages.render(reprompt_key), current_step,
                 diag={"non_data_input": True,
                       "hint_intent": _intent or "none",
                       "detector": "hint" if _by_hint else "regex"})


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


# Completions reached because the lead is ALREADY a customer (active paid
# account, or a lapsed/returning account). These leads already received
# their correct terminal message — login + support info for active, the
# reactivation Calendly link for returning. The onboarding-flavored
# `completion_followup_ack` ("team will call to finish setting up your free
# trial") is WRONG for them: anna+team confirmed they never call active
# customers, and returning customers are reactivating, not starting a
# trial. The cooldown handler stays silent for these reasons rather than
# echoing the onboarding promise.
_EXISTING_CUSTOMER_COMPLETE_REASONS = {"active_customer", "returning_customer"}


def _mark_complete(st: dict, reason: str | None = None) -> None:
    """Transition to `complete` and stamp completion time. Centralizing this
    means every path that ends a conversation (success, failure, timeout,
    active-customer redirect, returning-customer redirect) participates in
    the cooldown logic.

    `reason` records WHY the conversation completed so the cooldown
    re-engagement handler can tailor follow-ups — e.g. an active customer
    must never be told "someone will call you to set up your trial"."""
    st["step"] = "complete"
    st["completed_at"] = datetime.now(timezone.utc).isoformat()
    if reason:
        st["complete_reason"] = reason
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


def _seconds_since_complete(st: dict) -> float:
    """Seconds elapsed since the conversation entered the complete
    state. Returns infinity when there's no `completed_at` timestamp
    (state never completed) so callers can short-circuit on the
    'not just completed' branch."""
    completed_at = st.get("completed_at")
    if not completed_at:
        return float("inf")
    try:
        ts = datetime.fromisoformat(completed_at)
    except ValueError:
        return float("inf")
    return (datetime.now(timezone.utc) - ts).total_seconds()


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
    #
    # Verification backoff: cumulative sleep up to ~120s. Live prod
    # 2026-05-19 incident: Mohamed onboarded Bridgeview Pet Wellness
    # Clinic, the Bubble form submission succeeded and Xano eventually
    # had record id=57256 with `nonPayingClient: false`, but Jumper's
    # Bubble\u2192Xano sync took longer than the original 30s window
    # (1+2+4+8+15) so verification timed out and the bot emitted
    # `onboarding_error` ("team will call you") for a successful
    # onboarding. Extending the schedule to 120s (1+2+4+8+15+30+60)
    # gives the sync time to complete; if it STILL hasn't synced by
    # then we fall back to the polite onboarding_error which is correct
    # behavior for the rare "Bubble accepted but Xano never got it" path.
    # `xano_verify_backoff_schedule` is exposed via SKILL.md as a
    # comma-separated list of seconds so customers can tune per their
    # webhook latency profile.
    _backoff_raw = cfg.get("xano_verify_backoff_schedule") or "1,2,4,8,15,30,60"
    try:
        backoff_schedule = tuple(
            float(x) for x in str(_backoff_raw).split(",") if x.strip()
        )
    except (ValueError, TypeError):
        backoff_schedule = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 60.0)
    cid = gmb.get("cid") or ""
    verified = False
    if cid and cfg.get("enable_xano_check"):
        # Immediate check at t=0 BEFORE any sleep — when Bubble→Xano
        # sync is fast (sometimes <1s) the record is already there and
        # the lead gets the Calendly link without waiting a full
        # backoff cycle. Live prod 2026-05-19: the old "sleep first
        # then check" cost up to 30-120s of waiting even when the
        # record had already synced in the first second.
        status = xano_check.gmb_status(cid)
        if status in {"active", "inactive"}:
            verified = True
        else:
            for attempt_delay in backoff_schedule:
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
    # Playback SUCCEEDED but Xano sync hasn't completed within the
    # verify window. The record is likely on its way (Bubble→Xano
    # webhook is usually delivered within minutes). Send the optimistic
    # template with the Calendly link so the lead has a clear path
    # forward — they can book the call, and the team can verify the
    # Xano record async. Live prod 2026-05-19: records id 57256,
    # 57257, 57260 all confirmed in Xano shortly AFTER the bot sent
    # the misleading onboarding_error template; with this branch the
    # lead would have seen the Calendly link in the same turn.
    cal_url = (cfg.get("calendly_url") or "").strip()
    return _emit(
        messages.render("onboarding_unverified_optimistic", calendly_url=cal_url),
        "complete",
        diag={"onboarding": "unverified_optimistic",
              "reason": "form_submitted_xano_sync_pending",
              "playback_ok": True},
    )


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


def run(text: str, sender_id: str, classification: dict | None = None) -> dict:
    if not sender_id:
        return _emit(None, "error", diag={"error": "missing_sender_id"})

    # Wire the gateway's Gemini Flash classification hint into matcher's
    # module-global so each matcher (`is_affirmative`, `looks_like_closing`,
    # `extract_phone`, etc.) prefers the typed intent over its regex floor.
    # Cleared at end-of-turn to avoid cross-turn leakage on the long-running
    # sandbox process. None = no hint; matchers fall back to regex.
    matcher.set_classification(classification)
    try:
        return _run_with_classification(text, sender_id)
    finally:
        matcher.clear_classification()


def _run_with_classification(text: str, sender_id: str) -> dict:
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
        # Bypass the cooldown when the lead provides a clearly NEW
        # business signal (address or business_name). Live prod 2026-05-19:
        # Mohamed onboarded Starbucks → Xano flagged it as an active
        # customer → state moved to complete → he typed a different
        # address ("11700 Olio Rd") for a different business → the
        # cooldown's followup_ack fired ("Got it! Someone from our team
        # will give you a quick call shortly") which sounded final and
        # locked him out for 10+ min. Detecting the new business signal
        # lets him pivot to a different onboarding immediately.
        _hint_cls_complete = matcher.current_classification() or {}
        _hint_intent_complete = (_hint_cls_complete.get("intent") or "")
        _hint_conf_complete = float(_hint_cls_complete.get("confidence") or 0.0)
        _looks_like_new_business = (
            _looks_like_address(text_clean)
            or (_hint_conf_complete >= 0.55
                and _hint_intent_complete in {"business_name", "address"})
        )
        if _looks_like_new_business:
            st = {"step": "awaiting_name", "name_buffer": []}
            state_mod.save(sender_id, st)
            # Don't re-emit the full welcome — the lead already sees the
            # bot, just process the new business immediately. Falls
            # through to the awaiting_name handler below.
            step = "awaiting_name"
        else:
            cooldown = _config_int("re_engage_after_seconds", _RE_ENGAGE_DEFAULT_SECONDS)
            if _within_complete_cooldown(st, cooldown):
                # Existing-customer completions (active or returning) already
                # got their correct terminal message and must NOT receive the
                # onboarding-flavored completion_followup_ack. Live prod
                # 2026-05-20: 'Life by Pilates NJ' was an active customer
                # ("you already have an account, please log in"), then said
                # "Thanks" and got "team will give you a quick call shortly to
                # finish setting up your free Jumper Local trial" — promising a
                # call+trial to a paying customer. anna+team never call active
                # customers. Stay silent for these; a NEW business signal still
                # re-routes above via `_looks_like_new_business`.
                if st.get("complete_reason") in _EXISTING_CUSTOMER_COMPLETE_REASONS:
                    return _emit(None, "complete",
                                 diag={"in_cooldown": True,
                                       "existing_customer_silent":
                                           st.get("complete_reason"),
                                       "input": text_clean[:40]})
                # Suppress completion_followup_ack on ANY closing or
                # affirmative acknowledgement, regardless of timing.
                # Live prod 2026-05-19: anna+team customized
                # `completion_followup_ack` to the same copy as
                # `onboarding_error`, so even a 'thx' a couple minutes
                # after the terminal message produced an identical
                # echo. The followup_ack template is for substantive
                # re-engagements ('hey did this work?'), not for
                # acknowledging the bot's just-sent reply. If the lead
                # types 'thx' / 'ok' / 'cool' / 'got it', stay silent.
                # The cooldown window still applies for non-ack inputs
                # (questions, sentences) which DO get the followup_ack.
                _is_ack = (
                    matcher.looks_like_closing(text_clean)
                    or matcher.is_affirmative(text_clean)
                )
                if _is_ack:
                    # Mark the slot used so a second knee-jerk message
                    # ("ok thanks") doesn't trigger anything either.
                    st["completion_ack_sent"] = True
                    state_mod.save(sender_id, st)
                    return _emit(None, "complete",
                                 diag={"in_cooldown": True,
                                       "suppressed_ack": True,
                                       "input": text_clean[:40]})
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
        # Acknowledgements / non-name replies: when the lead types "ok lets
        # go" / "yes" / "sure" / "thanks" / "hello" in response to the
        # welcome (or after an LLM answer in the same state), treat it as
        # a non-name signal and re-emit the prompt. Without this, the
        # script feeds the acknowledgement to Places.text_search, gets 0
        # results, and replies with the confusing NOT_FOUND message
        # ("couldn't find that listing on Google") — exactly what
        # happened to Mohamed on 2026-05-19 after the LLM answered his
        # pricing question and he said "ok lets go".
        # State-aware non-name gate. Two flavors:
        #   1. Classifier hint is present (LLM-routed turn — the gateway
        #      dispatcher classified and is ALSO firing the agent's tool
        #      loop, which produces its own user-facing reply). For intents
        #      that signal "this isn't a business name" (greeting / question /
        #      closing / affirmative / negative / complaint / off_topic /
        #      email / phone / full_name), return reply=None so the SDR
        #      stays silent and the LLM's reply is the only thing the
        #      lead sees. This stops the prod bug where "how are you?"
        #      got "I'm doing great" from the LLM CONCATENATED with the
        #      NOT_FOUND template from the SDR's _process_name call.
        #   2. No classifier hint (scheduled rerun, debounce defer-fire,
        #      sandbox test, dispatcher classifier failed): fall back to
        #      a clean re-prompt so the lead isn't left wondering whether
        #      the bot heard them. Uses ask_business_name_when_address
        #      template (existing copy: "What's the name of your business?").
        _hint_cls = matcher.current_classification() or {}
        _hint_intent_now = (_hint_cls.get("intent") or "")
        _hint_conf = float(_hint_cls.get("confidence") or 0.0)
        # NOTE: `full_name` is intentionally NOT in this set. We're asking
        # for the lead's BUSINESS name here, and a huge share of local
        # businesses are named after a person — contractors, law firms,
        # dentists, salons, realtors. Live prod 2026-05-20: "Richard P.
        # Koenig Gen Contractor" was classified `full_name` (it leads with
        # a person name) and the gate rejected it three times with "Could
        # you share your business name?" while the lead insisted "I just
        # did". A person-name-shaped string must flow through to Places —
        # if it's a real business, Places finds it and we confirm; if not,
        # _process_name's NOT_FOUND re-prompt handles it.
        _NON_NAME_INTENTS = {
            "greeting", "question", "closing", "affirmative", "negative",
            "complaint", "off_topic", "email", "phone",
            "small_talk",  # forward-compat for the chitchat fast-path
        }
        _is_non_name_by_hint = (
            _hint_conf >= 0.55 and _hint_intent_now in _NON_NAME_INTENTS
        )
        _is_non_name_by_regex = (
            matcher.looks_like_closing(text_clean)
            or matcher.is_affirmative(text_clean)
            # Catches "how are you?", "what's up?", "u there?" coming via
            # the agent's LLM tool-call path where the dispatcher's
            # classification hint isn't propagated.
            or matcher.looks_like_question(text_clean)
            # Catches "i wanna sign up", "let's get started", "i'm ready",
            # "sign me up" — intent-to-proceed phrases that aren't
            # business names. Defense in depth: works even when the
            # classifier returns `other` and we have no hint to gate on
            # (live prod 2026-05-19, the classifier prompt update for
            # affirmative had not yet deployed).
            or matcher.looks_like_intent_to_proceed(text_clean)
        )
        if _is_non_name_by_hint or _is_non_name_by_regex:
            # Always re-prompt with the clean `ask_business_name` template
            # (the older `ask_business_name_when_address` said "I couldn't
            # find a business at that address" — misleading for inputs
            # like "who's life?" / "are you stupid?" that aren't addresses).
            # Re-prompting in both SDR-direct and LLM-tool-call paths means
            # the lead always sees a coherent message: in SDR-direct it's
            # the only reply; in LLM-tool-call the LLM relays it verbatim
            # alongside its own answer if any.
            return _emit(messages.render("ask_business_name"),
                         "awaiting_name",
                         diag={"non_name_input": True,
                               "hint_intent": _hint_intent_now or "none",
                               "detector": "hint" if _is_non_name_by_hint else "regex"})
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
        # Pure negative ("no", "nope", "wrong", "nah", "incorrect"): the
        # lead is rejecting the candidate but hasn't yet provided the
        # CORRECT business name. Without this branch, the script falls
        # through to _process_name(joined_name="no") and Places
        # fuzzy-matches "no" to "No 1911 Inc" in Baker City Oregon
        # (live prod 2026-05-19) — a real but utterly unrelated business.
        # Same trap on "nope" → various businesses. Re-prompt for the
        # actual business name.
        _is_pure_negative = (
            matcher.is_negative(text_clean)
            and len(text_clean.split()) <= 2
        )
        if _is_pure_negative:
            st["candidate_gmb"] = None
            st["step"] = "awaiting_name"
            st["name_buffer"] = []
            state_mod.save(sender_id, st)
            return _emit(messages.render("ask_business_name"),
                         "awaiting_name",
                         diag={"rejected_candidate": True})
        # Lead provided an address as the do-over ("202 S Franklin St,
        # Chicago, IL 60606" — live prod 2026-05-19). Route to the
        # address-lookup helper so nearby_search finds the actual
        # business at those coordinates, rather than text_search
        # returning the literal-address entity (which has no hours /
        # website / reviews and immediately disqualifies on the next
        # turn). Same trap that hit the awaiting_name handler before we
        # added its _looks_like_address gate.
        if _looks_like_address(text_clean):
            st["candidate_gmb"] = None
            st["step"] = "awaiting_name"
            st["name_buffer"] = []
            state_mod.save(sender_id, st)
            return _process_address_lookup(
                sender_id=sender_id, st=st, address=text_clean,
                min_reviews=min_reviews, min_rating=min_rating,
            )
        if matcher.is_negative(text_clean) or text_clean:
            # Treat anything else as a refined name → re-search. Combine
            # with the lead's previously-typed address (if known) so we
            # don't lose the address context when the lead corrects the
            # business name. Live prod 2026-05-19: "no its 'Starbucks
            # Coffee Company'" after a wrong-GMB offer at 9001 E 116th
            # St → script used to throw away the address and re-search
            # just the business name → Places returned multiple Starbucks
            # → asked for address again. Now we re-combine and re-search.
            cleaned_name = _strip_negation_prefix(text_clean)
            prev_addr = (
                st.get("last_address_input")
                or (st.get("candidate_gmb") or {}).get("formatted_address", "")
                or ""
            )
            joined = (
                f"{cleaned_name} {prev_addr}".strip()
                if (prev_addr and cleaned_name)
                else (cleaned_name or text_clean)
            )
            st["step"] = "awaiting_name"
            st["name_buffer"] = []
            state_mod.save(sender_id, st)
            # `after_address_round=True` picks the first candidate when
            # there's only one — fine here because the combined query is
            # specific enough to land on the right business.
            return _process_name(
                sender_id=sender_id, st=st, joined_name=joined,
                min_reviews=min_reviews, min_rating=min_rating,
                after_address_round=bool(prev_addr),
            )
        return _emit(messages.render("confirm_one"), "awaiting_gmb_confirm")

    # Lead-info collection.
    if step == "awaiting_full_name":
        if not text_clean:
            return _emit(messages.render("ask_full_name"), "awaiting_full_name")
        # Reject obvious non-name replies — without this, "yes" / "ok" / "k"
        # land in `lead.full_name` and the lead's downstream dashboard is
        # stamped with garbage. Three live cases on 2026-05-18: leads typed
        # "yes" expecting to confirm something and the bot accepted it as
        # their name. Don't gate on length alone — "Anna" / "Bob" / "Jay"
        # are legitimate first-only replies.
        # ALSO reject question-shaped inputs. Live prod 2026-05-19:
        # Mohamed asked "who's the founder?" while in awaiting_full_name.
        # The LLM gateway path handled the question correctly (KB answer
        # via behaviour_rules), but the LLM also called oya_messenger_sdr
        # as a tool with text="who's the founder?" — which the SDR
        # accepted as the lead's full_name. State moved to awaiting_email
        # with `lead.full_name = "who's the founder?"`. The validator now
        # checks for question shape (classifier hint or regex) and stays
        # silent / re-prompts.
        _non_data = _check_non_data_input(
            text_clean, current_step="awaiting_full_name",
            reprompt_key="ask_full_name",
        )
        if _non_data is not None:
            return _non_data
        if (matcher.is_affirmative(text_clean) or matcher.is_negative(text_clean)
                or matcher.looks_like_closing(text_clean)
                or matcher.looks_like_email(text_clean)
                or len(text_clean) < 2
                or text_clean.isdigit()):
            return _emit(messages.render("ask_full_name"), "awaiting_full_name",
                         diag={"reject_full_name": "looks_non_name",
                               "input": text_clean[:40]})
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
        # Reject question-shaped or chitchat inputs (see awaiting_full_name).
        # Without this, an LLM tool call that passes a question through
        # would extract_email() → None → re-emit ask_email, leaving the
        # lead confused. With this, the SDR stays silent so the LLM's
        # own KB-grounded answer (when present) is the only reply.
        _non_data = _check_non_data_input(
            text_clean, current_step="awaiting_email",
            reprompt_key="ask_email",
        )
        if _non_data is not None:
            return _non_data
        email = matcher.extract_email(text_clean)
        if not email:
            return _emit(messages.render("ask_email"), "awaiting_email")
        # Reject obvious placeholder emails BEFORE the browser playback
        # would attempt to register them with Bubble (and fail at the
        # form-validation step, surfacing the misleading onboarding_error
        # template). Live prod 2026-05-19: 'test+test@email.com' passed
        # the existing extract_email regex.
        if matcher.looks_like_test_email(email):
            return _emit(messages.render("ask_email_again_test_address"),
                         "awaiting_email",
                         diag={"reject_email": "placeholder",
                               "input": email[:60]})
        status = xano_check.email_status(email)
        if status == "active":
            _mark_complete(st, reason="active_customer")
            state_mod.save(sender_id, st)
            return _emit(messages.render("active_account_template"), "complete",
                         diag={"customer": "active"})
        if status == "inactive":
            _mark_complete(st, reason="returning_customer")
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
        # Same non-data gate as awaiting_full_name / awaiting_email.
        _non_data = _check_non_data_input(
            text_clean, current_step="awaiting_phone",
            reprompt_key="ask_phone",
        )
        if _non_data is not None:
            return _non_data
        phone = matcher.extract_phone(text_clean)
        if not phone:
            return _emit(messages.render("ask_phone"), "awaiting_phone")
        # Reject obvious placeholder phone numbers BEFORE the browser
        # playback. Live prod 2026-05-19 surfaced fake data like
        # 5555555555 / 1234567890 — the extract_phone regex was happy
        # with them but Bubble's form validation would reject them.
        if matcher.looks_like_test_phone(phone):
            return _emit(messages.render("ask_phone_again_test_number"),
                         "awaiting_phone",
                         diag={"reject_phone": "placeholder",
                               "input": phone[:30]})
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
        # Conversational closings ("thx", "thanks", "thank you", "got it",
        # "ok", "okay", "okaaay") are silent — polite acknowledgements,
        # not recheck signals and not a new search. Without this, "okaaay"
        # falls through into Places text_search and surfaces "OKY App", a
        # business in Florida unrelated to the disqualified GMB; "thank
        # you" surfaces "Thank You Berry Much Farms" in Oregon. Both were
        # observed live (Noor 2026-05-19, Anna 2026-05-08). Classifier
        # hint catches "okaaay" / "okay" / variants the regex floor misses.
        if text_clean and matcher.looks_like_closing(text_clean):
            return _emit(None, step, diag={"closing": True})
        # Manual-override escape: after 2 recheck attempts have already
        # failed AND the lead claims they've fixed the issue ("verified",
        # "i confirmed", "override", "i added it"), trust the lead, flag
        # the conversation for human review, and resume the flow at
        # awaiting_full_name. Live prod 2026-05-19: SALWAH JEWELRY had a
        # website on Google Maps but the Places API returned no
        # `websiteUri`. Without this escape the lead loops forever.
        # Anna+team see the `manual_review_requested` flag on state and
        # can spot-check the GMB in their Bubble dashboard.
        _override_attempts = int(st.get("recheck_attempts") or 0)
        if text_clean and _override_attempts >= 2 and _looks_like_override(text_clean):
            lead = dict(st.get("lead") or {})
            lead["manual_review_requested"] = True
            lead["manual_review_reason"] = st.get("disqual_reason") or "unspecified"
            st["lead"] = lead
            st["step"] = "awaiting_full_name"
            st.pop("idle_ping_sent_for", None)
            st.pop("recheck_attempts", None)
            state_mod.save(sender_id, st)
            _schedule_idle_ping(sender_id=sender_id,
                                delay_seconds=_config_int("idle_ping_seconds", 180))
            return _emit(messages.render("disqual_override_acknowledged"),
                         "awaiting_full_name",
                         diag={"manual_override": True,
                               "previous_disqual": st.get("disqual_reason")})
        # Address do-over: lead pastes a street address while disqualified.
        # Real prod 2026-05-19 — Mohamed got disqualified on Kava Espresso
        # (no website), then typed "5560 N Illinois St, Indianapolis, IN
        # 46208" expecting the bot to look up the business at that
        # address. Without this branch the script falls through to the
        # recheck path on the OLD GMB and re-emits the disqual message,
        # which is dead-end UX — the lead has to type MAPS to retry.
        # Route address inputs through the nearby-business resolver
        # (same as awaiting_name handles) so the lead can switch
        # businesses without resetting state.
        if text_clean and _looks_like_address(text_clean):
            st["step"] = "awaiting_name"
            st["name_buffer"] = []
            st["candidate_gmb"] = None
            st["disqual_reason"] = None
            state_mod.save(sender_id, st)
            return _process_address_lookup(
                sender_id=sender_id, st=st, address=text_clean,
                min_reviews=min_reviews, min_rating=min_rating,
            )
        # Restrict the "new business name" fast-path to inputs the
        # classifier flagged as business_name OR (when no classifier hint)
        # to inputs that actually look like a name search — not a single
        # short token that Places would fuzzy-match to anything ("Okaaay" →
        # "OKY App"). Either trust the classifier or require ≥ 2 tokens.
        if text_clean and not matcher.is_affirmative(text_clean):
            looks_like_new_search = (
                matcher.hint_is_business_name(text_clean) is True
                or (matcher.hint_is_business_name(text_clean) is None
                    and len(text_clean.split()) >= 2)
            )
            if looks_like_new_search:
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
        _qd = _qualify_diag(fresh, fail)
        if fail is None:
            st["step"] = "awaiting_full_name"
            st.pop("idle_ping_sent_for", None)
            st.pop("recheck_attempts", None)
            state_mod.save(sender_id, st)
            _schedule_idle_ping(sender_id=sender_id,
                                delay_seconds=_config_int("idle_ping_seconds", 180))
            return _emit(messages.render("ask_full_name"), "awaiting_full_name",
                         diag={"recheck": "passed"})
        st["step"] = _DISQUAL_STEP[fail]
        st["disqual_reason"] = fail
        # Recheck-still-failing counter. The lead has now tried at least
        # once and Google's API still shows the same issue. From the
        # second still-failing onward, swap the canned per-reason
        # template (which reads as "please add a website to your profile
        # and try again", repeated verbatim) for the softer
        # `disqual_recheck_still_failing` copy that acknowledges Google's
        # propagation lag. Live prod 2026-05-19: Mohamed disqualified on
        # missing website, said "it has website", saw the same canned
        # message twice — felt gaslit.
        attempts = int(st.get("recheck_attempts") or 0) + 1
        st["recheck_attempts"] = attempts
        state_mod.save(sender_id, st)
        if attempts >= 2:
            what_missing = {
                "hours": "no business hours listed",
                "website": "no website listed",
                "reviews": f"fewer than {min_reviews} reviews",
                "rating": f"a rating under {min_rating}",
            }.get(fail, "the same issue")
            return _emit(
                messages.render("disqual_recheck_still_failing",
                                what_is_missing=what_missing),
                _DISQUAL_STEP[fail],
                diag={"recheck": "still_failing", "attempts": attempts, **_qd},
            )
        return _emit(messages.render(_DISQUAL_MSG_KEY[fail]), _DISQUAL_STEP[fail],
                     diag={"recheck": "still_failing", "attempts": attempts, **_qd})

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
        # Widened from max_results=5 to max_results=10 so strip-mall
        # alternatives surface alongside the closest candidate. Live
        # prod 2026-05-19: '11630 Olio Rd' (a 6+ business strip mall)
        # would otherwise only show Olio Nail & Spa.
        candidates = places.nearby_search(
            latitude=lat, longitude=lng,
            radius_meters=75.0, max_results=10,
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
            _mark_complete(st, reason="active_customer")
            state_mod.save(sender_id, st)
            return _emit(messages.render("active_account_template"), "complete",
                         diag={"customer": "active",
                               "matched_at": "address_lookup", "cid": cid})
        if gs == "inactive":
            st["candidate_gmb"] = gmb
            _mark_complete(st, reason="returning_customer")
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
    # Remember the LEAD'S original address input so we can combine it
    # with a corrected business name if they reject the candidate.
    # Live prod 2026-05-19: Mohamed pasted "9001 E 116th St" → bot offered
    # AAA Fishers Office (snapped from 8997) → Mohamed said "no its
    # Starbucks Coffee Company". Without preserved context the script
    # threw away the address and asked for it again. With this we can
    # search "Starbucks Coffee Company 9001 E 116th St Fishers IN" and
    # land on the correct business in one turn.
    st["last_address_input"] = address
    st["step"] = "awaiting_gmb_confirm"
    st["name_buffer"] = []
    state_mod.save(sender_id, st)
    # Detect strip-mall (multiple distinct businesses at the same
    # street number). Surface alternatives so the lead can pick.
    siblings = _siblings_at_same_address(gmb, candidates, max_count=4)
    snap_mismatch = _addresses_differ_significantly(
        address, gmb.get("formatted_address", "")
    )
    if siblings:
        return _emit(
            messages.render("confirm_one_with_alternatives",
                            name=gmb.get("name", ""),
                            address=gmb.get("formatted_address", ""),
                            alternatives=", ".join(siblings)),
            "awaiting_gmb_confirm",
            diag={"input_was_address": True,
                  "strip_mall_alternatives": siblings,
                  "places_results": len(candidates),
                  "gmb_name": gmb.get("name"),
                  "gmb_address": gmb.get("formatted_address")},
        )
    # If nearby_search snapped to a noticeably-different street number,
    # be transparent. Default `confirm_one_with_listing` template silently
    # substitutes the candidate's address — which feels like a lie when
    # the lead typed "9001" and sees "8997" (live prod 2026-05-19,
    # Mohamed asked "why it changed the address"). The
    # `confirm_closest_listing` template explicitly acknowledges the snap.
    if snap_mismatch:
        return _emit(
            messages.render("confirm_closest_listing",
                            name=gmb.get("name", ""),
                            address=gmb.get("formatted_address", "")),
            "awaiting_gmb_confirm",
            diag={"input_was_address": True,
                  "address_snap_mismatch": True,
                  "lead_address": address,
                  "places_results": len(candidates),
                  "gmb_name": gmb.get("name"),
                  "gmb_address": gmb.get("formatted_address")},
        )
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

    # `drop_pure_address_results=True` filters out literal-address
    # entities (`types: ["street_address" / "premise" / "subpremise" /
    # "route"]`) from Places. Without this filter, lead inputs like
    # "202 S Franklin St, Chicago, IL 60606" (typed in awaiting_gmb_confirm
    # after rejecting a wrong candidate) come back with the bare address
    # entity at the top — which has no hours, website, or reviews, so it
    # immediately disqualifies on the next turn with a misleading "please
    # add business hours" message. Live prod 2026-05-19. The address-lookup
    # path (_process_address_lookup → nearby_search) is where actual
    # businesses-at-an-address are resolved; here we only want real
    # business entities.
    candidates = places.text_search(joined_name, drop_pure_address_results=True)
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
                _mark_complete(st, reason="active_customer")
                state_mod.save(sender_id, st)
                return _emit(messages.render("active_account_template"), "complete",
                             diag={"customer": "active", "matched_at": "name_lookup", "cid": cid})
            if gs == "inactive":
                st["candidate_gmb"] = gmb
                _mark_complete(st, reason="returning_customer")
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
    _qd = _qualify_diag(fresh, fail)
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
    return _emit(messages.render(_DISQUAL_MSG_KEY[fail]), _DISQUAL_STEP[fail],
                 diag=_qd or None)


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
    _qd = _qualify_diag(fresh, fail)
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
    return _emit(messages.render(_DISQUAL_MSG_KEY[fail]), _DISQUAL_STEP[fail],
                 diag=_qd or None)


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
    classification = inp.get("classification")
    if not isinstance(classification, dict):
        classification = None
    out = run(text=text, sender_id=sender_id, classification=classification)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
