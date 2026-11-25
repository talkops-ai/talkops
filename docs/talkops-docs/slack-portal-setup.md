# Slack App Portal & Server Setup Guide for TalkOps

> Connect TalkOps to your Slack workspace for two-way `@TalkOps` troubleshooting threads, Human-in-the-Loop (HITL) approval cards, two-stage webhook incident war-rooms, and one-click `Confirm RCA` / `Dispute Hypothesis` feedback.

Native Slack **Incoming Webhooks** are strictly one-way (`Agent → Slack`)—they can post an alert, but engineers cannot reply in the thread, click approval or RCA feedback buttons, run `/talkops` commands, or react with `👍` / `👎`. By creating a **Slack App** and pointing its Events API, Interactivity, and Slash Command URLs at your TalkOps server (`talkops serve`), you unlock full two-way ChatOps across channels, threads, and direct messages.

---

## 1. Prerequisites & Endpoints Checklist

Before opening the Slack App dashboard, have a public HTTPS URL pointing to your TalkOps server (for local testing, `cloudflared tunnel --url http://127.0.0.1:2024` or `ngrok http 2024` works cleanly):

| Slack App Feature | Request URL | What Flows Through It |
| :--- | :--- | :--- |
| **Event Subscriptions** | `https://<your-talkops-domain>/channels/slack/events` | `@TalkOps` mentions, thread replies, 1:1 DMs, `reaction_added` (`👍` / `👎` approvals & RCA feedback), and Slack's `url_verification` challenge. |
| **Interactivity & Shortcuts** | `https://<your-talkops-domain>/channels/slack/interactions` | **Approve / Reject** HITL buttons and **Confirm RCA / Dispute Hypothesis** buttons (with ephemeral `response_url` acks and in-place `chat.update` card locking). |
| **Slash Commands** *(Optional)* | `https://<your-talkops-domain>/channels/slack/commands` | `/talkops status`, `/talkops help`, `/talkops mode`, `/talkops model`, `/talkops pair`, and `/talkops stop`. |

---

## 2. Step-by-Step Slack App Portal Configuration

Open **[https://api.slack.com/apps](https://api.slack.com/apps)**, click **Create New App** → **From scratch**, name it (for example, `TalkOps SRE`), and select your Slack workspace.

### Step 2.1: Copy the Signing Secret (`Basic Information`)

1. In the left sidebar under **Settings**, click **Basic Information**.
2. Scroll to **App Credentials**, reveal **Signing Secret**, and save it as `TALKOPS_SLACK_SIGNING_SECRET`.
   *(TalkOps verifies every inbound Slack request with HMAC-SHA256 `X-Slack-Signature` and a 5-minute replay-protection window.)*

---

### Step 2.2: Add Least-Privilege Bot Token Scopes (`OAuth & Permissions`)

1. In the left sidebar under **Features**, click **OAuth & Permissions**.
2. Under **Scopes → Bot Token Scopes**, add this exact least-privilege set:

| Bot Token Scope | Why TalkOps Needs It |
| :--- | :--- |
| **`app_mentions:read`** | Receive `@TalkOps` mentions in public and private channels. |
| **`chat:write`** | Post incident cards, thread replies, streaming edits, ephemeral acks, and in-place button locking (`chat.update`). |
| **`channels:history`** | Hydrate prior thread messages in public channels on restart and detect bot participation for unmentioned follow-ups. |
| **`groups:history`** | Same thread history & follow-up detection inside private channels. |
| **`im:history`** | Support 1:1 direct message conversations with TalkOps. |
| **`reactions:read`** | Receive `👍` / `👎` (`+1` / `-1`) reactions for HITL tool approvals and RCA confirmation/dispute feedback. |
| **`reactions:write`** | Add `👀` while investigating a human chat message and swap to `✅` when complete. |
| **`commands`** | Enable `/talkops` slash commands *(optional if you only use mentions and threads)*. |
| **`files:read`** | Download screenshots, logs, or configs users attach in Slack messages. |
| **`files:write`** | Auto-upload downloadable `.md` postmortem files when a webhook RCA exceeds Slack's inline block budget. |

3. Scroll up and click **Install to Workspace** → **Allow**.
4. Copy the **Bot User OAuth Token** (`xoxb-...`) and save it as `TALKOPS_SLACK_BOT_TOKEN`.

> [!IMPORTANT]
> Whenever you add a new Bot Token Scope later (such as `files:write` or `reactions:read`), Slack displays a yellow banner at the top of the page—you **must** click **Reinstall to Workspace** for the new scope to take effect.

---

### Step 2.3: Enable Event Subscriptions (`Event Subscriptions`)

Start `talkops serve` (with `TALKOPS_SLACK_BOT_TOKEN` and `TALKOPS_SLACK_SIGNING_SECRET` exported) **first**, so Slack's verification check succeeds immediately:

1. In the left sidebar under **Features**, click **Event Subscriptions** and toggle **Enable Events** to **On**.
2. Set **Request URL** to:
   ```text
   https://<your-talkops-domain>/channels/slack/events
   ```
   TalkOps automatically validates the signature and answers Slack's `url_verification` challenge with a green **Verified** checkmark.
3. Expand **Subscribe to bot events** and add:
   - `app_mention`
   - `message.channels`
   - `message.groups`
   - `message.im`
   - `reaction_added` *(required for `👍` / `👎` reactions on both HITL approval cards and RCA cards)*
4. Click **Save Changes**.

---

### Step 2.4: Enable Interactivity (`Interactivity & Shortcuts`)

> [!IMPORTANT]
> Without this step, link buttons like **View Alert** still open in your browser, but clicking **Confirm RCA**, **Dispute Hypothesis**, or **Approve / Reject** will do nothing because Slack has nowhere to send button clicks.

1. In the left sidebar under **Features**, click **Interactivity & Shortcuts**.
2. Toggle **Interactivity** to **On**.
3. Set **Request URL** to:
   ```text
   https://<your-talkops-domain>/channels/slack/interactions
   ```
4. Click **Save Changes**. *(No workspace reinstall is needed for Interactivity URL changes.)*

---

### Step 2.5: Enable Slash Commands & Direct Messages *(Recommended)*

1. **Slash Commands** *(optional)*:
   - Under **Features → Slash Commands**, click **Create New Command**.
   - **Command**: `/talkops`
   - **Request URL**: `https://<your-talkops-domain>/channels/slack/commands`
   - **Short Description**: `Control TalkOps status, model, approval mode, and pairing`
   - Click **Save**.
2. **1:1 Direct Messages (`App Home`)**:
   - Under **Features → App Home**, scroll to **Show Tabs**, enable **Messages Tab**, and check **Allow users to send Slash commands and messages from the messages tab**.

---

### Step 2.6: Invite the Bot & Copy Slack IDs

1. In Slack, open your target ops / war-room channel (e.g., `#sre-alerts`) and run:
   ```text
   /invite @TalkOps
   ```
2. Copy the IDs you need for allowlists and alert routing:
   - **Channel ID (`C...`)**: Click the channel name at the top → scroll to the bottom of the **About** tab → copy the **Channel ID** (`TALKOPS_CHANNELS_SLACK_ALLOWLIST_CHATS` and `TALKOPS_WEBHOOK_NOTIFY_CHANNELS`).
   - **Your Operator User ID (`U...`)**: Click your own Slack profile → click **`⋮`** → **Copy member ID** (`TALKOPS_CHANNELS_SLACK_OPERATOR_ID`).

---

## 3. Configure TalkOps Server (`talkops serve`)

### Option A: Zero-Config Environment Variables (`.talkops/.env`)

Add to `.talkops/.env` (or export in your shell):

```bash
# Required Slack App credentials
export TALKOPS_SLACK_BOT_TOKEN="xoxb-..."
export TALKOPS_SLACK_SIGNING_SECRET="<your_slack_signing_secret>"

# Security & allowlist (recommended for production)
export TALKOPS_CHANNELS_SLACK_EXPOSURE_MODE="allowlist"
export TALKOPS_CHANNELS_SLACK_OPERATOR_ID="U0123456789"
export TALKOPS_CHANNELS_SLACK_ALLOWLIST_CHATS="C0123456789"

# Optional: route Alertmanager/PagerDuty/Datadog/SNS webhook alerts to a Slack channel
export TALKOPS_WEBHOOK_NOTIFY_CHANNELS="slack:C0123456789"
```

### Option B: Declarative `.talkops/addons.json` *(Team / Kubernetes Deployments)*

> **Why `.talkops/addons.json` instead of `~/.talkops/config.toml`?**
> `~/.talkops/config.toml` stores personal CLI/TUI preferences, whereas `.talkops/addons.json` holds version-controlled server channel configs with `${ENV_VAR}` secret pointers (see [Server Addons & `addons.json`](server-addons.md#why-addonsjson-is-separate-from-configtoml)).

```json
{
  "addons": {
    "channels": {
      "enabled": true,
      "verbosity": 2,
      "pairing_enabled": true,
      "default_approval_mode": "smart",
      "multitask_strategy": "interrupt",
      "slack": {
        "bot_token": "${TALKOPS_SLACK_BOT_TOKEN}",
        "signing_secret": "${TALKOPS_SLACK_SIGNING_SECRET}",
        "verify_signatures": true,
        "exposure_mode": "allowlist",
        "operator_id": "${TALKOPS_CHANNELS_SLACK_OPERATOR_ID}",
        "allowlist_chats": "${TALKOPS_CHANNELS_SLACK_ALLOWLIST_CHATS}"
      }
    }
  }
}
```

---

## 4. Start & Smoke-Test End-to-End

1. **Start TalkOps Server:**
   ```bash
   talkops serve --profile sre --host 0.0.0.0 --port 2024
   ```
   *(You can also run default `talkops serve`—RCA feedback still persists automatically to `OperationalStore`.)*
2. **Verify Slack adapter status:**
   ```bash
   curl -s http://127.0.0.1:2024/channels/status | python3 -m json.tool
   ```
   Confirm `"channel": "slack"`, `"configured": true`, `"has_bot_token": true`, and `"has_signing_secret": true`.
3. **Smoke-Test All Flows in Slack:**
   - **Channel Mention & Thread Follow-Up:** In your ops channel, send `@TalkOps Check checkout-api pod health`. Verify TalkOps reacts with `👀` while running, swaps to `✅` when done, and replies in a thread. Reply in that same thread **without** `@TalkOps`—TalkOps continues the same conversation.
   - **Slash Command:** Run `/talkops status` to verify ephemeral command responses.
   - **HITL Tool Approval:** Trigger a shell/write action in `--approval-mode manual` — approve via the **Approve** button, a `👍` reaction on the approval card, or replying `approve` in the thread.
   - **Two-Stage Webhook Incident & RCA Card:** Trigger an alert (e.g., `POST /webhooks/alertmanager`) — verify the parent card posts with a Red accent bar (`Status: Investigating`), then updates in place to Green (`RCA Complete (in thread)` + `Triage Time` + `Root Cause Takeaway`) while the full RCA (plus an automatic `.md` file upload on long reports) lands in the thread.
   - **RCA Feedback (`Confirm RCA` / `Dispute Hypothesis` & `👍`/`👎`):** Click **Confirm RCA** or **Dispute Hypothesis** (or react with `👍` / `👎` directly on the RCA message). Verify you receive an ephemeral acknowledgment (`Feedback recorded: ...`), the active buttons disappear and lock to `*RCA Confirmed* by @you` (while **View Alert** stays clickable), a second click returns `Feedback already recorded`, and the verdict persists in `OperationalStore` (`talkops_autoqa_scores`).
   - **DM Pairing:** Have a non-allowlisted teammate DM the bot → they receive an 8-character pairing code (`ABCD-EFGH`); approve it as operator via `/talkops pair approve ABCD-EFGH`.

---

## 5. Complete Slack Environment Variable Reference

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `TALKOPS_SLACK_BOT_TOKEN` *(or `TALKOPS_CHANNELS_SLACK_BOT_TOKEN`, `SLACK_BOT_TOKEN`)* | *(required)* | Bot User OAuth Token (`xoxb-...`) from **OAuth & Permissions**. |
| `TALKOPS_SLACK_SIGNING_SECRET` *(or `TALKOPS_CHANNELS_SLACK_SIGNING_SECRET`, `SLACK_SIGNING_SECRET`)* | *(required)* | Signing Secret from **Basic Information** for HMAC-SHA256 verification. |
| `TALKOPS_SLACK_BOT_USER_ID` *(or `TALKOPS_CHANNELS_SLACK_BOT_USER_ID`, `SLACK_BOT_USER_ID`)* | *(auto-resolved via `auth.test`)* | Optional override for the bot's Slack user ID (`U...`) used for echo-loop prevention. |
| `TALKOPS_CHANNELS_SLACK_API_BASE` | `"https://slack.com/api"` | Override for Slack Enterprise Grid proxies or local mock servers. |
| `TALKOPS_CHANNELS_SLACK_VERIFY_SIGNATURES` | `"1"` | Keep `"1"` in production (enforces `X-Slack-Signature` HMAC + timestamp check). |
| `TALKOPS_CHANNELS_SLACK_EXPOSURE_MODE` | `"allowlist"` | `"self"`, `"allowlist"`, or `"open"` (`open` requires `TALKOPS_CHANNELS_SLACK_OPEN_ACK=1`). |
| `TALKOPS_CHANNELS_SLACK_OPERATOR_ID` | `""` | Operator Slack member ID (`U...`) permitted to run `/pair` and approve HITL cards. |
| `TALKOPS_CHANNELS_SLACK_ALLOWLIST_USERS` | `""` | Comma-separated Slack member IDs allowed in `allowlist` mode. |
| `TALKOPS_CHANNELS_SLACK_ALLOWLIST_CHATS` | `""` | Comma-separated Slack channel IDs (`C...` / `G...`; child threads inherit parent channel admission). |
| `TALKOPS_CHANNELS_SLACK_REQUIRE_MENTION` | `"1"` | Require `@TalkOps` mention to start new threads in channels (`"0"` to respond to all channel messages). |

---

## 6. Troubleshooting Quick Reference

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| Clicking **Confirm RCA**, **Dispute Hypothesis**, or **Approve** does nothing (only **View Alert** works) | **Interactivity & Shortcuts** is OFF or its Request URL is missing/wrong. | In the Slack App portal, turn **Interactivity** ON and set Request URL to `https://<your-host>/channels/slack/interactions`, then click **Save Changes**. |
| Reacting with `👍` or `👎` does nothing | `reaction_added` event subscription or `reactions:read` scope is missing, or the RCA message was posted before the server restarted. | Add `reactions:read` (and **Reinstall to Workspace**), subscribe to `reaction_added` under **Event Subscriptions**, and test on a newly posted card. |
| Event Subscriptions URL fails verification | `talkops serve` isn't running yet, tunnel URL isn't reachable, or `TALKOPS_SLACK_SIGNING_SECRET` is mismatched. | Start `talkops serve` first, confirm `TALKOPS_SLACK_SIGNING_SECRET` matches **Basic Information → Signing Secret**, and click **Retry**. |
| Inbound requests return `401 invalid_slack_signature` | Wrong signing secret or clock skew > 5 minutes. | Verify exact Signing Secret and confirm your server clock is NTP-synced. |
| Long RCAs don't attach a `.md` file | Missing `files:write` Bot Token Scope. | Add `files:write` under **OAuth & Permissions** and click **Reinstall to Workspace**. |
| Users cannot type in a 1:1 DM with the bot | **Messages Tab** input is disabled in Slack App Home. | Open **App Home → Show Tabs**, enable **Messages Tab**, and check **Allow users to send Slash commands and messages from the messages tab**. |
| Bot doesn't post in your war-room channel (`not_in_channel`) | Bot hasn't been invited to the channel. | Run `/invite @TalkOps` inside the target Slack channel. |

---

## See Also

- **[Server Addons & ChatOps Hub](server-addons.md)** — Full `channels` addon architecture, multi-channel routing, and server profiles.
- **[Webhooks & Automated Incident Response](webhooks.md)** — Connecting PagerDuty, Alertmanager, Datadog, AWS SNS, ServiceNow, and GitHub alerts.
- **[Discord Developer Portal Setup Guide](discord-portal-setup.md)** · **[Microsoft Teams Portal Setup Guide](teams-portal-setup.md)** — Companion setup guides for Discord and Teams.
