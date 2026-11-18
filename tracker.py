"""Core booking rules and SQLite storage for the babysitting tracker."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import sqlite3


class BookingError(ValueError):
    """A booking request violates a business rule."""


def parse_datetime(value: str) -> datetime:
    """Parse a local, minute-precision date and time."""
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise BookingError("Use YYYY-MM-DD HH:MM (24-hour time).") from exc


def parse_rate(value: str) -> int:
    """Convert a GBP hourly rate to integer pence without float errors."""
    try:
        rate = Decimal(value.strip().removeprefix("£").strip())
        if not rate.is_finite() or not 0 < rate <= 1000:
            raise BookingError("Enter an hourly rate between £0.01 and £1000.")
        pence = rate * 100
        if pence != pence.to_integral_value():
            raise BookingError("Use at most two decimal places for the rate.")
        return int(pence)
    except (InvalidOperation, ValueError) as exc:
        if isinstance(exc, BookingError):
            raise
        raise BookingError("Enter a valid hourly rate, e.g. 11.50.") from exc


def money(pence: int) -> str:
    return f"£{Decimal(pence) / 100:,.2f}"


@dataclass(frozen=True)
class Booking:
    id: int
    client: str
    start: datetime
    end: datetime
    rate_pence: int
    status: str

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    @property
    def earnings_pence(self) -> int:
        amount = Decimal(self.minutes * self.rate_pence) / 60
        return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    @property
    def duration(self) -> str:
        hours, minutes = divmod(self.minutes, 60)
        return f"{hours}h {minutes:02d}m"


@dataclass(frozen=True)
class Earnings:
    completed: int
    scheduled: int
    completed_minutes: int
    scheduled_minutes: int
    completed_count: int
    scheduled_count: int

    @property
    def total(self) -> int:
        return self.completed + self.scheduled


class BookingStore:
    """A single-user database. Changes are atomic and survive restarts."""

    def __init__(self, path: Path | str):
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client TEXT NOT NULL,
                start TEXT NOT NULL,
                end TEXT NOT NULL CHECK (end > start),
                rate_pence INTEGER NOT NULL CHECK (rate_pence BETWEEN 1 AND 100000),
                status TEXT NOT NULL DEFAULT 'scheduled'
                    CHECK (status IN ('scheduled', 'completed', 'cancelled'))
            )
        """)
        self.connection.commit()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.close()

    @staticmethod
    def _booking(row: sqlite3.Row) -> Booking:
        return Booking(row["id"], row["client"], datetime.fromisoformat(row["start"]),
                       datetime.fromisoformat(row["end"]), row["rate_pence"], row["status"])

    @staticmethod
    def _validate(client, start, end, rate_pence):
        client = client.strip()
        if not client or len(client) > 80 or not client.isprintable():
            raise BookingError("Enter a client name or alias of 1–80 characters.")
        for timestamp in (start, end):
            if timestamp.tzinfo is not None or timestamp.second or timestamp.microsecond:
                raise BookingError("Use local times with minute precision.")
        if end <= start:
            raise BookingError("End must be after start. For overnight jobs, enter the next date.")
        if not isinstance(rate_pence, int) or not 1 <= rate_pence <= 100000:
            raise BookingError("Hourly rate must be between £0.01 and £1000.")
        return client

    def _check_overlap(self, start, end, exclude_id=0):
        # Half-open intervals: a new job can start exactly as another ends.
        conflict = self.connection.execute("""
            SELECT * FROM bookings
            WHERE status != 'cancelled' AND id != ? AND start < ? AND end > ?
            ORDER BY start LIMIT 1
        """, (exclude_id, end.isoformat(), start.isoformat())).fetchone()
        if conflict:
            other = self._booking(conflict)
            raise BookingError(f"Overlaps booking #{other.id} ({other.client}, "
                               f"{other.start:%d %b %H:%M}–{other.end:%d %b %H:%M}).")

    def get(self, booking_id: int) -> Booking:
        row = self.connection.execute("SELECT * FROM bookings WHERE id = ?",
                                      (booking_id,)).fetchone()
        if row is None:
            raise BookingError(f"Booking #{booking_id} does not exist.")
        return self._booking(row)

    def add(self, client: str, start: datetime, end: datetime, rate_pence: int) -> Booking:
        client = self._validate(client, start, end, rate_pence)
        with self.connection:
            # Lock before checking and writing to prevent concurrent double-bookings.
            self.connection.execute("BEGIN IMMEDIATE")
            self._check_overlap(start, end)
            cursor = self.connection.execute("""
                INSERT INTO bookings (client, start, end, rate_pence)
                VALUES (?, ?, ?, ?)
            """, (client, start.isoformat(), end.isoformat(), rate_pence))
            return self.get(cursor.lastrowid)

    def edit(self, booking_id: int, client: str, start: datetime,
             end: datetime, rate_pence: int) -> Booking:
        client = self._validate(client, start, end, rate_pence)
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            old = self.get(booking_id)
            if old.status == "cancelled":
                raise BookingError("Cancelled bookings cannot be edited; add a new booking.")
            self._check_overlap(start, end, booking_id)
            self.connection.execute("""
                UPDATE bookings SET client=?, start=?, end=?, rate_pence=? WHERE id=?
            """, (client, start.isoformat(), end.isoformat(), rate_pence, booking_id))
            return self.get(booking_id)

    def set_status(self, booking_id: int, status: str) -> Booking:
        if status not in ("completed", "cancelled"):
            raise BookingError("Choose completed or cancelled.")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            old = self.get(booking_id)
            if old.status != "scheduled":
                raise BookingError(f"Booking #{booking_id} is already {old.status}.")
            self.connection.execute("UPDATE bookings SET status=? WHERE id=?",
                                    (status, booking_id))
            return self.get(booking_id)

    def list(self, status: str | None = None) -> list[Booking]:
        if status is None:
            rows = self.connection.execute("SELECT * FROM bookings ORDER BY start, id")
        else:
            rows = self.connection.execute(
                "SELECT * FROM bookings WHERE status=? ORDER BY start, id", (status,))
        return [self._booking(row) for row in rows]

    def earnings(self, first: date, stop: date) -> Earnings:
        """Summarise bookings starting in [first, stop), excluding cancellations."""
        if stop <= first:
            raise BookingError("Report end must be after the start.")
        rows = self.connection.execute("""
            SELECT * FROM bookings WHERE start >= ? AND start < ?
                AND status != 'cancelled'
        """, (first.isoformat(), stop.isoformat()))
        bookings = [self._booking(row) for row in rows]
        completed = [b for b in bookings if b.status == "completed"]
        scheduled = [b for b in bookings if b.status == "scheduled"]
        return Earnings(sum(b.earnings_pence for b in completed),
                        sum(b.earnings_pence for b in scheduled),
                        sum(b.minutes for b in completed),
                        sum(b.minutes for b in scheduled),
                        len(completed), len(scheduled))


def week_bounds(day: date) -> tuple[date, date]:
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=7)


def month_bounds(day: date) -> tuple[date, date]:
    first = day.replace(day=1)
    stop = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, stop
