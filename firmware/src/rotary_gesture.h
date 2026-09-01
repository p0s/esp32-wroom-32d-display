#pragma once

#include <stdint.h>

class RotaryGestureDetector {
 public:
  enum class Event : uint8_t {
    Clockwise,
    CounterClockwise,
    Forward,
    Back,
  };

  explicit RotaryGestureDetector(uint32_t windowMs) : windowMs_(windowMs) {}

  void reset() {
    direction_ = 0;
    pendingDetents_ = 0;
    lastDetentAt_ = 0;
    passThrough_ = false;
  }

  template <typename Handler>
  void observe(int direction, uint32_t atMs, Handler handler) {
    advanceTo(atMs, handler);
    if (!direction) return;
    const int8_t normalized = direction > 0 ? 1 : -1;
    if (passThrough_) {
      handler(moveEvent(normalized));
      lastDetentAt_ = atMs;
      return;
    }
    if (!pendingDetents_) {
      begin(normalized, atMs);
      return;
    }
    if (normalized != direction_) {
      handler(direction_ > 0 ? Event::Forward : Event::Back);
      clearPending();
      lastDetentAt_ = atMs;
      return;
    }
    ++pendingDetents_;
    lastDetentAt_ = atMs;
    emitMoves(handler);
    passThrough_ = true;
    lastDetentAt_ = atMs;
  }

  template <typename Handler>
  void advanceTo(uint32_t atMs, Handler handler) {
    if (passThrough_) {
      if (atMs - lastDetentAt_ >= windowMs_) passThrough_ = false;
      return;
    }
    if (!pendingDetents_ || atMs - lastDetentAt_ < windowMs_) return;
    emitMoves(handler);
  }

  uint8_t pendingDetents() const { return pendingDetents_; }
  bool passThrough() const { return passThrough_; }

 private:
  static Event moveEvent(int8_t direction) {
    return direction > 0 ? Event::Clockwise : Event::CounterClockwise;
  }

  void begin(int8_t direction, uint32_t atMs) {
    direction_ = direction;
    pendingDetents_ = 1;
    lastDetentAt_ = atMs;
  }

  void clearPending() {
    direction_ = 0;
    pendingDetents_ = 0;
  }

  template <typename Handler>
  void emitMoves(Handler handler) {
    const Event event = moveEvent(direction_);
    const uint8_t count = pendingDetents_;
    clearPending();
    for (uint8_t index = 0; index < count; ++index) handler(event);
  }

  const uint32_t windowMs_;
  int8_t direction_ = 0;
  uint8_t pendingDetents_ = 0;
  uint32_t lastDetentAt_ = 0;
  bool passThrough_ = false;
};
