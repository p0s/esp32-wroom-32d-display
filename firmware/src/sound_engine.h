#pragma once

#include <Arduino.h>
#include <driver/i2s.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>

// The factory firmware configures I2S0 in internal PDM mode (mode 2 in
// ESP8266Audio) with eight DMA buffers. This engine keeps that proven hardware
// path and limits generated tones to a small fraction of full scale.
class SoundEngine {
 public:
  enum class Cue : uint8_t { Confirm, Start, Pause, Complete, Preview };

  bool begin() {
    events_ = xQueueCreate(4, sizeof(Event));
    if (!events_) return false;
    if (xTaskCreatePinnedToCore(taskEntry, "imdisplay-sound", 3072, this, 1, &task_, 0) !=
        pdPASS) {
      vQueueDelete(events_);
      events_ = nullptr;
      return false;
    }
    return true;
  }

  bool play(Cue cue, uint8_t volumePercent) {
    if (!events_ || volumePercent == 0) return false;
    const Event event{cue, min<uint8_t>(volumePercent, 100)};
    return xQueueSendToBack(events_, &event, 0) == pdTRUE;
  }

  bool ready() const { return events_ != nullptr; }
  bool driverReady() const { return driverReady_; }
  esp_err_t lastError() const { return lastError_; }

 private:
  struct Event {
    Cue cue;
    uint8_t volume;
  };

  static constexpr uint32_t kSampleRate = 16000;
  static constexpr size_t kFramesPerChunk = 96;

  static void taskEntry(void *context) {
    static_cast<SoundEngine *>(context)->taskLoop();
  }

  void taskLoop() {
    Event event{};
    while (true) {
      if (xQueueReceive(events_, &event, portMAX_DELAY) != pdTRUE) continue;
      if (!ensureDriver()) continue;
      playCue(event);
      writeSilence(28);
      i2s_zero_dma_buffer(I2S_NUM_0);
    }
  }

  bool ensureDriver() {
    if (driverReady_) return true;
    i2s_config_t config{};
    config.mode = static_cast<i2s_mode_t>(I2S_MODE_MASTER | I2S_MODE_TX | I2S_MODE_PDM);
    config.sample_rate = kSampleRate;
    config.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
    config.channel_format = I2S_CHANNEL_FMT_ALL_RIGHT;
    config.communication_format = I2S_COMM_FORMAT_STAND_MSB;
    config.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
    config.dma_buf_count = 8;
    config.dma_buf_len = 128;
    config.use_apll = false;
    config.tx_desc_auto_clear = true;
    config.fixed_mclk = 0;

    lastError_ = i2s_driver_install(I2S_NUM_0, &config, 0, nullptr);
    if (lastError_ != ESP_OK) return false;
    lastError_ = i2s_set_pin(I2S_NUM_0, nullptr);
    if (lastError_ == ESP_OK) lastError_ = i2s_set_dac_mode(I2S_DAC_CHANNEL_BOTH_EN);
    if (lastError_ != ESP_OK) {
      i2s_driver_uninstall(I2S_NUM_0);
      return false;
    }
    i2s_zero_dma_buffer(I2S_NUM_0);
    driverReady_ = true;
    return true;
  }

  void playCue(const Event &event) {
    switch (event.cue) {
      case Cue::Confirm:
        tone(659, 45, event.volume);
        writeSilence(18);
        tone(784, 60, event.volume);
        break;
      case Cue::Start:
        tone(523, 65, event.volume);
        writeSilence(20);
        tone(659, 95, event.volume);
        break;
      case Cue::Pause:
        tone(659, 55, event.volume);
        writeSilence(20);
        tone(523, 85, event.volume);
        break;
      case Cue::Complete:
        tone(523, 110, event.volume);
        writeSilence(45);
        tone(659, 110, event.volume);
        writeSilence(45);
        tone(784, 170, event.volume);
        break;
      case Cue::Preview:
        tone(587, 75, event.volume);
        writeSilence(30);
        tone(740, 85, event.volume);
        writeSilence(30);
        tone(880, 120, event.volume);
        break;
    }
  }

  void tone(uint16_t frequency, uint16_t durationMs, uint8_t volumePercent) {
    const uint32_t frameCount = static_cast<uint32_t>(durationMs) * kSampleRate / 1000;
    const uint32_t phaseStep =
        static_cast<uint32_t>((static_cast<uint64_t>(frequency) << 32) / kSampleRate);
    const int32_t amplitude = 300 + static_cast<int32_t>(volumePercent) * 35;
    const uint32_t envelopeFrames = min<uint32_t>(frameCount / 3, kSampleRate / 100);
    uint32_t phase = 0;
    uint32_t produced = 0;
    int16_t samples[kFramesPerChunk * 2];
    while (produced < frameCount) {
      const size_t frames = min<size_t>(kFramesPerChunk, frameCount - produced);
      for (size_t i = 0; i < frames; ++i) {
        const uint32_t position = produced + i;
        const uint16_t ramp = static_cast<uint16_t>(phase >> 16);
        const int32_t triangle =
            ramp < 32768 ? static_cast<int32_t>(ramp) * 2 - 32768
                         : 98303 - static_cast<int32_t>(ramp) * 2;
        uint32_t envelope = envelopeFrames;
        if (position < envelopeFrames) envelope = position;
        const uint32_t tail = frameCount - position - 1;
        if (tail < envelope) envelope = tail;
        const int32_t sample = static_cast<int32_t>(
            static_cast<int64_t>(triangle) * amplitude * envelope /
            (32768 * static_cast<int64_t>(max<uint32_t>(1, envelopeFrames))));
        samples[i * 2] = static_cast<int16_t>(sample);
        samples[i * 2 + 1] = static_cast<int16_t>(sample);
        phase += phaseStep;
      }
      if (!write(samples, frames * 2 * sizeof(int16_t))) return;
      produced += frames;
    }
  }

  void writeSilence(uint16_t durationMs) {
    const uint32_t frameCount = static_cast<uint32_t>(durationMs) * kSampleRate / 1000;
    int16_t samples[kFramesPerChunk * 2]{};
    uint32_t produced = 0;
    while (produced < frameCount) {
      const size_t frames = min<size_t>(kFramesPerChunk, frameCount - produced);
      if (!write(samples, frames * 2 * sizeof(int16_t))) return;
      produced += frames;
    }
  }

  bool write(const void *data, size_t bytes) {
    size_t written = 0;
    lastError_ = i2s_write(I2S_NUM_0, data, bytes, &written, portMAX_DELAY);
    if (lastError_ == ESP_OK && written != bytes) lastError_ = ESP_FAIL;
    return lastError_ == ESP_OK;
  }

  QueueHandle_t events_ = nullptr;
  TaskHandle_t task_ = nullptr;
  volatile bool driverReady_ = false;
  volatile esp_err_t lastError_ = ESP_OK;
};
