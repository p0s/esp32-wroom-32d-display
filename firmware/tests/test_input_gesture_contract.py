import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "src" / "main.cpp").read_text(encoding="utf-8")


def function_body(name: str) -> str:
    match = re.search(
        rf"void {re.escape(name)}\([^)]*\) \{{(?P<body>.*?)\n\}}",
        MAIN,
        re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"missing production function: {name}")
    return match.group("body")


class InputGestureContractTests(unittest.TestCase):
    def test_hold_is_the_primary_global_back_gesture(self) -> None:
        self.assertIn("constexpr uint32_t kButtonLongPressMs = 800;", MAIN)
        long_press = function_body("longPress")
        self.assertIn("toggleLauncherFromInput", long_press)
        self.assertIn('"LONG_PRESS_BACK"', long_press)
        self.assertNotIn("openMenuFromInput", long_press)

        toggle = function_body("toggleLauncherFromInput")
        self.assertIn("if (menuOpen)", toggle)
        self.assertIn("menuOpen = false", toggle)
        self.assertIn("openMenu()", toggle)

    def test_short_click_and_encoder_paths_remain_independent(self) -> None:
        self.assertNotIn("longPress(", function_body("shortPress"))
        self.assertNotIn("longPress(", function_body("encoderMoved"))
        self.assertIn("openMenuFromInput", function_body("shortPress"))

    def test_renderer_and_self_test_publish_the_new_contract(self) -> None:
        self.assertNotIn("DOUBLE BACK", MAIN)
        self.assertNotIn("HOLD MENU", MAIN)
        self.assertIn('footer("TURN SELECT  CLICK OPEN  HOLD CLOSE")', MAIN)
        self.assertIn('input["longPressThresholdMs"] = kButtonLongPressMs;', MAIN)
        self.assertIn('selfTest["schema"] = 4;', MAIN)
        self.assertIn('selfTest["longPressBack"]', MAIN)
        self.assertIn('selfTest["doubleClickFallback"]', MAIN)


if __name__ == "__main__":
    unittest.main()
