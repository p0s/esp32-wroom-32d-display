import copy
import struct
import unittest

import screen_capture


def encode_bmp(pixels: bytearray) -> bytes:
    header = bytearray(screen_capture.BMP_HEADER_BYTES)
    header[:2] = b"BM"
    struct.pack_into("<I", header, 2, screen_capture.BMP_BYTES)
    struct.pack_into("<I", header, 10, screen_capture.BMP_HEADER_BYTES)
    struct.pack_into("<IiiHHII", header, 14, 40, 320, 240, 1, 4, 0, 38400)
    struct.pack_into("<I", header, 46, 16)
    rows = bytearray()
    for y in range(screen_capture.HEIGHT - 1, -1, -1):
        start = y * screen_capture.WIDTH
        for x in range(0, screen_capture.WIDTH, 2):
            rows.append((pixels[start + x] << 4) | pixels[start + x + 1])
    return bytes(header + rows)


def make_overview_bmp(remaining: int) -> bytes:
    pixels = bytearray(screen_capture.WIDTH * screen_capture.HEIGHT)
    for y in range(32):
        start = y * screen_capture.WIDTH
        pixels[start : start + screen_capture.WIDTH] = bytes([4]) * screen_capture.WIDTH
    filled = remaining * 280 // 100
    bar_start = 160 * screen_capture.WIDTH + 20
    pixels[bar_start : bar_start + filled] = bytes([5]) * filled
    pixels[bar_start + filled : bar_start + 280] = bytes([1]) * (280 - filled)
    return encode_bmp(pixels)


def make_menu_bmp() -> bytes:
    pixels = bytearray(screen_capture.WIDTH * screen_capture.HEIGHT)
    for y in range(7, 21):
        start = y * screen_capture.WIDTH + 18
        pixels[start : start + 96] = bytes([2]) * 96
    for y in range(34, 65):
        start = y * screen_capture.WIDTH + 18
        pixels[start : start + 284] = bytes([1]) * 284
        pixels[start : start + 5] = bytes([4]) * 5
    return encode_bmp(pixels)


class ScreenCaptureTests(unittest.TestCase):
    def test_presentation_key_ignores_age_but_tracks_rendered_state(self):
        state = {
            "bootId": 7,
            "page": "TIMER",
            "menu": False,
            "ageSeconds": 10,
            "screen": {"mode": "PAGE", "page": "TIMER"},
            "timer": {"remainingSeconds": 60, "running": True},
            "render": {
                "fullFrames": 2,
                "timerPartialUpdates": 3,
                "menuPartialUpdates": 0,
            },
        }
        later = copy.deepcopy(state)
        later["ageSeconds"] = 11
        self.assertEqual(
            screen_capture.presentation_key(state),
            screen_capture.presentation_key(later),
        )

        later["timer"]["remainingSeconds"] = 59
        later["render"]["timerPartialUpdates"] = 4
        self.assertNotEqual(
            screen_capture.presentation_key(state),
            screen_capture.presentation_key(later),
        )

    def test_validates_complete_overview_pixel_mirror(self):
        body = make_overview_bmp(48)
        state = {
            "product": "imDisplay",
            "page": "OVERVIEW",
            "menu": False,
            "valid": True,
            "remaining": 48,
            "display": {
                "width": 320,
                "height": 240,
                "pixels": 76800,
                "mirrorFormat": "BMP4",
                "mirrorBytes": 38400,
                "mirrorUnknownColors": 0,
            },
        }
        report = screen_capture.validate_bmp(body, state)
        self.assertEqual(report["pixels"], 76800)
        self.assertGreater(report["nonBackgroundPixels"], 5000)

    def test_rejects_quota_bar_that_disagrees_with_state(self):
        body = make_overview_bmp(48)
        state = {
            "product": "imDisplay",
            "page": "OVERVIEW",
            "menu": False,
            "valid": True,
            "remaining": 50,
            "display": {
                "width": 320,
                "height": 240,
                "pixels": 76800,
                "mirrorFormat": "BMP4",
                "mirrorBytes": 38400,
                "mirrorUnknownColors": 0,
            },
        }
        with self.assertRaisesRegex(RuntimeError, "quota bar"):
            screen_capture.validate_bmp(body, state)

    def test_validates_launcher_without_a_blue_title_bar(self):
        state = {
            "product": "imDisplay",
            "page": "OVERVIEW",
            "menu": True,
            "display": {
                "width": 320,
                "height": 240,
                "pixels": 76800,
                "mirrorFormat": "BMP4",
                "mirrorBytes": 38400,
                "mirrorUnknownColors": 0,
            },
        }
        report = screen_capture.validate_bmp(make_menu_bmp(), state)
        self.assertEqual(report["pixels"], 76800)


if __name__ == "__main__":
    unittest.main()
