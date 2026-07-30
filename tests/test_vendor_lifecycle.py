import unittest

from vet.race import Race
from vet.vendors import FLEET, fresh_fleet


class FreshFleetTests(unittest.TestCase):
    def test_each_race_gets_clean_vendor_state(self):
        first = fresh_fleet()
        first[0].eliminated_at = "probe-16"
        first[0].reason = "test elimination"
        first[0].spend = 1.25
        first[0].latencies.append(420)

        second = fresh_fleet()

        self.assertTrue(all(v.active for v in second))
        self.assertTrue(all(v.spend == 0.0 for v in second))
        self.assertTrue(all(v.latencies == [] for v in second))
        self.assertIsNot(first[0], second[0])
        self.assertIsNot(first[0].stack, second[0].stack)

    def test_race_mutation_does_not_touch_configuration(self):
        runtime = fresh_fleet()
        race = Race(runtime, {"s1": 1.0})
        race.vendors["groq"].eliminated_at = "probe-20"
        race.vendors["groq"].spend = 0.42

        configured_groq = next(v for v in FLEET if v.id == "groq")
        replacement_groq = next(v for v in fresh_fleet() if v.id == "groq")

        self.assertTrue(configured_groq.active)
        self.assertEqual(configured_groq.spend, 0.0)
        self.assertTrue(replacement_groq.active)
        self.assertEqual(replacement_groq.spend, 0.0)

    def test_act_three_fleets_are_independent_and_include_shady(self):
        naive = fresh_fleet(include_shady=True)
        stealth = fresh_fleet(include_shady=True)

        self.assertEqual([v.id for v in naive], [v.id for v in stealth])
        self.assertEqual(naive[-1].id, "shady")
        self.assertTrue(all(a is not b for a, b in zip(naive, stealth)))


if __name__ == "__main__":
    unittest.main()
