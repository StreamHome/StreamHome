from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from lifecycle.clock import server_clock_snapshot


class ServerClockTests(unittest.TestCase):
    def test_utc_and_wall_time_are_reported_without_browser_conversion(self):
        value = datetime(2026, 10, 13, 12, 30, 45, tzinfo=timezone.utc)
        self.assertEqual(server_clock_snapshot(value), {
            "time_zone": "UTC", "utc_offset": "UTC+00:00", "local_time": "2026-10-13 12:30:45",
        })

    def test_fractional_and_negative_offsets(self):
        for minutes, expected in [(345, "UTC+05:45"), (-210, "UTC-03:30")]:
            with self.subTest(minutes=minutes):
                value = datetime(2026, 10, 13, 12, tzinfo=timezone(timedelta(minutes=minutes)))
                self.assertEqual(server_clock_snapshot(value)["utc_offset"], expected)

    def test_daylight_saving_offset_is_sampled_for_the_current_date(self):
        zone = ZoneInfo("America/New_York")
        winter = server_clock_snapshot(datetime(2026, 1, 13, 12, tzinfo=zone))
        summer = server_clock_snapshot(datetime(2026, 7, 13, 12, tzinfo=zone))
        self.assertEqual((winter["time_zone"], winter["utc_offset"]), ("EST", "UTC-05:00"))
        self.assertEqual((summer["time_zone"], summer["utc_offset"]), ("EDT", "UTC-04:00"))

    def test_naive_injected_time_is_rejected(self):
        with self.assertRaises(ValueError):
            server_clock_snapshot(datetime(2026, 10, 13, 12))

    def test_default_reads_the_process_local_clock(self):
        value = datetime(2026, 10, 13, 12, tzinfo=timezone(timedelta(hours=3), "EEST"))
        with patch("lifecycle.clock.datetime") as clock:
            clock.now.return_value.astimezone.return_value = value
            self.assertEqual(server_clock_snapshot()["utc_offset"], "UTC+03:00")
            clock.now.assert_called_once_with()
            clock.now.return_value.astimezone.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
