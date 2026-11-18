#!/usr/bin/env python3
"""appointment-reminders: send reminders so clients show up. Standard library only.

    python remind.py add --name "Priya Naik" --phone 98765 43210 --service Checkup --at "2026-11-20 15:30"
    python remind.py import clients.csv          # columns: name,phone,email,service,start
    python remind.py list                        # next 14 days
    python remind.py run --dry-run               # preview what would be sent right now
    python remind.py run                         # send due reminders (schedule this every 15 min)
    python remind.py watch --every 300           # or leave this running
    python remind.py mark 3 attended|no_show|cancelled|confirmed
    python remind.py report                      # no-show rates, with vs without reminders
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import engine
import senders
from store import STATUSES, Store

HERE = Path(__file__).parent


def load_cfg(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def add_one(store: Store, cfg: dict, name: str, phone: str | None, email: str | None,
            service: str, start_text: str, notes: str = "") -> int:
    name = engine.clean_text(name)
    if not name:
        raise ValueError("name is required")
    phone_n = engine.normalize_phone(phone, cfg.get("default_country_code", "91")) if phone else None
    if phone and not phone_n:
        raise ValueError(f"can't use phone number {phone!r}")
    if email and not engine.valid_email(email):
        raise ValueError(f"can't use email {email!r}")
    if not phone_n and not email:
        raise ValueError("need a phone number or an email to send reminders to")
    return store.add(name, phone_n, email or None, engine.clean_text(service, 60),
                     engine.parse_dt(start_text), engine.clean_text(notes, 200))


def show(rows) -> None:
    if not rows:
        print("(none)")
    for r in rows:
        print(f"#{r['id']:<4} {r['start']}  {r['status']:<9} {r['name']} · {r['service']}"
              f"  [{r['phone'] or ''} {r['email'] or ''}]".rstrip())


def cmd_run(args, store: Store, cfg: dict) -> None:
    now = engine.parse_dt(args.now) if args.now else datetime.now()
    result = engine.run(store, cfg, now, lambda ch, appt, text: senders.send(ch, appt, text, cfg),
                        dry_run=args.dry_run)
    if args.dry_run:
        for appt_id, kind, text in result["preview"]:
            print(f"[would send #{appt_id} {kind}] {text}")
    print(f"sent={result['sent']} failed={result['failed']} skipped={result['skipped']}"
          + (f" would_send={len(result['preview'])}" if args.dry_run else ""))


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # piped output on Windows defaults to cp1252 and can't print names/emoji
        if not stream.isatty():
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="appointments.db")
    ap.add_argument("--config", default=str(HERE / "config.json"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add")
    a.add_argument("--name", required=True)
    a.add_argument("--phone", nargs="+", default=None)
    a.add_argument("--email")
    a.add_argument("--service", default="")
    a.add_argument("--at", required=True, help="'YYYY-MM-DD HH:MM'")
    a.add_argument("--notes", default="")
    i = sub.add_parser("import")
    i.add_argument("csv_file")
    l = sub.add_parser("list")
    l.add_argument("--days", type=int, default=14)
    l.add_argument("--all", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--now", help="pretend it is this time (testing)")
    w = sub.add_parser("watch")
    w.add_argument("--every", type=int, default=300, help="seconds between checks")
    m = sub.add_parser("mark")
    m.add_argument("id", type=int)
    m.add_argument("status", choices=STATUSES)
    d = sub.add_parser("delete")
    d.add_argument("id", type=int)
    sub.add_parser("report")
    args = ap.parse_args(argv)

    senders.load_env(HERE / ".env")
    cfg = load_cfg(args.config)
    store = Store(args.db)

    try:
        if args.cmd == "add":
            phone = " ".join(args.phone) if args.phone else None
            print(f"added #{add_one(store, cfg, args.name, phone, args.email, args.service, args.at, args.notes)}")
        elif args.cmd == "import":
            ok = bad = 0
            with open(args.csv_file, newline="", encoding="utf-8-sig") as f:
                for n, row in enumerate(csv.DictReader(f), start=2):
                    try:
                        add_one(store, cfg, row.get("name", ""), row.get("phone"), row.get("email"),
                                row.get("service", ""), row.get("start", ""), row.get("notes", ""))
                        ok += 1
                    except ValueError as e:
                        bad += 1
                        print(f"line {n}: {e}", file=sys.stderr)
            print(f"imported {ok}, rejected {bad}")
        elif args.cmd == "list":
            now = datetime.now()
            if args.all:
                show(store.all())
            else:
                show(store.between(now.replace(hour=0, minute=0), now + timedelta(days=args.days)))
        elif args.cmd == "run":
            cmd_run(args, store, cfg)
        elif args.cmd == "watch":
            print(f"checking every {args.every}s (Ctrl+C to stop)")
            ns = argparse.Namespace(dry_run=False, now=None)
            try:
                while True:
                    cmd_run(ns, store, cfg)
                    time.sleep(args.every)
            except KeyboardInterrupt:
                print("\nbye")
        elif args.cmd == "mark":
            print("updated" if store.set_status(args.id, args.status) else "no such appointment")
        elif args.cmd == "delete":
            print("deleted" if store.delete(args.id) else "no such appointment")
        elif args.cmd == "report":
            rep = engine.report(store)
            print(f"appointments: {rep['total']}   by status: {rep['by_status']}")
            for label, key in (("reminded", "reminded"), ("not reminded", "not_reminded")):
                b = rep[key]
                rate = f"{100 * b['no_show'] / b['n']:.0f}%" if b["n"] else "n/a"
                print(f"no-show rate ({label}): {rate}  ({b['no_show']}/{b['n']})")
            for svc, b in sorted(rep["by_service"].items()):
                print(f"  {svc}: {b['no_show']}/{b['n']} no-shows")
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
