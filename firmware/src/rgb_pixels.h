#pragma once

#include <Arduino.h>
#include <driver/rmt.h>

struct RgbColor {
  uint8_t red;
  uint8_t green;
  uint8_t blue;
};

class RgbPixels {
 public:
  static constexpr size_t kCount = 4;

  bool begin(gpio_num_t pin, rmt_channel_t channel = RMT_CHANNEL_0) {
    channel_ = channel;
    rmt_config_t config = RMT_DEFAULT_CONFIG_TX(pin, channel_);
    config.clk_div = 2;  // 40 MHz RMT ticks (25 ns).
    config.tx_config.idle_output_en = true;
    config.tx_config.idle_level = RMT_IDLE_LEVEL_LOW;
    ready_ = rmt_config(&config) == ESP_OK && rmt_driver_install(channel_, 0, 0) == ESP_OK;
    return ready_;
  }

  bool show(const RgbColor (&colors)[kCount], uint8_t brightnessPercent) {
    if (!ready_) return false;
    brightnessPercent = min<uint8_t>(brightnessPercent, 100);
    rmt_item32_t items[kCount * 24];
    size_t item = 0;
    for (const RgbColor &color : colors) {
      // WS2812/SK6812 pixels consume bytes in GRB order.
      const uint8_t bytes[] = {
          scale(color.green, brightnessPercent), scale(color.red, brightnessPercent),
          scale(color.blue, brightnessPercent)};
      for (uint8_t value : bytes) {
        for (int bit = 7; bit >= 0; --bit) {
          const bool high = value & (1U << bit);
          items[item].level0 = 1;
          items[item].duration0 = high ? 28 : 14;  // 700 ns / 350 ns.
          items[item].level1 = 0;
          items[item].duration1 = high ? 24 : 32;  // 600 ns / 800 ns.
          ++item;
        }
      }
    }
    const bool written = rmt_write_items(channel_, items, item, true) == ESP_OK;
    delayMicroseconds(80);  // Latch/reset interval.
    return written;
  }

 private:
  static uint8_t scale(uint8_t value, uint8_t percent) {
    return static_cast<uint8_t>((static_cast<uint16_t>(value) * percent + 50) / 100);
  }

  rmt_channel_t channel_ = RMT_CHANNEL_0;
  bool ready_ = false;
};
