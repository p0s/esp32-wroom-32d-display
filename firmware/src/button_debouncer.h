#pragma once

#include <stdint.h>

class ButtonDebouncer {
 public:
  enum class Event : uint8_t {
    Pressed,
    Released,
    LongPressed,
  };

  ButtonDebouncer(uint32_t debounceMs, uint32_t longPressMs)
      : debounceMs_(debounceMs), longPressMs_(longPressMs) {}

  void reset(bool down, uint32_t atMs) {
    rawDown_ = down;
    stableDown_ = down;
    longHandled_ = false;
    rawChangedAt_ = atMs;
    pressedAt_ = atMs;
  }

  template <typename Handler>
  void observe(bool down, uint32_t atMs, Handler handler) {
    advanceTo(atMs, handler);
    if (down != rawDown_) {
      rawDown_ = down;
      rawChangedAt_ = atMs;
    }
  }

  template <typename Handler>
  void advanceTo(uint32_t atMs, Handler handler) {
    if (rawDown_ != stableDown_ && atMs - rawChangedAt_ >= debounceMs_) {
      const uint32_t transitionAt = rawChangedAt_ + debounceMs_;
      serviceLongPress(transitionAt, handler);
      stableDown_ = rawDown_;
      if (stableDown_) {
        pressedAt_ = transitionAt;
        longHandled_ = false;
        handler(Event::Pressed, transitionAt);
      } else if (!longHandled_) {
        handler(Event::Released, transitionAt);
      }
    }
    serviceLongPress(atMs, handler);
  }

  bool rawDown() const { return rawDown_; }
  bool stableDown() const { return stableDown_; }
  bool longHandled() const { return longHandled_; }

 private:
  template <typename Handler>
  void serviceLongPress(uint32_t atMs, Handler handler) {
    if (stableDown_ && !longHandled_ && atMs - pressedAt_ >= longPressMs_) {
      longHandled_ = true;
      handler(Event::LongPressed, pressedAt_ + longPressMs_);
    }
  }

  const uint32_t debounceMs_;
  const uint32_t longPressMs_;
  bool rawDown_ = false;
  bool stableDown_ = false;
  bool longHandled_ = false;
  uint32_t rawChangedAt_ = 0;
  uint32_t pressedAt_ = 0;
};
