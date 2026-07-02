import unittest

from uagents import Model

from uagents_trace.recorder import payload_summary


class Hello(Model):
    text: str
    count: int


class PayloadSummaryTests(unittest.TestCase):
    def test_text_field(self):
        self.assertEqual(payload_summary(Hello(text="Hi Bob!", count=1)), "Hi Bob!")

    def test_falls_back_to_json(self):
        class Empty(Model):
            pass

        summary = payload_summary(Empty())
        self.assertIn("Empty", summary)


if __name__ == "__main__":
    unittest.main()
