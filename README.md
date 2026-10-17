# outlook-campaigns

Email + LinkedIn outreach sequences, run from your own M365 Outlook, with a local dashboard.

- **Sequences mix channels:** Email, LinkedIn connect, LinkedIn message, View profile. Each step has a wait and an optional condition (`only if connected` / `only if not connected`).
- **Email** sends through Microsoft Graph as you, and lands in your Sent Items. Follow-ups reply in the same thread.
- **LinkedIn runs as an assisted queue.** When a sequence reaches a LinkedIn step, a task shows up with the text already personalized. You click *Copy & open profile*, paste, send, then *Done*. Nothing touches LinkedIn automatically, so there's no ban risk.
- **Detection:** email replies, bounces and out-of-office replies come from your mailbox. LinkedIn accepts and replies come from LinkedIn's notification emails in the same mailbox. A reply on any channel stops that lead in every campaign.
- **Dashboard:** overview funnel and 14-day activity, per-step stats, sequence editor with live preview per lead, lead timeline, CSV/Excel import with column mapping.

It runs on your Mac only (`127.0.0.1`). The data lives in `data/app.db` and never leaves the machine except as the emails you send.

## Setup

### 1. Register an app in Entra ID (5 min; IT may need to do this)

1. https://entra.microsoft.com → **App registrations** → **New registration**. Name it `Outlook Campaigns`, choose *this organizational directory only*, then **Register**.
2. **Authentication** → **Add a platform** → **Mobile and desktop applications** → add `http://localhost`. Under *Advanced settings*, set **Allow public client flows = Yes**.
3. **API permissions** → Microsoft Graph → **Delegated**: add `Mail.Send` and `Mail.ReadWrite`. If your tenant blocks user consent, an admin must click **Grant admin consent**.
4. Copy the *Application (client) ID* and *Directory (tenant) ID* from **Overview**.

```bash
cp .env.example .env   # paste both IDs
```

### 2. Install and run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./oc serve                 # http://127.0.0.1:8765
```

Open **Settings → Connect Outlook**. Or run `./oc login`, adding `--device-code` if browser sign-in is blocked. The token is stored encrypted in your Keychain and refreshes silently.

To keep it running in the background and start it at login:

```bash
./oc install               # ./oc uninstall to remove
```

To explore first, run `./oc demo`. It seeds a **draft** campaign with example.com leads that never sends.

### 3. LinkedIn notification emails

Detection reads LinkedIn's emails. In LinkedIn → Settings → Communications → Email, keep **invitation accepted** and **new messages** on. Matching is by the lead's full name in the notification. If one is missed, use *Mark connected* or *They replied* on the lead or task.

## Workflow

1. **Campaigns → Create.** You get a five-step starter: Connect → Email → LinkedIn message → Email bump → Email break-up.
2. **Sequence tab.** Edit steps, waits and conditions. The preview renders each step for a real lead. Write plain text; a blank line starts a new paragraph. Variables: `{{ first_name }}`, `{{ company }}`, `{{ title | default('there', true) }}`, plus any extra import column by its snake_case name.
3. **Import.** Upload CSV or Excel and map the columns. Duplicates merge on email or LinkedIn URL.
4. **Settings tab.** Set the send window, daily caps (email and LinkedIn), the gap between emails, and your signature. Graph doesn't add your Outlook signature.
5. **Activate.** The scheduler runs every 10 min:
   check replies → queue due LinkedIn tasks → send due emails inside the window.
6. **LinkedIn queue.** Work through it once or twice a day.

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
| Template error (e.g. missing variable) | `error`, with the reason in the note; nothing sent |
| Outlook disconnected or reply check failing | emails held; LinkedIn tasks still queue |

Waits count from the previous step being done or skipped. The first step counts from enrollment, or from the campaign's start date.

## Limits and caveats

- **The Mac must be awake.** Missed runs catch up when it wakes.
- **M365:** about 10k recipients a day and 30 messages a minute. Keep the daily caps far below that. This is for personal outreach, not bulk mail.
- **LinkedIn:** about 100 invites a week. Free accounts get a few personalized notes a month, capped at 200 characters. Keep LinkedIn tasks per day ≤ 20.
- **Detection is heuristic.** A missed out-of-office counts as a reply, which fails safe: no more follow-ups. A missed LinkedIn notification just means you mark it by hand.
- **Compliance is on you:** GDPR/PECR for the EU and UK, CAN-SPAM for the US. Keep an easy opt-out; the starter's last email has one.

## Commands

```
./oc serve [--port 8765] [--every 10] [--no-ticker]   dashboard + scheduler
./oc install / uninstall                              launchd: start at login, keep alive
./oc login [--device-code]                            connect Outlook
./oc tick [--dry-run]                                 one run now
./oc demo                                             seed a draft demo campaign
```

## Tests

```bash
.venv/bin/python -m unittest discover tests
```
