import unittest

from ui import ReimbursementApp


class MotionTests(unittest.TestCase):
    def setUp(self):
        self.app = ReimbursementApp()
        self.app.withdraw()
        self.app.update_idletasks()
        self.app.motion.enabled = True

    def tearDown(self):
        if self.app is not None:
            self.app.update_idletasks()
            self.app.destroy()

    def test_interrupted_motion_reaches_latest_target_and_destroy_cancels_it(self):
        panel = self.app.home_content
        self.app.motion.move(panel, 0, start=8, duration=120)
        self.app.after(32, lambda: self.app.motion.move(panel, 3, duration=100))
        self.app.after(240, self.app.quit)
        self.app.mainloop()
        self.assertEqual(int(panel.place_info()["y"]), 3)
        self.assertFalse(self.app.motion.jobs)
        self.app.motion.move(panel, 0, start=8)
        self.assertTrue(self.app.motion.jobs)
        motion = self.app.motion
        self.app.destroy()
        self.app = None
        self.assertFalse(motion.jobs)

    def test_keyboard_reduced_motion_and_navigation_finish_without_old_callbacks(self):
        panel = self.app.home_content
        for enabled, pointer in ((False, True), (True, False)):
            self.app.motion.enabled, self.app.motion.pointer = enabled, pointer
            self.app.motion.move(panel, 0, start=8)
            self.assertEqual(int(panel.place_info()["y"]), 0)
            self.assertFalse(self.app.motion.jobs)
        self.app.motion.enabled = self.app.motion.pointer = True
        self.app.new_application()
        self.app.show_home()
        self.app.motion.move(panel, 0, start=8)
        self.app.continue_home()
        self.assertFalse(self.app.home_visible)
        self.assertNotIn(panel, self.app.motion.jobs)
        self.assertEqual(int(panel.place_info()["y"]), 0)


if __name__ == "__main__":
    unittest.main()
