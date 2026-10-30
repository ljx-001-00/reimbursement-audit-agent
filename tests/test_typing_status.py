import time
import unittest
from unittest.mock import patch

import assistant
from assistant import Reply
from ui import ReimbursementApp


class TypingStatusTests(unittest.TestCase):
    """The waiting state lives in the panel header, never as a three-dot bubble."""

    def setUp(self):
        self.app = ReimbursementApp()
        self.app.withdraw()
        self.app.update_idletasks()

    def tearDown(self):
        self.app.update_idletasks()
        self.app.destroy()

    def _pump(self, seconds=0.05):
        self.app.update()
        time.sleep(seconds)

    def test_waiting_status_lives_in_header_not_a_bubble(self):
        ball = self.app.assistant
        ball.open_chat()
        window = ball.window
        self.app.update()
        ball.chat_config = lambda: {"enabled": True, "base_url": "http://127.0.0.1:9",
                                    "model": "fake", "api_key": ""}
        quick = Reply(text="", level="none")  # no sources: used to start the typing dots
        answer = Reply(text="这是测试答复。", mode="local")

        def slow_ask(**_payload):
            time.sleep(0.4)
            return answer

        with patch.object(assistant, "instant_answer", return_value=quick), \
             patch.object(assistant, "ask", side_effect=slow_ask):
            window.send("你好")
            self._pump()
            # While waiting: no three-dot bubble row, the header carries the status.
            self.assertIsNone(window._typing_row)
            self.assertTrue(all(row.text != "…" and not row.typing for row in window.rows))
            self.assertEqual(window.mode_text, "财税助手 · 思考中…")
            deadline = time.time() + 10
            while window.sending and time.time() < deadline:
                self._pump(0.02)
            self.assertFalse(window.sending)
            self.assertFalse(window._thinking)
            self.assertTrue(window.mode_text.startswith("财税助手 · "))
            self.assertNotEqual(window.mode_text, "财税助手 · 思考中…")
            self.assertEqual(window.rows[-1].text, "这是测试答复。")


if __name__ == "__main__":
    unittest.main()
