"""End-to-end smoke: a tiny exam synthesizes real audio, races the SIM fleet,
scores it, and writes a coherent event log. Needs macOS TTS (`say`)."""
import asyncio
import json
import os
import shutil
import tempfile
import unittest

from vet.compiler import ExamPlan, Stratum
from vet.exam import Emitter, act_race, memo, synthesize
from vet.score import check_entity, normalize, wer
from vet.vendors import fresh_fleet

HAS_TTS = bool(shutil.which("say") and shutil.which("afconvert"))

TINY_PLAN = ExamPlan(
    headline="smoke",
    decision_metric="entity error",
    strata=[
        Stratum("s1", "callbacks", "digits survive babble", "callback", "clinic_phone", 2, 0.6),
        Stratum("s2", "prescriptions", "drug names survive switching", "prescription", "clinic_quiet", 2, 0.4),
    ],
)


class ScoreTests(unittest.TestCase):
    def test_spoken_digits_normalise_to_one_number(self):
        hyp = "call me at three four six, five eight eight, five two five nine"
        self.assertTrue(check_entity("phone", "346-588-5259", "", hyp))
        self.assertFalse(check_entity("phone", "346-588-5250", "", hyp))

    def test_spanish_drug_folds_to_english(self):
        self.assertTrue(check_entity("drug", "ibuprofen", "ibuprofeno",
                                     "le recetaron ibuprofeno de 600"))
        self.assertIn("ibuprofen", normalize("ibuprofeno"))

    def test_wer_is_zero_on_identical_text_modulo_formatting(self):
        self.assertEqual(wer("Call me at 713-555-0142.", "call me at 713 555 0142"), 0.0)


@unittest.skipUnless(HAS_TTS, "needs macOS `say` + `afconvert`")
class ExamSmokeTests(unittest.TestCase):
    def test_tiny_exam_races_scores_and_logs(self):
        with tempfile.TemporaryDirectory() as td:
            em = Emitter("smoke", out_dir=td)
            try:
                async def drive():
                    probes = await synthesize(em, TINY_PLAN)
                    fleet = fresh_fleet()
                    race, done, saved = await act_race(em, TINY_PLAN, probes, fleet, "sim")
                    return probes, race
                probes, race = asyncio.run(drive())
            finally:
                em.close()

            self.assertEqual(len(probes), 4)
            for p in probes:
                self.assertTrue(os.path.exists(p.audio_path))
                self.assertGreater(p.duration_s, 1.0)
                self.assertTrue(p.entities)

            # every vendor was scored on every probe, and the estimates are sane
            self.assertEqual(set(race.stats), {v.id for v in fresh_fleet()})
            for s in race.stats.values():
                self.assertLessEqual(s["lo"], s["hi"])
                self.assertGreaterEqual(s["mean"], 0.0)
                self.assertLessEqual(s["mean"], 1.0)

            verdict = memo(TINY_PLAN, race, len(race.probes_run), 0, "sim")
            self.assertIn(verdict["winner"], race.stats)
            self.assertTrue(verdict["memo_md"].strip())

            with open(em.path) as fh:
                events = [json.loads(l) for l in fh]
            types = {e["type"] for e in events}
            for expected in ("synth", "vendor", "probe", "result", "standing", "cost"):
                self.assertIn(expected, types)
            seqs = [e["seq"] for e in events]
            self.assertEqual(seqs, sorted(seqs))


if __name__ == "__main__":
    unittest.main()
