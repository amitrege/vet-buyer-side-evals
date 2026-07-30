"""The demo deck must stay self-contained: every asset it names must exist,
and nothing may point at files this repo no longer ships."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DECK = ROOT / "index.html"


class DeckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = DECK.read_text()

    def test_every_referenced_clip_exists(self):
        clips = set(re.findall(r"clips/[\w.-]+\.wav", self.html))
        self.assertGreaterEqual(len(clips), 3)
        missing = sorted(c for c in clips if not (ROOT / c).is_file())
        self.assertEqual(missing, [])

    def test_no_references_to_removed_files(self):
        for gone in ("app.js", "styles.css", "demo.jsonl", "fixture.jsonl",
                     "hero_probe.json", "vet/ui"):
            self.assertNotIn(gone, self.html, f"deck still references {gone}")

    def test_no_external_network_dependencies(self):
        self.assertIsNone(re.search(r'(src|href)="https?://', self.html),
                          "deck must load nothing from the network")

    def test_slide_structure(self):
        slides = re.findall(r'<section class="slide[^"]*" data-k="([\w-]+)"', self.html)
        self.assertEqual(len(slides), 11)
        self.assertEqual(slides[0], "s1")
        self.assertEqual(slides[-1], "close")
        # the three multi-beat slides declare their step counts
        self.assertIn('data-k="s8" data-steps="2"', self.html)
        self.assertIn('data-k="s9" data-steps="2"', self.html)
        self.assertIn('data-k="s10" data-steps="2"', self.html)

    def test_no_em_dashes_in_slide_copy(self):
        deck_body = self.html.split("<main", 1)[1].split("</main>", 1)[0]
        self.assertNotIn("—", deck_body, "em dashes are banned from slide copy")

    def test_the_three_audio_moments_are_wired(self):
        self.assertIn('id="probeAudio" src="clips/p14e0d7a4.wav"', self.html)
        self.assertIn('data-audio="clips/demo_naive.wav"', self.html)
        self.assertIn('data-audio="clips/demo_stealth.wav"', self.html)


if __name__ == "__main__":
    unittest.main()
