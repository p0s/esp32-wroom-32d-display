#include <Arduino.h>
#include <ArduinoJson.h>
#include <cmath>
#include <Preferences.h>
#include <SPI.h>
#include <Update.h>
#include <WebServer.h>
#include <WiFi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>
#include <mbedtls/sha256.h>
#include <soc/gpio_struct.h>

#include "button_debouncer.h"
#include "budget_pull_client.h"
#include "font5x7.h"
#include "rgb_pixels.h"
#include "rotary_gesture.h"
#include "sound_engine.h"

namespace {

constexpr int kTftDc = 15;
constexpr int kTftCs = 27;
constexpr int kTftSck = 14;
constexpr int kTftMosi = 12;
constexpr int kTftReset = 13;
constexpr int kTftBacklight = 4;
constexpr int kTftBacklightChannel = 0;
constexpr uint32_t kTftBacklightHz = 1000;
constexpr uint8_t kTftBacklightBits = 8;
constexpr uint8_t kMinDisplayBrightness = 10;
constexpr uint32_t kTftSpiHz = 50000000;
constexpr int kEncoderButton = 5;
constexpr int kEncoderA = 16;
constexpr int kEncoderB = 17;
constexpr gpio_num_t kRgbLedPin = GPIO_NUM_18;
constexpr int kEncoderEdgesPerDetent = 2;
constexpr uint32_t kButtonDebounceMs = 8;
constexpr uint32_t kButtonLongPressMs = 800;
constexpr uint32_t kDoubleClickMs = 550;
constexpr uint32_t kRotaryGestureWindowMs = 550;
constexpr uint8_t kEncoderDetentQueueCapacity = 32;
constexpr uint8_t kButtonEdgeQueueCapacity = 32;
constexpr uint32_t kButtonEncoderGuardMs = 40;
constexpr uint32_t kMenuDwellSelectMs = 1500;
constexpr uint32_t kSettingsCommitDelayMs = 600;
constexpr int kWidth = 320;
constexpr int kHeight = 240;
constexpr uint32_t kStaleAfterMs = 150000;
constexpr size_t kMaxBudgetPayloadBytes = 2047;
constexpr size_t kMaxBudgetPullConfigBytes = 511;
constexpr size_t kMaxWindows = 6;
constexpr uint32_t kMaxSourceAgeSeconds = 150;
constexpr uint32_t kBudgetPullTaskStackBytes = 12 * 1024;
constexpr size_t kMaxFirmwareBytes = 0x1f0000;
constexpr uint32_t kSerialOtaTimeoutMs = 30000;
constexpr uint32_t kStationReconnectMs = 15000;
constexpr char kWebUser[] = "xsure";
constexpr char kSerialOtaV1Prefix[] = "XSURE_OTA_V1 ";
constexpr char kSerialOtaV2Prefix[] = "XSURE_OTA_V2 ";

DRAM_ATTR const int8_t kEncoderTransitions[16] = {
    0, -1, 1, 0, 1, 0, 0, -1, -1, 0, 0, 1, 0, 1, -1, 0};

constexpr uint16_t rgb565(uint8_t r, uint8_t g, uint8_t b) {
  return ((r & 0xf8) << 8) | ((g & 0xfc) << 3) | (b >> 3);
}

constexpr uint16_t kBlack = rgb565(5, 8, 14);
constexpr uint16_t kPanel = rgb565(15, 23, 38);
constexpr uint16_t kWhite = rgb565(240, 246, 255);
constexpr uint16_t kMuted = rgb565(132, 148, 170);
constexpr uint16_t kBlue = rgb565(49, 130, 246);
constexpr uint16_t kGreen = rgb565(45, 211, 128);
constexpr uint16_t kAmber = rgb565(251, 191, 36);
constexpr uint16_t kRed = rgb565(248, 113, 113);
constexpr size_t kMirrorRowBytes = kWidth / 2;
constexpr size_t kMirrorBytes = kMirrorRowBytes * kHeight;
constexpr size_t kBmpHeaderBytes = 14 + 40 + 16 * 4;
constexpr uint16_t kMirrorPalette[16] = {
    kBlack, kPanel, kWhite, kMuted, kBlue, kGreen, kAmber, kRed,
    kBlack, kBlack, kBlack, kBlack, kBlack, kBlack, kBlack, rgb565(255, 0, 255),
};

struct LedPreset {
  const char *name;
  RgbColor colors[RgbPixels::kCount];
};

struct SoundProfile {
  const char *name;
  bool confirmations;
};

constexpr LedPreset kLedPresets[] = {
    {"OFF", {{0, 0, 0}, {0, 0, 0}, {0, 0, 0}, {0, 0, 0}}},
    {"CODEX", {{49, 130, 246}, {49, 130, 246}, {49, 130, 246}, {49, 130, 246}}},
    {"FOCUS", {{45, 211, 128}, {45, 211, 128}, {45, 211, 128}, {45, 211, 128}}},
    {"WARM", {{251, 191, 36}, {251, 191, 36}, {251, 191, 36}, {251, 191, 36}}},
    {"ALERT", {{248, 113, 113}, {248, 113, 113}, {248, 113, 113}, {248, 113, 113}}},
    {"RAINBOW", {{49, 130, 246}, {45, 211, 128}, {251, 191, 36}, {248, 113, 113}}},
};

constexpr size_t kLedPresetCount = sizeof(kLedPresets) / sizeof(kLedPresets[0]);
constexpr SoundProfile kSoundProfiles[] = {
    {"MUTE", false},
    {"MINIMAL", false},
    {"SOFT", true},
};
constexpr size_t kSoundProfileCount = sizeof(kSoundProfiles) / sizeof(kSoundProfiles[0]);

const char *soundCueName(uint8_t cue) {
  switch (static_cast<SoundEngine::Cue>(cue)) {
    case SoundEngine::Cue::Confirm: return "CONFIRM";
    case SoundEngine::Cue::Start: return "START";
    case SoundEngine::Cue::Pause: return "PAUSE";
    case SoundEngine::Cue::Complete: return "COMPLETE";
    case SoundEngine::Cue::Preview: return "PREVIEW";
  }
  return "NONE";
}

class St7789 {
 public:
  void begin() {
    // Factory firmware uses LEDC channel 0 at 1 kHz on GPIO 4. Without this
    // separate enable path, the controller can render while the IPS panel
    // remains visibly black.
    const uint32_t backlightHz =
        ledcSetup(kTftBacklightChannel, kTftBacklightHz, kTftBacklightBits);
    ledcAttachPin(kTftBacklight, kTftBacklightChannel);
    setBacklightPercent(backlightPercent_);
    Serial.printf("display backlight=gpio%d channel=%d hz=%u ready=%d\n",
                  kTftBacklight, kTftBacklightChannel, backlightHz,
                  backlightHz == kTftBacklightHz);

    pinMode(kTftDc, OUTPUT);
    pinMode(kTftCs, OUTPUT);
    pinMode(kTftReset, OUTPUT);
    digitalWrite(kTftCs, HIGH);
    digitalWrite(kTftReset, HIGH);
    SPI.begin(kTftSck, -1, kTftMosi, kTftCs);

    // Match the factory Arduino_GFX ST7789 reset path exactly. This panel also
    // requires SPI mode 3; mode 0 can leave it visibly black.
    delay(100);
    digitalWrite(kTftReset, LOW);
    delay(120);
    digitalWrite(kTftReset, HIGH);
    delay(120);

    command(0x01);  // Software reset (factory Arduino_GFX startup path)
    delay(120);
    command(0x11);  // Sleep out
    delay(120);
    dataCommand(0x3a, {0x55});
    dataCommand(0x36, {0x00});  // Factory initialization-table value
    dataCommand(0xb2, {0x0c, 0x0c, 0x00, 0x33, 0x33});
    dataCommand(0xb7, {0x35});
    dataCommand(0xbb, {0x19});
    dataCommand(0xc0, {0x2c});
    dataCommand(0xc2, {0x01});
    dataCommand(0xc3, {0x12});
    dataCommand(0xc4, {0x20});
    dataCommand(0xc6, {0x0f});
    dataCommand(0xd0, {0xa4, 0xa1});
    dataCommand(0xe0, {0xf0, 0x09, 0x13, 0x12, 0x12, 0x2b, 0x3c, 0x44,
                       0x4b, 0x1b, 0x18, 0x17, 0x1d, 0x21});
    dataCommand(0xe1, {0xf0, 0x09, 0x13, 0x0c, 0x0d, 0x27, 0x3b, 0x44,
                       0x4d, 0x0b, 0x17, 0x17, 0x1d, 0x21});
    command(0x21);  // Inversion on (IPS)
    // Rotate once counterclockwise from the factory portrait orientation.
    dataCommand(0x36, {0x60});  // Landscape: MV | MX
    command(0x13);  // Normal display
    delay(10);
    command(0x29);  // Display on
    delay(120);
    fillScreen(kBlack);
  }

  void setBacklightPercent(uint8_t percent) {
    backlightPercent_ = min<uint8_t>(100, max<uint8_t>(kMinDisplayBrightness, percent));
    backlightDuty_ = static_cast<uint8_t>(
        (static_cast<uint16_t>(backlightPercent_) * 255 + 50) / 100);
    ledcWrite(kTftBacklightChannel, backlightDuty_);
  }

  uint8_t backlightPercent() const { return backlightPercent_; }
  uint8_t backlightDuty() const { return backlightDuty_; }
  size_t mirrorBytes() const { return sizeof(mirror_); }
  uint32_t mirrorUnknownColors() const { return mirrorUnknownColors_; }
  size_t bmpBytes() const { return kBmpHeaderBytes + sizeof(mirror_); }

  void beginFrame() { frameBuffered_ = true; }

  void endFrame() {
    if (!frameBuffered_) return;
    frameBuffered_ = false;

    uint8_t bytes[128];
    size_t byteCount = 0;
    beginWrite();
    setWindowInTransaction(0, 0, kWidth - 1, kHeight - 1);
    digitalWrite(kTftDc, HIGH);
    for (const uint8_t packed : mirror_) {
      const uint16_t first = kMirrorPalette[packed >> 4];
      const uint16_t second = kMirrorPalette[packed & 0x0f];
      bytes[byteCount++] = first >> 8;
      bytes[byteCount++] = first & 0xff;
      bytes[byteCount++] = second >> 8;
      bytes[byteCount++] = second & 0xff;
      if (byteCount == sizeof(bytes)) {
        SPI.writeBytes(bytes, byteCount);
        byteCount = 0;
      }
    }
    if (byteCount) SPI.writeBytes(bytes, byteCount);
    endWrite();
  }

  bool writeBmp(WiFiClient client) const {
    uint8_t header[kBmpHeaderBytes]{};
    header[0] = 'B';
    header[1] = 'M';
    putLe32(header + 2, bmpBytes());
    putLe32(header + 10, kBmpHeaderBytes);
    putLe32(header + 14, 40);
    putLe32(header + 18, kWidth);
    putLe32(header + 22, kHeight);
    putLe16(header + 26, 1);
    putLe16(header + 28, 4);
    putLe32(header + 34, sizeof(mirror_));
    putLe32(header + 38, 2835);
    putLe32(header + 42, 2835);
    putLe32(header + 46, 16);
    for (size_t i = 0; i < 16; ++i) {
      const uint16_t color = kMirrorPalette[i];
      header[54 + i * 4] = expand5(color & 0x1f);
      header[55 + i * 4] = expand6((color >> 5) & 0x3f);
      header[56 + i * 4] = expand5((color >> 11) & 0x1f);
    }
    if (!writeAll(client, header, sizeof(header))) return false;
    for (int y = kHeight - 1; y >= 0; --y) {
      const uint8_t *row = mirror_ + static_cast<size_t>(y) * kMirrorRowBytes;
      if (!writeAll(client, row, kMirrorRowBytes)) return false;
    }
    return true;
  }

  void fillScreen(uint16_t color) { fillRect(0, 0, kWidth, kHeight, color); }

  void fillRect(int x, int y, int w, int h, uint16_t color) {
    if (x < 0) { w += x; x = 0; }
    if (y < 0) { h += y; y = 0; }
    if (x + w > kWidth) w = kWidth - x;
    if (y + h > kHeight) h = kHeight - y;
    if (w <= 0 || h <= 0) return;
    mirrorFillRect(x, y, w, h, color);
    if (frameBuffered_) return;
    uint8_t bytes[128];
    for (size_t i = 0; i < sizeof(bytes); i += 2) {
      bytes[i] = color >> 8;
      bytes[i + 1] = color & 0xff;
    }
    size_t remaining = static_cast<size_t>(w) * h * 2;
    beginWrite();
    setWindowInTransaction(x, y, x + w - 1, y + h - 1);
    digitalWrite(kTftDc, HIGH);
    while (remaining) {
      const size_t count = min(remaining, sizeof(bytes));
      SPI.writeBytes(bytes, count);
      remaining -= count;
    }
    endWrite();
  }

  void text(int x, int y, const char *value, uint16_t color, uint8_t scale = 2) {
    while (*value) {
      drawChar(x, y, *value++, color, scale);
      x += 6 * scale;
    }
  }

  void centered(int y, const char *value, uint16_t color, uint8_t scale = 2) {
    const int width = strlen(value) * 6 * scale - scale;
    text(max(0, (kWidth - width) / 2), y, value, color, scale);
  }

 private:
  uint8_t backlightPercent_ = 100;
  uint8_t backlightDuty_ = 255;
  uint8_t mirror_[kMirrorBytes]{};
  uint32_t mirrorUnknownColors_ = 0;
  bool frameBuffered_ = false;

  static void putLe16(uint8_t *target, uint16_t value) {
    target[0] = value & 0xff;
    target[1] = value >> 8;
  }

  static void putLe32(uint8_t *target, uint32_t value) {
    target[0] = value & 0xff;
    target[1] = (value >> 8) & 0xff;
    target[2] = (value >> 16) & 0xff;
    target[3] = value >> 24;
  }

  static uint8_t expand5(uint8_t value) { return (value << 3) | (value >> 2); }
  static uint8_t expand6(uint8_t value) { return (value << 2) | (value >> 4); }

  static bool writeAll(WiFiClient &client, const uint8_t *bytes, size_t count) {
    while (count) {
      const size_t written = client.write(bytes, count);
      if (!written) return false;
      bytes += written;
      count -= written;
    }
    return true;
  }

  uint8_t mirrorPaletteIndex(uint16_t color) {
    for (uint8_t i = 0; i < 8; ++i) {
      if (kMirrorPalette[i] == color) return i;
    }
    ++mirrorUnknownColors_;
    return 15;
  }

  void mirrorFillRect(int x, int y, int w, int h, uint16_t color) {
    const uint8_t index = mirrorPaletteIndex(color);
    const uint8_t packed = static_cast<uint8_t>((index << 4) | index);
    const int right = x + w;
    for (int rowIndex = y; rowIndex < y + h; ++rowIndex) {
      uint8_t *row = mirror_ + static_cast<size_t>(rowIndex) * kMirrorRowBytes;
      int column = x;
      if (column & 1) {
        row[column / 2] = static_cast<uint8_t>((row[column / 2] & 0xf0) | index);
        ++column;
      }
      const int pairs = (right - column) / 2;
      if (pairs > 0) {
        memset(row + column / 2, packed, pairs);
        column += pairs * 2;
      }
      if (column < right) {
        row[column / 2] =
            static_cast<uint8_t>((index << 4) | (row[column / 2] & 0x0f));
      }
    }
  }

  void beginWrite() {
    SPI.beginTransaction(SPISettings(kTftSpiHz, MSBFIRST, SPI_MODE3));
    digitalWrite(kTftCs, LOW);
  }

  void endWrite() {
    digitalWrite(kTftCs, HIGH);
    SPI.endTransaction();
  }

  void writeCommand(uint8_t value) {
    digitalWrite(kTftDc, LOW);
    SPI.write(value);
  }

  void writeData(const uint8_t *bytes, size_t count) {
    digitalWrite(kTftDc, HIGH);
    SPI.writeBytes(const_cast<uint8_t *>(bytes), count);
  }

  void command(uint8_t value) {
    beginWrite();
    writeCommand(value);
    endWrite();
  }

  void dataCommand(uint8_t cmd, std::initializer_list<uint8_t> bytes) {
    beginWrite();
    writeCommand(cmd);
    writeData(bytes.begin(), bytes.size());
    endWrite();
  }

  void setWindowInTransaction(int x0, int y0, int x1, int y1) {
    const uint8_t columns[] = {static_cast<uint8_t>(x0 >> 8), static_cast<uint8_t>(x0),
                               static_cast<uint8_t>(x1 >> 8), static_cast<uint8_t>(x1)};
    const uint8_t rows[] = {static_cast<uint8_t>(y0 >> 8), static_cast<uint8_t>(y0),
                            static_cast<uint8_t>(y1 >> 8), static_cast<uint8_t>(y1)};
    writeCommand(0x2a);
    writeData(columns, sizeof(columns));
    writeCommand(0x2b);
    writeData(rows, sizeof(rows));
    writeCommand(0x2c);
  }

  void drawChar(int x, int y, char value, uint16_t color, uint8_t scale) {
    uint8_t columns[5];
    loadGlyph(value, columns);
    for (uint8_t row = 0; row < 7; ++row) {
      uint8_t runStart = 0;
      bool inRun = false;
      for (uint8_t column = 0; column <= 5; ++column) {
        const bool filled = column < 5 && (columns[column] & (1 << row));
        if (filled && !inRun) {
          runStart = column;
          inRun = true;
        } else if (!filled && inRun) {
          fillRect(x + runStart * scale, y + row * scale,
                   (column - runStart) * scale, scale, color);
          inRun = false;
        }
      }
    }
  }
};

struct BudgetWindow {
  char id[56]{};
  char label[32]{};
  char window[20]{};
  char resetText[32]{};
  float remaining = 0;
};

struct BudgetState {
  bool received = false;
  bool valid = false;
  uint32_t receivedAt = 0;
  char checkedText[32]{};
  char error[32] = "NO DATA";
  BudgetWindow windows[kMaxWindows];
  size_t count = 0;
};

struct BudgetPullSettings {
  bool configured = false;
  bool enabled = false;
  bool legacyPushEnabled = false;
  imdisplay::budget_pull::PullRequest request;
};

struct SerialOtaState {
  bool active = false;
  size_t expected = 0;
  size_t received = 0;
  size_t chunkSize = 0;
  size_t chunkReceived = 0;
  uint32_t lastByteAt = 0;
  char expectedSha256[65]{};
  mbedtls_sha256_context sha256;
};

struct HttpOtaState {
  bool attempted = false;
  bool active = false;
  bool hashInitialized = false;
  bool failed = false;
  uint16_t failureStatus = 500;
  size_t expected = 0;
  size_t received = 0;
  char expectedSha256[65]{};
  char failure[64]{};
  mbedtls_sha256_context sha256;
};

enum class Page : uint8_t {
  Overview,
  Windows,
  Timer,
  Applets,
  Leds,
  Display,
  Sounds,
  Connection,
  RecoveryWifiToggle,
  About
};

constexpr size_t kMaxMenuEntries = 10;

struct MenuEntry {
  const char *label;
  Page page;
};

struct WorkTimerState {
  uint32_t durationSeconds = 25 * 60;
  uint32_t remainingSeconds = 25 * 60;
  uint32_t startedAt = 0;
  bool running = false;
};

St7789 display;
RgbPixels rgbPixels;
SoundEngine soundEngine;
BudgetState budget;
BudgetPullSettings budgetPullSettings;
WorkTimerState workTimer;
WebServer webServer(80);
Preferences preferences;
Page currentPage = Page::Overview;
bool menuOpen = false;
int menuIndex = 0;
int windowIndex = 0;
int appletIndex = 0;
int ledField = 0;
int soundField = 0;
bool codexAppletInstalled = true;
bool timerAppletInstalled = true;
bool rgbPixelsReady = false;
bool soundTaskReady = false;
uint8_t ledPresetIndex = 3;
uint8_t ledBrightness = 20;
bool ledFeedbackEnabled = true;
uint32_t ledFeedbackUntil = 0;
uint8_t soundProfileIndex = 2;
uint8_t soundVolume = 20;
uint8_t displayBrightness = 100;
uint32_t timerRenderedSecond = UINT32_MAX;
uint32_t fullRenderCount = 0;
uint32_t lastFullRenderAt = 0;
uint32_t lastFullRenderDurationUs = 0;
uint32_t maxFullRenderDurationUs = 0;
uint32_t timerPartialRenderCount = 0;
uint32_t menuPartialRenderCount = 0;
portMUX_TYPE encoderMux = portMUX_INITIALIZER_UNLOCKED;
volatile uint8_t encoderPreviousRaw = 0;
volatile int16_t encoderAccumulatorRaw = 0;
struct EncoderDetent {
  uint32_t atMs;
  int8_t direction;
};
volatile EncoderDetent encoderDetentQueue[kEncoderDetentQueueCapacity];
volatile uint8_t encoderDetentHead = 0;
volatile uint8_t encoderDetentTail = 0;
volatile uint32_t encoderRawDetentCount = 0;
volatile uint32_t encoderDetentOverflowCount = 0;
struct ButtonEdge {
  uint32_t atMs;
  bool down;
};
portMUX_TYPE buttonMux = portMUX_INITIALIZER_UNLOCKED;
volatile ButtonEdge buttonEdgeQueue[kButtonEdgeQueueCapacity];
volatile uint8_t buttonEdgeHead = 0;
volatile uint8_t buttonEdgeTail = 0;
volatile uint32_t buttonRawEdgeCount = 0;
volatile uint32_t buttonRawFallCount = 0;
volatile uint32_t buttonRawRiseCount = 0;
volatile uint32_t buttonEdgeOverflowCount = 0;
volatile uint32_t lastButtonRawEdgeAt = 0;
ButtonDebouncer buttonDebouncer(kButtonDebounceMs, kButtonLongPressMs);
RotaryGestureDetector rotaryGesture(kRotaryGestureWindowMs);
bool buttonLongHandled = false;
uint32_t buttonPressedAt = 0;
uint32_t lastButtonPressDurationMs = 0;
uint32_t suppressedEncoderDetentCount = 0;
bool secondClickInProgress = false;
uint16_t dirtySettings = 0;
uint32_t settingsChangedAt = 0;
uint32_t settingsPersistSessions = 0;
uint32_t settingsKeyWriteAttempts = 0;
uint32_t settingsPersistFailures = 0;
uint16_t lastPersistedSettings = 0;
bool needsRender = true;
bool otaInProgress = false;
bool otaSucceeded = false;
bool accessPointReady = false;
bool recoveryAccessPointEnabled = false;
bool accessPointChangePending = false;
uint32_t accessPointChangeAt = 0;
bool stationConfigured = false;
bool stationWasConnected = false;
bool budgetPullInFlight = false;
bool budgetPullWorkerReady = false;
uint32_t restartAt = 0;
uint32_t bootId = 0;
uint32_t lastStationReconnectAt = 0;
uint32_t budgetPullAttempts = 0;
uint32_t budgetPullSuccesses = 0;
uint32_t budgetPullFailures = 0;
uint32_t budgetPullLastResultAt = 0;
uint32_t encoderEventCount = 0;
uint32_t shortPressCount = 0;
uint32_t longPressCount = 0;
uint32_t buttonReleaseCount = 0;
uint32_t doubleClickCount = 0;
bool singleClickPending = false;
uint32_t singleClickPendingAt = 0;
uint32_t secondClickLatchCount = 0;
uint32_t navigationTransitionCount = 0;
const char *lastNavigationMethod = "NONE";
Page lastNavigationPage = Page::Overview;
bool lastNavigationFromMenu = false;
bool lastNavigationToMenu = false;
uint32_t remoteInputCount = 0;
uint32_t dwellSelectionCount = 0;
uint32_t rotaryForwardGestureCount = 0;
uint32_t rotaryBackGestureCount = 0;
uint32_t inputSelfTestRuns = 0;
uint32_t inputSelfTestAt = 0;
bool inputSelfTestPassed = false;
bool inputSelfTestNavigationPassed = false;
bool inputSelfTestPageToLauncher = false;
bool inputSelfTestLauncherToPage = false;
bool inputSelfTestNestedBack = false;
bool inputSelfTestLongPressBack = false;
bool inputSelfTestDoubleClickFallback = false;
bool inputSelfTestSecondPressGrace = false;
bool inputSelfTestQueuedPulse = false;
bool inputSelfTestQueueHealthy = false;
bool inputSelfTestRestored = false;
uint8_t inputSelfTestPageRoundTrips = 0;
uint8_t inputSelfTestBackActions = 0;
const char *inputSelfTestFailure = "NOT_RUN";
const char *inputSelfTestFailurePage = "NONE";
int lastEncoderDirection = 0;
const char *lastRemoteAction = "NONE";
uint32_t menuSelectionChangedAt = 0;
bool menuDwellArmed = false;
char serialLine[2048];
size_t serialLength = 0;
char accessPointName[32]{};
char accessPointPassword[20]{};
char budgetPullLastResult[32] = "not-configured";
SerialOtaState serialOta;
HttpOtaState httpOta;
imdisplay::budget_pull::PullSchedule budgetPullSchedule;
QueueHandle_t budgetPullRequestQueue = nullptr;
QueueHandle_t budgetPullResultQueue = nullptr;
TaskHandle_t budgetPullTaskHandle = nullptr;

bool validSha256(const char *value);
void requestBudgetPull();

constexpr uint8_t kDigitSegments[10] = {
    0x3f, 0x06, 0x5b, 0x4f, 0x66, 0x6d, 0x7d, 0x07, 0x7f, 0x6f};

void drawSevenSegmentDigit(int x, int y, int width, int height, int thickness,
                           int digit, uint16_t color) {
  if (digit < 0 || digit > 9) return;
  const uint8_t segments = kDigitSegments[digit];
  const int horizontalWidth = width - thickness * 2;
  const int verticalHeight = (height - thickness * 3) / 2;
  const int middleY = y + (height - thickness) / 2;
  if (segments & (1 << 0)) {
    display.fillRect(x + thickness, y, horizontalWidth, thickness, color);
  }
  if (segments & (1 << 1)) {
    display.fillRect(x + width - thickness, y + thickness, thickness,
                     verticalHeight, color);
  }
  if (segments & (1 << 2)) {
    display.fillRect(x + width - thickness, middleY + thickness, thickness,
                     verticalHeight, color);
  }
  if (segments & (1 << 3)) {
    display.fillRect(x + thickness, y + height - thickness, horizontalWidth,
                     thickness, color);
  }
  if (segments & (1 << 4)) {
    display.fillRect(x, middleY + thickness, thickness, verticalHeight, color);
  }
  if (segments & (1 << 5)) {
    display.fillRect(x, y + thickness, thickness, verticalHeight, color);
  }
  if (segments & (1 << 6)) {
    display.fillRect(x + thickness, middleY, horizontalWidth, thickness, color);
  }
}

void drawPercentRing(int x, int y, int size, int thickness, uint16_t color) {
  display.fillRect(x, y, size, thickness, color);
  display.fillRect(x, y + size - thickness, size, thickness, color);
  display.fillRect(x, y + thickness, thickness, size - thickness * 2, color);
  display.fillRect(x + size - thickness, y + thickness, thickness,
                   size - thickness * 2, color);
}

void drawPercentGlyph(int x, int y, int width, int height, uint16_t color) {
  constexpr int ring = 16;
  constexpr int ringThickness = 4;
  constexpr int inset = 8;
  drawPercentRing(x, y + inset, ring, ringThickness, color);
  drawPercentRing(x + width - ring, y + height - ring - inset, ring,
                  ringThickness, color);

  const int slashTop = y + 20;
  const int slashHeight = height - 40;
  for (int row = 0; row < slashHeight; row += 2) {
    const int slashX = x + width - 9 -
                       (row * (width - 18) / max(1, slashHeight - 1));
    display.fillRect(slashX - 2, slashTop + row, 5, 2, color);
  }
}

void drawPercentValue(int value, int y, uint16_t color) {
  value = constrain(value, 0, 100);
  char digits[4];
  snprintf(digits, sizeof(digits), "%d", value);
  constexpr int digitWidth = 56;
  constexpr int digitHeight = 107;
  constexpr int thickness = 9;
  constexpr int gap = 8;
  constexpr int percentWidth = 44;
  const int count = strlen(digits);
  const int width = count * digitWidth + (count - 1) * gap + gap + percentWidth;
  int x = (kWidth - width) / 2;
  for (int i = 0; i < count; ++i) {
    drawSevenSegmentDigit(x, y, digitWidth, digitHeight, thickness,
                          digits[i] - '0', color);
    x += digitWidth + gap;
  }
  drawPercentGlyph(x, y, percentWidth, digitHeight, color);
}

void drawTimerValue(uint32_t seconds, int y, uint16_t color) {
  const uint32_t minutes = min<uint32_t>(99, seconds / 60);
  const int digits[] = {static_cast<int>(minutes / 10),
                        static_cast<int>(minutes % 10),
                        static_cast<int>((seconds % 60) / 10),
                        static_cast<int>(seconds % 10)};
  constexpr int digitWidth = 24;
  constexpr int digitHeight = 44;
  constexpr int thickness = 4;
  constexpr int gap = 4;
  constexpr int colonWidth = 8;
  constexpr int totalWidth = digitWidth * 4 + gap * 4 + colonWidth;
  int x = (kWidth - totalWidth) / 2;
  drawSevenSegmentDigit(x, y, digitWidth, digitHeight, thickness, digits[0], color);
  x += digitWidth + gap;
  drawSevenSegmentDigit(x, y, digitWidth, digitHeight, thickness, digits[1], color);
  x += digitWidth + gap;
  display.fillRect(x + 2, y + 12, thickness, thickness, color);
  display.fillRect(x + 2, y + 29, thickness, thickness, color);
  x += colonWidth + gap;
  drawSevenSegmentDigit(x, y, digitWidth, digitHeight, thickness, digits[2], color);
  x += digitWidth + gap;
  drawSevenSegmentDigit(x, y, digitWidth, digitHeight, thickness, digits[3], color);
}

enum DirtySetting : uint16_t {
  DirtyAppletCodex = 1U << 0,
  DirtyAppletTimer = 1U << 1,
  DirtyLedPreset = 1U << 2,
  DirtyLedBrightness = 1U << 3,
  DirtyLedFeedback = 1U << 4,
  DirtySoundProfile = 1U << 5,
  DirtySoundVolume = 1U << 6,
  DirtyDisplay = 1U << 7,
  DirtyTimer = 1U << 8,
  DirtyRecoveryAccessPoint = 1U << 9,
};

enum class InputSource : uint8_t {
  Physical,
  Remote,
  Dwell,
  RotaryGesture,
};

void IRAM_ATTR captureEncoderEdge() {
  const uint32_t levels = GPIO.in;
  const uint8_t current = static_cast<uint8_t>(
      (((levels >> kEncoderA) & 1U) << 1) | ((levels >> kEncoderB) & 1U));
  portENTER_CRITICAL_ISR(&encoderMux);
  encoderAccumulatorRaw +=
      kEncoderTransitions[(encoderPreviousRaw << 2) | current];
  encoderPreviousRaw = current;
  int8_t direction = 0;
  if (encoderAccumulatorRaw >= kEncoderEdgesPerDetent) {
    encoderAccumulatorRaw -= kEncoderEdgesPerDetent;
    direction = 1;
  } else if (encoderAccumulatorRaw <= -kEncoderEdgesPerDetent) {
    encoderAccumulatorRaw += kEncoderEdgesPerDetent;
    direction = -1;
  }
  if (direction) {
    ++encoderRawDetentCount;
    const uint8_t next =
        (encoderDetentHead + 1U) % kEncoderDetentQueueCapacity;
    if (next == encoderDetentTail) {
      ++encoderDetentOverflowCount;
    } else {
      encoderDetentQueue[encoderDetentHead].atMs = millis();
      encoderDetentQueue[encoderDetentHead].direction = direction;
      encoderDetentHead = next;
    }
  }
  portEXIT_CRITICAL_ISR(&encoderMux);
}

void IRAM_ATTR captureButtonEdge() {
  const uint32_t levels = GPIO.in;
  const bool down = ((levels >> kEncoderButton) & 1U) == 0;
  const uint32_t now = millis();
  portENTER_CRITICAL_ISR(&buttonMux);
  ++buttonRawEdgeCount;
  down ? ++buttonRawFallCount : ++buttonRawRiseCount;
  lastButtonRawEdgeAt = now;
  const uint8_t next = (buttonEdgeHead + 1U) % kButtonEdgeQueueCapacity;
  if (next == buttonEdgeTail) {
    ++buttonEdgeOverflowCount;
  } else {
    buttonEdgeQueue[buttonEdgeHead].atMs = now;
    buttonEdgeQueue[buttonEdgeHead].down = down;
    buttonEdgeHead = next;
  }
  portEXIT_CRITICAL_ISR(&buttonMux);
}

const char *pageName(Page page) {
  switch (page) {
    case Page::Overview: return "OVERVIEW";
    case Page::Windows: return "WINDOWS";
    case Page::Timer: return "TIMER";
    case Page::Applets: return "APPLETS";
    case Page::Leds: return "LEDS";
    case Page::Display: return "DISPLAY";
    case Page::Sounds: return "SOUNDS";
    case Page::Connection: return "CONNECTION";
    case Page::RecoveryWifiToggle: return "RECOVERY WIFI";
    case Page::About: return "ABOUT";
  }
  return "ABOUT";
}

size_t buildMenu(MenuEntry *entries) {
  size_t count = 0;
  if (codexAppletInstalled) {
    entries[count++] = {"CODEX", Page::Overview};
    entries[count++] = {"WINDOWS", Page::Windows};
  }
  if (timerAppletInstalled) entries[count++] = {"WORK TIMER", Page::Timer};
  entries[count++] = {"APPLETS", Page::Applets};
  entries[count++] = {"LED SETTINGS", Page::Leds};
  entries[count++] = {"DISPLAY SETTINGS", Page::Display};
  entries[count++] = {"SOUND SETTINGS", Page::Sounds};
  entries[count++] = {"CONNECTION", Page::Connection};
  entries[count++] = {recoveryAccessPointEnabled ? "RECOVERY WIFI OFF"
                                                  : "RECOVERY WIFI ON",
                      Page::RecoveryWifiToggle};
  entries[count++] = {"ABOUT", Page::About};
  return count;
}

void setBudgetPullLastResult(const char *value) {
  strlcpy(budgetPullLastResult, value, sizeof(budgetPullLastResult));
  budgetPullLastResultAt = millis();
}

void loadBudgetPullSettings() {
  preferences.begin("xsure-budget", true);
  const bool valid = preferences.getBool("pull-valid", false);
  const bool enabled = preferences.getBool("pull-on", false);
  const bool legacyPush = preferences.getBool("push-legacy", false);
  const String host = preferences.getString("pull-host", "");
  const uint16_t port = preferences.getUShort("pull-port", 0);
  const String key = preferences.getString("pull-key", "");
  preferences.end();

  uint8_t decodedKey[32];
  budgetPullSettings = BudgetPullSettings{};
  if (!valid || !imdisplay::budget_pull::isValidPullHost(host.c_str()) ||
      port < 1024 || !imdisplay::budget_pull::decodeKeyHex(key.c_str(), decodedKey)) {
    setBudgetPullLastResult("not-configured");
    return;
  }
  budgetPullSettings.configured = true;
  budgetPullSettings.enabled = enabled;
  budgetPullSettings.legacyPushEnabled = legacyPush;
  strlcpy(budgetPullSettings.request.host, host.c_str(),
          sizeof(budgetPullSettings.request.host));
  budgetPullSettings.request.port = port;
  memcpy(budgetPullSettings.request.key, decodedKey, sizeof(decodedKey));
  setBudgetPullLastResult(enabled ? "waiting" : "disabled");
}

void scheduleSettingsPersist(uint16_t setting) {
  dirtySettings |= setting;
  settingsChangedAt = millis();
}

bool notePreferenceWrite(size_t written, size_t expected) {
  ++settingsKeyWriteAttempts;
  if (written == expected) return true;
  ++settingsPersistFailures;
  return false;
}

bool persistSettings(uint16_t pending) {
  if (!pending) return true;
  if (!preferences.begin("xsure-budget", false)) {
    ++settingsPersistFailures;
    return false;
  }
  ++settingsPersistSessions;
  bool saved = true;
  if (pending & DirtyAppletCodex) {
    saved &= notePreferenceWrite(preferences.putBool("app-codex", codexAppletInstalled), 1);
  }
  if (pending & DirtyAppletTimer) {
    saved &= notePreferenceWrite(preferences.putBool("app-timer", timerAppletInstalled), 1);
  }
  if (pending & DirtyLedPreset) {
    saved &= notePreferenceWrite(preferences.putUChar("led-preset", ledPresetIndex), 1);
  }
  if (pending & DirtyLedBrightness) {
    saved &= notePreferenceWrite(preferences.putUChar("led-bright", ledBrightness), 1);
  }
  if (pending & DirtyLedFeedback) {
    saved &= notePreferenceWrite(preferences.putBool("led-feedback", ledFeedbackEnabled), 1);
  }
  if (pending & DirtySoundProfile) {
    saved &= notePreferenceWrite(preferences.putUChar("snd-profile", soundProfileIndex), 1);
  }
  if (pending & DirtySoundVolume) {
    saved &= notePreferenceWrite(preferences.putUChar("snd-volume", soundVolume), 1);
  }
  if (pending & DirtyDisplay) {
    saved &= notePreferenceWrite(preferences.putUChar("disp-bright", displayBrightness), 1);
  }
  if (pending & DirtyTimer) {
    saved &= notePreferenceWrite(
        preferences.putUShort("timer-min", workTimer.durationSeconds / 60), 2);
  }
  if (pending & DirtyRecoveryAccessPoint) {
    saved &= notePreferenceWrite(
        preferences.putBool("recovery-ap", recoveryAccessPointEnabled), 1);
  }
  preferences.end();
  lastPersistedSettings = pending;
  Serial.printf("settings persisted mask=0x%04x keys=%u ok=%d\n", pending,
                static_cast<unsigned>(settingsKeyWriteAttempts), saved);
  return saved;
}

void persistSettingsNow(uint16_t setting) {
  dirtySettings &= ~setting;
  if (!persistSettings(setting)) scheduleSettingsPersist(setting);
}

void flushScheduledSettings(bool force = false) {
  if (!dirtySettings ||
      (!force && millis() - settingsChangedAt < kSettingsCommitDelayMs)) {
    return;
  }
  const uint16_t pending = dirtySettings;
  dirtySettings = 0;
  if (!persistSettings(pending)) scheduleSettingsPersist(pending);
}

void loadProductSettings() {
  preferences.begin("xsure-budget", true);
  codexAppletInstalled = preferences.getBool("app-codex", true);
  timerAppletInstalled = preferences.getBool("app-timer", true);
  ledPresetIndex = min<uint8_t>(preferences.getUChar("led-preset", 3), kLedPresetCount - 1);
  ledBrightness = min<uint8_t>(preferences.getUChar("led-bright", 20), 100);
  ledFeedbackEnabled = preferences.getBool("led-feedback", true);
  soundProfileIndex =
      min<uint8_t>(preferences.getUChar("snd-profile", 2), kSoundProfileCount - 1);
  soundVolume = min<uint8_t>(preferences.getUChar("snd-volume", 20), 100);
  displayBrightness = min<uint8_t>(
      100, max<uint8_t>(kMinDisplayBrightness,
                        preferences.getUChar("disp-bright", 100)));
  recoveryAccessPointEnabled = preferences.getBool("recovery-ap", false);
  const uint16_t timerMinutes = constrain(preferences.getUShort("timer-min", 25), 5, 120);
  preferences.end();
  workTimer.durationSeconds = timerMinutes * 60;
  workTimer.remainingSeconds = workTimer.durationSeconds;
  if (!codexAppletInstalled) {
    currentPage = timerAppletInstalled ? Page::Timer : Page::Applets;
  }
}

void applyRecoveryAccessPointSetting() {
  accessPointChangePending = false;
  if (recoveryAccessPointEnabled) {
    WiFi.mode(WIFI_AP_STA);
    if (!accessPointReady) {
      accessPointReady = WiFi.softAP(accessPointName, accessPointPassword, 6, false, 2);
    }
  } else {
    if (accessPointReady) WiFi.softAPdisconnect(true);
    accessPointReady = false;
    WiFi.mode(WIFI_STA);
  }
  needsRender = true;
  Serial.printf("recovery wifi enabled=%d ready=%d ip=%s\n",
                recoveryAccessPointEnabled, accessPointReady,
                accessPointReady ? WiFi.softAPIP().toString().c_str() : "0.0.0.0");
}

void setRecoveryAccessPointEnabled(bool enabled, bool deferApply = true) {
  const bool changed = recoveryAccessPointEnabled != enabled;
  recoveryAccessPointEnabled = enabled;
  if (changed) persistSettingsNow(DirtyRecoveryAccessPoint);
  if (deferApply) {
    accessPointChangePending = true;
    accessPointChangeAt = millis() + 300;
  } else {
    applyRecoveryAccessPointSetting();
  }
  needsRender = true;
}

void applyLedSettings() {
  if (!rgbPixelsReady) return;
  const bool shown = rgbPixels.show(kLedPresets[ledPresetIndex].colors, ledBrightness);
  Serial.printf("led preset=%s brightness=%u ready=%d shown=%d\n",
                kLedPresets[ledPresetIndex].name, ledBrightness, rgbPixelsReady, shown);
}

void setLedSettings(uint8_t preset, uint8_t brightness, bool persist = true) {
  const uint8_t nextPreset = min<uint8_t>(preset, kLedPresetCount - 1);
  const uint8_t nextBrightness = min<uint8_t>(brightness, 100);
  const bool presetChanged = nextPreset != ledPresetIndex;
  const bool brightnessChanged = nextBrightness != ledBrightness;
  if (!presetChanged && !brightnessChanged) return;
  const uint16_t changed = (presetChanged ? DirtyLedPreset : 0) |
                           (brightnessChanged ? DirtyLedBrightness : 0);
  ledFeedbackUntil = 0;
  ledPresetIndex = nextPreset;
  ledBrightness = nextBrightness;
  applyLedSettings();
  if (persist) {
    persistSettingsNow(changed);
  } else {
    scheduleSettingsPersist(changed);
  }
  needsRender = true;
}

void setLedFeedbackEnabled(bool enabled, bool persist = true) {
  if (enabled == ledFeedbackEnabled) return;
  ledFeedbackEnabled = enabled;
  ledFeedbackUntil = 0;
  applyLedSettings();
  if (persist) {
    persistSettingsNow(DirtyLedFeedback);
  } else {
    scheduleSettingsPersist(DirtyLedFeedback);
  }
  needsRender = true;
}

void showLedFeedback(RgbColor color, uint32_t durationMs) {
  if (!rgbPixelsReady || !ledFeedbackEnabled || !ledBrightness) return;
  const RgbColor colors[RgbPixels::kCount] = {color, color, color, color};
  if (rgbPixels.show(colors, ledBrightness)) ledFeedbackUntil = millis() + durationMs;
}

void setSoundSettings(uint8_t profile, uint8_t volume, bool persist = true) {
  const uint8_t nextProfile = min<uint8_t>(profile, kSoundProfileCount - 1);
  const uint8_t nextVolume = min<uint8_t>(volume, 100);
  const bool profileChanged = nextProfile != soundProfileIndex;
  const bool volumeChanged = nextVolume != soundVolume;
  if (!profileChanged && !volumeChanged) return;
  const uint16_t changed = (profileChanged ? DirtySoundProfile : 0) |
                           (volumeChanged ? DirtySoundVolume : 0);
  soundProfileIndex = nextProfile;
  soundVolume = nextVolume;
  if (persist) {
    persistSettingsNow(changed);
  } else {
    scheduleSettingsPersist(changed);
  }
  needsRender = true;
}

void setDisplayBrightness(uint8_t brightness, bool persist = true) {
  const uint8_t nextBrightness = min<uint8_t>(
      100, max<uint8_t>(kMinDisplayBrightness, brightness));
  const bool settingChanged = nextBrightness != displayBrightness;
  const bool hardwareChanged = display.backlightPercent() != nextBrightness;
  if (!settingChanged && !hardwareChanged) return;
  displayBrightness = nextBrightness;
  display.setBacklightPercent(displayBrightness);
  if (settingChanged) {
    if (persist) {
      persistSettingsNow(DirtyDisplay);
    } else {
      scheduleSettingsPersist(DirtyDisplay);
    }
  }
  needsRender = true;
  Serial.printf("display brightness=%u duty=%u\n", displayBrightness,
                display.backlightDuty());
}

void playSound(SoundEngine::Cue cue, bool essential = false, bool preview = false) {
  if (!soundTaskReady || !soundProfileIndex || !soundVolume) return;
  if (!preview && !essential && !kSoundProfiles[soundProfileIndex].confirmations) return;
  soundEngine.play(cue, soundVolume);
}

uint32_t timerRemainingSeconds() {
  if (!workTimer.running) return workTimer.remainingSeconds;
  const uint32_t elapsed = (millis() - workTimer.startedAt) / 1000;
  return elapsed >= workTimer.remainingSeconds ? 0 : workTimer.remainingSeconds - elapsed;
}

void startWorkTimer() {
  if (workTimer.running) return;
  if (!workTimer.remainingSeconds) workTimer.remainingSeconds = workTimer.durationSeconds;
  workTimer.startedAt = millis();
  workTimer.running = true;
  playSound(SoundEngine::Cue::Start);
  showLedFeedback({45, 211, 128}, 450);
  needsRender = true;
}

void pauseWorkTimer() {
  if (!workTimer.running) return;
  workTimer.remainingSeconds = timerRemainingSeconds();
  workTimer.running = false;
  playSound(SoundEngine::Cue::Pause);
  showLedFeedback({49, 130, 246}, 350);
  needsRender = true;
}

void resetWorkTimer() {
  if (!workTimer.running && workTimer.remainingSeconds == workTimer.durationSeconds) return;
  workTimer.running = false;
  workTimer.remainingSeconds = workTimer.durationSeconds;
  playSound(SoundEngine::Cue::Confirm);
  needsRender = true;
}

void setWorkTimerMinutes(int minutes, bool persist = true) {
  minutes = constrain(minutes, 5, 120);
  if (workTimer.durationSeconds == static_cast<uint32_t>(minutes * 60)) return;
  workTimer.durationSeconds = minutes * 60;
  workTimer.remainingSeconds = workTimer.durationSeconds;
  workTimer.running = false;
  if (persist) {
    persistSettingsNow(DirtyTimer);
  } else {
    scheduleSettingsPersist(DirtyTimer);
  }
  needsRender = true;
}

void openMenu() {
  MenuEntry entries[kMaxMenuEntries];
  const size_t count = buildMenu(entries);
  menuIndex = 0;
  for (size_t i = 0; i < count; ++i) {
    if (entries[i].page == currentPage) {
      menuIndex = i;
      break;
    }
  }
  menuOpen = true;
  menuDwellArmed = false;
  needsRender = true;
}

void recordNavigation(const char *method, Page fromPage, bool fromMenu) {
  ++navigationTransitionCount;
  lastNavigationMethod = method;
  lastNavigationPage = fromPage;
  lastNavigationFromMenu = fromMenu;
  lastNavigationToMenu = menuOpen;
}

void openMenuFromInput(const char *method) {
  const Page fromPage = currentPage;
  const bool fromMenu = menuOpen;
  if (!menuOpen) openMenu();
  recordNavigation(method, fromPage, fromMenu);
}

void toggleLauncherFromInput(const char *method) {
  const Page fromPage = currentPage;
  const bool fromMenu = menuOpen;
  if (menuOpen) {
    menuOpen = false;
  } else {
    openMenu();
  }
  recordNavigation(method, fromPage, fromMenu);
  needsRender = true;
}

void title(const char *value) {
  display.fillScreen(kBlack);
  display.fillRect(0, 0, kWidth, 32, kBlue);
  display.centered(8, value, kWhite, 2);
}

int textWidth(const char *value, uint8_t scale) {
  return strlen(value) * 6 * scale - scale;
}

void rightAligned(int right, int y, const char *value, uint16_t color,
                  uint8_t scale = 2) {
  display.text(max(0, right - textWidth(value, scale)), y, value, color, scale);
}

void footer(const char *hint) {
  display.fillRect(16, 216, 288, 1, kPanel);
  display.centered(226, hint, kMuted, 1);
}

void drawBackAction(int y, bool selected = true) {
  display.fillRect(16, y, 288, 32, selected ? kPanel : kBlack);
  if (selected) display.fillRect(16, y, 5, 32, kBlue);
  display.text(30, y + 9, "BACK TO LAUNCHER", selected ? kWhite : kMuted, 2);
  rightAligned(292, y + 12, "QUICK L-R", selected ? kWhite : kMuted, 1);
}

void drawSettingRow(int y, const char *label, const char *value, bool selected,
                    uint16_t valueColor = kWhite) {
  display.fillRect(12, y, 296, 36, selected ? kPanel : kBlack);
  if (selected) display.fillRect(12, y, 5, 36, kBlue);
  display.text(24, y + 7, label, selected ? kWhite : kMuted, 1);
  rightAligned(294, y + 10, value, selected ? kWhite : valueColor,
               strlen(value) > 8 ? 1 : 2);
}

uint16_t quotaColor(float remaining) {
  if (remaining >= 40) return kGreen;
  if (remaining >= 15) return kAmber;
  return kRed;
}

const BudgetWindow *primaryWindow(const BudgetState &state) {
  for (size_t i = 0; i < state.count; ++i) {
    if (!strcmp(state.windows[i].id, "codex:primary")) return &state.windows[i];
  }
  return state.count ? &state.windows[0] : nullptr;
}

const BudgetWindow *primaryWindow() { return primaryWindow(budget); }

uint32_t ageSeconds() {
  return budget.received ? (millis() - budget.receivedAt) / 1000 : 0;
}

bool isStale() {
  return budget.received && millis() - budget.receivedAt > kStaleAfterMs;
}

bool overviewPresentationChanged(const BudgetState &previous, const BudgetState &next) {
  if (!previous.received || previous.valid != next.valid) return true;
  if (!next.valid) return false;
  const BudgetWindow *before = primaryWindow(previous);
  const BudgetWindow *after = primaryWindow(next);
  if (!before || !after) return before != after;
  return before->remaining != after->remaining ||
         strcmp(before->resetText, after->resetText);
}

bool windowsPresentationChanged(const BudgetState &previous, const BudgetState &next) {
  if (!previous.received || previous.valid != next.valid || previous.count != next.count ||
      strcmp(previous.error, next.error)) {
    return true;
  }
  for (size_t i = 0; i < next.count; ++i) {
    const BudgetWindow &before = previous.windows[i];
    const BudgetWindow &after = next.windows[i];
    if (strcmp(before.id, after.id) || strcmp(before.label, after.label) ||
        strcmp(before.window, after.window) ||
        strcmp(before.resetText, after.resetText) || before.remaining != after.remaining) {
      return true;
    }
  }
  return false;
}

void renderOverview() {
  title("CODEX BUDGET");
  const BudgetWindow *primary = primaryWindow();
  if (!budget.valid || !primary) {
    display.centered(64, "NO DATA", kRed, 5);
    display.centered(124, "WAITING FOR MAC", kMuted, 2);
  } else {
    const int roundedRemaining = static_cast<int>(primary->remaining + 0.5f);
    const uint16_t color = quotaColor(primary->remaining);
    drawPercentValue(roundedRemaining, 38, color);
    display.fillRect(20, 154, 280, 14, kPanel);
    display.fillRect(20, 154, roundedRemaining * 280 / 100, 14, color);
    display.centered(176, "REMAINING", kWhite, 2);
    char reset[48];
    snprintf(reset, sizeof(reset), "RESET %s", primary->resetText);
    display.fillRect(14, 198, 292, 24, kPanel);
    display.centered(207, reset, isStale() ? kAmber : kMuted, 1);
  }
  if (!budget.received) {
    display.fillRect(14, 178, 292, 44, kPanel);
    display.centered(191, "SOURCE WAIT", kAmber, 2);
  } else if (isStale()) {
    display.text(258, 176, "STALE", kAmber, 1);
  }
  footer("QUICK R-L MENU  QUICK L-R BACK");
}

void renderWindows() {
  title("QUOTA WINDOWS");
  if (!budget.valid || !budget.count) {
    display.centered(96, "NO DATA", kRed, 3);
  } else {
    const size_t start = min(static_cast<size_t>(windowIndex), budget.count - 1);
    size_t shown = 0;
    for (size_t i = start; i < budget.count && shown < 3; ++i, ++shown) {
      const int y = 40 + shown * 58;
      display.fillRect(10, y, 300, 48, i == start ? kPanel : kBlack);
      display.text(18, y + 7, budget.windows[i].label, kWhite, 1);
      char value[12];
      snprintf(value, sizeof(value), "%.0f%%", budget.windows[i].remaining);
      display.text(250, y + 7, value, quotaColor(budget.windows[i].remaining), 2);
      display.text(18, y + 26, budget.windows[i].window, kMuted, 1);
    }
  }
  footer("TURN SCROLL  QUICK R-L MENU  QUICK L-R BACK");
}

void renderConnection() {
  title("CONNECTION");
  display.text(12, 44, "LOCAL WIFI", kMuted, 1);
  if (WiFi.status() == WL_CONNECTED) {
    display.text(12, 61, WiFi.SSID().c_str(), kGreen, 1);
    display.text(12, 82, WiFi.localIP().toString().c_str(), kBlue, 2);
  } else {
    display.text(12, 66, stationConfigured ? "CONNECTING" : "NOT CONFIGURED",
                 stationConfigured ? kAmber : kMuted, 2);
  }
  display.text(202, 44, "RECOVERY AP", kMuted, 1);
  display.text(202, 61, accessPointReady ? accessPointName : "OFF",
               accessPointReady ? kWhite : kGreen, 1);
  display.text(202, 84, accessPointReady ? "PASSWORD" : "ENABLE IN MENU",
               kMuted, 1);
  if (accessPointReady) display.text(202, 101, accessPointPassword, kAmber, 1);
  display.text(12, 130, "CONTROL / OTA", kMuted, 1);
  display.text(12, 148,
               WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString().c_str()
               : (accessPointReady ? "192.168.4.1" : "OFFLINE"),
               kBlue, 2);
  display.text(202, 148, "LOGIN: XSURE", kMuted, 1);
  footer("TURN MENU  QUICK L-R BACK");
}

void renderTimerClock(bool includeStatus = true) {
  display.fillRect(78, 42, 164, 52, kBlack);
  const uint32_t remaining = timerRemainingSeconds();
  timerRenderedSecond = remaining;
  drawTimerValue(remaining, 46, remaining ? kGreen : kAmber);
  if (includeStatus) {
    display.fillRect(0, 96, kWidth, 22, kBlack);
    display.centered(100, workTimer.running ? "FOCUSING" : (remaining ? "PAUSED" : "DONE"),
                     workTimer.running ? kBlue : kMuted, 2);
  }
}

void renderTimer() {
  title("WORK TIMER");
  renderTimerClock();
  char duration[32];
  snprintf(duration, sizeof(duration), "SESSION %lu MIN",
           static_cast<unsigned long>(workTimer.durationSeconds / 60));
  display.centered(132, duration, kWhite, 2);
  display.centered(183,
                   workTimer.running ? "QUICK RIGHT-LEFT: PAUSE"
                                     : "TURN: 5 MIN  QUICK R-L: START",
                   kMuted, 1);
  footer(workTimer.running
             ? "QUICK R-L PAUSE  QUICK L-R BACK"
             : "TURN 5 MIN  QUICK R-L START  QUICK L-R BACK");
}

void renderApplets() {
  title("APPLETS");
  const char *names[] = {"CODEX BUDGET", "WORK TIMER"};
  const bool installed[] = {codexAppletInstalled, timerAppletInstalled};
  for (int i = 0; i < 2; ++i) {
    const int y = 44 + i * 58;
    const bool selected = i == appletIndex;
    display.fillRect(12, y, 296, 52, selected ? kPanel : kBlack);
    if (selected) display.fillRect(12, y, 5, 52, kBlue);
    display.text(24, y + 8, names[i], selected ? kWhite : kMuted, 2);
    rightAligned(294, y + 8, installed[i] ? "ON" : "OFF",
                 selected ? kWhite : (installed[i] ? kGreen : kAmber), 2);
    if (selected) {
      display.text(24, y + 34,
                   installed[i] ? "QUICK R-L TO DISABLE"
                                : "QUICK R-L TO ENABLE",
                   kMuted, 1);
    }
  }
  char active[20];
  snprintf(active, sizeof(active), "ACTIVE %u OF 2",
           static_cast<unsigned>(codexAppletInstalled + timerAppletInstalled));
  display.centered(160, active,
                   codexAppletInstalled || timerAppletInstalled ? kGreen : kAmber, 1);
  drawBackAction(174, appletIndex == 2);
  footer("TURN SELECT  QUICK R-L ACT  QUICK L-R BACK");
}

void renderLeds() {
  title("LED SETTINGS");
  char brightness[12];
  snprintf(brightness, sizeof(brightness), "%u%%", ledBrightness);
  drawSettingRow(40, "COLOR PRESET", kLedPresets[ledPresetIndex].name,
                 ledField == 0);
  drawSettingRow(82, "BRIGHTNESS", brightness, ledField == 1);
  drawSettingRow(124, "EVENT GLOW", ledFeedbackEnabled ? "ON" : "OFF",
                 ledField == 2, ledFeedbackEnabled ? kGreen : kMuted);
  drawBackAction(174, ledField == 3);
  footer("TURN CHANGE  QUICK R-L NEXT  QUICK L-R BACK");
}

void renderDisplay() {
  title("DISPLAY SETTINGS");
  display.centered(50, "BACKLIGHT", kMuted, 2);
  char brightness[12];
  snprintf(brightness, sizeof(brightness), "%u%%", displayBrightness);
  display.centered(79, brightness, kWhite, 4);
  display.fillRect(42, 134, 236, 18, kPanel);
  const int barWidth = static_cast<int>(displayBrightness) * 236 / 100;
  display.fillRect(42, 134, barWidth, 18, kBlue);
  display.text(42, 160, "10 MIN", kMuted, 1);
  rightAligned(278, 160, "100 MAX", kMuted, 1);
  drawBackAction(174);
  footer("TURN ADJUST  QUICK L-R BACK");
}

void renderSounds() {
  title("SOUND SETTINGS");
  char volume[12];
  snprintf(volume, sizeof(volume), "%u%%", soundVolume);
  drawSettingRow(40, "CUE STYLE", kSoundProfiles[soundProfileIndex].name,
                 soundField == 0);
  drawSettingRow(82, "VOLUME", volume, soundField == 1);
  drawSettingRow(124, "TEST CUE", soundProfileIndex ? "PLAY" : "MUTED",
                 soundField == 2, soundProfileIndex ? kGreen : kMuted);
  drawBackAction(174, soundField == 3);
  footer("TURN CHANGE  QUICK R-L NEXT  QUICK L-R BACK");
}

void renderAbout() {
  title("ABOUT");
  display.centered(52, "imDisplay", kWhite, 3);
  display.centered(84, "APPLET DISPLAY", kBlue, 2);
  display.text(50, 126, "FIRMWARE " CODEX_BUDGET_FIRMWARE_VERSION, kMuted, 1);
  display.text(50, 148, "ESP32-WROOM-32D", kMuted, 1);
  display.text(50, 170, "FACTORY RESTORE SAVED", kGreen, 1);
  footer("QUICK R-L MENU  QUICK L-R BACK");
}

int menuFirstIndex(int selected, int count) {
  return constrain(selected - 2, 0, max(0, count - 5));
}

void renderMenuRow(const MenuEntry &entry, int row, bool selected) {
  const int y = 34 + row * 37;
  display.fillRect(18, y, 284, 31, selected ? kPanel : kBlack);
  if (selected) {
    display.fillRect(18, y, 5, 31, kBlue);
  } else {
    display.fillRect(18, y + 30, 284, 1, kPanel);
  }
  display.text(30, y + 8, entry.label, selected ? kWhite : kMuted, 2);
}

void renderMenu() {
  MenuEntry entries[kMaxMenuEntries];
  const size_t count = buildMenu(entries);
  menuIndex = constrain(menuIndex, 0, static_cast<int>(count) - 1);
  const int first = menuFirstIndex(menuIndex, static_cast<int>(count));
  display.fillScreen(kBlack);
  display.text(18, 8, "IMDISPLAY", kWhite, 2);
  rightAligned(302, 12, "LAUNCHER", kMuted, 1);
  for (int row = 0; row < 5 && first + row < static_cast<int>(count); ++row) {
    const int index = first + row;
    renderMenuRow(entries[index], row, index == menuIndex);
  }
  footer("TURN SELECT  QUICK R-L OPEN  QUICK L-R CLOSE");
}

void render() {
  const uint32_t startedAtUs = micros();
  display.beginFrame();
  if (menuOpen) {
    renderMenu();
  } else {
    switch (currentPage) {
      case Page::Overview: renderOverview(); break;
      case Page::Windows: renderWindows(); break;
      case Page::Timer: renderTimer(); break;
      case Page::Applets: renderApplets(); break;
      case Page::Leds: renderLeds(); break;
      case Page::Display: renderDisplay(); break;
      case Page::Sounds: renderSounds(); break;
      case Page::Connection: renderConnection(); break;
      case Page::RecoveryWifiToggle: renderConnection(); break;
      case Page::About: renderAbout(); break;
    }
  }
  display.endFrame();
  lastFullRenderDurationUs = micros() - startedAtUs;
  maxFullRenderDurationUs = max(maxFullRenderDurationUs, lastFullRenderDurationUs);
  ++fullRenderCount;
  lastFullRenderAt = millis();
  needsRender = false;
}

void setAppletInstalled(int index, bool installed) {
  const bool current = index == 0 ? codexAppletInstalled : timerAppletInstalled;
  if (current == installed) return;
  if (index == 0) {
    codexAppletInstalled = installed;
    if (!installed && (currentPage == Page::Overview || currentPage == Page::Windows)) {
      currentPage = Page::Applets;
    }
  } else {
    timerAppletInstalled = installed;
    if (!installed && currentPage == Page::Timer) currentPage = Page::Applets;
  }
  persistSettingsNow(index == 0 ? DirtyAppletCodex : DirtyAppletTimer);
  playSound(SoundEngine::Cue::Confirm);
  needsRender = true;
  Serial.printf("applet id=%s installed=%d\n", index == 0 ? "codex" : "timer", installed);
}

void encoderMoved(int direction, InputSource source = InputSource::Physical) {
  if (source == InputSource::Physical) {
    ++encoderEventCount;
    lastEncoderDirection = direction;
  } else if (source == InputSource::Remote) {
    ++remoteInputCount;
  }
  bool selectionChanged = false;
  if (menuOpen) {
    MenuEntry entries[kMaxMenuEntries];
    const int count = buildMenu(entries);
    const int previous = menuIndex;
    const int previousFirst = menuFirstIndex(previous, count);
    const int next = (menuIndex + direction + count) % count;
    selectionChanged = next != menuIndex;
    menuIndex = next;
    const int nextFirst = menuFirstIndex(next, count);
    if (selectionChanged) {
      menuDwellArmed = source == InputSource::Physical;
      menuSelectionChangedAt = millis();
    }
    if (selectionChanged && !needsRender && !otaInProgress &&
        previousFirst == nextFirst) {
      renderMenuRow(entries[previous], previous - previousFirst, false);
      renderMenuRow(entries[next], next - nextFirst, true);
      ++menuPartialRenderCount;
      selectionChanged = false;
    }
  } else if (currentPage == Page::Overview || currentPage == Page::Connection ||
             currentPage == Page::About ||
             (currentPage == Page::Windows && !budget.count)) {
    openMenu();
    MenuEntry entries[kMaxMenuEntries];
    const int count = buildMenu(entries);
    menuIndex = (menuIndex + direction + count) % count;
    menuDwellArmed = source == InputSource::Physical;
    menuSelectionChangedAt = millis();
  } else if (currentPage == Page::Windows && budget.count) {
    const int next = constrain(windowIndex + direction, 0,
                               static_cast<int>(budget.count) - 1);
    selectionChanged = next != windowIndex;
    windowIndex = next;
  } else if (currentPage == Page::Timer && !workTimer.running) {
    setWorkTimerMinutes(static_cast<int>(workTimer.durationSeconds / 60) + direction * 5,
                        false);
  } else if (currentPage == Page::Applets) {
    const int next = (appletIndex + direction + 3) % 3;
    selectionChanged = next != appletIndex;
    appletIndex = next;
  } else if (currentPage == Page::Leds) {
    if (ledField == 3) {
      ledField = direction > 0 ? 0 : 2;
      selectionChanged = true;
    } else if (ledField == 0) {
      const int presetCount = static_cast<int>(kLedPresetCount);
      const int next = (static_cast<int>(ledPresetIndex) + direction + presetCount) % presetCount;
      setLedSettings(next, ledBrightness, false);
    } else {
      if (ledField == 1) {
        setLedSettings(ledPresetIndex,
                       constrain(static_cast<int>(ledBrightness) + direction * 10, 0, 100),
                       false);
      } else {
        setLedFeedbackEnabled(!ledFeedbackEnabled, false);
      }
    }
  } else if (currentPage == Page::Display) {
    setDisplayBrightness(constrain(static_cast<int>(displayBrightness) + direction * 10,
                                   static_cast<int>(kMinDisplayBrightness), 100),
                         false);
  } else if (currentPage == Page::Sounds) {
    if (soundField == 3) {
      soundField = direction > 0 ? 0 : 2;
      selectionChanged = true;
    } else if (soundField == 0) {
      const int profileCount = static_cast<int>(kSoundProfileCount);
      const int next =
          (static_cast<int>(soundProfileIndex) + direction + profileCount) % profileCount;
      setSoundSettings(next, soundVolume, false);
    } else if (soundField == 1) {
      setSoundSettings(soundProfileIndex,
                       constrain(static_cast<int>(soundVolume) + direction * 10, 0, 100),
                       false);
    }
  }
  if (selectionChanged) needsRender = true;
  Serial.printf("input encoder=%d page=%s menu=%d index=%d\n", direction,
                pageName(currentPage), menuOpen, menuIndex);
}

void longPress(InputSource source = InputSource::Physical);

void shortPress(InputSource source = InputSource::Physical) {
  if (source == InputSource::Physical) {
    ++shortPressCount;
  } else if (source == InputSource::Remote) {
    ++remoteInputCount;
  } else if (source == InputSource::Dwell) {
    ++dwellSelectionCount;
  } else {
    ++rotaryForwardGestureCount;
  }
  menuDwellArmed = false;
  if (menuOpen) {
    const Page fromPage = currentPage;
    const bool fromMenu = menuOpen;
    MenuEntry entries[kMaxMenuEntries];
    const size_t count = buildMenu(entries);
    menuIndex = constrain(menuIndex, 0, static_cast<int>(count) - 1);
    const Page selectedPage = entries[menuIndex].page;
    if (selectedPage == Page::RecoveryWifiToggle) {
      setRecoveryAccessPointEnabled(!recoveryAccessPointEnabled);
      currentPage = Page::Connection;
    } else {
      currentPage = selectedPage;
    }
    menuOpen = false;
    if (currentPage == Page::Overview || currentPage == Page::Windows) {
      requestBudgetPull();
    }
    playSound(SoundEngine::Cue::Confirm);
    recordNavigation(source == InputSource::RotaryGesture ? "ROTARY_FORWARD"
                                                          : "MENU_SELECT",
                     fromPage, fromMenu);
  } else if (currentPage == Page::Timer) {
    workTimer.running ? pauseWorkTimer() : startWorkTimer();
  } else if (currentPage == Page::Applets) {
    if (appletIndex == 2) {
      openMenuFromInput("BACK_ROW");
    } else {
      setAppletInstalled(appletIndex,
                         !(appletIndex == 0 ? codexAppletInstalled
                                           : timerAppletInstalled));
    }
  } else if (currentPage == Page::Leds) {
    if (ledField == 3) {
      openMenuFromInput("BACK_ROW");
    } else {
      ledField = (ledField + 1) % 4;
      playSound(SoundEngine::Cue::Confirm);
    }
  } else if (currentPage == Page::Display) {
    openMenuFromInput("BACK_ROW");
  } else if (currentPage == Page::Sounds) {
    if (soundField == 3) {
      openMenuFromInput("BACK_ROW");
    } else if (soundField == 2) {
      playSound(SoundEngine::Cue::Preview, false, true);
      soundField = 3;
    } else {
      ++soundField;
      playSound(SoundEngine::Cue::Confirm);
    }
  } else {
    openMenuFromInput(source == InputSource::RotaryGesture ? "ROTARY_FORWARD"
                                                           : "SHORT_MENU");
  }
  needsRender = true;
  Serial.printf("input button=short page=%s menu=%d index=%d\n",
                pageName(currentPage), menuOpen, menuIndex);
}

void handleRotaryGestureEvent(RotaryGestureDetector::Event event) {
  switch (event) {
    case RotaryGestureDetector::Event::Clockwise:
      encoderMoved(1);
      break;
    case RotaryGestureDetector::Event::CounterClockwise:
      encoderMoved(-1);
      break;
    case RotaryGestureDetector::Event::Forward:
      shortPress(InputSource::RotaryGesture);
      Serial.println("input rotary=right-left-forward");
      break;
    case RotaryGestureDetector::Event::Back:
      longPress(InputSource::RotaryGesture);
      Serial.println("input rotary=left-right-back");
      break;
  }
}

bool popEncoderDetent(EncoderDetent &detent) {
  bool available = false;
  portENTER_CRITICAL(&encoderMux);
  if (encoderDetentTail != encoderDetentHead) {
    detent.atMs = encoderDetentQueue[encoderDetentTail].atMs;
    detent.direction = encoderDetentQueue[encoderDetentTail].direction;
    encoderDetentTail =
        (encoderDetentTail + 1U) % kEncoderDetentQueueCapacity;
    available = true;
  }
  portEXIT_CRITICAL(&encoderMux);
  return available;
}

void pollEncoder() {
  uint32_t lastButtonEdgeAt = 0;
  portENTER_CRITICAL(&buttonMux);
  lastButtonEdgeAt = lastButtonRawEdgeAt;
  portEXIT_CRITICAL(&buttonMux);
  EncoderDetent detent{};
  while (popEncoderDetent(detent)) {
    const int32_t signedDistance =
        static_cast<int32_t>(detent.atMs - lastButtonEdgeAt);
    const uint32_t buttonEdgeDistance =
        static_cast<uint32_t>(signedDistance < 0 ? -signedDistance : signedDistance);
    if (buttonDebouncer.rawDown() || buttonEdgeDistance <= kButtonEncoderGuardMs) {
      ++suppressedEncoderDetentCount;
      continue;
    }
    if (singleClickPending) {
      singleClickPending = false;
      secondClickInProgress = false;
      shortPress();
    }
    rotaryGesture.observe(detent.direction, detent.atMs,
                          handleRotaryGestureEvent);
  }
  rotaryGesture.advanceTo(millis(), handleRotaryGestureEvent);
}

void longPress(InputSource source) {
  if (source == InputSource::Physical) {
    ++longPressCount;
  } else if (source == InputSource::Remote) {
    ++remoteInputCount;
  } else if (source == InputSource::RotaryGesture) {
    ++rotaryBackGestureCount;
  }
  menuDwellArmed = false;
  const char *method = source == InputSource::Remote
                           ? "REMOTE_HOLD_BACK"
                           : (source == InputSource::RotaryGesture ? "ROTARY_BACK"
                                                                   : "LONG_PRESS_BACK");
  toggleLauncherFromInput(method);
  Serial.printf("input button=long-back page=%s menu=%d\n", pageName(currentPage),
                menuOpen);
}

void navigateBack(InputSource source = InputSource::Physical) {
  if (source == InputSource::Physical) {
    ++doubleClickCount;
  } else if (source == InputSource::Remote) {
    ++remoteInputCount;
  }
  menuDwellArmed = false;
  toggleLauncherFromInput(source == InputSource::Remote ? "REMOTE_BACK"
                                                         : "DOUBLE_CLICK");
  Serial.printf("input back page=%s menu=%d\n", pageName(currentPage), menuOpen);
}

void handlePhysicalPress(uint32_t now) {
  buttonPressedAt = now;
  buttonLongHandled = false;
  if (singleClickPending) {
    if (now - singleClickPendingAt <= kDoubleClickMs) {
      secondClickInProgress = true;
      ++secondClickLatchCount;
    } else {
      singleClickPending = false;
      shortPress();
    }
  }
}

void handlePhysicalClickRelease(uint32_t now) {
  ++buttonReleaseCount;
  menuDwellArmed = false;
  lastButtonPressDurationMs = now - buttonPressedAt;
  if (secondClickInProgress) {
    secondClickInProgress = false;
    singleClickPending = false;
    navigateBack();
    return;
  }
  if (singleClickPending && now - singleClickPendingAt <= kDoubleClickMs) {
    singleClickPending = false;
    navigateBack();
    return;
  }
  if (singleClickPending) {
    singleClickPending = false;
    shortPress();
  }
  singleClickPending = true;
  singleClickPendingAt = now;
}

void pollPendingSingleClick() {
  if (!singleClickPending || buttonDebouncer.stableDown() || secondClickInProgress ||
      millis() - singleClickPendingAt <= kDoubleClickMs) {
    return;
  }
  singleClickPending = false;
  shortPress();
}

void pollMenuDwell() {
  if (!menuOpen || !menuDwellArmed ||
      millis() - menuSelectionChangedAt < kMenuDwellSelectMs) {
    return;
  }
  shortPress(InputSource::Dwell);
  Serial.println("input menu=dwell-select");
}

bool popButtonEdge(ButtonEdge &edge) {
  bool available = false;
  portENTER_CRITICAL(&buttonMux);
  if (buttonEdgeTail != buttonEdgeHead) {
    edge.atMs = buttonEdgeQueue[buttonEdgeTail].atMs;
    edge.down = buttonEdgeQueue[buttonEdgeTail].down;
    buttonEdgeTail = (buttonEdgeTail + 1U) % kButtonEdgeQueueCapacity;
    available = true;
  }
  portEXIT_CRITICAL(&buttonMux);
  return available;
}

void handleDebouncedButtonEvent(ButtonDebouncer::Event event, uint32_t atMs) {
  if (event == ButtonDebouncer::Event::Pressed) {
    handlePhysicalPress(atMs);
  } else if (event == ButtonDebouncer::Event::Released) {
    if (!buttonLongHandled) handlePhysicalClickRelease(atMs);
  } else {
    buttonLongHandled = true;
    lastButtonPressDurationMs = atMs - buttonPressedAt;
    singleClickPending = false;
    secondClickInProgress = false;
    longPress();
  }
}

void pollButton() {
  auto handle = [](ButtonDebouncer::Event event, uint32_t atMs) {
    handleDebouncedButtonEvent(event, atMs);
  };
  ButtonEdge edge{};
  while (popButtonEdge(edge)) buttonDebouncer.observe(edge.down, edge.atMs, handle);
  const uint32_t now = millis();
  buttonDebouncer.observe(digitalRead(kEncoderButton) == LOW, now, handle);
  buttonDebouncer.advanceTo(now, handle);
}

void selfTestButtonLevel(bool down) {
  if (down) {
    digitalWrite(kEncoderButton, LOW);
    pinMode(kEncoderButton, OUTPUT_OPEN_DRAIN);
  } else {
    pinMode(kEncoderButton, INPUT_PULLUP);
  }
}

void serviceButtonFor(uint32_t durationMs) {
  const uint32_t startedAt = millis();
  do {
    pollButton();
    pollPendingSingleClick();
    delay(1);
  } while (millis() - startedAt < durationMs);
  pollButton();
  pollPendingSingleClick();
}

void resetButtonDebounceState(bool down) {
  portENTER_CRITICAL(&buttonMux);
  buttonEdgeTail = buttonEdgeHead;
  portEXIT_CRITICAL(&buttonMux);
  const uint32_t now = millis();
  buttonDebouncer.reset(down, now);
  buttonLongHandled = false;
  buttonPressedAt = now;
}

void resetSelfTestClickState() {
  singleClickPending = false;
  secondClickInProgress = false;
  resetButtonDebounceState(false);
}

void selfTestTap(uint32_t heldMs, uint32_t releasedMs) {
  selfTestButtonLevel(true);
  serviceButtonFor(heldMs);
  selfTestButtonLevel(false);
  serviceButtonFor(releasedMs);
}

void selfTestQueuedTap(uint32_t heldMs, uint32_t releasedMs) {
  selfTestButtonLevel(true);
  delay(heldMs);
  selfTestButtonLevel(false);
  delay(kButtonDebounceMs + 2);
  serviceButtonFor(releasedMs);
}

void selfTestDoubleClick(bool holdSecondPastSingleDeadline = false) {
  selfTestTap(45, 70);
  selfTestButtonLevel(true);
  serviceButtonFor(holdSecondPastSingleDeadline ? kDoubleClickMs + 80 : 45);
  selfTestButtonLevel(false);
  serviceButtonFor(30);
}

void selfTestLongPress() {
  selfTestButtonLevel(true);
  serviceButtonFor(kButtonLongPressMs + 30);
  selfTestButtonLevel(false);
  serviceButtonFor(20);
}

bool runInputSelfTest(uint32_t &releaseDelta, uint32_t &shortDelta,
                      uint32_t &doubleDelta, uint32_t &longDelta,
                      uint32_t &latchDelta) {
  if (digitalRead(kEncoderButton) == LOW) return false;

  const Page savedPage = currentPage;
  const bool savedMenuOpen = menuOpen;
  const int savedMenuIndex = menuIndex;
  const int savedAppletIndex = appletIndex;
  const int savedLedField = ledField;
  const int savedSoundField = soundField;
  const bool savedMenuDwellArmed = menuDwellArmed;
  const uint32_t savedMenuSelectionChangedAt = menuSelectionChangedAt;
  const bool savedSingleClickPending = singleClickPending;
  const uint32_t savedSingleClickPendingAt = singleClickPendingAt;
  const bool savedSecondClickInProgress = secondClickInProgress;
  const bool savedNeedsRender = needsRender;

  menuDwellArmed = false;
  resetSelfTestClickState();
  uint32_t edgeOverflowsBefore = 0;
  portENTER_CRITICAL(&buttonMux);
  edgeOverflowsBefore = buttonEdgeOverflowCount;
  portEXIT_CRITICAL(&buttonMux);

  const uint32_t releasesBefore = buttonReleaseCount;
  const uint32_t shortsBefore = shortPressCount;
  const uint32_t doublesBefore = doubleClickCount;
  const uint32_t longsBefore = longPressCount;
  const uint32_t latchesBefore = secondClickLatchCount;

  constexpr Page testedPages[] = {
      Page::Overview, Page::Windows, Page::Timer, Page::Applets, Page::Leds,
      Page::Display, Page::Sounds, Page::Connection, Page::About};
  constexpr uint8_t expectedRoundTrips =
      sizeof(testedPages) / sizeof(testedPages[0]);
  inputSelfTestPageRoundTrips = 0;
  inputSelfTestPageToLauncher = true;
  inputSelfTestLauncherToPage = true;
  inputSelfTestNestedBack = false;
  inputSelfTestLongPressBack = false;
  inputSelfTestDoubleClickFallback = false;
  inputSelfTestSecondPressGrace = false;
  inputSelfTestQueuedPulse = false;
  inputSelfTestQueueHealthy = false;
  inputSelfTestRestored = false;
  inputSelfTestFailure = "NONE";
  inputSelfTestFailurePage = "NONE";

  for (uint8_t index = 0; index < expectedRoundTrips; ++index) {
    currentPage = testedPages[index];
    menuOpen = false;
    resetSelfTestClickState();
    selfTestLongPress();
    const bool opened = menuOpen && currentPage == testedPages[index];
    inputSelfTestPageToLauncher &= opened;
    if (!opened && !strcmp(inputSelfTestFailure, "NONE")) {
      inputSelfTestFailure = "LONG_PAGE_TO_LAUNCHER";
      inputSelfTestFailurePage = pageName(testedPages[index]);
    }

    selfTestLongPress();
    const bool closed = !menuOpen && currentPage == testedPages[index];
    inputSelfTestLauncherToPage &= closed;
    if (!closed && !strcmp(inputSelfTestFailure, "NONE")) {
      inputSelfTestFailure = "LONG_LAUNCHER_TO_PAGE";
      inputSelfTestFailurePage = pageName(testedPages[index]);
    }
    if (opened && closed) ++inputSelfTestPageRoundTrips;
  }

  currentPage = Page::About;
  menuOpen = false;
  resetSelfTestClickState();
  selfTestDoubleClick(true);
  const bool doubleOpened = menuOpen && currentPage == Page::About;
  inputSelfTestSecondPressGrace = doubleOpened && shortPressCount == shortsBefore;
  selfTestDoubleClick();
  const bool doubleClosed = !menuOpen && currentPage == Page::About;
  inputSelfTestDoubleClickFallback = doubleOpened && doubleClosed;
  if (!inputSelfTestDoubleClickFallback && !strcmp(inputSelfTestFailure, "NONE")) {
    inputSelfTestFailure = "DOUBLE_CLICK_FALLBACK";
    inputSelfTestFailurePage = "ABOUT";
  }

  constexpr Page backPages[] = {
      Page::Applets, Page::Leds, Page::Display, Page::Sounds};
  constexpr uint8_t expectedBackActions =
      sizeof(backPages) / sizeof(backPages[0]);
  inputSelfTestBackActions = 0;
  inputSelfTestNestedBack = true;
  for (uint8_t index = 0; index < expectedBackActions; ++index) {
    currentPage = backPages[index];
    menuOpen = false;
    if (currentPage == Page::Applets) appletIndex = 2;
    if (currentPage == Page::Leds) ledField = 3;
    if (currentPage == Page::Sounds) soundField = 3;
    resetSelfTestClickState();
    if (index == 0) {
      selfTestQueuedTap(45, kDoubleClickMs + 40);
    } else {
      selfTestTap(45, kDoubleClickMs + 40);
    }
    const bool opened = menuOpen && currentPage == backPages[index];
    if (index == 0) inputSelfTestQueuedPulse = opened;
    inputSelfTestNestedBack &= opened;
    if (opened) {
      ++inputSelfTestBackActions;
    } else if (!strcmp(inputSelfTestFailure, "NONE")) {
      inputSelfTestFailure = "BACK_ROW";
      inputSelfTestFailurePage = pageName(backPages[index]);
    }
  }

  inputSelfTestLongPressBack =
      inputSelfTestPageToLauncher && inputSelfTestLauncherToPage &&
      inputSelfTestPageRoundTrips == expectedRoundTrips;

  releaseDelta = buttonReleaseCount - releasesBefore;
  shortDelta = shortPressCount - shortsBefore;
  doubleDelta = doubleClickCount - doublesBefore;
  longDelta = longPressCount - longsBefore;
  latchDelta = secondClickLatchCount - latchesBefore;
  const bool countersPassed =
      releaseDelta == 4 + expectedBackActions &&
      shortDelta == expectedBackActions &&
      doubleDelta == 2 && longDelta == expectedRoundTrips * 2 &&
      latchDelta == 2;
  const bool gpioHigh = digitalRead(kEncoderButton) == HIGH;
  uint32_t edgeOverflowsAfter = 0;
  portENTER_CRITICAL(&buttonMux);
  edgeOverflowsAfter = buttonEdgeOverflowCount;
  portEXIT_CRITICAL(&buttonMux);
  inputSelfTestQueueHealthy = edgeOverflowsAfter == edgeOverflowsBefore;
  inputSelfTestNavigationPassed =
      inputSelfTestPageToLauncher && inputSelfTestLauncherToPage &&
      inputSelfTestNestedBack && inputSelfTestLongPressBack &&
      inputSelfTestDoubleClickFallback &&
      inputSelfTestSecondPressGrace && inputSelfTestQueuedPulse &&
      inputSelfTestQueueHealthy &&
      inputSelfTestPageRoundTrips == expectedRoundTrips &&
      inputSelfTestBackActions == expectedBackActions;
  if (!countersPassed && !strcmp(inputSelfTestFailure, "NONE")) {
    inputSelfTestFailure = "COUNTERS";
    inputSelfTestFailurePage = "ALL";
  }
  if (!gpioHigh && !strcmp(inputSelfTestFailure, "NONE")) {
    inputSelfTestFailure = "GPIO_HIGH";
    inputSelfTestFailurePage = "GPIO5";
  }

  currentPage = savedPage;
  menuOpen = savedMenuOpen;
  menuIndex = savedMenuIndex;
  appletIndex = savedAppletIndex;
  ledField = savedLedField;
  soundField = savedSoundField;
  menuDwellArmed = savedMenuDwellArmed;
  menuSelectionChangedAt = savedMenuSelectionChangedAt;
  singleClickPending = savedSingleClickPending;
  singleClickPendingAt = savedSingleClickPendingAt;
  secondClickInProgress = savedSecondClickInProgress;
  needsRender = savedNeedsRender;
  resetButtonDebounceState(false);
  inputSelfTestRestored = currentPage == savedPage && menuOpen == savedMenuOpen &&
                          menuIndex == savedMenuIndex &&
                          appletIndex == savedAppletIndex &&
                          ledField == savedLedField && soundField == savedSoundField;
  if (!inputSelfTestRestored && !strcmp(inputSelfTestFailure, "NONE")) {
    inputSelfTestFailure = "RESTORE";
    inputSelfTestFailurePage = pageName(savedPage);
  }
  return countersPassed && gpioHigh && inputSelfTestNavigationPassed &&
         inputSelfTestRestored;
}

void beginInputCapture() {
  pinMode(kEncoderButton, INPUT_PULLUP);
  pinMode(kEncoderA, INPUT);
  pinMode(kEncoderB, INPUT);
  const uint32_t levels = GPIO.in;
  encoderPreviousRaw = static_cast<uint8_t>(
      (((levels >> kEncoderA) & 1U) << 1) | ((levels >> kEncoderB) & 1U));
  resetButtonDebounceState(((levels >> kEncoderButton) & 1U) == 0);
  attachInterrupt(digitalPinToInterrupt(kEncoderA), captureEncoderEdge, CHANGE);
  attachInterrupt(digitalPinToInterrupt(kEncoderB), captureEncoderEdge, CHANGE);
  attachInterrupt(digitalPinToInterrupt(kEncoderButton), captureButtonEdge, CHANGE);
  Serial.println("input capture=edge-queued-interrupt");
}

bool copyAsciiJsonString(JsonVariantConst value, char *output, size_t capacity) {
  if (!value.is<const char *>()) return false;
  const char *text = value.as<const char *>();
  const size_t length = strnlen(text, capacity);
  if (!length || length >= capacity) return false;
  for (size_t i = 0; i < length; ++i) {
    const uint8_t character = static_cast<uint8_t>(text[i]);
    if (character < 0x20 || character > 0x7e) return false;
  }
  memcpy(output, text, length + 1);
  return true;
}

bool acceptPayload(const char *line, bool authenticatedPull = false,
                   bool *freshPayload = nullptr) {
  if (freshPayload) *freshPayload = false;
  if (!line || strlen(line) > kMaxBudgetPayloadBytes) {
    Serial.println("payload rejected");
    return false;
  }
  static StaticJsonDocument<2048> document;
  document.clear();
  const DeserializationError error = deserializeJson(document, line);
  if (error || document["schema"] != 1 ||
      strcmp(document["kind"] | "", "codex_budget") ||
      !document["windows"].is<JsonArray>()) {
    Serial.println("payload rejected");
    return false;
  }

  if (!document["ok"].is<bool>()) {
    Serial.println("payload rejected");
    return false;
  }
  const bool payloadOk = document["ok"].as<bool>();
  const JsonArrayConst windows = document["windows"].as<JsonArrayConst>();
  if (windows.size() > kMaxWindows || (payloadOk && !windows.size())) {
    Serial.println("payload rejected");
    return false;
  }

  uint32_t sourceAgeSeconds = 0;
  if (authenticatedPull) {
    const size_t expectedRootFields = payloadOk ? 8 : 9;
    if (document.size() != expectedRootFields || !document["stale"].is<bool>()) {
      Serial.println("payload rejected");
      return false;
    }
    if (payloadOk) {
      const JsonVariantConst age = document["sourceAgeSeconds"];
      const JsonVariantConst checkedAt = document["checkedAt"];
      if ((!checkedAt.is<unsigned long>() && !checkedAt.is<long>()) ||
          checkedAt.as<long>() <= 0 ||
          (!age.is<unsigned long>() && !age.is<long>()) ||
          age.as<long>() < 0 || age.as<unsigned long>() > kMaxSourceAgeSeconds ||
          (document["stale"] | true)) {
        Serial.println("payload rejected");
        return false;
      }
      sourceAgeSeconds = age.as<uint32_t>();
    } else {
      const bool stale = document["stale"] | false;
      const char *expectedError = stale ? "Stale data" : "No data";
      const JsonVariantConst age = document["sourceAgeSeconds"];
      const JsonVariantConst checkedAt = document["checkedAt"];
      const bool staleMetadataValid =
          stale && (checkedAt.is<unsigned long>() || checkedAt.is<long>()) &&
          checkedAt.as<long>() > 0 &&
          (age.is<unsigned long>() || age.is<long>()) &&
          age.as<long>() > static_cast<long>(kMaxSourceAgeSeconds);
      const bool noDataMetadataValid =
          !stale && checkedAt.isNull() && age.isNull();
      if (windows.size() || strcmp(document["error"] | "", expectedError) ||
          (!staleMetadataValid && !noDataMetadataValid)) {
        Serial.println("payload rejected");
        return false;
      }
      setBudgetPullLastResult(stale ? "stale" : "no-data");
      return true;
    }
  }

  BudgetState next;
  next.received = true;
  next.receivedAt = millis() - sourceAgeSeconds * 1000U;
  if (!copyAsciiJsonString(document["checkedText"], next.checkedText,
                           sizeof(next.checkedText))) {
    Serial.println("payload rejected");
    return false;
  }
  const char *errorText = document["error"] | "NO DATA";
  if (strlen(errorText) >= sizeof(next.error)) {
    Serial.println("payload rejected");
    return false;
  }
  strlcpy(next.error, errorText, sizeof(next.error));
  for (JsonObjectConst item : windows) {
    if (authenticatedPull && item.size() != 6) {
      Serial.println("payload rejected");
      return false;
    }
    const JsonVariantConst remainingValue = item["remaining"];
    if (!remainingValue.is<float>() && !remainingValue.is<long>() &&
        !remainingValue.is<unsigned long>()) {
      Serial.println("payload rejected");
      return false;
    }
    const float remaining = remainingValue.as<float>();
    if (!std::isfinite(remaining) || remaining < 0.0f || remaining > 100.0f) {
      Serial.println("payload rejected");
      return false;
    }
    const JsonVariantConst resetsAt = item["resetsAt"];
    const bool resetsAtValid =
        resetsAt.isNull() ||
        (resetsAt.is<unsigned long>() && resetsAt.as<unsigned long>() > 0 &&
         resetsAt.as<unsigned long>() <= 4102444800UL) ||
        (resetsAt.is<long>() && resetsAt.as<long>() > 0);
    if (authenticatedPull && !resetsAtValid) {
      Serial.println("payload rejected");
      return false;
    }
    BudgetWindow &destination = next.windows[next.count++];
    if (!copyAsciiJsonString(item["id"], destination.id, sizeof(destination.id)) ||
        !copyAsciiJsonString(item["label"], destination.label,
                             sizeof(destination.label)) ||
        !copyAsciiJsonString(item["window"], destination.window,
                             sizeof(destination.window)) ||
        !copyAsciiJsonString(item["resetText"], destination.resetText,
                             sizeof(destination.resetText))) {
      Serial.println("payload rejected");
      return false;
    }
    destination.remaining = remaining;
  }
  if (payloadOk && !next.count) {
    Serial.println("payload rejected");
    return false;
  }
  next.valid = payloadOk && next.count > 0;
  const bool wasStale = isStale();
  const bool refreshOverview = overviewPresentationChanged(budget, next) || wasStale;
  const bool refreshWindows = windowsPresentationChanged(budget, next) || wasStale;
  int retainedWindow = next.count
                           ? constrain(windowIndex, 0, static_cast<int>(next.count) - 1)
                           : 0;
  if (budget.count && next.count) {
    const int previousWindow = constrain(windowIndex, 0,
                                         static_cast<int>(budget.count) - 1);
    for (size_t i = 0; i < next.count; ++i) {
      if (!strcmp(budget.windows[previousWindow].id, next.windows[i].id)) {
        retainedWindow = static_cast<int>(i);
        break;
      }
    }
  }
  budget = next;
  if (freshPayload) *freshPayload = true;
  windowIndex = retainedWindow;
  if (!menuOpen && ((currentPage == Page::Overview && refreshOverview) ||
                    (currentPage == Page::Windows && refreshWindows))) {
    needsRender = true;
  }
  Serial.printf("budget update: %u windows\n", static_cast<unsigned>(budget.count));
  return true;
}

void budgetPullWorker(void *) {
  imdisplay::budget_pull::PullRequest request;
  while (true) {
    if (xQueueReceive(budgetPullRequestQueue, &request, portMAX_DELAY) != pdTRUE) {
      continue;
    }
    imdisplay::budget_pull::PullResult result;
    imdisplay::budget_pull::performPull(request, result);
    xQueueOverwrite(budgetPullResultQueue, &result);
  }
}

void startBudgetPullWorker() {
  budgetPullRequestQueue =
      xQueueCreate(1, sizeof(imdisplay::budget_pull::PullRequest));
  budgetPullResultQueue =
      xQueueCreate(1, sizeof(imdisplay::budget_pull::PullResult));
  if (!budgetPullRequestQueue || !budgetPullResultQueue ||
      xTaskCreate(budgetPullWorker, "budget-pull", kBudgetPullTaskStackBytes,
                  nullptr, 1, &budgetPullTaskHandle) != pdPASS) {
    if (budgetPullRequestQueue) vQueueDelete(budgetPullRequestQueue);
    if (budgetPullResultQueue) vQueueDelete(budgetPullResultQueue);
    budgetPullRequestQueue = nullptr;
    budgetPullResultQueue = nullptr;
    budgetPullTaskHandle = nullptr;
    setBudgetPullLastResult("worker-unavailable");
    return;
  }
  budgetPullWorkerReady = true;
}

bool codexBudgetRelevant() {
  return codexAppletInstalled && !menuOpen &&
         (currentPage == Page::Overview || currentPage == Page::Windows);
}

void requestBudgetPull() {
  imdisplay::budget_pull::requestImmediate(budgetPullSchedule);
}

void pollBudgetPull() {
  if (!budgetPullWorkerReady) return;
  imdisplay::budget_pull::PullResult result;
  if (budgetPullInFlight &&
      xQueueReceive(budgetPullResultQueue, &result, 0) == pdTRUE) {
    budgetPullInFlight = false;
    bool freshPayload = false;
    const bool authenticated = result.code == imdisplay::budget_pull::PullResultCode::Success;
    const bool accepted = authenticated &&
                          acceptPayload(result.body, true, &freshPayload);
    const bool success = accepted && freshPayload;
    if (success) {
      ++budgetPullSuccesses;
      setBudgetPullLastResult("fresh");
    } else {
      ++budgetPullFailures;
      if (!authenticated) {
        setBudgetPullLastResult(imdisplay::budget_pull::resultCodeName(result.code));
      } else if (!accepted) {
        setBudgetPullLastResult("payload-rejected");
      }
    }
    imdisplay::budget_pull::recordResult(budgetPullSchedule, millis(), success);
    Serial.printf("budget pull result=%s fresh=%d failures=%u\n",
                  budgetPullLastResult, success,
                  budgetPullSchedule.consecutiveFailures);
  }

  const bool configured = budgetPullSettings.configured && budgetPullSettings.enabled;
  if (!imdisplay::budget_pull::shouldStart(
          budgetPullSchedule, millis(), codexBudgetRelevant(),
          WiFi.status() == WL_CONNECTED, configured, budgetPullInFlight,
          otaInProgress)) {
    return;
  }
  if (xQueueSend(budgetPullRequestQueue, &budgetPullSettings.request, 0) != pdTRUE) {
    setBudgetPullLastResult("worker-queue-full");
    return;
  }
  imdisplay::budget_pull::recordAttempt(budgetPullSchedule, millis());
  budgetPullInFlight = true;
  ++budgetPullAttempts;
  setBudgetPullLastResult("in-flight");
}

bool requireWebAuthentication() {
  if (webServer.authenticate(kWebUser, accessPointPassword)) return true;
  webServer.requestAuthentication();
  return false;
}

void sendState() {
  if (!requireWebAuthentication()) return;
  static StaticJsonDocument<5120> document;
  document.clear();
  document["firmware"] = CODEX_BUDGET_FIRMWARE_VERSION;
  document["product"] = "imDisplay";
  document["bootId"] = bootId;
  document["page"] = pageName(currentPage);
  document["menu"] = menuOpen;
  document["received"] = budget.received;
  document["valid"] = budget.valid;
  document["ageSeconds"] = ageSeconds();
  document["ota"] = otaInProgress;
  document["freeHeap"] = ESP.getFreeHeap();
  document["minFreeHeap"] = ESP.getMinFreeHeap();
  document["stationConfigured"] = stationConfigured;
  document["stationConnected"] = WiFi.status() == WL_CONNECTED;
  document["accessPoint"] = accessPointName;
  document["accessPointEnabled"] = recoveryAccessPointEnabled;
  document["accessPointReady"] = accessPointReady;
  if (WiFi.status() == WL_CONNECTED) {
    document["stationSsid"] = WiFi.SSID();
    document["stationIp"] = WiFi.localIP().toString();
  }
  JsonObject pull = document.createNestedObject("budgetPull");
  pull["protocol"] = imdisplay::budget_pull::kProtocolVersion;
  pull["configured"] = budgetPullSettings.configured;
  pull["enabled"] = budgetPullSettings.enabled;
  pull["legacyPushEnabled"] = budgetPullSettings.legacyPushEnabled;
  pull["workerReady"] = budgetPullWorkerReady;
  pull["inFlight"] = budgetPullInFlight;
  pull["attempts"] = budgetPullAttempts;
  pull["successes"] = budgetPullSuccesses;
  pull["failures"] = budgetPullFailures;
  pull["consecutiveFailures"] = budgetPullSchedule.consecutiveFailures;
  pull["lastResult"] = budgetPullLastResult;
  pull["lastResultAgeSeconds"] = (millis() - budgetPullLastResultAt) / 1000;
  pull["transport"] = "local-http+hmac-sha256";
  pull["discovery"] = "runtime-mdns-or-private-ip";
  if (budgetPullSettings.configured) {
    pull["macHost"] = budgetPullSettings.request.host;
    pull["macPort"] = budgetPullSettings.request.port;
  }
  if (budgetPullTaskHandle) {
    pull["workerStackHighWater"] = uxTaskGetStackHighWaterMark(budgetPullTaskHandle);
  }
  const BudgetWindow *primary = primaryWindow();
  if (primary) {
    document["remaining"] = primary->remaining;
    document["reset"] = primary->resetText;
  }
  JsonArray applets = document.createNestedArray("applets");
  JsonObject codex = applets.createNestedObject();
  codex["id"] = "codex";
  codex["name"] = "Codex Budget";
  codex["installed"] = codexAppletInstalled;
  JsonObject timer = applets.createNestedObject();
  timer["id"] = "timer";
  timer["name"] = "Work Timer";
  timer["installed"] = timerAppletInstalled;
  JsonObject led = document.createNestedObject("leds");
  led["ready"] = rgbPixelsReady;
  led["preset"] = kLedPresets[ledPresetIndex].name;
  led["brightness"] = ledBrightness;
  led["feedback"] = ledFeedbackEnabled;
  JsonObject screen = document.createNestedObject("display");
  screen["width"] = kWidth;
  screen["height"] = kHeight;
  screen["pixels"] = kWidth * kHeight;
  screen["brightness"] = displayBrightness;
  screen["backlightDuty"] = display.backlightDuty();
  screen["mirrorFormat"] = "BMP4";
  screen["mirrorBytes"] = display.mirrorBytes();
  screen["mirrorUnknownColors"] = display.mirrorUnknownColors();
  JsonObject renderState = document.createNestedObject("render");
  renderState["fullFrames"] = fullRenderCount;
  renderState["lastFullAtMs"] = lastFullRenderAt;
  renderState["lastFullAgeMs"] = millis() - lastFullRenderAt;
  renderState["lastFullDurationUs"] = lastFullRenderDurationUs;
  renderState["maxFullDurationUs"] = maxFullRenderDurationUs;
  renderState["timerPartialUpdates"] = timerPartialRenderCount;
  renderState["menuPartialUpdates"] = menuPartialRenderCount;
  JsonObject sound = document.createNestedObject("sound");
  sound["enabled"] = soundProfileIndex > 0 && soundVolume > 0;
  sound["profile"] = kSoundProfiles[soundProfileIndex].name;
  sound["volume"] = soundVolume;
  sound["engineReady"] = soundTaskReady;
  sound["driverReady"] = soundEngine.driverReady();
  sound["lastError"] = soundEngine.lastError();
  sound["active"] = soundEngine.active();
  sound["queued"] = soundEngine.queuedCount();
  sound["rejected"] = soundEngine.rejectedCount();
  sound["played"] = soundEngine.playedCount();
  sound["bytesWritten"] = soundEngine.bytesWritten();
  sound["writeFailures"] = soundEngine.writeFailures();
  sound["lastCue"] = soundCueName(soundEngine.lastCue());
  JsonObject timerState = document.createNestedObject("timer");
  timerState["running"] = workTimer.running;
  timerState["remainingSeconds"] = timerRemainingSeconds();
  timerState["durationMinutes"] = workTimer.durationSeconds / 60;
  JsonObject input = document.createNestedObject("input");
  uint32_t rawEdges = 0;
  uint32_t rawFalls = 0;
  uint32_t rawRises = 0;
  uint32_t edgeOverflows = 0;
  uint32_t lastRawEdgeAt = 0;
  uint8_t queuedEdges = 0;
  uint32_t rawEncoderDetents = 0;
  uint32_t encoderDetentOverflows = 0;
  uint8_t queuedEncoderDetents = 0;
  portENTER_CRITICAL(&buttonMux);
  rawEdges = buttonRawEdgeCount;
  rawFalls = buttonRawFallCount;
  rawRises = buttonRawRiseCount;
  edgeOverflows = buttonEdgeOverflowCount;
  lastRawEdgeAt = lastButtonRawEdgeAt;
  queuedEdges = (buttonEdgeHead + kButtonEdgeQueueCapacity - buttonEdgeTail) %
                kButtonEdgeQueueCapacity;
  portEXIT_CRITICAL(&buttonMux);
  portENTER_CRITICAL(&encoderMux);
  rawEncoderDetents = encoderRawDetentCount;
  encoderDetentOverflows = encoderDetentOverflowCount;
  queuedEncoderDetents =
      (encoderDetentHead + kEncoderDetentQueueCapacity - encoderDetentTail) %
      kEncoderDetentQueueCapacity;
  portEXIT_CRITICAL(&encoderMux);
  input["capture"] = "edge-queued-interrupt";
  input["encoderCapture"] = "ordered-detent-queue";
  input["buttonPin"] = kEncoderButton;
  input["rawEdges"] = rawEdges;
  input["rawFalls"] = rawFalls;
  input["rawRises"] = rawRises;
  input["edgeQueueDepth"] = queuedEdges;
  input["edgeQueueOverflows"] = edgeOverflows;
  input["lastRawEdgeAgeMs"] = rawEdges ? millis() - lastRawEdgeAt : 0;
  input["lastPressDurationMs"] = lastButtonPressDurationMs;
  input["suppressedEncoderDetents"] = suppressedEncoderDetentCount;
  input["encoderEvents"] = encoderEventCount;
  input["encoderRawDetents"] = rawEncoderDetents;
  input["encoderDetentQueueDepth"] = queuedEncoderDetents;
  input["encoderDetentQueueOverflows"] = encoderDetentOverflows;
  input["buttonReleases"] = buttonReleaseCount;
  input["shortPresses"] = shortPressCount;
  input["doubleClicks"] = doubleClickCount;
  input["longPresses"] = longPressCount;
  input["longPressThresholdMs"] = kButtonLongPressMs;
  input["doubleClickWindowMs"] = kDoubleClickMs;
  input["secondPressLatched"] = secondClickInProgress;
  input["secondPressLatches"] = secondClickLatchCount;
  input["navigationTransitions"] = navigationTransitionCount;
  input["lastNavigationMethod"] = lastNavigationMethod;
  input["lastNavigationPage"] = pageName(lastNavigationPage);
  input["lastNavigationFromMenu"] = lastNavigationFromMenu;
  input["lastNavigationToMenu"] = lastNavigationToMenu;
  input["remoteEvents"] = remoteInputCount;
  input["dwellSelections"] = dwellSelectionCount;
  input["rotaryForwardGestures"] = rotaryForwardGestureCount;
  input["rotaryBackGestures"] = rotaryBackGestureCount;
  input["rotaryGestureWindowMs"] = kRotaryGestureWindowMs;
  input["rotaryPendingDetents"] = rotaryGesture.pendingDetents();
  input["rotaryPassThrough"] = rotaryGesture.passThrough();
  input["lastRemoteAction"] = lastRemoteAction;
  input["lastDirection"] = lastEncoderDirection;
  input["encoderA"] = digitalRead(kEncoderA);
  input["encoderB"] = digitalRead(kEncoderB);
  input["buttonPressed"] = digitalRead(kEncoderButton) == LOW;
  // Read-only raw levels let authenticated diagnostics compare released and
  // held hardware without reconfiguring, driving, or interrupting any pin.
  input["gpioLevels0To31"] = static_cast<uint32_t>(GPIO.in);
  input["gpioLevels32To39"] = static_cast<uint32_t>(GPIO.in1.val & 0xffU);
  input["settingsPendingMask"] = dirtySettings;
  JsonObject persistence = document.createNestedObject("persistence");
  persistence["commitDelayMs"] = kSettingsCommitDelayMs;
  persistence["sessions"] = settingsPersistSessions;
  persistence["keyWriteAttempts"] = settingsKeyWriteAttempts;
  persistence["failures"] = settingsPersistFailures;
  persistence["lastMask"] = lastPersistedSettings;
  persistence["pendingMask"] = dirtySettings;
  JsonObject selfTest = document.createNestedObject("selfTest");
  selfTest["schema"] = 4;
  selfTest["inputRuns"] = inputSelfTestRuns;
  selfTest["inputPassed"] = inputSelfTestPassed;
  selfTest["inputLastAtMs"] = inputSelfTestAt;
  selfTest["navigationPassed"] = inputSelfTestNavigationPassed;
  selfTest["pageToLauncher"] = inputSelfTestPageToLauncher;
  selfTest["launcherToPage"] = inputSelfTestLauncherToPage;
  selfTest["nestedBack"] = inputSelfTestNestedBack;
  selfTest["longPressBack"] = inputSelfTestLongPressBack;
  selfTest["doubleClickFallback"] = inputSelfTestDoubleClickFallback;
  selfTest["secondPressGrace"] = inputSelfTestSecondPressGrace;
  selfTest["queuedPulse"] = inputSelfTestQueuedPulse;
  selfTest["edgeQueueHealthy"] = inputSelfTestQueueHealthy;
  selfTest["stateRestored"] = inputSelfTestRestored;
  selfTest["pageRoundTrips"] = inputSelfTestPageRoundTrips;
  selfTest["pageRoundTripsExpected"] = 9;
  selfTest["backActions"] = inputSelfTestBackActions;
  selfTest["backActionsExpected"] = 4;
  selfTest["failure"] = inputSelfTestFailure;
  selfTest["failurePage"] = inputSelfTestFailurePage;
  JsonObject screenModel = document.createNestedObject("screen");
  screenModel["mode"] = menuOpen ? "MENU" : "PAGE";
  screenModel["page"] = pageName(currentPage);
  if (menuOpen) {
    MenuEntry entries[kMaxMenuEntries];
    const int count = buildMenu(entries);
    menuIndex = constrain(menuIndex, 0, count - 1);
    screenModel["selectionIndex"] = menuIndex;
    screenModel["selection"] = entries[menuIndex].label;
    screenModel["dwellArmed"] = menuDwellArmed;
  } else if (currentPage == Page::Windows) {
    screenModel["selectionIndex"] = windowIndex;
  } else if (currentPage == Page::Applets) {
    screenModel["selectionIndex"] = appletIndex;
  } else if (currentPage == Page::Leds) {
    screenModel["selectionIndex"] = ledField;
  } else if (currentPage == Page::Sounds) {
    screenModel["selectionIndex"] = soundField;
  }
  if (document.overflowed()) {
    webServer.send(500, "text/plain", "State serialization overflow.\n");
    return;
  }
  String body;
  if (!body.reserve(measureJson(document) + 1) || !serializeJson(document, body)) {
    webServer.send(500, "text/plain", "State serialization failed.\n");
    return;
  }
  webServer.sendHeader("Cache-Control", "no-store");
  webServer.send(200, "application/json", body);
}

void sendScreenBmp() {
  if (!requireWebAuthentication()) return;
  webServer.sendHeader("Cache-Control", "no-store");
  webServer.sendHeader("Content-Disposition", "inline; filename=imdisplay-screen.bmp");
  webServer.setContentLength(display.bmpBytes());
  webServer.send(200, "image/bmp", "");
  if (!display.writeBmp(webServer.client())) {
    Serial.println("screen mirror transfer incomplete");
  }
}

void remoteControl() {
  if (!requireWebAuthentication()) return;
  const String target = webServer.arg("page");
  const Page previousPage = currentPage;
  const bool previousMenu = menuOpen;
  bool accepted = true;
  if (target == "menu") {
    if (!menuOpen) openMenu();
  } else if ((target.equalsIgnoreCase("overview") || target.equalsIgnoreCase("codex")) &&
             codexAppletInstalled) {
    currentPage = Page::Overview;
    menuOpen = false;
  } else if (target.equalsIgnoreCase("windows") && codexAppletInstalled) {
    currentPage = Page::Windows;
    menuOpen = false;
  } else if (target.equalsIgnoreCase("timer") && timerAppletInstalled) {
    currentPage = Page::Timer;
    menuOpen = false;
  } else if (target.equalsIgnoreCase("applets")) {
    currentPage = Page::Applets;
    menuOpen = false;
  } else if (target.equalsIgnoreCase("leds")) {
    currentPage = Page::Leds;
    menuOpen = false;
  } else if (target.equalsIgnoreCase("display")) {
    currentPage = Page::Display;
    menuOpen = false;
  } else if (target.equalsIgnoreCase("sounds")) {
    currentPage = Page::Sounds;
    menuOpen = false;
  } else if (target.equalsIgnoreCase("connection")) {
    currentPage = Page::Connection;
    menuOpen = false;
  } else if (target.equalsIgnoreCase("about")) {
    currentPage = Page::About;
    menuOpen = false;
  } else {
    accepted = false;
  }
  if (accepted && (currentPage != previousPage || menuOpen != previousMenu)) {
    needsRender = true;
    if (!menuOpen &&
        (currentPage == Page::Overview || currentPage == Page::Windows)) {
      requestBudgetPull();
    }
  }
  webServer.sendHeader("Cache-Control", "no-store");
  webServer.send(accepted ? 204 : 404);
}

void remoteInput() {
  if (!requireWebAuthentication()) return;
  const String action = webServer.arg("action");
  if (action == "press") {
    lastRemoteAction = "PRESS";
    shortPress(InputSource::Remote);
  } else if (action == "back") {
    lastRemoteAction = "BACK";
    navigateBack(InputSource::Remote);
  } else if (action == "hold") {
    lastRemoteAction = "HOLD";
    longPress(InputSource::Remote);
  } else if (action == "clockwise") {
    lastRemoteAction = "CLOCKWISE";
    encoderMoved(1, InputSource::Remote);
  } else if (action == "counterclockwise") {
    lastRemoteAction = "COUNTERCLOCKWISE";
    encoderMoved(-1, InputSource::Remote);
  } else {
    webServer.send(400, "text/plain",
                   "Use action=press|back|hold|clockwise|counterclockwise.\n");
    return;
  }
  webServer.sendHeader("Cache-Control", "no-store");
  webServer.send(204);
}

void remoteSelfTest() {
  if (!requireWebAuthentication()) return;
  if (webServer.arg("input") != "1") {
    webServer.send(400, "text/plain", "Use input=1.\n");
    return;
  }
  if (otaInProgress || digitalRead(kEncoderButton) == LOW || singleClickPending ||
      secondClickInProgress) {
    webServer.send(409, "text/plain", "Input self-test is not currently safe.\n");
    return;
  }
  uint32_t releaseDelta = 0;
  uint32_t shortDelta = 0;
  uint32_t doubleDelta = 0;
  uint32_t longDelta = 0;
  uint32_t latchDelta = 0;
  ++inputSelfTestRuns;
  inputSelfTestPassed = runInputSelfTest(
      releaseDelta, shortDelta, doubleDelta, longDelta, latchDelta);
  inputSelfTestAt = millis();

  StaticJsonDocument<768> result;
  result["pass"] = inputSelfTestPassed;
  result["schema"] = 4;
  result["capture"] = "edge-queued-interrupt";
  result["buttonPin"] = kEncoderButton;
  result["releaseDelta"] = releaseDelta;
  result["shortPressDelta"] = shortDelta;
  result["doubleClickDelta"] = doubleDelta;
  result["longPressDelta"] = longDelta;
  result["secondPressLatchDelta"] = latchDelta;
  result["navigationPassed"] = inputSelfTestNavigationPassed;
  result["pageToLauncher"] = inputSelfTestPageToLauncher;
  result["launcherToPage"] = inputSelfTestLauncherToPage;
  result["nestedBack"] = inputSelfTestNestedBack;
  result["longPressBack"] = inputSelfTestLongPressBack;
  result["doubleClickFallback"] = inputSelfTestDoubleClickFallback;
  result["secondPressGrace"] = inputSelfTestSecondPressGrace;
  result["queuedPulse"] = inputSelfTestQueuedPulse;
  result["edgeQueueHealthy"] = inputSelfTestQueueHealthy;
  result["stateRestored"] = inputSelfTestRestored;
  result["pageRoundTrips"] = inputSelfTestPageRoundTrips;
  result["pageRoundTripsExpected"] = 9;
  result["backActions"] = inputSelfTestBackActions;
  result["backActionsExpected"] = 4;
  result["failure"] = inputSelfTestFailure;
  result["failurePage"] = inputSelfTestFailurePage;
  String body;
  serializeJson(result, body);
  webServer.sendHeader("Cache-Control", "no-store");
  webServer.send(inputSelfTestPassed ? 200 : 500, "application/json", body);
}

void remoteApplets() {
  if (!requireWebAuthentication()) return;
  const String id = webServer.arg("id");
  const String installedText = webServer.arg("installed");
  if ((id != "codex" && id != "timer") ||
      (installedText != "0" && installedText != "1")) {
    webServer.send(400, "text/plain", "Use id=codex|timer and installed=0|1.\n");
    return;
  }
  setAppletInstalled(id == "codex" ? 0 : 1, installedText == "1");
  webServer.sendHeader("Cache-Control", "no-store");
  webServer.send(204);
}

bool parseBoundedInt(const String &text, int minimum, int maximum, int &value) {
  if (!text.length()) return false;
  char *end = nullptr;
  const long parsed = strtol(text.c_str(), &end, 10);
  if (end == text.c_str() || *end != '\0' || parsed < minimum || parsed > maximum) {
    return false;
  }
  value = static_cast<int>(parsed);
  return true;
}

void remoteLeds() {
  if (!requireWebAuthentication()) return;
  int preset = ledPresetIndex;
  int brightness = ledBrightness;
  bool feedback = ledFeedbackEnabled;
  if (webServer.hasArg("preset")) {
    preset = -1;
    for (size_t i = 0; i < kLedPresetCount; ++i) {
      if (webServer.arg("preset").equalsIgnoreCase(kLedPresets[i].name)) {
        preset = i;
        break;
      }
    }
  }
  if (webServer.hasArg("brightness") &&
      !parseBoundedInt(webServer.arg("brightness"), 0, 100, brightness)) {
    webServer.send(400, "text/plain", "LED brightness must be 0..100.\n");
    return;
  }
  if (webServer.hasArg("feedback")) {
    if (webServer.arg("feedback") != "0" && webServer.arg("feedback") != "1") {
      webServer.send(400, "text/plain", "LED feedback must be 0 or 1.\n");
      return;
    }
    feedback = webServer.arg("feedback") == "1";
  }
  if (preset < 0) {
    webServer.send(400, "text/plain", "Invalid LED preset.\n");
    return;
  }
  setLedSettings(preset, brightness, false);
  setLedFeedbackEnabled(feedback, false);
  flushScheduledSettings(true);
  webServer.sendHeader("Cache-Control", "no-store");
  webServer.send(204);
}

void remoteDisplay() {
  if (!requireWebAuthentication()) return;
  if (!webServer.hasArg("brightness")) {
    webServer.send(400, "text/plain", "Use brightness=10..100.\n");
    return;
  }
  int brightness = 0;
  if (!parseBoundedInt(webServer.arg("brightness"), kMinDisplayBrightness, 100,
                       brightness)) {
    webServer.send(400, "text/plain", "Display brightness must be 10..100.\n");
    return;
  }
  setDisplayBrightness(brightness);
  webServer.sendHeader("Cache-Control", "no-store");
  webServer.send(204);
}

void remoteRecoveryWifi() {
  if (!requireWebAuthentication()) return;
  const String enabled = webServer.arg("enabled");
  if (enabled != "0" && enabled != "1") {
    webServer.send(400, "text/plain", "Recovery Wi-Fi enabled must be 0 or 1.\n");
    return;
  }
  setRecoveryAccessPointEnabled(enabled == "1");
  webServer.sendHeader("Cache-Control", "no-store");
  webServer.send(202, "text/plain",
                 enabled == "1" ? "Recovery Wi-Fi enabling.\n"
                                  : "Recovery Wi-Fi disabling.\n");
}

void remoteSound() {
  if (!requireWebAuthentication()) return;
  int profile = soundProfileIndex;
  int volume = soundVolume;
  if (webServer.hasArg("profile")) {
    profile = -1;
    for (size_t i = 0; i < kSoundProfileCount; ++i) {
      if (webServer.arg("profile").equalsIgnoreCase(kSoundProfiles[i].name)) {
        profile = i;
        break;
      }
    }
  }
  if (webServer.hasArg("volume") &&
      !parseBoundedInt(webServer.arg("volume"), 0, 100, volume)) {
    webServer.send(400, "text/plain", "Sound volume must be 0..100.\n");
    return;
  }
  const String test = webServer.arg("test");
  if (profile < 0 || (test.length() && test != "1")) {
    webServer.send(400, "text/plain", "Invalid sound profile, volume, or test value.\n");
    return;
  }
  setSoundSettings(profile, volume);
  if (test == "1") playSound(SoundEngine::Cue::Preview, false, true);
  webServer.sendHeader("Cache-Control", "no-store");
  webServer.send(204);
}

void remoteTimer() {
  if (!requireWebAuthentication()) return;
  if (!timerAppletInstalled) {
    webServer.send(409, "text/plain", "Work Timer is not installed.\n");
    return;
  }
  if (webServer.hasArg("minutes")) {
    int minutes = 0;
    if (!parseBoundedInt(webServer.arg("minutes"), 5, 120, minutes)) {
      webServer.send(400, "text/plain", "Timer minutes must be 5..120.\n");
      return;
    }
    setWorkTimerMinutes(minutes);
  }
  const String action = webServer.arg("action");
  if (action == "start") {
    startWorkTimer();
  } else if (action == "pause") {
    pauseWorkTimer();
  } else if (action == "reset") {
    resetWorkTimer();
  } else if (action.length()) {
    webServer.send(400, "text/plain", "Use action=start|pause|reset.\n");
    return;
  }
  webServer.sendHeader("Cache-Control", "no-store");
  webServer.send(204);
}

void remoteReboot() {
  if (!requireWebAuthentication()) return;
  if (otaInProgress) {
    webServer.send(409, "text/plain", "An update is in progress.\n");
    return;
  }
  flushScheduledSettings(true);
  webServer.sendHeader("Cache-Control", "no-store");
  webServer.send(202, "text/plain", "imDisplay is rebooting.\n");
  restartAt = millis() + 750;
}

bool saveBudgetPullConfiguration(bool enabled, const char *host, uint16_t port,
                                 const char *keyHex, bool legacyPushEnabled) {
  uint8_t decodedKey[32];
  if (!imdisplay::budget_pull::isValidPullHost(host) || port < 1024 ||
      !imdisplay::budget_pull::decodeKeyHex(keyHex, decodedKey)) {
    return false;
  }
  const bool changed =
      !budgetPullSettings.configured || budgetPullSettings.enabled != enabled ||
      budgetPullSettings.legacyPushEnabled != legacyPushEnabled ||
      strcmp(budgetPullSettings.request.host, host) ||
      budgetPullSettings.request.port != port ||
      memcmp(budgetPullSettings.request.key, decodedKey, sizeof(decodedKey));
  if (!changed) return true;

  preferences.begin("xsure-budget", false);
  bool saved = preferences.putBool("pull-valid", false) == 1;
  saved = preferences.putString("pull-host", host) == strlen(host) && saved;
  saved = preferences.putUShort("pull-port", port) == sizeof(port) && saved;
  saved = preferences.putString("pull-key", keyHex) == 64 && saved;
  saved = preferences.putBool("pull-on", enabled) == 1 && saved;
  saved = preferences.putBool("push-legacy", legacyPushEnabled) == 1 && saved;
  if (saved) saved = preferences.putBool("pull-valid", true) == 1;
  preferences.end();
  if (!saved) {
    loadBudgetPullSettings();
    return false;
  }

  budgetPullSettings.configured = true;
  budgetPullSettings.enabled = enabled;
  budgetPullSettings.legacyPushEnabled = legacyPushEnabled;
  strlcpy(budgetPullSettings.request.host, host,
          sizeof(budgetPullSettings.request.host));
  budgetPullSettings.request.port = port;
  memcpy(budgetPullSettings.request.key, decodedKey, sizeof(decodedKey));
  budgetPullSchedule = imdisplay::budget_pull::PullSchedule{};
  setBudgetPullLastResult(enabled ? "waiting" : "disabled");
  if (enabled) requestBudgetPull();
  return true;
}

void configureBudgetPull() {
  if (!requireWebAuthentication()) return;
  if (budgetPullInFlight) {
    webServer.send(409, "text/plain", "A budget pull is in progress; retry shortly.\n");
    return;
  }
  const String body = webServer.arg("plain");
  if (!body.length() || body.length() > kMaxBudgetPullConfigBytes) {
    webServer.send(400, "text/plain", "Invalid budget pull configuration.\n");
    return;
  }
  StaticJsonDocument<512> document;
  const DeserializationError error = deserializeJson(document, body);
  const JsonVariantConst portValue = document["macPort"];
  if (error || document.size() != 7 || document["schema"] != 1 ||
      strcmp(document["kind"] | "", "budget_pull_config") ||
      !document["enabled"].is<bool>() ||
      !document["legacyPushEnabled"].is<bool>() ||
      !document["macHost"].is<const char *>() ||
      !document["readOnlyKey"].is<const char *>() ||
      (!portValue.is<unsigned long>() && !portValue.is<long>()) ||
      portValue.as<long>() < 1024 || portValue.as<unsigned long>() > 65535) {
    webServer.send(400, "text/plain", "Invalid budget pull configuration.\n");
    return;
  }
  const char *host = document["macHost"].as<const char *>();
  const char *key = document["readOnlyKey"].as<const char *>();
  if (!saveBudgetPullConfiguration(document["enabled"].as<bool>(), host,
                                   portValue.as<uint16_t>(), key,
                                   document["legacyPushEnabled"].as<bool>())) {
    webServer.send(400, "text/plain", "Could not validate or save budget pull configuration.\n");
    return;
  }
  webServer.sendHeader("Cache-Control", "no-store");
  webServer.send(204);
}

void receiveHttpBudget() {
  if (!requireWebAuthentication()) return;
  if (!budgetPullSettings.legacyPushEnabled) {
    webServer.sendHeader("Cache-Control", "no-store");
    webServer.send(409, "text/plain", "Legacy budget push is disabled.\n");
    return;
  }
  const String body = webServer.arg("plain");
  const bool accepted = body.length() <= kMaxBudgetPayloadBytes &&
                        acceptPayload(body.c_str());
  webServer.sendHeader("Cache-Control", "no-store");
  webServer.send(accepted ? 204 : 400);
}

bool saveStationCredentials(const String &ssid, const String &password) {
  if (ssid.length() < 1 || ssid.length() > 32 || password.length() < 8 ||
      password.length() > 63) {
    return false;
  }
  preferences.begin("xsure-budget", true);
  const String currentSsid = preferences.getString("wifi-ssid", "");
  const String currentPassword = preferences.getString("wifi-pass", "");
  preferences.end();
  const bool changed = currentSsid != ssid || currentPassword != password;
  if (changed) {
    preferences.begin("xsure-budget", false);
    const bool saved = preferences.putString("wifi-ssid", ssid) == ssid.length() &&
                       preferences.putString("wifi-pass", password) == password.length();
    preferences.end();
    if (!saved) return false;
  }
  stationConfigured = true;
  if (changed || WiFi.status() != WL_CONNECTED || WiFi.SSID() != ssid) {
    WiFi.begin(ssid.c_str(), password.c_str());
    lastStationReconnectAt = millis();
  }
  if (!menuOpen && currentPage == Page::Connection) needsRender = true;
  return true;
}

bool acceptWifiPayload(const char *line) {
  StaticJsonDocument<256> document;
  if (deserializeJson(document, line) || strcmp(document["kind"] | "", "wifi_config")) {
    return false;
  }
  if (document["schema"] != 1 ||
      !saveStationCredentials(String(document["ssid"] | ""), String(document["password"] | ""))) {
    Serial.println("wifi rejected");
    return true;
  }
  Serial.println("wifi saved");
  return true;
}

void configureStation() {
  if (!requireWebAuthentication()) return;
  webServer.sendHeader("Cache-Control", "no-store");
  if (webServer.arg("forget") == "1") {
    preferences.begin("xsure-budget", false);
    preferences.remove("wifi-ssid");
    preferences.remove("wifi-pass");
    preferences.end();
    stationConfigured = false;
    WiFi.disconnect(false, true);
    if (!menuOpen && currentPage == Page::Connection) needsRender = true;
    webServer.sendHeader("Cache-Control", "no-store");
    webServer.send(200, "text/plain",
                   "Local Wi-Fi forgotten. Enable recovery Wi-Fi explicitly if needed.\n");
    return;
  }

  const String ssid = webServer.arg("ssid");
  const String password = webServer.arg("password");
  if (!saveStationCredentials(ssid, password)) {
    webServer.send(400, "text/plain", "Could not validate or save local Wi-Fi settings.\n");
    return;
  }
  webServer.sendHeader("Cache-Control", "no-store");
  webServer.send(202, "text/plain", "Saved. imDisplay is connecting to local Wi-Fi.\n");
}

void sendHome() {
  if (!requireWebAuthentication()) return;
  static constexpr char page[] PROGMEM = R"HTML(<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>imDisplay</title><style>
body{font:16px system-ui;background:#05080e;color:#f0f6ff;max-width:640px;margin:32px auto;padding:0 18px}
h1{font-size:28px}#remaining{font-size:64px;color:#2dd380;font-weight:800;margin:24px 0 4px}
button,a,input{font:inherit}button,a{display:inline-block;background:#1e293b;color:#fff;border:0;border-radius:9px;padding:12px 16px;margin:5px;text-decoration:none;font-weight:650}
input{display:block;width:100%;box-sizing:border-box;margin:8px 0;padding:10px;border-radius:7px;border:1px solid #334155;background:#0f172a;color:#fff}
.primary{background:#3182f6}.muted{color:#8494aa}code{color:#fbbf24}</style></head>
<body><h1>imDisplay</h1><div id="remaining">--%</div><div id="meta" class="muted">Waiting for quota</div>
<h2>Launcher</h2><div id="controls"></div>
<h2>Virtual Knob</h2><button onclick="knob('counterclockwise')">&#8634;</button><button class="primary" onclick="knob('press')">PRESS</button><button onclick="knob('clockwise')">&#8635;</button><button onclick="knob('back')">BACK</button><button onclick="knob('hold')">HOLD</button><p id="screenState" class="muted"></p>
<h2>Applets</h2><div id="applets"></div>
<h2>Work Timer</h2><button onclick="timer('start')">Start</button><button onclick="timer('pause')">Pause</button><button onclick="timer('reset')">Reset</button><p id="timer" class="muted"></p>
<h2>LED Settings</h2><div id="presets"></div><label>Brightness <span id="brightnessText"></span><input id="brightness" type="range" min="0" max="100" step="10" onchange="led(null,this.value,null)"></label><button id="ledFeedback" onclick="led(null,null,this.dataset.next)">Event glow</button>
<h2>Display Settings</h2><label>Backlight <span id="displayText"></span><input id="displayBrightness" type="range" min="10" max="100" step="10" onchange="screenBrightness(this.value)"></label><p id="displayGeometry" class="muted"></p>
<h2>Sound Settings</h2><div id="soundProfiles"></div><label>Volume <span id="soundText"></span><input id="soundVolume" type="range" min="0" max="100" step="10" onchange="sound(null,this.value,false)"></label><button onclick="sound(null,null,true)">Play gentle test</button>
<h2>Recovery Wi-Fi</h2><button id="recoveryWifi" onclick="recoveryWifi(this.dataset.next)"></button><p id="recoveryWifiState" class="muted"></p>
<p><a class="primary" href="/update">Install OTA update</a></p>
<p><button onclick="if(confirm('Reboot imDisplay?'))fetch('/api/reboot',{method:'POST'})">Reboot imDisplay</button></p>
<h2>Local Wi-Fi</h2><form method="POST" action="/api/wifi"><input name="ssid" maxlength="32" autocomplete="off" placeholder="Wi-Fi name" required>
<input name="password" type="password" minlength="8" maxlength="63" autocomplete="new-password" placeholder="Wi-Fi password" required><button class="primary">Save and connect</button></form>
<form method="POST" action="/api/wifi?forget=1"><button>Forget local Wi-Fi</button></form><p id="wifi" class="muted"></p>
<p class="muted">Authenticated local control. Firmware <code id="firmware">-</code></p>
<script>
const pages=['overview','windows','timer','applets','leds','display','sounds','connection','about','menu'];
const controls=document.querySelector('#controls');
for(const p of pages){const b=document.createElement('button');b.textContent=p.toUpperCase();b.onclick=()=>fetch('/api/control?page='+p,{method:'POST'});controls.append(b)}
for(const p of ['off','codex','focus','warm','alert','rainbow']){const b=document.createElement('button');b.textContent=p.toUpperCase();b.onclick=()=>led(p,null,null);document.querySelector('#presets').append(b)}
for(const p of ['mute','minimal','soft']){const b=document.createElement('button');b.textContent=p.toUpperCase();b.onclick=()=>sound(p,null,false);document.querySelector('#soundProfiles').append(b)}
async function applet(id,installed){await fetch('/api/applets?id='+id+'&installed='+(installed?1:0),{method:'POST'});refresh()}
async function led(p,b,f){const q=new URLSearchParams();if(p!==null)q.set('preset',p);if(b!==null)q.set('brightness',b);if(f!==null)q.set('feedback',f);await fetch('/api/leds?'+q,{method:'POST'});refresh()}
async function screenBrightness(b){await fetch('/api/display?brightness='+b,{method:'POST'});refresh()}
async function recoveryWifi(e){await fetch('/api/recovery-wifi?enabled='+e,{method:'POST'});setTimeout(refresh,500)}
async function sound(p,v,test){const q=new URLSearchParams();if(p!==null)q.set('profile',p);if(v!==null)q.set('volume',v);if(test)q.set('test','1');await fetch('/api/sound?'+q,{method:'POST'});refresh()}
async function timer(a){await fetch('/api/timer?action='+a,{method:'POST'});refresh()}
async function knob(a){await fetch('/api/input?action='+a,{method:'POST'});refresh()}
async function refresh(){const r=await fetch('/api/state',{cache:'no-store'});const s=await r.json();
document.querySelector('#remaining').textContent=s.remaining===undefined?'NO DATA':Math.round(s.remaining)+'%';
document.querySelector('#meta').textContent=(s.page||'')+' / '+(s.received?s.ageSeconds+'s old':'waiting');
document.querySelector('#wifi').textContent=s.stationConnected?'LAN '+s.stationIp+' / '+s.stationSsid:(s.stationConfigured?'Connecting to saved Wi-Fi':'Not configured');
document.querySelector('#firmware').textContent=s.firmware;
document.querySelector('#screenState').textContent=s.screen.mode+' / '+s.screen.page+(s.screen.selection?' / '+s.screen.selection:'');
document.querySelector('#timer').textContent=(s.timer.running?'Running ':'Paused ')+Math.floor(s.timer.remainingSeconds/60)+':'+String(s.timer.remainingSeconds%60).padStart(2,'0');
document.querySelector('#brightness').value=s.leds.brightness;document.querySelector('#brightnessText').textContent=s.leds.brightness+'% / '+s.leds.preset;
const lf=document.querySelector('#ledFeedback');lf.textContent='Event glow '+(s.leds.feedback?'ON':'OFF');lf.dataset.next=s.leds.feedback?'0':'1';
document.querySelector('#displayBrightness').value=s.display.brightness;document.querySelector('#displayText').textContent=s.display.brightness+'%';document.querySelector('#displayGeometry').textContent=s.display.width+' x '+s.display.height+' / '+s.display.pixels.toLocaleString()+' native pixels';
const rw=document.querySelector('#recoveryWifi');rw.textContent=s.accessPointEnabled?'Turn recovery Wi-Fi off':'Turn recovery Wi-Fi on';rw.dataset.next=s.accessPointEnabled?'0':'1';document.querySelector('#recoveryWifiState').textContent=s.accessPointReady?s.accessPoint+' is broadcasting':'Off';
document.querySelector('#soundVolume').value=s.sound.volume;document.querySelector('#soundText').textContent=s.sound.volume+'% / '+s.sound.profile;
const a=document.querySelector('#applets');a.replaceChildren();for(const x of s.applets){const b=document.createElement('button');b.textContent=(x.installed?'Uninstall ':'Install ')+x.name;b.onclick=()=>applet(x.id,!x.installed);a.append(b)}}
refresh();setInterval(refresh,10000);</script></body></html>)HTML";
  webServer.sendHeader("Cache-Control", "no-store");
  webServer.send_P(200, "text/html", page);
}

void sendUpdateForm() {
  if (!requireWebAuthentication()) return;
  static constexpr char form[] PROGMEM = R"HTML(<!doctype html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1"><title>imDisplay OTA</title>
<style>body{font:16px system-ui;max-width:560px;margin:40px auto;padding:20px;background:#05080e;color:#f0f6ff}button{padding:12px;background:#3182f6;color:white;border:0;border-radius:8px}.muted{color:#8494aa}</style>
</head><body><h1>imDisplay update</h1><p>Upload the exact <code>firmware.bin</code> built for this device.</p>
<form id="update"><input id="firmware" type="file" accept=".bin" required><button id="install">Verify, install, and reboot</button></form>
<p id="status" class="muted">The device verifies exact size and SHA-256 before activation.</p><p><a href="/">Cancel</a></p>
<script>document.querySelector('#update').addEventListener('submit',async e=>{e.preventDefault();
const status=document.querySelector('#status'),button=document.querySelector('#install'),file=document.querySelector('#firmware').files[0];
if(!file)return;if(!globalThis.crypto?.subtle){status.textContent='This browser blocks local SHA-256. Use scripts/wifi_ota.py from the imDisplay repository.';return}
button.disabled=true;status.textContent='Hashing firmware locally...';try{const digest=await crypto.subtle.digest('SHA-256',await file.arrayBuffer());
const sha=[...new Uint8Array(digest)].map(x=>x.toString(16).padStart(2,'0')).join('');const data=new FormData();data.append('firmware',file,'firmware.bin');
status.textContent='Installing verified firmware...';const response=await fetch('/update?size='+file.size+'&sha256='+sha,{method:'POST',body:data});
status.textContent=await response.text();if(!response.ok)button.disabled=false}catch(error){status.textContent='Update failed: '+error;button.disabled=false}});</script>
</body></html>)HTML";
  webServer.sendHeader("Cache-Control", "no-store");
  webServer.send_P(200, "text/html", form);
}

void clearHttpOtaState() {
  if (httpOta.hashInitialized) {
    mbedtls_sha256_free(&httpOta.sha256);
  }
  httpOta = HttpOtaState{};
}

void failHttpOta(const char *reason, uint16_t status, bool abortOwnTransfer) {
  const bool ownedTransfer = httpOta.active;
  if (abortOwnTransfer && ownedTransfer) Update.abort();
  if (httpOta.hashInitialized) {
    mbedtls_sha256_free(&httpOta.sha256);
    httpOta.hashInitialized = false;
  }
  httpOta.active = false;
  httpOta.failed = true;
  httpOta.failureStatus = status;
  strlcpy(httpOta.failure, reason, sizeof(httpOta.failure));
  if (abortOwnTransfer && ownedTransfer) {
    otaInProgress = false;
    needsRender = true;
  }
  Serial.printf("http ota rejected: %s\n", reason);
}

void sendHttpOtaFailure() {
  const uint16_t status = httpOta.failureStatus;
  char response[160];
  snprintf(response, sizeof(response), "Update rejected: %s. Current firmware remains active.\n",
           httpOta.failure[0] ? httpOta.failure : "unknown failure");
  clearHttpOtaState();
  webServer.sendHeader("Cache-Control", "no-store");
  webServer.send(status, "text/plain", response);
}

bool parseFirmwareSize(const String &text, size_t &size) {
  if (!text.length()) return false;
  char *end = nullptr;
  const unsigned long value = strtoul(text.c_str(), &end, 10);
  if (end == text.c_str() || *end != '\0' || !value || value > kMaxFirmwareBytes) {
    return false;
  }
  size = static_cast<size_t>(value);
  return true;
}

void finishUpdateRequest() {
  if (!requireWebAuthentication()) return;
  if (!httpOta.attempted) {
    webServer.send(409, "text/plain", "No HTTP update is active.\n");
    return;
  }
  if (httpOta.failed) {
    sendHttpOtaFailure();
    return;
  }
  if (!httpOta.active || httpOta.received != httpOta.expected) {
    failHttpOta("incomplete firmware body", 422, true);
    sendHttpOtaFailure();
    return;
  }

  uint8_t digest[32];
  if (mbedtls_sha256_finish_ret(&httpOta.sha256, digest) != 0) {
    failHttpOta("SHA-256 finalization failed", 500, true);
    sendHttpOtaFailure();
    return;
  }
  mbedtls_sha256_free(&httpOta.sha256);
  httpOta.hashInitialized = false;
  char actual[65];
  for (size_t i = 0; i < sizeof(digest); ++i) {
    snprintf(actual + i * 2, 3, "%02x", digest[i]);
  }
  if (strcmp(actual, httpOta.expectedSha256) != 0) {
    failHttpOta("SHA-256 mismatch", 422, true);
    sendHttpOtaFailure();
    return;
  }
  if (Update.hasError() || !Update.end(false)) {
    failHttpOta("flash finalization failed", 500, true);
    sendHttpOtaFailure();
    return;
  }

  httpOta.active = false;
  otaSucceeded = true;
  clearHttpOtaState();
  display.fillScreen(kBlack);
  display.centered(74, "OTA COMPLETE", kGreen, 3);
  display.centered(130, "REBOOTING", kWhite, 2);
  webServer.sendHeader("Cache-Control", "no-store");
  webServer.send(200, "text/plain", "Update installed and SHA-256 verified. Rebooting.\n");
  Serial.printf("http ota installed: %s\n", actual);
  restartAt = millis() + 1200;
}

void receiveUpdateChunk() {
  HTTPUpload &upload = webServer.upload();
  if (upload.status == UPLOAD_FILE_START) {
    if (!webServer.authenticate(kWebUser, accessPointPassword)) return;
    if (httpOta.attempted) {
      failHttpOta("multiple firmware parts", 400, true);
      return;
    }
    clearHttpOtaState();
    httpOta.attempted = true;
    if (otaInProgress) {
      failHttpOta("another update is active", 409, false);
      return;
    }
    const String expectedSha = webServer.arg("sha256");
    if (upload.name != "firmware" ||
        !parseFirmwareSize(webServer.arg("size"), httpOta.expected) ||
        !validSha256(expectedSha.c_str())) {
      failHttpOta("valid size and SHA-256 are required", 400, false);
      return;
    }
    for (size_t i = 0; i < 64; ++i) {
      httpOta.expectedSha256[i] =
          static_cast<char>(tolower(static_cast<unsigned char>(expectedSha[i])));
    }
    flushScheduledSettings(true);
    if (!Update.begin(httpOta.expected, U_FLASH)) {
      failHttpOta("flash begin failed", 500, false);
      return;
    }
    httpOta.active = true;
    mbedtls_sha256_init(&httpOta.sha256);
    httpOta.hashInitialized = true;
    if (mbedtls_sha256_starts_ret(&httpOta.sha256, 0) != 0) {
      failHttpOta("SHA-256 initialization failed", 500, true);
      return;
    }
    otaInProgress = true;
    otaSucceeded = false;
    display.fillScreen(kBlack);
    display.centered(74, "OTA UPDATE", kBlue, 3);
    display.centered(130, "DO NOT POWER OFF", kAmber, 2);
    Serial.printf("http ota ready: %u bytes\n", static_cast<unsigned>(httpOta.expected));
  } else if (upload.status == UPLOAD_FILE_WRITE) {
    if (!httpOta.active || httpOta.failed) return;
    if (httpOta.received > httpOta.expected ||
        upload.currentSize > httpOta.expected - httpOta.received ||
        Update.write(upload.buf, upload.currentSize) != upload.currentSize ||
        mbedtls_sha256_update_ret(&httpOta.sha256, upload.buf, upload.currentSize) != 0) {
      failHttpOta("firmware write failed", 500, true);
      return;
    }
    httpOta.received += upload.currentSize;
  } else if (upload.status == UPLOAD_FILE_ABORTED) {
    failHttpOta("upload aborted", 400, httpOta.active);
  }
}

void makeAccessPointCredentials() {
  preferences.begin("xsure-budget", false);
  String saved = preferences.getString("ap-pass", "");
  if (saved.length() < 12) {
    static constexpr char alphabet[] = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
    char generated[15];
    generated[0] = 'X';
    generated[1] = 'S';
    for (size_t i = 2; i < sizeof(generated) - 1; ++i) {
      generated[i] = alphabet[esp_random() % (sizeof(alphabet) - 1)];
    }
    generated[sizeof(generated) - 1] = '\0';
    saved = generated;
    preferences.putString("ap-pass", saved);
  }
  strlcpy(accessPointPassword, saved.c_str(), sizeof(accessPointPassword));
  preferences.end();
  const uint32_t macTail = static_cast<uint32_t>(ESP.getEfuseMac());
  snprintf(accessPointName, sizeof(accessPointName), "imDisplay-%04X", macTail & 0xffff);
}

void startRemoteControl() {
  makeAccessPointCredentials();
  preferences.begin("xsure-budget", true);
  const String stationSsid = preferences.getString("wifi-ssid", "");
  const String stationPassword = preferences.getString("wifi-pass", "");
  preferences.end();
  stationConfigured = stationSsid.length() > 0 && stationPassword.length() >= 8;

  WiFi.persistent(false);
  WiFi.setAutoReconnect(true);
  applyRecoveryAccessPointSetting();
  if (stationConfigured) {
    WiFi.begin(stationSsid.c_str(), stationPassword.c_str());
    lastStationReconnectAt = millis();
  }
  webServer.on("/", HTTP_GET, sendHome);
  webServer.on("/api/state", HTTP_GET, sendState);
  webServer.on("/api/screen.bmp", HTTP_GET, sendScreenBmp);
  webServer.on("/api/control", HTTP_POST, remoteControl);
  webServer.on("/api/input", HTTP_POST, remoteInput);
  webServer.on("/api/self-test", HTTP_POST, remoteSelfTest);
  webServer.on("/api/applets", HTTP_POST, remoteApplets);
  webServer.on("/api/leds", HTTP_POST, remoteLeds);
  webServer.on("/api/display", HTTP_POST, remoteDisplay);
  webServer.on("/api/recovery-wifi", HTTP_POST, remoteRecoveryWifi);
  webServer.on("/api/sound", HTTP_POST, remoteSound);
  webServer.on("/api/timer", HTTP_POST, remoteTimer);
  webServer.on("/api/reboot", HTTP_POST, remoteReboot);
  webServer.on("/api/budget-source", HTTP_POST, configureBudgetPull);
  webServer.on("/api/budget", HTTP_POST, receiveHttpBudget);
  webServer.on("/api/wifi", HTTP_POST, configureStation);
  webServer.on("/update", HTTP_GET, sendUpdateForm);
  webServer.on("/update", HTTP_POST, finishUpdateRequest, receiveUpdateChunk);
  webServer.onNotFound([]() {
    if (!requireWebAuthentication()) return;
    webServer.send(404, "text/plain", "Not found\n");
  });
  webServer.begin();
  // UART is the physical recovery channel, so it may disclose the device-local
  // AP credential to the directly attached owner for initial provisioning.
  Serial.printf("control AP=%s password=%s ip=%s ready=%d\n", accessPointName,
                accessPointPassword,
                accessPointReady ? WiFi.softAPIP().toString().c_str() : "0.0.0.0",
                accessPointReady);
  Serial.printf("local wifi configured=%d\n", stationConfigured);
}

void rejectSerialOta(const char *reason) {
  if (serialOta.active) {
    Update.abort();
    mbedtls_sha256_free(&serialOta.sha256);
  }
  serialOta.active = false;
  otaInProgress = false;
  needsRender = true;
  Serial.printf("ota rejected: %s\n", reason);
}

bool validSha256(const char *value) {
  if (strlen(value) != 64) return false;
  for (size_t i = 0; i < 64; ++i) {
    if (!isxdigit(static_cast<unsigned char>(value[i]))) return false;
  }
  return true;
}

void beginSerialOta(const char *line) {
  unsigned long expected = 0;
  unsigned long chunkSize = 0;
  char sha256[65]{};
  char trailing = '\0';
  const bool v2 = strncmp(line, kSerialOtaV2Prefix, strlen(kSerialOtaV2Prefix)) == 0;
  const int matched =
      v2 ? sscanf(line, "XSURE_OTA_V2 %lu %64s %lu %c", &expected, sha256, &chunkSize,
                  &trailing)
         : sscanf(line, "XSURE_OTA_V1 %lu %64s %c", &expected, sha256, &trailing);
  if (matched != (v2 ? 3 : 2) || expected == 0 || expected > kMaxFirmwareBytes ||
      !validSha256(sha256) || (v2 && (chunkSize < 64 || chunkSize > 2048))) {
    Serial.println("ota rejected: invalid header");
    return;
  }
  if (otaInProgress) {
    Serial.println("ota rejected: update busy");
    return;
  }
  flushScheduledSettings(true);
  if (!Update.begin(static_cast<size_t>(expected), U_FLASH)) {
    Serial.println("ota rejected: flash begin failed");
    return;
  }

  serialOta = SerialOtaState{};
  serialOta.active = true;
  serialOta.expected = static_cast<size_t>(expected);
  serialOta.chunkSize = static_cast<size_t>(chunkSize);
  serialOta.lastByteAt = millis();
  for (size_t i = 0; i < 64; ++i) {
    serialOta.expectedSha256[i] = static_cast<char>(tolower(static_cast<unsigned char>(sha256[i])));
  }
  mbedtls_sha256_init(&serialOta.sha256);
  if (mbedtls_sha256_starts_ret(&serialOta.sha256, 0) != 0) {
    rejectSerialOta("sha256 init failed");
    return;
  }

  otaInProgress = true;
  otaSucceeded = false;
  display.fillScreen(kBlack);
  display.centered(74, "SERIAL UPDATE", kBlue, 2);
  display.centered(130, "DO NOT POWER OFF", kAmber, 2);
  if (v2) {
    Serial.printf("ota ready: %lu bytes chunk=%lu\n", expected, chunkSize);
  } else {
    Serial.printf("ota ready: %lu bytes\n", expected);
  }
}

void finishSerialOta() {
  uint8_t digest[32];
  if (mbedtls_sha256_finish_ret(&serialOta.sha256, digest) != 0) {
    rejectSerialOta("sha256 finish failed");
    return;
  }
  mbedtls_sha256_free(&serialOta.sha256);
  serialOta.active = false;

  char actual[65];
  for (size_t i = 0; i < sizeof(digest); ++i) {
    snprintf(actual + i * 2, 3, "%02x", digest[i]);
  }
  if (strcmp(actual, serialOta.expectedSha256) != 0) {
    Update.abort();
    otaInProgress = false;
    needsRender = true;
    Serial.println("ota rejected: sha256 mismatch");
    return;
  }
  if (Update.hasError() || !Update.end(false)) {
    Update.abort();
    otaInProgress = false;
    needsRender = true;
    Serial.println("ota rejected: flash finalize failed");
    return;
  }

  otaSucceeded = true;
  display.fillScreen(kBlack);
  display.centered(74, "UPDATE COMPLETE", kGreen, 2);
  display.centered(130, "REBOOTING", kWhite, 2);
  Serial.printf("ota installed: %s\n", actual);
  restartAt = millis() + 1200;
}

void pollSerialOta() {
  if (millis() - serialOta.lastByteAt > kSerialOtaTimeoutMs) {
    rejectSerialOta("transfer timeout");
    return;
  }
  uint8_t buffer[1024];
  const size_t remaining = serialOta.expected - serialOta.received;
  const size_t chunkRemaining = serialOta.chunkSize
                                    ? min(serialOta.chunkSize - serialOta.chunkReceived, remaining)
                                    : remaining;
  const size_t available = static_cast<size_t>(Serial.available());
  const size_t wanted = min(chunkRemaining, min(available, sizeof(buffer)));
  if (!wanted) return;
  const size_t count = Serial.read(buffer, wanted);
  if (!count) return;
  if (Update.write(buffer, count) != count ||
      mbedtls_sha256_update_ret(&serialOta.sha256, buffer, count) != 0) {
    rejectSerialOta("flash write failed");
    return;
  }
  serialOta.received += count;
  serialOta.chunkReceived += count;
  serialOta.lastByteAt = millis();
  if (serialOta.chunkSize &&
      (serialOta.chunkReceived == serialOta.chunkSize || serialOta.received == serialOta.expected)) {
    serialOta.chunkReceived = 0;
    Serial.printf("ota chunk: %u\n", static_cast<unsigned>(serialOta.received));
  }
  if (serialOta.received == serialOta.expected) finishSerialOta();
}

void pollSerial() {
  if (serialOta.active) {
    pollSerialOta();
    return;
  }
  while (Serial.available()) {
    const char value = Serial.read();
    if (value == '\r') continue;
    if (value == '\n') {
      serialLine[serialLength] = '\0';
      if (serialLength) {
        if (strncmp(serialLine, kSerialOtaV1Prefix, strlen(kSerialOtaV1Prefix)) == 0 ||
            strncmp(serialLine, kSerialOtaV2Prefix, strlen(kSerialOtaV2Prefix)) == 0) {
          beginSerialOta(serialLine);
        } else if (acceptWifiPayload(serialLine)) {
          // Credentials are accepted only through the physically attached
          // owner UART and are never echoed back.
        } else if (budgetPullSettings.legacyPushEnabled) {
          acceptPayload(serialLine);
        }
      }
      serialLength = 0;
      if (serialOta.active) return;
    } else if (serialLength + 1 < sizeof(serialLine)) {
      serialLine[serialLength++] = value;
    } else {
      serialLength = 0;
    }
  }
}

}  // namespace

void setup() {
  Serial.setRxBufferSize(4096);
  Serial.begin(115200);
  delay(100);
  Serial.println();
  Serial.println("imDisplay " CODEX_BUDGET_FIRMWARE_VERSION);
  bootId = esp_random();
  const uint64_t mac = ESP.getEfuseMac();
  Serial.printf("reset=%d mac=%04X%08X flash=%u heap=%u\n", esp_reset_reason(),
                static_cast<uint16_t>(mac >> 32), static_cast<uint32_t>(mac),
                ESP.getFlashChipSize(), ESP.getFreeHeap());

  beginInputCapture();
  loadProductSettings();
  loadBudgetPullSettings();
  startBudgetPullWorker();
  soundTaskReady = soundEngine.begin();
  Serial.printf("sound factory-pdm task=%d\n", soundTaskReady);
  rgbPixelsReady = rgbPixels.begin(kRgbLedPin);
  applyLedSettings();
  display.begin();
  setDisplayBrightness(displayBrightness, false);
  startRemoteControl();
  render();
}

void loop() {
  static bool lastStaleState = false;
  pollSerial();
  pollButton();
  pollEncoder();
  pollPendingSingleClick();
  pollMenuDwell();
  webServer.handleClient();
  if (accessPointChangePending &&
      static_cast<int32_t>(millis() - accessPointChangeAt) >= 0) {
    applyRecoveryAccessPointSetting();
  }
  const bool stationConnected = WiFi.status() == WL_CONNECTED;
  if (stationConnected != stationWasConnected) {
    stationWasConnected = stationConnected;
    if (stationConnected) requestBudgetPull();
    if (!menuOpen && currentPage == Page::Connection) needsRender = true;
    Serial.printf("local wifi connected=%d ip=%s\n", stationConnected,
                  stationConnected ? WiFi.localIP().toString().c_str() : "0.0.0.0");
  }
  if (stationConfigured && !stationConnected &&
      millis() - lastStationReconnectAt >= kStationReconnectMs) {
    lastStationReconnectAt = millis();
    Serial.printf("local wifi retry=%d\n", WiFi.reconnect());
  }
  pollBudgetPull();
  if (restartAt && static_cast<int32_t>(millis() - restartAt) >= 0) ESP.restart();
  if (workTimer.running) {
    const uint32_t remaining = timerRemainingSeconds();
    if (!remaining) {
      workTimer.running = false;
      workTimer.remainingSeconds = 0;
      playSound(SoundEngine::Cue::Complete, true);
      showLedFeedback({251, 191, 36}, 2500);
      needsRender = true;
      Serial.println("timer complete");
    } else if (remaining != timerRenderedSecond && currentPage == Page::Timer &&
               !menuOpen && !otaInProgress && !needsRender) {
      renderTimerClock(false);
      ++timerPartialRenderCount;
    }
  }
  const bool staleNow = isStale();
  if (staleNow != lastStaleState) {
    lastStaleState = staleNow;
    if (!menuOpen &&
        (currentPage == Page::Overview || currentPage == Page::Windows)) {
      needsRender = true;
    }
  }
  if (ledFeedbackUntil && static_cast<int32_t>(millis() - ledFeedbackUntil) >= 0) {
    ledFeedbackUntil = 0;
    applyLedSettings();
  }
  flushScheduledSettings();
  if (needsRender && !otaInProgress) render();
  delay(2);
}
