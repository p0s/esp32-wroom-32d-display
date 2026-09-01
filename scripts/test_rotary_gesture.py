import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RotaryGestureTests(unittest.TestCase):
    def test_direction_reversals_are_actions_without_breaking_scroll(self) -> None:
        compiler = shutil.which("c++")
        self.assertIsNotNone(compiler, "a host C++ compiler is required")
        source = textwrap.dedent(
            r"""
            #include <stdint.h>
            #include <cstdlib>
            #include <vector>
            #include "rotary_gesture.h"

            using Event = RotaryGestureDetector::Event;

            static void expect(bool condition) {
              if (!condition) std::abort();
            }

            int main() {
              RotaryGestureDetector rotary(550);
              std::vector<Event> seen;
              auto record = [&](Event event) { seen.push_back(event); };

              // A single detent becomes ordinary movement after the window.
              rotary.observe(1, 100, record);
              rotary.advanceTo(649, record);
              expect(seen.empty());
              rotary.advanceTo(650, record);
              expect(seen.size() == 1 && seen[0] == Event::Clockwise);

              // A right-left wiggle means forward/select without changing the
              // underlying position.
              seen.clear();
              rotary.observe(1, 1000, record);
              rotary.observe(-1, 1200, record);
              expect(seen.size() == 1 && seen[0] == Event::Forward);

              // A left-right wiggle means Back.
              seen.clear();
              rotary.observe(-1, 2000, record);
              rotary.observe(1, 2150, record);
              expect(seen.size() == 1 && seen[0] == Event::Back);

              // Two or more same-direction detents are ordinary movement and
              // enter immediate pass-through for the rest of the burst.
              seen.clear();
              rotary.observe(1, 3000, record);
              rotary.observe(1, 3050, record);
              rotary.observe(1, 3100, record);
              expect(seen.size() == 3);
              for (Event event : seen) expect(event == Event::Clockwise);
              rotary.advanceTo(3650, record);
              expect(!rotary.passThrough());

              // After the burst pause, a single opposite detent remains normal
              // movement rather than retroactively becoming an action.
              seen.clear();
              rotary.observe(-1, 3750, record);
              rotary.advanceTo(4300, record);
              expect(seen.size() == 1 && seen[0] == Event::CounterClockwise);

              // Unsigned time arithmetic remains valid across millis wrap.
              seen.clear();
              rotary.observe(1, UINT32_MAX - 100, record);
              rotary.observe(-1, 50, record);
              expect(seen.size() == 1 && seen[0] == Event::Forward);
              return 0;
            }
            """
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source_path = directory / "rotary_gesture_test.cpp"
            binary_path = directory / "rotary_gesture_test"
            source_path.write_text(source, encoding="utf-8")
            compile_result = subprocess.run(
                [
                    compiler,
                    "-std=c++11",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-I",
                    str(ROOT / "firmware" / "src"),
                    str(source_path),
                    "-o",
                    str(binary_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            run_result = subprocess.run(
                [str(binary_path)], check=False, capture_output=True, text=True
            )
            self.assertEqual(run_result.returncode, 0, run_result.stderr)


if __name__ == "__main__":
    unittest.main()
