#pragma once

#include <stddef.h>
#include <stdint.h>

namespace imdisplay {
namespace budget_pull {

constexpr uint8_t kProtocolVersion = 1;
constexpr size_t kNonceHexLength = 32;
constexpr size_t kSha256HexLength = 64;
constexpr size_t kMaximumBodyBytes = 2047;
constexpr size_t kMaximumResponseBytes = 4096;
constexpr size_t kMaximumRequestBytes = 512;
constexpr size_t kMaximumHostLength = 63;
constexpr uint32_t kMinimumPollIntervalMs = 60000;
constexpr uint32_t kMaximumRetryBackoffMs = 300000;

struct CryptoProvider {
  bool (*sha256)(const uint8_t *data, size_t length, uint8_t output[32]);
  bool (*hmacSha256)(const uint8_t *key, size_t keyLength,
                     const uint8_t *data, size_t length, uint8_t output[32]);
};

struct PullSchedule {
  bool hasAttempt = false;
  bool immediate = false;
  uint8_t consecutiveFailures = 0;
  uint32_t notBeforeMs = 0;
};

bool isLowerHex(const char *value, size_t length);
bool decodeKeyHex(const char *value, uint8_t output[32]);
bool parsePrivateIpv4(const char *value, uint8_t octets[4]);
bool isPrivateIpv4(const uint8_t octets[4]);
bool isLocalHostname(const char *value);
bool isValidPullHost(const char *value);
bool copyLocalHostnameLabel(const char *host, char *output, size_t capacity);
bool constantTimeEqual(const char *left, const char *right, size_t length);

bool buildAuthenticatedRequest(char *output, size_t capacity,
                               const char *host, uint16_t port,
                               const char nonce[kNonceHexLength + 1],
                               const uint8_t key[32],
                               const CryptoProvider &crypto);

bool validateAuthenticatedResponse(
    const char *response, size_t responseLength,
    const char expectedNonce[kNonceHexLength + 1], const uint8_t key[32],
    const CryptoProvider &crypto, size_t &bodyOffset, size_t &bodyLength);

bool deadlineReached(uint32_t now, uint32_t deadline);
uint32_t retryBackoffMs(uint8_t consecutiveFailures);
void requestImmediate(PullSchedule &schedule);
bool shouldStart(const PullSchedule &schedule, uint32_t now, bool relevant,
                 bool connected, bool configured, bool inFlight,
                 bool updateInProgress);
void recordAttempt(PullSchedule &schedule, uint32_t now);
void recordResult(PullSchedule &schedule, uint32_t now, bool success);

}  // namespace budget_pull
}  // namespace imdisplay
