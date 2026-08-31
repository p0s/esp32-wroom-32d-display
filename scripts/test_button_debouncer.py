import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ButtonDebouncerTests(unittest.TestCase):
    def test_edge_history_survives_blocked_main_loop_and_bounce(self) -> None:
        compiler = shutil.which("c++")
        self.assertIsNotNone(compiler, "a host C++ compiler is required")
        source = textwrap.dedent(
            r"""
            #include <stdint.h>
            #include <cstdlib>
            #include <utility>
            #include <vector>
            #include "button_debouncer.h"

            using Seen = std::vector<std::pair<ButtonDebouncer::Event, uint32_t>>;

            static void expect(bool condition) {
              if (!condition) std::abort();
            }

            int main() {
              ButtonDebouncer button(8, 1000);
              Seen seen;
              auto record = [&](ButtonDebouncer::Event event, uint32_t at) {
                seen.emplace_back(event, at);
              };

              // A whole 50 ms click occurs while the UI thread is blocked.
              button.reset(false, 0);
              button.observe(true, 100, record);
              button.observe(false, 150, record);
              button.advanceTo(200, record);
              expect(seen.size() == 2);
              expect(seen[0].first == ButtonDebouncer::Event::Pressed);
              expect(seen[0].second == 108);
              expect(seen[1].first == ButtonDebouncer::Event::Released);
              expect(seen[1].second == 158);

              // Contact bounce produces one logical press and release.
              seen.clear();
              button.reset(false, 1000);
              button.observe(true, 1010, record);
              button.observe(false, 1012, record);
              button.observe(true, 1014, record);
              button.advanceTo(1030, record);
              button.observe(false, 1060, record);
              button.advanceTo(1070, record);
              expect(seen.size() == 2);
              expect(seen[0].second == 1022);
              expect(seen[1].second == 1068);

              // A complete long press is classified even if servicing resumes
              // only after the physical release.
              seen.clear();
              button.reset(false, 0);
              button.observe(true, 10, record);
              button.observe(false, 1200, record);
              button.advanceTo(1300, record);
              expect(seen.size() == 2);
              expect(seen[0].first == ButtonDebouncer::Event::Pressed);
              expect(seen[1].first == ButtonDebouncer::Event::LongPressed);
              expect(seen[1].second == 1018);

              // Unsigned time arithmetic remains correct across millis wrap.
              seen.clear();
              button.reset(false, UINT32_MAX - 15);
              button.observe(true, UINT32_MAX - 7, record);
              button.advanceTo(5, record);
              expect(seen.size() == 1);
              expect(seen[0].first == ButtonDebouncer::Event::Pressed);
              expect(seen[0].second == 0);
              return 0;
            }
            """
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source_path = directory / "button_debouncer_test.cpp"
            binary_path = directory / "button_debouncer_test"
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
