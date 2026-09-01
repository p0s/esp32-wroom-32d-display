import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ENGINE = (ROOT / "firmware" / "src" / "sound_engine.h").read_text(
    encoding="utf-8"
)
MAIN = (ROOT / "firmware" / "src" / "main.cpp").read_text(encoding="utf-8")
LAN = (ROOT / "scripts" / "lan_control.py").read_text(encoding="utf-8")


class SoundContractTests(unittest.TestCase):
    def test_transport_matches_the_factory_pdm_constructor(self) -> None:
        self.assertIn("I2S_MODE_MASTER | I2S_MODE_TX | I2S_MODE_PDM", ENGINE)
        self.assertIn("I2S_CHANNEL_FMT_RIGHT_LEFT", ENGINE)
        self.assertIn("I2S_COMM_FORMAT_STAND_I2S", ENGINE)
        self.assertIn("I2S_DAC_CHANNEL_BOTH_EN", ENGINE)
        self.assertIn("config.sample_rate = 44100", ENGINE)
        self.assertIn("i2s_set_sample_rates(I2S_NUM_0, kSampleRate)", ENGINE)

    def test_pdm_uses_signed_stereo_pcm_and_zero_silence(self) -> None:
        self.assertIn("int16_t samples[kFramesPerChunk * 2]", ENGINE)
        self.assertIn("samples[i * 2] = static_cast<int16_t>(sample)", ENGINE)
        self.assertIn("int16_t samples[kFramesPerChunk * 2]{}", ENGINE)
        self.assertEqual(ENGINE.count("i2s_zero_dma_buffer(I2S_NUM_0)"), 1)

    def test_full_volume_is_audible_but_keeps_headroom(self) -> None:
        self.assertIn(
            "static_cast<int32_t>(volumePercent) * 300",
            ENGINE,
        )
        self.assertIn("tone(587, 180, event.volume)", ENGINE)
        self.assertIn("tone(880, 260, event.volume)", ENGINE)

    def test_state_and_matrix_require_complete_audio_writes(self) -> None:
        for field in (
            "queued",
            "rejected",
            "played",
            "bytesWritten",
            "writeFailures",
            "lastCue",
        ):
            self.assertIn(f'sound["{field}"]', MAIN)
        self.assertIn('changed_sound.get("played", 0)', LAN)
        self.assertIn('changed_sound.get("bytesWritten", 0)', LAN)
        self.assertIn('changed_sound.get("lastCue") != "PREVIEW"', LAN)


if __name__ == "__main__":
    unittest.main()
