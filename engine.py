"""Reminder logic: what is due, to whom, with what text. No network code here."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from string import Template
from typing import Callable, Optional

from store import FMT, Store


# ---------------------------------------------------------------- parsing
def parse_dt(text: str) -> datetime:
    t = text.strip().replace("T", " ")
    for fmt in (FMT, "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            pass
    raise ValueError(f"can't read date/time {text!r}; use 'YYYY-MM-DD HH:MM'")


def clean_text(s: str, limit: int = 80) -> str:
    """Strip control characters (incl. newlines) so names can't break messages or headers."""
    return re.sub(r"[\x00-\x1f\x7f]", " ", s or "").strip()[:limit]


def normalize_phone(raw: Optional[str], default_cc: str = "91") -> Optional[str]:
    """Digits only, with country code. '98765 43210' -> '919876543210'. None if unusable."""
    if not raw:
        return None
    d = re.sub(r"\D", "", raw)
    if raw.strip().startswith("00"):
        d = d[2:]
    if len(d) == 10:
        d = default_cc + d
    elif len(d) == 11 and d.startswith("0"):
        d = default_cc + d[1:]
    return d if 11 <= len(d) <= 15 else None


def valid_email(s: Optional[str]) -> bool:
    return bool(s) and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", s) is not None


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def in_quiet_hours(now: datetime, cfg: dict) -> bool:
    q = cfg.get("quiet_hours")
    if not q:
        return False
    start, end = _hm(q["start"]), _hm(q["end"])
    t = now.time()
    return (start <= t or t < end) if start > end else (start <= t < end)


# -------------------------------------------------------------- messages
def fmt_time(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def fmt_date(dt: datetime) -> str:
    return f"{dt.strftime('%a')} {dt.day} {dt.strftime('%b')}"


def context(appt, cfg: dict, now: datetime) -> dict[str, str]:
    start = datetime.strptime(appt["start"], FMT)
    delta_days = (start.date() - now.date()).days
    when = "today" if delta_days == 0 else "tomorrow" if delta_days == 1 else fmt_date(start)
    return {
        "name": appt["name"], "first_name": appt["name"].split()[0] if appt["name"].split() else "",
        "service": appt["service"] or "appointment", "date": fmt_date(start),
        "time": fmt_time(start), "when": when,
        "business": cfg["business_name"], "phone": cfg.get("business_phone", ""),
    }


def render(template: str, ctx: dict[str, str]) -> str:
    """$name-style substitution only (string.Template), so templates can never run code."""
    return Template(template).safe_substitute(ctx)


# ------------------------------------------------------------- scheduling
@dataclass
class Action:
    appt: object
    kind: str
    skip: bool = False   # True = mark as skipped, send nothing


def plan(store: Store, cfg: dict, now: datetime) -> list[Action]:
    """Work out what to send and what to skip right now.

    For each upcoming booked/confirmed appointment, only the NEAREST reminder window
    that has opened is sent. Earlier windows that were missed (e.g. booked an hour
    before) are skipped, so nobody gets a 'tomorrow' reminder after the fact.
    """
    offsets = sorted(cfg["offsets"], key=lambda o: o["minutes_before"])  # nearest first
    quiet = in_quiet_hours(now, cfg)
    min_notice = timedelta(minutes=int(cfg.get("min_notice_minutes", 10)))
    actions: list[Action] = []
    for appt in store.active_after(now):
        start = datetime.strptime(appt["start"], FMT)
        opened = [o for o in offsets if now >= start - timedelta(minutes=o["minutes_before"])]
        if not opened:
            continue
        done = store.done_kinds(appt["id"])
        nearest = opened[0]
        for o in opened[1:]:
            if o["name"] not in done:
                actions.append(Action(appt, o["name"], skip=True))
        if nearest["name"] in done:
            continue
        if start - now < min_notice:
            actions.append(Action(appt, nearest["name"], skip=True))
        elif not quiet:
            actions.append(Action(appt, nearest["name"]))
        # in quiet hours: leave pending, it will go out when quiet hours end
    return actions


SendFn = Callable[[str, object, str], tuple[bool, str]]  # (channel, appt, text) -> (ok, detail)


def run(store: Store, cfg: dict, now: datetime, send: SendFn, dry_run: bool = False) -> dict:
    summary = {"sent": 0, "failed": 0, "skipped": 0, "preview": []}
    for act in plan(store, cfg, now):
        if act.skip:
            if not dry_run:
                store.record(act.appt["id"], act.kind, "skipped", "", now, "window missed")
            summary["skipped"] += 1
            continue
        ctx = context(act.appt, cfg, now)
        tmpl = cfg["templates"].get(act.kind) or cfg["templates"]["default"]
        text = render(tmpl, ctx)
        if dry_run:
            summary["preview"].append((act.appt["id"], act.kind, text))
            continue
        ok, channel, detail = False, "", "no usable contact details"
        for ch in cfg.get("channels", ["console"]):
            ok, detail = send(ch, act.appt, text)
            if ok:
                channel = ch
                break
        store.record(act.appt["id"], act.kind, "sent" if ok else "failed", channel, now, detail)
        summary["sent" if ok else "failed"] += 1
    return summary


# ----------------------------------------------------------------- report
def report(store: Store) -> dict:
    """No-show statistics, split by whether a reminder was sent."""
    rows = store.all()
    out = {"total": len(rows), "by_status": {}, "by_service": {}, "reminded": {"n": 0, "no_show": 0},
           "not_reminded": {"n": 0, "no_show": 0}}
    for r in rows:
        out["by_status"][r["status"]] = out["by_status"].get(r["status"], 0) + 1
        if r["status"] not in ("attended", "no_show"):
            continue
        svc = out["by_service"].setdefault(r["service"] or "(none)", {"n": 0, "no_show": 0})
        svc["n"] += 1
        got = any(x["status"] == "sent" for x in store.reminders_for(r["id"]))
        bucket = out["reminded"] if got else out["not_reminded"]
        bucket["n"] += 1
        if r["status"] == "no_show":
            svc["no_show"] += 1
            bucket["no_show"] += 1
    return out
