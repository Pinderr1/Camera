# Sentinel AI Guide

The **Guide** tab is a conversational front end to Sentinel's existing installation, diagnostics, configuration and history tools. A customer describes what they want ("I already have eight Hikvision cameras", "watch my driveway at night", "why did Sentinel alert me last night?"), and the guide answers from live system data, points to the right screen, and proposes changes that the customer confirms.

It does not replace the installation wizard, troubleshooter, zone editor or rule builder. It runs them, reads their results, and opens them when the customer wants more control.

## Two ways of answering

| Mode | When | What leaves the hub |
| --- | --- | --- |
| **Built-in guide** (default) | No AI provider is configured, the provider fails or times out, or it declines a request | Nothing |
| **AI assistant** (Claude, by Anthropic) | `assistant.provider: "anthropic"` and an API key on the hub | The customer's messages, a short status summary, and tool results with addresses replaced by placeholders (below) |

Both modes use the same tools, so every fact comes from Sentinel itself. The built-in guide recognises the common requests with keyword rules: setup and the four installation paths, existing cameras and recorders, connection problems (with one next step and Retest), new cameras (PoE versus Wi-Fi, placement from the property plan), drones, security rules from plain language, "stop notifying me about the sidewalk", "is my house protected?", "why did it alert?", "what happened while I was away?", storage, unusual activity, and improvements. The UI says which mode answered each message.

## What the assistant can do

**Read live data** (no confirmation needed): installation progress and readiness, registered devices and their connection levels, camera health, hardware compatibility profiles, installation guidance, the property plan's approximate coverage and equipment estimate, camera suggestions, this hub's network settings, diagnosis of one private address (ping, camera ports, routing, discovery), a connection test of a configured camera with its stored credentials, rules and zones, rule validation and the rule test (simulated walk on a throwaway database), incidents and their explanations (observed versus inferred), activity insights, system health and storage, drone status, and notification settings.

**Show things** (no side effects): buttons to existing screens (installation steps, zone editor, troubleshooter, camera wizard, incident timeline, System, Drone, the printable plan), quick replies, live camera previews (displayed on the hub only), and the **secure sign-in form**.

**Propose changes** (applied only after Confirm): installation type and step marks, a network search, saving a rule, turning a rule on or off, notification channels, registering or removing a device, and a drone inspection *request*.

Every proposal card is written by Sentinel, not the model: its title, summary (for rules, the rule builder's own plain-language description) and effects come from the backend. Confirm runs the existing validated backend call (`SiteConfigManager`, the installation manager, the drone manager), records it in the audit log, and the card shows what the backend returned. Proposals expire after 30 minutes and apply at most once.

Rules follow the rule builder's policy: a rule can only be proposed for turning on after that exact version passed the rule test in the same conversation.

### What it cannot do

Run commands, read files, change router, firewall, computer or camera network settings, change camera IP addresses, update firmware, reveal stored secrets, approve or command a drone flight, or search a network without the customer's confirmation. Diagnosis only probes private addresses. None of these exist as tools.

## Camera installation flow (existing cameras)

1. "I already have Hikvision cameras" → the guide asks permission to search the network (a confirmation card) and offers to check an address the customer reads from the recorder's own screen.
2. Search results list each device, its brand and whether it is on this hub's network or another one.
3. Diagnosis explains the evidence and gives **one** next step. Findings keep their status: a confirmed fault is called confirmed; a likely but unproven cause is labelled "not confirmed". Our Hikvision regression (PC 10.0.0.57/24, recorder 192.168.1.84 with gateway 192.168.1.254) gives "Confirmed: cannot be reached", "Confirmed cause: set up for a different network" when the recorder answered SADP, or "Most likely cause (not confirmed)" when discovery replies are blocked, and the step to switch the recorder to DHCP on its own screen.
4. **Retest** reruns the same checks and says whether anything changed.
5. When the recorder answers, the guide shows the **secure sign-in form**. The password goes straight to the existing channel search (`POST /api/v1/onboarding/channels`) and the credential store; it never goes to the assistant endpoints or an AI service, and the field is cleared after use. A refused sign-in is reported from Sentinel's own audit record, with the lockout warning.
6. The customer ticks the channels to add; they are added through `POST /api/v1/config/cameras`, and the guide links to Verify.

## Privacy

- Conversations are stored only on the hub (`<storage>/assistant/<id>.json`) and can be deleted from the Guide tab (`DELETE /api/v1/assistant/conversations/{id}`).
- Passwords, keys, tokens and credential-bearing URLs typed into the chat are removed before the message is stored, logged or sent, and the customer is told to use the secure form.
- With an external provider, **network addresses are replaced by placeholders** (`[ip-1]`, `[net-1]`) in everything sent: the customer's text, the status summary and tool results. The provider's answer is turned back into real addresses only for display on the hub. Tools accept placeholders. `assistant.share_network_details: true` turns this off.
- Never sent: video, pictures, recordings, passwords, API keys, the property name or location.
- Device names, camera names and discovered metadata are treated as untrusted data: printable characters only, bounded length, and the system prompt tells the model to ignore instructions inside them. More importantly, the model can only *propose* changes; it cannot apply them.
- The assistant uses the API's existing authentication. Audit entries record what happened (source, proposal kinds, confirmations), never message content.

## Configuring the AI provider

1. Install the SDK (already in `requirements.txt`): `pip install anthropic`.
2. Provide the key on the hub, never in the browser or the config file: set `ANTHROPIC_API_KEY` in the service environment, or paste it into **Guide → Assistant settings → API key** (stored in the DPAPI-encrypted credential store; write-only).
3. Choose "Claude by Anthropic" in Assistant settings (or set `"assistant": {"provider": "anthropic"}` in the site config). The person is asked to confirm what will be shared.

Settings (`assistant` config section, validated):

| Key | Default | Meaning |
| --- | --- | --- |
| `provider` | `none` | `none` (built-in guide only) or `anthropic` |
| `model` | `claude-opus-5-5` | Model id |
| `effort` | `low` | `low`, `medium` or `high`; chat rarely needs more than low |
| `share_network_details` | `false` | Send real private addresses to the provider |
| `max_tool_rounds` | `8` | Tool calls per customer message before the guide stops and asks |
| `timeout_s` | `60` | Per request |

API keys in the config are rejected by validation.

### Cost and reliability

- One provider request per tool round, at most `max_tool_rounds` per message; the stable system prompt and tool list are cached (`cache_control`), and the per-message status summary is a few lines.
- History is append-only, so cached prefixes and thinking blocks stay valid across turns.
- Requests use the server-side refusal fallback (`fallbacks: "default"`); a refusal that still comes back, a timeout, a rate limit, a bad key or a network error falls back to the built-in guide with a visible note. A failed attempt leaves no partial history.
- Malformed tool calls (unknown tool, unknown or missing arguments, wrong types) return an error result to the model; nothing raises.

## API

See [api.md](./api.md#assistant).

## Design for voice later

Messages are plain text in and plain text plus structured cards out, so a voice front end can map speech to `POST .../messages` and read the `text`, presenting cards as spoken choices. No voice infrastructure is included.

## Status

| Capability | Status |
| --- | --- |
| Built-in guide, all flows above | Built; backend and Edge tests on simulated cameras, drone, sensors and the simulated Hikvision network |
| AI tool loop, confirmation gating, prompt-injection containment, address placeholders, provider failure fallback | Built; tested with a scripted provider |
| Anthropic provider (request shape, tool calls, refusal fallback handling, errors) | Built; tested against a local fake Messages API. **Not yet exercised against the live Claude API** |
| Real cameras, recorders and networks | **Not tested**: the same troubleshooter and onboarding code as the Install tab, which has not met real hardware yet |
| Real sensors, drone video | Not available: the guide says so |
