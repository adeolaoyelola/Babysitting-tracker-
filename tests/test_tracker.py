"""Regression tests for validation, scheduling, persistence and earnings."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tracker import BookingError, BookingStore, month_bounds, parse_datetime, parse_rate, week_bounds


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.path = Path(self.folder.name) / "test.sqlite3"
        self.store = BookingStore(self.path)
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(lambda: self.store.connection.close())

    def add(self, start="2026-11-16 17:00", end="2026-11-16 20:30", rate=1100):
        return self.store.add("Example family", parse_datetime(start), parse_datetime(end), rate)

    def test_calculates_fractional_hours(self):
        booking = self.add()
        self.assertEqual(booking.minutes, 210)
        self.assertEqual(booking.earnings_pence, 3850)

    def test_rounds_half_penny_up(self):
        self.assertEqual(self.add(end="2026-11-16 17:01", rate=30).earnings_pence, 1)

    def test_overnight_booking(self):
        booking = self.add("2026-11-16 19:00", "2026-11-17 08:30", 500)
        self.assertEqual(booking.minutes, 810)
        self.assertEqual(booking.earnings_pence, 6750)

    def test_rejects_invalid_end(self):
        for end in ("2026-11-16 17:00", "2026-11-16 16:59"):
            with self.subTest(end=end), self.assertRaises(BookingError):
                self.add(end=end)

    def test_rejects_invalid_rates(self):
        for rate in ("-5", "0", "NaN", "Infinity", "11.555", "abc", "1001"):
            with self.subTest(rate=rate), self.assertRaises(BookingError):
                parse_rate(rate)
        self.assertEqual(parse_rate("£11.50"), 1150)

    def test_rejects_invalid_datetime(self):
        with self.assertRaises(BookingError):
            parse_datetime("2026-02-30 17:00")

    def test_rejects_blank_client(self):
        with self.assertRaises(BookingError):
            self.store.add("   ", parse_datetime("2026-11-16 17:00"),
                           parse_datetime("2026-11-16 20:00"), 1000)

    def test_overlapping_bookings_rejected(self):
        self.add()
        for start, end in (
            ("2026-11-16 17:00", "2026-11-16 20:30"),
            ("2026-11-16 18:00", "2026-11-16 19:00"),
            ("2026-11-16 16:00", "2026-11-16 18:00"),
            ("2026-11-16 20:00", "2026-11-16 22:00"),
        ):
            with self.subTest(start=start, end=end), self.assertRaises(BookingError):
                self.add(start, end)
        self.assertEqual(len(self.store.list()), 1)

    def test_overnight_conflict(self):
        self.add("2026-11-16 19:00", "2026-11-17 08:30")
        with self.assertRaises(BookingError):
            self.add("2026-11-17 07:00", "2026-11-17 09:00")

    def test_back_to_back_bookings_allowed(self):
        self.add()
        self.add("2026-11-16 20:30", "2026-11-16 21:30")
        self.assertEqual(len(self.store.list()), 2)

    def test_edit_and_conflict_rollback(self):
        first = self.add()
        second = self.add("2026-11-17 17:00", "2026-11-17 20:30")
        with self.assertRaises(BookingError):
            self.store.edit(second.id, "New", first.start, first.end, 1000)
        self.assertEqual(self.store.get(second.id), second)
        edited = self.store.edit(first.id, "Updated", first.start, first.end, 1200)
        self.assertEqual(edited.client, "Updated")
        self.assertEqual(edited.earnings_pence, 4200)

    def test_cancel_frees_slot_and_retains_history(self):
        first = self.add()
        cancelled = self.store.set_status(first.id, "cancelled")
        self.assertEqual(cancelled.status, "cancelled")
        self.add()
        self.assertEqual(len(self.store.list()), 2)
        self.assertEqual(self.store.earnings(*week_bounds(date(2026, 11, 16))).total, 3850)
        with self.assertRaises(BookingError):
            self.store.edit(first.id, "New", first.start, first.end, 1000)

    def test_completion_and_monthly_report(self):
        first = self.add()
        self.store.set_status(first.id, "completed")
        self.add("2026-11-30 17:00", "2026-11-30 19:00")
        self.add("2026-12-01 17:00", "2026-12-01 19:00")
        result = self.store.earnings(*month_bounds(date(2026, 11, 18)))
        self.assertEqual((result.completed, result.scheduled, result.total), (3850, 2200, 6050))
        self.assertEqual((result.completed_count, result.scheduled_count), (1, 1))
        self.assertEqual(result.completed_minutes, 210)

    def test_week_boundary_and_overnight_attribution(self):
        self.add("2026-11-22 23:30", "2026-11-23 01:30")
        self.assertEqual(self.store.earnings(*week_bounds(date(2026, 11, 18))).total, 2200)
        self.assertEqual(self.store.earnings(*week_bounds(date(2026, 11, 23))).total, 0)

    def test_month_boundary_december(self):
        self.assertEqual(month_bounds(date(2026, 12, 31)),
                         (date(2026, 12, 1), date(2027, 1, 1)))

    def test_persistence_after_restart(self):
        booking = self.add()
        self.store.connection.close()
        self.store = BookingStore(self.path)
        self.assertEqual(self.store.get(booking.id), booking)

    def test_missing_booking_and_invalid_transition(self):
        with self.assertRaises(BookingError):
            self.store.get(999)
        first = self.add()
        self.store.set_status(first.id, "completed")
        with self.assertRaises(BookingError):
            self.store.set_status(first.id, "cancelled")


if __name__ == "__main__":
    unittest.main()
