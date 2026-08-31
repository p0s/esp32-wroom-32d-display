#include <Arduino.h>
#include <Update.h>
#include <mbedtls/sha256.h>

namespace {

constexpr size_t kMaxFirmwareBytes = 0x1f0000;
constexpr uint32_t kTransferTimeoutMs = 30000;
constexpr char kPrefix[] = "XSURE_OTA_V2 ";

struct TransferState {
  bool active = false;
  size_t expected = 0;
  size_t received = 0;
  size_t chunkSize = 0;
  size_t chunkReceived = 0;
  uint32_t lastByteAt = 0;
  char expectedSha256[65]{};
  mbedtls_sha256_context sha256;
};

TransferState transfer;
char commandLine[160];
size_t commandLength = 0;
uint32_t restartAt = 0;

bool validSha256(const char *value) {
  if (strlen(value) != 64) return false;
  for (size_t index = 0; index < 64; ++index) {
    if (!isxdigit(static_cast<unsigned char>(value[index]))) return false;
  }
  return true;
}

void reject(const char *reason) {
  if (transfer.active) {
    Update.abort();
    mbedtls_sha256_free(&transfer.sha256);
  }
  transfer.active = false;
  Serial.printf("ota rejected: %s\n", reason);
}

void beginTransfer(const char *line) {
  unsigned long expected = 0;
  unsigned long chunkSize = 0;
  char sha256[65]{};
  char trailing = '\0';
  if (sscanf(line, "XSURE_OTA_V2 %lu %64s %lu %c", &expected, sha256, &chunkSize,
             &trailing) != 3 ||
      expected == 0 || expected > kMaxFirmwareBytes || chunkSize < 64 || chunkSize > 2048 ||
      !validSha256(sha256)) {
    Serial.println("ota rejected: invalid header");
    return;
  }
  if (!Update.begin(static_cast<size_t>(expected), U_FLASH)) {
    Serial.println("ota rejected: flash begin failed");
    return;
  }

  transfer = TransferState{};
  transfer.active = true;
  transfer.expected = static_cast<size_t>(expected);
  transfer.chunkSize = static_cast<size_t>(chunkSize);
  transfer.lastByteAt = millis();
  for (size_t index = 0; index < 64; ++index) {
    transfer.expectedSha256[index] =
        static_cast<char>(tolower(static_cast<unsigned char>(sha256[index])));
  }
  mbedtls_sha256_init(&transfer.sha256);
  if (mbedtls_sha256_starts_ret(&transfer.sha256, 0) != 0) {
    reject("sha256 init failed");
    return;
  }
  Serial.printf("ota ready: %lu bytes chunk=%lu\n", expected, chunkSize);
}

void finishTransfer() {
  uint8_t digest[32];
  if (mbedtls_sha256_finish_ret(&transfer.sha256, digest) != 0) {
    reject("sha256 finish failed");
    return;
  }
  mbedtls_sha256_free(&transfer.sha256);
  transfer.active = false;

  char actual[65];
  for (size_t index = 0; index < sizeof(digest); ++index) {
    snprintf(actual + index * 2, 3, "%02x", digest[index]);
  }
  if (strcmp(actual, transfer.expectedSha256) != 0) {
    Update.abort();
    Serial.println("ota rejected: sha256 mismatch");
    return;
  }
  if (Update.hasError() || !Update.end(false)) {
    Serial.println("ota rejected: flash finalize failed");
    return;
  }
  Serial.printf("ota installed: %s\n", actual);
  restartAt = millis() + 1200;
}

void pollTransfer() {
  if (millis() - transfer.lastByteAt > kTransferTimeoutMs) {
    reject("transfer timeout");
    return;
  }
  uint8_t buffer[1024];
  const size_t remaining = transfer.expected - transfer.received;
  const size_t chunkRemaining = min(transfer.chunkSize - transfer.chunkReceived, remaining);
  const size_t available = static_cast<size_t>(Serial.available());
  const size_t wanted = min(chunkRemaining, min(available, sizeof(buffer)));
  if (!wanted) return;
  const size_t count = Serial.read(buffer, wanted);
  if (!count) return;
  if (Update.write(buffer, count) != count ||
      mbedtls_sha256_update_ret(&transfer.sha256, buffer, count) != 0) {
    reject("flash write failed");
    return;
  }
  transfer.received += count;
  transfer.chunkReceived += count;
  transfer.lastByteAt = millis();
  if (transfer.chunkReceived == transfer.chunkSize || transfer.received == transfer.expected) {
    transfer.chunkReceived = 0;
    Serial.printf("ota chunk: %u\n", static_cast<unsigned>(transfer.received));
  }
  if (transfer.received == transfer.expected) finishTransfer();
}

void pollCommand() {
  while (Serial.available()) {
    const char value = Serial.read();
    if (value == '\r') continue;
    if (value == '\n') {
      commandLine[commandLength] = '\0';
      if (strncmp(commandLine, kPrefix, strlen(kPrefix)) == 0) {
        beginTransfer(commandLine);
      } else if (commandLength) {
        Serial.println("ota rejected: command required");
      }
      commandLength = 0;
      return;
    }
    if (commandLength + 1 < sizeof(commandLine)) {
      commandLine[commandLength++] = value;
    } else {
      commandLength = 0;
    }
  }
}

}  // namespace

void setup() {
  Serial.setRxBufferSize(4096);
  Serial.begin(115200);
  delay(100);
  Serial.println();
  Serial.println("X-SURE OTA Bootstrap " XSURE_OTA_BOOTSTRAP_VERSION);
}

void loop() {
  if (transfer.active) {
    pollTransfer();
  } else {
    pollCommand();
  }
  if (restartAt && static_cast<int32_t>(millis() - restartAt) >= 0) ESP.restart();
  delay(2);
}
