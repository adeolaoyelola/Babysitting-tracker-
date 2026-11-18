"""Interactive command-line menu for the Babysitting Booking & Earnings Tracker."""

import argparse
from datetime import date, datetime, time, timedelta
from pathlib import Path
import sqlite3
import tempfile

from tracker import (BookingError, BookingStore, money, month_bounds, parse_datetime,
                     parse_rate, week_bounds)


def heading(title):
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def prompt(label, default=None):
    hint = f" [{default}]" if default is not None else ""
    value = input(f"{label}{hint}: ").strip()
    return str(default) if not value and default is not None else value


def booking_id():
    try:
        number = int(prompt("Booking ID"))
        if number > 0:
            return number
    except ValueError:
        pass
    raise BookingError("Enter a positive booking ID, e.g. 1.")


def show_bookings(bookings):
    if not bookings:
        print("No bookings found.")
        return
    for booking in bookings:
        print(f"#{booking.id:03d}  {booking.client}  [{booking.status.upper()}]")
        print(f"      {booking.start:%a %d %b %Y, %H:%M} → "
              f"{booking.end:%a %d %b %Y, %H:%M}")
        print(f"      {booking.duration} × {money(booking.rate_pence)}/h"
              f"  |  {money(booking.earnings_pence)}"
              + (" (cancelled; excluded)" if booking.status == "cancelled" else ""))


def booking_details(previous=None):
    print("Use YYYY-MM-DD HH:MM in 24-hour time. Overnight? Use the next date.")
    if previous:
        print("Press Enter to keep an existing value.")
    client = prompt("Client name or alias", previous.client if previous else None)
    start = parse_datetime(prompt("Start", previous.start.strftime("%Y-%m-%d %H:%M")
                                  if previous else None))
    end = parse_datetime(prompt("End", previous.end.strftime("%Y-%m-%d %H:%M")
                                if previous else None))
    rate = parse_rate(prompt("Hourly rate (£)", money(previous.rate_pence).replace(",", "")
                             if previous else None))
    return client, start, end, rate


def show_report(store, period, day):
    first, stop = week_bounds(day) if period == "week" else month_bounds(day)
    result = store.earnings(first, stop)
    heading(f"{period.upper()}LY EARNINGS | {first:%d %b %Y} – "
            f"{stop - timedelta(days=1):%d %b %Y}" if period == "week" else
            f"MONTHLY EARNINGS | {first:%B %Y}")
    print(f"Completed earnings   {money(result.completed):>14}")
    print(f"Scheduled earnings   {money(result.scheduled):>14}")
    print("-" * 36)
    print(f"Total projected      {money(result.total):>14}")
    print(f"Completed bookings: {result.completed_count}  |  "
          f"Scheduled bookings: {result.scheduled_count}")
    hours, mins = divmod(result.completed_minutes, 60)
    print(f"Completed hours: {hours}h {mins:02d}m")
    hours, mins = divmod(result.scheduled_minutes, 60)
    print(f"Scheduled hours: {hours}h {mins:02d}m")
    print("Cancelled jobs are excluded. Jobs are counted by their start date.")


def report_date():
    try:
        return date.fromisoformat(prompt("Choose a date (YYYY-MM-DD)", date.today()))
    except ValueError as exc:
        raise BookingError("Enter a valid date in YYYY-MM-DD format.") from exc


def menu(store):
    heading("BABYSITTING | Booking & Earnings Tracker")
    print("Your schedule, earnings and completed jobs — in one place.")
    while True:
        today = date.today()
        week = store.earnings(*week_bounds(today))
        month = store.earnings(*month_bounds(today))
        print(f"\nThis week: {money(week.completed)} completed"
              f"  |  This month: {money(month.completed)} completed")
        print("""
  BOOKING MANAGEMENT                  EARNINGS
  1. Add booking                      6. Weekly report
  2. View all bookings                7. Monthly report
  3. Edit booking
  4. Cancel booking                   0. Save & exit
  5. Mark booking completed
""")
        try:
            choice = prompt("Select an option")
            if choice == "1":
                heading("ADD BOOKING")
                booking = store.add(*booking_details())
                print("Booking saved successfully.")
                show_bookings([booking])
            elif choice == "2":
                heading("BOOKING HISTORY")
                show_bookings(store.list())
            elif choice == "3":
                heading("EDIT BOOKING")
                show_bookings(store.list())
                old = store.get(booking_id())
                if old.status == "cancelled":
                    raise BookingError("Cancelled bookings cannot be edited.")
                updated = store.edit(old.id, *booking_details(old))
                print("Booking updated.")
                show_bookings([updated])
            elif choice in ("4", "5"):
                heading("CANCEL BOOKING" if choice == "4" else "COMPLETE BOOKING")
                show_bookings(store.list("scheduled"))
                old = store.get(booking_id())
                status = "cancelled" if choice == "4" else "completed"
                if prompt(f"Mark #{old.id} as {status}? (y/N)").lower() == "y":
                    show_bookings([store.set_status(old.id, status)])
                    print(f"Booking {status}.")
                else:
                    print("No changes made.")
            elif choice == "6":
                show_report(store, "week", report_date())
            elif choice == "7":
                show_report(store, "month", report_date())
            elif choice == "0":
                print("All changes saved. See you next time!")
                return
            else:
                print("Choose an option from 0 to 7.")
        except BookingError as exc:
            print(f"Could not complete request: {exc}")
        except sqlite3.Error:
            print("Database operation failed. No incomplete change was saved; try again.")
        except (EOFError, KeyboardInterrupt):
            print("\nExiting. Previous changes are saved.")
            return


def add_demo_bookings(store):
    """Create fictional examples, strictly inside the temporary demo database."""
    monday, _ = week_bounds(date.today())
    first = datetime.combine(monday, time(17))
    a = store.add("Sample family A", first, first + timedelta(hours=3), 1100)
    store.set_status(a.id, "completed")
    second = first + timedelta(days=2)
    store.add("Sample family B", second, second + timedelta(hours=4, minutes=30), 1200)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(__file__).resolve().parent / "data" /
                        "bookings.sqlite3", help="Database file (default: data/bookings.sqlite3)")
    parser.add_argument("--demo", action="store_true",
                        help="Explore fictional bookings; changes are discarded on exit")
    args = parser.parse_args()
    try:
        if args.demo:
            with tempfile.TemporaryDirectory() as folder:
                with BookingStore(Path(folder) / "demo.sqlite3") as store:
                    add_demo_bookings(store)
                    print("DEMO MODE — fictional records only, no real data is modified.")
                    menu(store)
        else:
            args.db.parent.mkdir(parents=True, exist_ok=True)
            with BookingStore(args.db) as store:
                menu(store)
    except (OSError, sqlite3.Error) as exc:
        parser.exit(1, f"Unable to open the booking database: {exc}\n")


if __name__ == "__main__":
    main()
