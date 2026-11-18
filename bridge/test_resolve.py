import unittest
import resolve


class Describe(unittest.TestCase):
    def test_click_and_fields(self):
        steps = resolve.compress([
            {"k": "click", "t": "Create Account"},
            {"k": "input", "t": "Email Address", "v": "me@x.com", "type": "text"},
            {"k": "input", "t": "Password", "v": "hunter2hunter2", "type": "password"},
            {"k": "input", "t": "Are you 18 or older?", "v": "Yes", "type": "select-one"},
            {"k": "input", "t": "Are you 18 or older?", "v": "No", "type": "select-one"},
            {"k": "nav", "t": "x.com/apply/step2"},
        ])
        self.assertEqual(steps[0], 'Clicked "Create Account"')
        self.assertIn("value not stored", steps[1])
        self.assertIn("value not stored", steps[2])
        self.assertEqual(steps[3], 'Set "Are you 18 or older?" to "No"')
        self.assertEqual(steps[4], "Went to x.com/apply/step2")

    def test_sensitive_label_never_stores_value(self):
        s = resolve.describe({"k": "input", "t": "Verification code", "v": "123456", "type": "text"})
        self.assertNotIn("123456", s)


if __name__ == "__main__":
    unittest.main()
