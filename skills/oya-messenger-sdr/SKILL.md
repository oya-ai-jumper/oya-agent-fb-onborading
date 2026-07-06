---
name: oya-messenger-sdr
display_name: "Messenger SDR (template)"
description: "Procedural Messenger SDR template. State machine drives MAPS-trigger → GMB lookup → qualification → onboarding handoff. Per-agent config drives all messages, thresholds, and integrations."
category: sales
icon: message-circle
skill_type: sandbox
catalog_type: addon
direct_relay: true
entry_point: scripts/script.py
requirements: "httpx>=0.25"
metadata:
  # Tells the gateway dispatcher (app/gateways/fast_path.py) that this skill
  # has a deterministic welcome turn — when an inbound matches the gateway's
  # trigger_keywords on a fresh conversation, render `welcome_template`,
  # send synchronously, seed psid:{sender_id} → awaiting_name, persist a
  # Thread+Messages pair, and warm the sandbox in the background. Skips the
  # full job → LLM → sandbox round-trip for the welcome turn (cuts median
  # latency from ~28s to ~1s without changing behaviour for any subsequent
  # turn). Per CLAUDE.md any new gateway flow needs a feature flag, but this
  # is opt-in by skill installation and falls back to the normal path on
  # any failure inside try_fast_path_welcome — the worst-case is the
  # original behaviour.
  fast_path_welcome:
    reply_template_field: welcome_template
    state_key_template: "psid:{sender_id}"
    initial_state:
      step: awaiting_name
      name_buffer: []
    state_ttl_seconds: 2592000  # 30 days, matches scripts/state.py
resource_requirements:
  - env_var: GOOGLE_PLACES_API_KEY
    name: "Google Places API Key"
    description: "Used to look up the lead's GMB by name + address. Get one at https://console.cloud.google.com/google/maps-apis/credentials. Restrict the key to the Places API and to your server IPs."
    secret: true
    required: true
  - env_var: XANO_MCP_URL
    name: "Xano MCP URL"
    description: "Full URL of the Xano MCP streamable-HTTP endpoint, e.g. https://xktx-zdsw-4yq2.n7.xano.io/x2/mcp/<id>/mcp/stream. The skill calls the `get_gmb` tool with the lead's email to detect returning/current customers. Leave blank to disable the customer-existence check."
    required: false
  - env_var: XANO_MCP_BEARER
    name: "Xano MCP Bearer Token"
    description: "Bearer token for the MCP server's Authorization header."
    secret: true
    required: false
  - env_var: BROWSER_API_BASE
    name: "Browser API Base URL"
    description: "Auto-injected from the agent's connected browser gateway. Required for the deterministic onboarding form-fill in scripts/playback.py — without this, the SDR returns the polite onboarding_error fallback instead of attempting the form. Connect a Browser gateway in sandbox mode and this fills in automatically."
    required: false
  - env_var: BROWSER_API_KEY
    name: "Browser API Key"
    description: "Auto-injected from the agent's connected browser gateway. Sandbox-mode browsers use the literal value `local`; pool-mode browsers use the customer's AgentChrome key."
    secret: true
    required: false
config_schema:
  properties:
    bot_name:
      type: string
      label: "Bot Name"
      description: "How the bot introduces itself in the welcome message ({bot_name} interpolates into welcome_template)."
      default: "Hannah"
      group: "branding"
    self_company_name:
      type: string
      label: "Your Company Name"
      description: "If a lead types this name as their business, the bot replies with self_company_response (PDF spec rule #12)."
      default: "Jumper Media"
      group: "branding"
    trigger_keyword:
      type: string
      label: "Trigger / reset keyword"
      description: "Word the lead can send AT ANY TIME to wipe state and start a fresh conversation. Match is case-insensitive and must be an exact full-message match (e.g. 'MAPS', not 'I'm at MAPS Coffee'). This is the same keyword the Facebook Messenger gateway gates new conversations on (configured separately on the gateway)."
      default: "MAPS"
      group: "branding"
    onboarding_url:
      type: string
      label: "Onboarding URL"
      description: "URL the agent's browser-onboarding step navigates to after collecting the lead's details. Use {place_id} for the lead's confirmed Google Places ID, {gmb_name} for the GMB name. The skill substitutes them at runtime."
      default: "https://local.jumpermedia.co/onboarding?placeID={place_id}"
      group: "urls"
    calendly_url:
      type: string
      label: "Booking Calendly URL"
      description: "Sent to the lead at the end of a successful onboarding."
      default: "https://calendly.com/jmpsales/google-ranking-increase-jumper-local"
      group: "urls"
    returning_calendly_url:
      type: string
      label: "Returning Customer Calendly URL"
      description: "Optional separate URL for returning (lapsed) customers. Leave blank to use calendly_url for both."
      default: ""
      group: "urls"
    support_email:
      type: string
      label: "Support email"
      description: "Used in the active-account message when a current customer pings the agent."
      default: "cs@jumpermedia.co"
      group: "urls"
    enable_xano_check:
      type: boolean
      label: "Enable Xano customer check"
      description: "When on, the skill calls the Xano MCP `get_gmb` tool with the lead's email after they provide it; if the record's `nonPayingClient` is true → returning-customer reply, false → active-account reply. Requires XANO_MCP_URL + XANO_MCP_BEARER credentials."
      default: false
      group: "xano"
    xano_verify_backoff_schedule:
      type: string
      label: "Xano post-onboarding verify backoff (seconds, comma-separated)"
      description: "After the browser playback successfully submits the Bubble form, the script polls Xano's `get_gmb` to verify the record was synced before sending the Calendly link. Each value is a `time.sleep` before the next attempt — total of the schedule is the maximum window we'll wait for Bubble→Xano to sync. Default 1,2,4,8,15,30,60 (~120s) suits Jumper's current webhook latency profile; live prod 2026-05-19 showed the prior 30s window was too short. Lower if your sync is faster; raise if you see false-negative `onboarding_error` messages on successfully-onboarded leads."
      default: "1,2,4,8,15,30,60"
      group: "xano"
    min_reviews:
      type: integer
      label: "Minimum review count"
      description: "GMB must have at least this many reviews to qualify."
      default: 10
      group: "thresholds"
    min_rating:
      type: number
      label: "Minimum rating"
      description: "GMB rating must be strictly greater than this value to qualify (PDF: 'Rating above 3.0')."
      default: 3.0
      group: "thresholds"
    debounce_seconds:
      type: integer
      label: "Name-debounce seconds"
      description: "Wait this long after the last lead message before searching Places. Lets leads send their business name across multiple short messages."
      default: 4
      group: "thresholds"
    onboarding_timeout_seconds:
      type: integer
      label: "Onboarding timeout (seconds)"
      description: "If the agent's browser-onboarding step doesn't complete within this window, the lead receives onboarding_error and the conversation ends. Also acts as the queue-depth cap — leads waiting for a slot longer than this get the polite fallback."
      default: 120
      group: "thresholds"
    onboarding_slot_retry_seconds:
      type: integer
      label: "Browser-slot retry cadence (seconds)"
      description: "How often a queued lead retries to claim the browser-onboarding slot. The shared slot serializes the JM Bubble.io form-fill so concurrent leads don't clobber each other's pages."
      default: 5
      group: "thresholds"
    onboarding_slot_lease_seconds:
      type: integer
      label: "Browser-slot lease (seconds)"
      description: "How long a claimed browser-onboarding slot is held before it auto-expires (covers worker crashes mid-onboarding). Should be greater than onboarding_timeout_seconds so the timeout fallback always fires before the lease lapses."
      default: 200
      group: "thresholds"
    re_engage_after_seconds:
      type: integer
      label: "Re-engage cooldown (seconds)"
      description: "After a lead's conversation ends (Calendly sent, polite fallback sent, timeout fired), how long to wait before treating their next message as a brand-new conversation. Within this window, the lead receives a single polite acknowledgement (`completion_followup_ack`) and subsequent messages stay silent — prevents the wizard from restarting mid-cooldown when the lead is just following up on the previous failure."
      default: 600
      group: "thresholds"
    idle_ping_seconds:
      type: integer
      label: "Idle-ping delay (seconds)"
      description: "While collecting full_name / email / phone, if the lead goes silent for this long, send the `idle_ping_template` once. One ping per state — never nags. Set to 0 to disable."
      default: 180
      group: "thresholds"
    preparing_nudge_seconds:
      type: integer
      label: "\"Almost done\" nudge delay (seconds)"
      description: "If the browser playback still hasn't completed this many seconds after phone collection, send `still_preparing_template` so the lead doesn't feel abandoned. Set to 0 to disable. Should be smaller than `onboarding_timeout_seconds` so the nudge fires before the failure fallback."
      default: 30
      group: "thresholds"
    welcome_template:
      type: textarea
      label: "Welcome message"
      description: "Sent on the trigger keyword (MAPS) or when a brand-new conversation starts. {bot_name} interpolates."
      default: "Hey there! I'm {bot_name} 👋 Give me your business name. Going to look you up to see if we can help"
      group: "messages"
    confirm_one:
      type: textarea
      label: "Confirm GMB (single result)"
      description: "Asked when Places returns exactly one match."
      default: "Is this your business?"
      group: "messages"
    ask_address:
      type: textarea
      label: "Ask for address (multi-result disambiguation)"
      default: "Sorry! Couldn't find your profile. What's your business address?"
      group: "messages"
    not_found:
      type: textarea
      label: "GMB not found"
      default: "Hmm, I wasn't able to find that listing on Google. Could you double-check the name? It should appear exactly as it does when you search your business on Google Maps."
      group: "messages"
    disqual_hours:
      type: textarea
      label: "Disqualified — no hours"
      default: "Looks like your Google Business Profile doesn't meet all of our requirements. Please add business hours to your profile and try again."
      group: "messages"
    disqual_website:
      type: textarea
      label: "Disqualified — no website"
      default: "Looks like your Google Business Profile doesn't have a website on file. Please add a URL to the 'Website' field of your Google Business Profile (not just a social link or a description) and try again."
      group: "messages"
    disqual_reviews:
      type: textarea
      label: "Disqualified — too few reviews"
      description: "{min_reviews} interpolates from the threshold above."
      default: "Looks like your Google Business Profile doesn't meet all of our requirements. We need to see at least {min_reviews} reviews on your profile."
      group: "messages"
    disqual_rating:
      type: textarea
      label: "Disqualified — rating too low"
      description: "{min_rating} interpolates from the threshold above."
      default: "Looks like your Google Business Profile doesn't meet all of our requirements. We need to see at least a {min_rating} or higher rating on your Google Business Profile."
      group: "messages"
    ask_full_name:
      type: string
      label: "Ask full name"
      default: "Whats your full name?"
      group: "messages"
    ask_email:
      type: textarea
      label: "Ask email"
      default: "Perfect. I'll create your dashboard now. What's the best email for your login?"
      group: "messages"
    ask_phone:
      type: string
      label: "Ask phone"
      default: "And what phone number can I text your login details to?"
      group: "messages"
    calendly_book_template:
      type: textarea
      label: "Onboarding-complete book template"
      description: "{calendly_url} interpolates."
      default: "Awesome! Your free trial of Jumper Local has been initiated. You should see improved rankings in less than a week. The last step is to schedule with a specialist to go over your results. Choose a time that works best for you here: {calendly_url}"
      group: "messages"
    active_account_template:
      type: textarea
      label: "Active customer message"
      description: "Shown when Xano confirms an active subscription. {support_email} interpolates."
      default: "Looks like you already have active account with us! Please login https://local.jumpermedia.co/ or contact customer support at {support_email}"
      group: "messages"
    returning_template:
      type: textarea
      label: "Returning customer message"
      description: "Shown when Xano confirms a lapsed subscription. {calendly_url} or {returning_calendly_url} (if set) interpolates."
      default: "Welcome back! Please schedule a call with one of our representative to reactive your account {calendly_url}"
      group: "messages"
    onboarding_error:
      type: textarea
      label: "Onboarding error / fallback"
      description: "Shown when the agent's browser handoff actually FAILED (form submission rejected by Bubble, network error, timeout before submit). Distinct from `onboarding_unverified_optimistic` — that one fires when the playback succeeded but Xano sync hasn't completed yet."
      default: "Got it. I've logged your details and the team will reach out shortly to finish setting up your dashboard."
      group: "messages"
    onboarding_unverified_optimistic:
      type: textarea
      label: "Onboarding unverified (playback succeeded, Xano sync pending)"
      description: "Sent when the browser playback successfully submitted the form to Bubble, but the post-submit Xano `get_gmb` lookup didn't confirm the record within the verify window (typically because Bubble→Xano webhook sync takes longer than the window). The lead's account IS being created — sending the Calendly link gives them a clear path forward without misleading them into thinking it failed. `{calendly_url}` interpolates. Live prod 2026-05-19: Wallace Construction Group LLC / Murphys PubHouse / Bridgeview Pet Wellness Clinic all confirmed in Xano shortly AFTER the bot had already sent the misleading 'team will call you' onboarding_error template."
      default: "Awesome! Your free trial of Jumper Local is being set up. Schedule a quick call with our team here to go over your results: {calendly_url}"
      group: "messages"
    onboarding_queued_notice:
      type: textarea
      label: "Queued-for-onboarding one-shot notice"
      description: "Sent ONCE when a lead arrives at the onboarding step but another lead is currently using the shared browser slot. Subsequent retries while waiting are silent (typing dots cover the wait)."
      default: "Just running a few checks. Back in a sec 👀"
      group: "messages"
    self_company_response:
      type: textarea
      label: "Self-company response (rule #12)"
      description: "Shown when the lead types your own company name as their business."
      default: "Hey thats us, whats YOUR business name? :)"
      group: "messages"
    ask_business_name_when_address:
      type: textarea
      label: "Address-pasted-as-name re-prompt"
      description: "Sent when the lead pastes a street address into the business-name slot. Catches inputs like '11689 Olio Rd Geist, McCordsville, IN 46037' before they hit Places (which would otherwise return a literal-address entity that then fails qualification with a misleading no-business-hours message)."
      default: "Looks like that's an address! What's the name of your business?"
      group: "messages"
    ask_business_name:
      type: textarea
      label: "Generic 'what's your business?' re-prompt"
      description: "Sent in awaiting_name when the lead's input clearly isn't a business name and no classifier hint can route the turn elsewhere — gibberish, hostile chitchat, acknowledgements, off-topic chatter. Distinct from the address-specific re-prompt above because the lead never mentioned an address; using 'at that address' phrasing here is confusing."
      default: "Could you share your business name?"
      group: "messages"
    disqual_recheck_still_failing:
      type: textarea
      label: "Recheck still failing — softer protest reply"
      description: "Sent from the 2nd disqualified-state recheck onward when the GMB still doesn't qualify on the same reason. Replaces the canned per-reason template (which would otherwise repeat verbatim and feel like the bot is gaslighting the lead). `{what_is_missing}` interpolates to a concrete description of the unmet requirement. Includes a hint about the 'verified' override path."
      default: "Hmm, Google is still showing the same issue on your profile ({what_is_missing}). Sometimes it takes a few minutes for changes to propagate — could you double-check the update is saved on your Google Business Profile and try again in a moment? If you've verified it's there, reply 'verified' and our team will double-check on their end."
      group: "messages"
    disqual_override_acknowledged:
      type: textarea
      label: "Manual override acknowledged"
      description: "Fired when the lead types 'verified' / 'override' / 'i confirmed' / 'i added it' after 2+ recheck attempts. Bypasses the canned disqual loop, sets `manual_review_requested=true` on the lead's state for anna+team to spot-check in the dashboard, and resumes onboarding at awaiting_full_name. Trade-off: a small number of unqualified leads slip into onboarding, but the alternative (looping forever) is worse UX especially when the Places API genuinely has stale data."
      default: "Got it — I'll flag this for our team to verify on their side. Let's keep going. What's your full name?"
      group: "messages"
    confirm_closest_listing:
      type: textarea
      label: "Confirm — closest listing when address snap drifted"
      description: "Sent when the lead pasted an address but nearby_search snapped to a business at a noticeably-different street number (e.g. lead typed '9001 E 116th St' but Google's nearest business is at '8997 E 116th St'). The default confirm template would silently substitute the snapped address, which feels like the bot lied. This template is transparent about the snap. `{name}` and `{address}` interpolate."
      default: "I'm not seeing a business directly at that address. The closest one is:\n{name}\n{address}\n\nIs this your business? If not, just share the actual name and I'll search again."
      group: "messages"
    confirm_one_with_alternatives:
      type: textarea
      label: "Confirm — strip mall with multiple businesses at same address"
      description: "Sent when nearby_search returns 2+ distinct businesses sharing the same street number (strip mall scenario). Surfaces the closest candidate AND lists 3-4 sibling business names so the lead can pick by typing the right name. `{name}` / `{address}` interpolate the top candidate; `{alternatives}` is a comma-separated list of sibling names. Live prod 2026-05-19: lead pasted '11630 Olio Rd' (Fishers IN — a 6+ business strip mall) and bot offered only Olio Nail & Spa silently, hiding Vita Dental / Jimmy John's / MOCHINUT / Papa Murphy's at the same address."
      default: "Is this your business?\n{name}\n{address}\n\nI also see these at the same address: {alternatives}. If yours is one of those, just reply with the name."
      group: "messages"
    ask_email_again_test_address:
      type: textarea
      label: "Reject placeholder email — re-ask"
      description: "Sent when the lead's email is an obvious placeholder (test@test.com, foo@bar.com, anything@example.com, test+test@email.com, etc.). Catches bad data BEFORE the browser playback into Bubble would attempt to register it and fail at form-validation. Real leads with unusual but valid emails (gmail with plus-tags, custom domains, etc.) are not affected."
      default: "That looks like a placeholder email — could you share the actual email you'll use to log into your dashboard?"
      group: "messages"
    ask_phone_again_test_number:
      type: textarea
      label: "Reject placeholder phone — re-ask"
      description: "Sent when the phone number is an obvious placeholder: all-same-digit (5555555555), strictly sequential (1234567890), or a US 555-01XX fictional number. Real phone numbers like 3175551234 (where 555 is in the local part not the exchange position) pass through."
      default: "That looks like a placeholder phone number — could you share the real number where you'd like to receive your login details?"
      group: "messages"
    completion_followup_ack:
      type: textarea
      label: "Post-completion acknowledgement"
      description: "Sent ONCE when a lead replies within `re_engage_after_seconds` of the conversation ending (success, failure, or timeout). Replaces the wizard-restart that used to happen on every post-completion message — keeps the lead from being re-prompted for business name / name / email / phone all over again. Subsequent inbounds during the cooldown window stay silent."
      default: "Thanks! The team has your info and will be in touch shortly. Hang tight 🙌"
      group: "messages"
    idle_ping_template:
      type: textarea
      label: "Idle-ping (\"are you still there?\")"
      description: "Sent ONCE per data-collection state if the lead goes silent for `idle_ping_seconds` while the bot is waiting on full_name / email / phone."
      default: "Hey, are you still there? 👋 Just need a couple more details to finish setting up your account."
      group: "messages"
    preparing_dashboard_template:
      type: textarea
      label: "Preparing-dashboard preamble (sent right after phone)"
      description: "Sent immediately after the lead provides their phone — covers the ~60s window during which the browser playback + Xano verify run silently, so the lead has acknowledgement they're being processed. Customer-facing — DO NOT mention browser / automation / playback. Frame as the team taking care of them."
      default: "Awesome! Our team is onboarding you now. Hang tight, you'll get a link in a sec 🙌"
      group: "messages"
    still_preparing_template:
      type: textarea
      label: "\"Almost done\" nudge"
      description: "Sent ~30s after phone collection (configurable via `preparing_nudge_seconds`) if the browser playback hasn't completed yet — keeps the lead engaged. Same framing rule: no mention of browser / automation."
      default: "Almost done. Just finishing up your account, one sec 👀"
      group: "messages"
tool_schema:
  name: oya_messenger_sdr
  description: "Drive a single Messenger turn for the SDR template. Pass the lead's exact message text. Returns a JSON object with `reply` (the EXACT text to send to the lead, verbatim) and `step` (current state, for logging only). If `reply` is empty/null, send NOTHING — the skill is debouncing or the conversation is complete. If the response carries `next_action.type='browser_onboarding'`, do NOT message the lead — call the `browser` skill to fill the onboarding form per `next_action.fields`, then call this tool again with `text='onboarding_complete'` (or `'onboarding_failed: <reason>'` on error). The platform injects the lead's identity automatically — you only pass `text`."
  parameters:
    type: object
    properties:
      text:
        type: string
        description: "The lead's message text exactly as received. Pass 'onboarding_complete' or 'onboarding_failed: <reason>' to resume after a browser handoff."
    required: [text]
---
# Oya Messenger SDR (template)

A procedural state machine that runs an SDR onboarding flow on Facebook Messenger — from the trigger keyword through GMB lookup, qualification, lead-info collection, and onboarding handoff. The skill enforces verbatim messages from a per-agent message table; the agent's LLM is told to relay each `reply` field word-for-word.

## How the agent should use this skill

1. On every Messenger DM received, call `oya_messenger_sdr` exactly once with:
   - `text` = the lead's message verbatim
   - `sender_id` = the chat_id from the Facebook Messenger context line
2. Send the value of `reply` to the lead **EXACTLY AS RETURNED**. No paraphrasing. No additions. No summary. No follow-up message.
3. If `reply` is empty/null, send **nothing**. The skill is either debouncing (waiting for the lead to finish typing across multiple messages) or the conversation has completed.
4. If `next_action.type == 'browser_onboarding'`, **do not message the lead.** Call the `browser` skill to navigate to `next_action.onboarding_url`, fill the form per `next_action.fields`, submit, and verify success. Then call this tool again with `text='onboarding_complete'` (or `'onboarding_failed: <reason>'` on any error). The tool's next reply is what the lead sees.

## What the skill handles internally

- Greeting on the trigger keyword.
- Multi-message debounce when the lead types their business name across several short messages.
- Google Places API lookup (single result, multiple results → ask address, no result → ask to double-check).
- Silent qualification check: hours present, website set, ≥`min_reviews` reviews, rating > `min_rating`.
- Returning / current-customer detection via Xano (when `enable_xano_check` is on and `XANO_*` credentials are configured).
- Self-company rule (when a lead types your own company name).
- Disqualification re-check on a returning lead's next message.
- Onboarding-timeout fallback so the lead never sits silent if the agent's browser step hangs.

## Required configuration

- `GOOGLE_PLACES_API_KEY` (credential, required) — see field hint.
- All `messages` group fields — defaults match the original Jumper Media script; customers should review and customize.
- `XANO_API_GROUP_BASE_URL` + `XANO_AUTH_TOKEN` (credentials, optional) — only needed when `enable_xano_check` is on.

## Required gateway

- A Facebook Messenger gateway with `trigger_keywords: "MAPS"` (or whatever your trigger word is) so the agent only opens a new conversation on that keyword. Follow-ups inside an opened conversation pass through automatically.
