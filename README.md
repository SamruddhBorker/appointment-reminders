# appointment-reminders

[![tests](https://github.com/SamruddhBorker/appointment-reminders/actions/workflows/tests.yml/badge.svg)](https://github.com/SamruddhBorker/appointment-reminders/actions/workflows/tests.yml)

Automatic reminders for clinics, salons, tutors and any business that loses
money to no-shows. Add appointments, schedule `python remind.py run`, and
clients get a "day before" and a "two hours before" message by WhatsApp or email.

**The problem:** a missed appointment is an empty slot nobody can fill.
A short reminder is the cheapest fix, and this shows you whether it works with a
no-show report that compares clients who were reminded with those who weren't.

- Python 3.9+, **standard library only**, SQLite storage
- Works from the command line; no server or app needed
- Safe by default: `console` channel prints messages instead of sending them

## Quick start

```bash
python remind.py add --name "Priya Naik" --phone 98765 43210 --service Checkup --at "2026-11-20 15:30"
python remind.py import clients.csv        # name,phone,email,service,start
python remind.py list
python remind.py run --dry-run             # preview what would go out right now
python remind.py run                       # send what's due
python remind.py mark 3 attended           # or no_show / cancelled / confirmed
python remind.py report
```

Run `python remind.py run` every 15 minutes with Windows Task Scheduler or cron,
or leave `python remind.py watch --every 300` running.

## Smart scheduling

- Reminders at configurable offsets (default 24 h and 2 h before)
- **Only the nearest window is sent.** Someone who books at noon for 3 pm gets
  the "2 hours" reminder, not a stale "tomorrow" one
- **Quiet hours** (default 9 pm to 8 am): reminders wait until morning
- Never sends twice; failed sends are retried on the next run
- Never reminds cancelled or past appointments, or within 10 minutes of the start
- **Channel fallback**: `["whatsapp", "email"]` tries WhatsApp first, then email
- Message templates in `config.json` with `$first_name`, `$service`, `$time`, `$when`, `$date`, `$business`, `$phone`

## Channels

| Channel | Needs |
|---|---|
| `console` | nothing (prints; good for testing) |
| `whatsapp` | `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_ID` in `.env` ([Cloud API](https://developers.facebook.com/docs/whatsapp/cloud-api/get-started)) |
| `email` | `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD` (optional `SMTP_PORT`, `SMTP_FROM`) in `.env` |

Copy `.env.example` to `.env`. It is git-ignored; never commit it.

> **WhatsApp rule:** outside the 24-hour customer-service window, a business may
> only message a customer using a pre-approved *template*. Create one in Meta
> Business Manager and set `whatsapp.template` in `config.json` (with
> `template_params`). Without a template the message is sent as plain text,
> which WhatsApp only delivers inside that window.

## The no-show report

```
no-show rate (reminded): 8%  (2/25)
no-show rate (not reminded): 21%  (4/19)
```

Mark each appointment `attended` or `no_show` after the visit and the report shows
whether reminders are paying off. (Small samples are noisy: treat it as a hint.)

## Safety

- Names and notes are stripped of control characters, so they can't inject email headers or break messages
- Templates use `string.Template`, which can't run code
- Phone numbers are validated and normalised (10-digit numbers get the default country code, 91 for India)
- Secrets live in `.env`, never in the repo
- You are responsible for having your clients' consent to be messaged

## Layout

| File | Role |
|---|---|
| `remind.py` | command-line interface |
| `engine.py` | scheduling rules, templates, report |
| `senders.py` | console, WhatsApp, email delivery |
| `store.py` | SQLite storage |
| `config.json` | business name, offsets, quiet hours, templates |

## Tests

```bash
python -m unittest -v
```

## License

MIT
