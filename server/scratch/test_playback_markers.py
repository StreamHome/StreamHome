"""Pure persisted-marker completion boundaries; no runtime or database setup."""

import unittest

from services.playback_markers import is_credits_exit_position


class CreditsExitPositionRegression(unittest.TestCase):
    def test_lead_boundary_and_separate_intervals_preserve_scene_gaps(self) -> None:
        markers = {"credits": [{"start": 100, "end": 120}, {"start": 160, "end": 200}]}
        for clock, expected in ((89.999, False), (90, True), (99, True), (100, True),
                                (119.999, True), (120, False), (140, False), (149.999, False),
                                (150, True), (199.999, True), (200, False)):
            with self.subTest(clock=clock):
                self.assertEqual(is_credits_exit_position(markers, clock, 200), expected)

    def test_numeric_strings_case_insensitive_exact_name_and_duration_clipping(self) -> None:
        self.assertTrue(is_credits_exit_position({"CrEdItS": [{"start": " 1e2 ", "end": "250.5"}]}, 199, 200))
        self.assertTrue(is_credits_exit_position({"credits": [{"start": 5, "end": 20}]}, 0, 200))
        for name in ("end_credits", "credits_extra", " credits", "credit", "preview", "intro"):
            with self.subTest(name=name):
                self.assertFalse(is_credits_exit_position({name: [{"start": 100, "end": 200}]}, 110, 200))
        self.assertFalse(is_credits_exit_position({"credits": [{"start": 210, "end": 250}]}, 195, 200))

    def test_malformed_legacy_values_cannot_prove_completion(self) -> None:
        invalid = (None, True, False, "", "0x64", "Infinity", "NaN", float("nan"), float("inf"), [], {}, -1, 10**400)
        for value in invalid:
            with self.subTest(value=repr(value)[:40]):
                self.assertFalse(is_credits_exit_position({"credits": [{"start": value, "end": 200}]}, 110, 200))
                self.assertFalse(is_credits_exit_position({"credits": [{"start": 100, "end": value}]}, 110, 200))
        for markers in (None, [], "credits", {"credits": None}, {"credits": {}}, {"credits": [None, [], "100"]},
                        {"credits": [{"start": 100, "end": 100}]}, {"credits": [{"start": 110, "end": 100}]}):
            with self.subTest(markers=markers):
                self.assertFalse(is_credits_exit_position(markers, 110, 200))
        for duration in (None, 0, -1, True, float("inf"), float("nan")):
            self.assertFalse(is_credits_exit_position({"credits": [{"start": 100, "end": 200}]}, 110, duration))


if __name__ == "__main__":
    unittest.main()
