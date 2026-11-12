# outlook-campaigns

Email + LinkedIn outreach sequences, run from your own M365 Outlook, with a local dashboard.

- **Sequences mix channels:** Email, LinkedIn connect, LinkedIn message, View profile. Each step has a wait and an optional condition (`only if connected` / `only if not connected`).
- **Several mailboxes:** sign in as many M365 accounts as you like, and pick the From mailbox per campaign. Mail goes out through Microsoft Graph and lands in that mailbox's Sent Items. Follow-ups reply in the same thread, from the mailbox that started it. Each mailbox has its own daily cap.
- **Tracking (optional):** a small Cloudflare Worker on your own subdomain handles signed open pixels, click redirects and an unsubscribe page. Scanner and prefetch hits are filtered out.
- **Deliverability:** per-mailbox sent, delivered, bounce, open, click, reply and unsubscribe rates, plus SPF/DKIM/DMARC/MX checks for each sending domain.
- **LinkedIn runs as an assisted queue.** When a sequence reaches a LinkedIn step, a task shows up with the text already personalized. You click *Copy & open profile*, paste, send, then *Done*. Nothing touches LinkedIn automatically, so there's no ban risk.
- **Weekly LinkedIn engagement:** while a lead is in a live sequence (invite sent or emailed), the queue gets one *Like + comment* task per person per week for a post from the last 6 months. Claude drafts a specific comment from the post text you paste; you edit and post it yourself. It stops as soon as they reply.
- **Detection:** email replies, bounces and out-of-office replies come from your mailbox. LinkedIn accepts and replies come from LinkedIn's notification emails in the same mailbox. A reply on any channel stops that lead in every campaign.
- **Dashboard:** overview funnel and 14-day activity, per-campaign and per-step email metrics, sequence editor with live preview per lead, lead timeline, CSV/Excel import with column mapping.

It runs on your own Mac or Windows PC only (`127.0.0.1`). The data lives in `data/app.db`. It leaves the machine only as the emails you send, plus, if you enable tracking, anonymous send IDs in pixel and link URLs.

## Setup

### 1. Outlook sign-in: nothing to register

The app signs in through Microsoft's own public app, "Microsoft Graph Command Line Tools" (the one Microsoft's Graph PowerShell uses). You just sign in and accept two permissions: send mail and read mail.

- Sign-ins show under that app's name in your company's Microsoft 365 logs.
- If your company blocks that app or requires admin approval, either ask IT to approve it, or register your own app (*App registrations* → new, *Mobile and desktop* platform with `http://localhost`, public client flows on, delegated `Mail.Send` + `Mail.ReadWrite`) and put its IDs in `.env` as `OC_CLIENT_ID` / `OC_TENANT_ID`.

### 2. Install and run

You need Python 3.11+, Git, and Google Chrome (only for the LinkedIn bot).

**macOS**

```bash
git clone https://github.com/vibhorkumar1209/outlook-campaigns.git && cd outlook-campaigns
cp .env.example .env
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./oc serve                 # http://127.0.0.1:8765
```

**Windows** (PowerShell or Command Prompt). Install [Python](https://www.python.org/downloads/) (tick *Add python.exe to PATH*) and [Git](https://git-scm.com/download/win) first.

```powershell
git clone https://github.com/vibhorkumar1209/outlook-campaigns.git
cd outlook-campaigns
copy .env.example .env
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.\oc serve                 # http://127.0.0.1:8765
```

On Windows every command below is `.\oc …` instead of `./oc …`.

Open **Senders → Connect a mailbox** and sign in. Repeat for each address you'll send from; on the Microsoft page, pick *Use another account*. Or run `./oc login` once per mailbox, adding `--device-code` if browser sign-in is blocked. Tokens are stored encrypted (Keychain on macOS, your Windows login's DPAPI on Windows) and refresh silently. The first mailbox becomes the default sender and the inbox that's read for LinkedIn notifications; both can be changed.

To keep it running in the background and start it at login:

```bash
./oc install               # ./oc uninstall to remove
```

On macOS that's a launchd agent; on Windows, a Task Scheduler task (*OutlookCampaigns*, runs at your logon with no console window). Either way it restarts the dashboard if it stops. The log is `data/dashboard.log`.

### Getting updates

Everyone running from a git clone gets new versions from GitHub:

- **Settings → App updates** checks every 6 hours. *Update now* pulls the new code, installs any new requirements, and restarts the dashboard once it's idle (never in the middle of a send or a LinkedIn action).
- Or from a terminal: `./oc update`.

Your data (`data/`, `.env`, sign-ins) is never part of an update. If you've edited the code yourself, the update only goes through when your edits don't clash with the new ones; otherwise it stops and changes nothing.

To explore first, run `./oc demo`. It seeds a **draft** campaign with example.com leads that never sends.

### 3. LinkedIn notification emails

Detection reads LinkedIn's emails. In LinkedIn → Settings → Communications → Email, keep **invitation accepted** and **new messages** on. Matching is by the lead's full name in the notification. If one is missed, use *Mark connected* or *They replied* on the lead or task.

### 4. Open & click tracking (optional, ~15 min)

Recipients' mail apps load the pixel and follow links over the internet, so this part runs on Cloudflare (free tier). Your dashboard stays local and pulls the events.

1. Put your domain on Cloudflare (free plan), or use a separate domain that's already there. A subdomain like `links.yourcompany.com` makes links match your sending domain, which spam filters prefer.
2. Generate the shared secrets into `.env` and print the deploy steps:
   ```bash
   ./oc tracker-setup --domain links.yourcompany.com
   ```
3. Follow the printed steps: `wrangler login`, create the D1 database, load the secrets, set the route, deploy.
4. Once `https://links.yourcompany.com/health` answers, add `OC_TRACK_URL=https://links.yourcompany.com` to `.env` and restart. **Settings → Open & click tracking** should say *working*.

Tracking is per campaign (Settings tab: opens and clicks separately). If the worker stops answering, emails automatically go out with plain links until it's back. `{{ unsubscribe_url }}` works in any template: with tracking it's a one-click opt-out page; without, a mailto link.

**Read the numbers with care:**
- Opens are estimates. Apple Mail preloads images, inflating opens; Outlook blocks images, hiding them. Hits within 60s of sending, HEAD requests, and known scanner user agents (Safe Links, Mimecast, Proofpoint…) are excluded and counted separately.
- Clicks are more reliable. A click also counts as an open.
- "Delivered" means not hard-bounced. Inbox vs spam placement can't be measured from the sending side.
- Pixels and redirected links can hurt cold-email deliverability. For first touches to strangers, consider clicks-only or tracking off. In the EU, open tracking generally needs consent.

### 5. Comment drafts (optional)

Add `ANTHROPIC_API_KEY=...` to `.env` (from platform.claude.com) and restart. Drafts use Claude Opus 5.5 with Anthropic's server-side fallback on, so if a draft request is declined it's re-run on a fallback model. In **Settings → LinkedIn engagement**, describe yourself (role, expertise, tone) so drafts sound like you, and adjust the cadence (default every 7 days) and post window (default 6 months). Without a key, Engage tasks still work; you write the comments yourself.

### 6. LinkedIn bot (optional)

By default you do the LinkedIn steps yourself from the queue. In **bot** mode, a dedicated Chrome window (your installed Google Chrome, with a separate profile) does them:
- profile views;
- invites, with your personalized note (or without one if your free account can't add notes);
- messages to 1st-degree connections;
- likes on a post from the last 6 months, chosen by the bot;
- comments, only after you approve them: the bot likes the post and Claude drafts a comment. You approve or edit it on the queue card, and the bot posts it on its next run. Drafts nobody approves expire after 3 days and count as like-only.

Turning it on:
1. **Settings → LinkedIn automation → Sign in to LinkedIn**, or run `./oc linkedin-login`. A Chrome window opens on LinkedIn; you sign in there yourself. The app never sees your password.
2. Optionally test one profile read-only (it clicks nothing): `./oc linkedin-check https://www.linkedin.com/in/someone`
3. Choose **Bot does them**, check the daily caps and hours, then save.

Guardrails:
- **Daily caps** per action (default 40 views, 15 invites, 20 messages, 25 likes, 8 comments), plus a 7-day invite cap (80; LinkedIn's own limit is about 100).
- 45–120s between actions, at most 12 actions per run, and only during bot hours (default 09:00–18:00 weekdays).
- **It never solves CAPTCHAs or security checks.** On a login wall, checkpoint or verification page, the bot pauses, the tasks show up in your manual queue, and a banner explains why. Clear it yourself in the bot's window, then click **Resume**.
- If an action fails twice (button missing, dialog changed), that task is left to you with the reason.

**Read this first:** this automates your LinkedIn account, which LinkedIn's User Agreement prohibits. Accounts it catches automating can be restricted or banned. LinkedIn also changes its pages often; when it does, actions fail into the manual queue until the selectors in `campaigner/linkedin_bot.py` (`ChromeDriver`) are updated. `./oc linkedin-check` shows what still matches. The Mac must be awake, and a Chrome window appears while the bot works.

## Workflow

1. **Campaigns → Create.** You get a five-step starter: Connect → Email → LinkedIn message → Email bump → Email break-up.
2. **Sequence tab.** Edit steps, waits and conditions. The preview renders each step for a real lead. Write plain text; a blank line starts a new paragraph. Variables: `{{ first_name }}`, `{{ company }}`, `{{ title | default('there', true) }}`, plus any extra import column by its snake_case name.
3. **Import.** Upload CSV or Excel and map the columns. Duplicates merge on email or LinkedIn URL.
4. **Settings tab.** Choose the **Send from** mailbox and tracking options. Set the send window, daily caps (email and LinkedIn), the gap between emails, and your signature. Graph doesn't add your Outlook signature.
5. **Activate.** The scheduler runs every 10 min:
   check replies → queue due LinkedIn tasks → send due emails inside the window.
6. **LinkedIn queue.** Work through it once or twice a day. For a *Like + comment* task: click **Open their recent posts**, pick a post newer than the date shown that isn't listed under *Already engaged*, and like it. To comment, paste its text (and URL) into the card, click **Draft comment**, edit, then **Copy comment** and paste it under the post. Tick what you did and click **Done**. If they haven't posted in the window, click **No recent posts**; the next try comes a week later.

### How steps behave

| Situation | What happens |
|---|---|
| Email step, lead has no email | skipped |
| LinkedIn step, lead has no LinkedIn URL | skipped |
| Connect, but already connected or invited | skipped |
| LinkedIn message, or any `only if connected` step, while not connected | waits up to 7 days past due, then skips |
| Open LinkedIn task | the sequence waits until you mark it Done or Skip |
| Reply on any channel | that campaign: `replied`; the lead's other campaigns: `stopped` |
| Bounce | `bounced` |
| Open *Like + comment* task | doesn't hold up the sequence; at most one per person at a time, across campaigns |
| Unsubscribe link, or *Do not contact* on the lead | lead marked do-not-contact; every campaign stops, including future enrollments |
| Sending mailbox signed out, or its reply check fails | that mailbox's emails wait; other mailboxes keep sending |
| Mailbox daily cap reached | its emails wait until tomorrow (the cap spans all campaigns using it) |
| Template error (e.g. missing variable) | `error`, with the reason in the note; nothing sent |
| Outlook disconnected or reply check failing | emails held; LinkedIn tasks still queue |

Waits count from the previous step being done or skipped. The first step counts from enrollment, or from the campaign's start date.

## Limits and caveats

- **The Mac or PC must be awake.** *Settings → Keep awake* stops idle sleep while a campaign is active; closing a laptop lid still sleeps it (see the note there). Missed runs catch up when it wakes.
- **M365:** about 10k recipients a day and 30 messages a minute per mailbox. Keep the daily caps far below that, and ramp new mailboxes slowly. Keep bounce rate under 2%; the Senders page flags 2% and up as a warning and 5% and up as critical. This is for personal outreach, not bulk mail.
- **LinkedIn:** about 100 invites a week. Free accounts get a few personalized notes a month, capped at 200 characters. The bot's defaults stay under LinkedIn's fair-use limits: per day 20 invites (90 per rolling week), 60 profile views, 40 messages, 30 likes, 10 comments. Change them in **Settings → LinkedIn automation**.
- **Detection is heuristic.** A missed out-of-office counts as a reply, which fails safe: no more follow-ups. A missed LinkedIn notification just means you mark it by hand.
- **Compliance is on you:** GDPR/PECR for the EU and UK, CAN-SPAM for the US. Keep an easy opt-out; the starter's last email has one.

## Commands

On Windows, `.\oc` instead of `./oc`.

```
./oc serve [--port 8765] [--every 10] [--no-ticker]   dashboard + scheduler
./oc install / uninstall                              start at login, keep alive (launchd / Task Scheduler)
./oc update                                           pull the latest version, restart when idle
./oc restart                                          restart the dashboard when idle
./oc login [--device-code]                            connect a mailbox (repeat per mailbox)
./oc tracker-setup [--domain links.you.com]           tracking secrets + Cloudflare deploy steps
./oc linkedin-login                                   sign the LinkedIn bot's Chrome profile in
./oc linkedin-check <profile-url>                     read-only test of the bot on one profile
./oc tick [--dry-run]                                 one run now
./oc demo                                             seed a draft demo campaign
```

## Tests

```bash
.venv/bin/python -m unittest discover tests        # Windows: .venv\Scripts\python -m unittest discover tests
```

GitHub Actions runs them on macOS and Windows for every PR.
