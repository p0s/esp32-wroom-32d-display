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

    def test_renderer_and_self_test_publish_the_dual_input_contract(self) -> None:
        self.assertNotIn("DOUBLE BACK", MAIN)
        self.assertNotIn("HOLD MENU", MAIN)
        self.assertIn(
            'footer("TURN SELECT PUSH/R-L OPEN HOLD/L-R CLOSE")', MAIN
        )
        self.assertIn('input["longPressThresholdMs"] = kButtonLongPressMs;', MAIN)
        self.assertIn('selfTest["schema"] = 4;', MAIN)
        self.assertIn('selfTest["longPressBack"]', MAIN)
        self.assertIn('selfTest["doubleClickFallback"]', MAIN)

    def test_working_push_button_remains_first_class(self) -> None:
        release = function_body("handlePhysicalClickRelease")
        self.assertIn("shortPress();", release)
        self.assertIn("navigateBack();", release)

        debounced = function_body("handleDebouncedButtonEvent")
        self.assertIn("handlePhysicalPress(atMs);", debounced)
        self.assertIn("handlePhysicalClickRelease(atMs);", debounced)
        self.assertIn("longPress();", debounced)

        rendered_ui = MAIN.split("void renderOverview()", 1)[1].split(
            "void encoderMoved(", 1
        )[0]
        self.assertIn("PUSH", rendered_ui)
        self.assertIn("HOLD", rendered_ui)
        self.assertIn("R-L", rendered_ui)
        self.assertIn("L-R", rendered_ui)

    def test_authenticated_state_exposes_side_effect_free_gpio_levels(self) -> None:
        self.assertIn(
            'input["gpioLevels0To31"] = static_cast<uint32_t>(GPIO.in);', MAIN
        )
        self.assertIn(
            'input["gpioLevels32To39"] = static_cast<uint32_t>(GPIO.in1.val & 0xffU);',
            MAIN,
        )
        diagnostic_block = MAIN.split('input["gpioLevels0To31"]', 1)[1].split(
            'input["settingsPendingMask"]', 1
        )[0]
        self.assertNotIn("pinMode(", diagnostic_block)

    def test_rotary_wiggles_map_to_forward_and_back(self) -> None:
        self.assertIn('#include "rotary_gesture.h"', MAIN)
        self.assertIn("constexpr uint32_t kRotaryGestureWindowMs = 550;", MAIN)
        self.assertIn(
            "shortPress(InputSource::RotaryGesture);",
            function_body("handleRotaryGestureEvent"),
        )
        self.assertIn(
            "longPress(InputSource::RotaryGesture);",
            function_body("handleRotaryGestureEvent"),
        )
        self.assertIn('"ROTARY_FORWARD"', MAIN)
        self.assertIn('"ROTARY_BACK"', MAIN)
        self.assertIn('input["rotaryForwardGestures"]', MAIN)
        self.assertIn('input["rotaryBackGestures"]', MAIN)

    def test_encoder_detents_preserve_order_for_reversal_gestures(self) -> None:
        self.assertIn("encoderDetentQueue[kEncoderDetentQueueCapacity]", MAIN)
        self.assertIn("encoderDetentQueue[encoderDetentHead].direction = direction;", MAIN)
        self.assertNotIn("pendingEncoderDetents", MAIN)
        self.assertIn('input["encoderCapture"] = "ordered-detent-queue";', MAIN)
        self.assertIn('input["encoderDetentQueueOverflows"]', MAIN)

    def test_rotation_only_action_matrix_has_no_dead_end(self) -> None:
        forward = function_body("shortPress")
        self.assertIn("if (menuOpen)", forward)
        self.assertIn("currentPage == Page::Timer", forward)
        self.assertIn("currentPage == Page::Applets", forward)
        self.assertIn("currentPage == Page::Leds", forward)
        self.assertIn("currentPage == Page::Display", forward)
        self.assertIn("currentPage == Page::Sounds", forward)
        self.assertIn(
            "openMenuFromInput(source == InputSource::RotaryGesture", forward
        )

        back = function_body("longPress")
        self.assertIn("toggleLauncherFromInput(method)", back)
        self.assertIn('"ROTARY_BACK"', back)

        self.assertIn('rightAligned(292, y + 12, "PUSH / R-L"', MAIN)
        for footer in (
            "PUSH/R-L MENU  HOLD/L-R BACK",
            "TURN SCROLL  PUSH/R-L MENU  HOLD/L-R BACK",
            "PUSH/R-L PAUSE  HOLD/L-R BACK",
            "TURN SELECT PUSH/R-L ACT HOLD/L-R BACK",
            "TURN CHANGE PUSH/R-L NEXT HOLD/L-R BACK",
            "TURN ADJUST PUSH/R-L/HOLD/L-R BACK",
            "TURN SELECT PUSH/R-L OPEN HOLD/L-R CLOSE",
        ):
            self.assertIn(f'"{footer}"', MAIN)

        footer_widths = [
            len(hint) * 6 - 1
            for hint in re.findall(r'footer\("([^"]+)"\)', MAIN)
        ]
        self.assertTrue(footer_widths)
        self.assertLessEqual(max(footer_widths), 288)


if __name__ == "__main__":
    unittest.main()
