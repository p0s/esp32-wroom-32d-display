#include "budget_pull_client.h"

#include <Arduino.h>
#include <ESPmDNS.h>
#include <WiFi.h>
#include <esp_system.h>
#include <mbedtls/md.h>
#include <mbedtls/sha256.h>
#include <stdio.h>
#include <string.h>

namespace imdisplay {
namespace budget_pull {
namespace {

constexpr uint32_t kConnectTimeoutMs = 750;
constexpr uint32_t kReadTimeoutMs = 1500;
constexpr uint32_t kMdnsQueryTimeoutMs = 1000;
constexpr uint32_t kResolutionCacheMs = 5 * 60 * 1000;

struct RuntimeResolution {
  bool valid = false;
  uint32_t resolvedAt = 0;
  char host[kMaximumHostLength + 1]{};
  IPAddress endpoint;
};

RuntimeResolution runtimeResolution;
bool mdnsReady = false;

bool sha256(const uint8_t *data, size_t length, uint8_t output[32]) {
  mbedtls_sha256_context context;
  mbedtls_sha256_init(&context);
  bool valid = mbedtls_sha256_starts_ret(&context, 0) == 0 &&
               mbedtls_sha256_update_ret(&context, data, length) == 0 &&
               mbedtls_sha256_finish_ret(&context, output) == 0;
  mbedtls_sha256_free(&context);
  return valid;
}

bool hmacSha256(const uint8_t *key, size_t keyLength, const uint8_t *data,
                size_t length, uint8_t output[32]) {
  const mbedtls_md_info_t *info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  return info && mbedtls_md_hmac(info, key, keyLength, data, length, output) == 0;
}

constexpr CryptoProvider kCrypto = {sha256, hmacSha256};

void randomNonce(char output[kNonceHexLength + 1]) {
  static constexpr char digits[] = "0123456789abcdef";
  uint8_t random[16];
  esp_fill_random(random, sizeof(random));
  for (size_t i = 0; i < sizeof(random); ++i) {
    output[i * 2] = digits[random[i] >> 4];
    output[i * 2 + 1] = digits[random[i] & 0x0f];
  }
  output[kNonceHexLength] = '\0';
}

bool writeAll(WiFiClient &client, const char *bytes, size_t length) {
  size_t written = 0;
  const uint32_t startedAt = millis();
  while (written < length && millis() - startedAt <= kReadTimeoutMs) {
    if (!client.connected()) return false;
    const size_t count = client.write(
        reinterpret_cast<const uint8_t *>(bytes + written), length - written);
    if (count) {
      written += count;
    } else {
      vTaskDelay(1);
    }
  }
  return written == length;
}

bool privateEndpoint(const IPAddress &endpoint) {
  const uint8_t octets[4] = {endpoint[0], endpoint[1], endpoint[2], endpoint[3]};
  return isPrivateIpv4(octets);
}

bool ensureMdnsReady() {
  if (mdnsReady) return true;
  if (WiFi.status() != WL_CONNECTED) return false;
  char clientLabel[32];
  const uint32_t identity = static_cast<uint32_t>(ESP.getEfuseMac());
  const int length = snprintf(clientLabel, sizeof(clientLabel),
                              "imdisplay-%06lx",
                              static_cast<unsigned long>(identity & 0xffffffU));
  if (length <= 0 || static_cast<size_t>(length) >= sizeof(clientLabel)) {
    return false;
  }
  if (!MDNS.begin(clientLabel)) {
    MDNS.end();
    return false;
  }
  mdnsReady = true;
  return true;
}

void resetMdns() {
  if (mdnsReady) MDNS.end();
  mdnsReady = false;
}

PullResultCode resolveEndpoint(const PullRequest &request, IPAddress &endpoint) {
  uint8_t octets[4];
  if (parsePrivateIpv4(request.host, octets)) {
    endpoint = IPAddress(octets[0], octets[1], octets[2], octets[3]);
    return PullResultCode::Success;
  }
  if (!isLocalHostname(request.host)) return PullResultCode::InvalidConfiguration;
  const uint32_t now = millis();
  if (runtimeResolution.valid && !strcmp(runtimeResolution.host, request.host) &&
      now - runtimeResolution.resolvedAt < kResolutionCacheMs) {
    endpoint = runtimeResolution.endpoint;
    return PullResultCode::Success;
  }
  char label[kMaximumHostLength + 1];
  if (!copyLocalHostnameLabel(request.host, label, sizeof(label)) ||
      !ensureMdnsReady()) {
    return PullResultCode::ResolveFailed;
  }
  const IPAddress resolved = MDNS.queryHost(label, kMdnsQueryTimeoutMs);
  if (!static_cast<uint32_t>(resolved)) {
    resetMdns();
    return PullResultCode::ResolveFailed;
  }
  if (!privateEndpoint(resolved)) return PullResultCode::UnsafeResolution;
  runtimeResolution.valid = true;
  runtimeResolution.resolvedAt = now;
  strlcpy(runtimeResolution.host, request.host, sizeof(runtimeResolution.host));
  runtimeResolution.endpoint = resolved;
  endpoint = resolved;
  return PullResultCode::Success;
}

void invalidateRuntimeResolution(const PullRequest &request) {
  if (isLocalHostname(request.host) && runtimeResolution.valid &&
      !strcmp(runtimeResolution.host, request.host)) {
    runtimeResolution = RuntimeResolution{};
  }
}

}  // namespace

void performPull(const PullRequest &request, PullResult &result) {
  result = PullResult{};
  if (!isValidPullHost(request.host) || request.port < 1024) {
    result.code = PullResultCode::InvalidConfiguration;
    return;
  }

  char nonce[kNonceHexLength + 1];
  randomNonce(nonce);
  char outbound[kMaximumRequestBytes + 1];
  if (!buildAuthenticatedRequest(outbound, sizeof(outbound), request.host,
                                 request.port, nonce, request.key, kCrypto)) {
    result.code = PullResultCode::InvalidConfiguration;
    return;
  }

  WiFiClient client;
  client.setTimeout(kReadTimeoutMs);
  IPAddress endpoint;
  result.code = resolveEndpoint(request, endpoint);
  if (result.code != PullResultCode::Success) return;
  if (!client.connect(endpoint, request.port, kConnectTimeoutMs)) {
    invalidateRuntimeResolution(request);
    result.code = PullResultCode::ConnectFailed;
    return;
  }
  if (!writeAll(client, outbound, strlen(outbound))) {
    invalidateRuntimeResolution(request);
    result.code = PullResultCode::WriteFailed;
    client.stop();
    return;
  }

  char response[kMaximumResponseBytes];
  size_t received = 0;
  const uint32_t startedAt = millis();
  bool tooLarge = false;
  while (millis() - startedAt <= kReadTimeoutMs) {
    const int available = client.available();
    if (available > 0) {
      if (received == sizeof(response)) {
        tooLarge = true;
        break;
      }
      const size_t capacity = sizeof(response) - received;
      const int count = client.read(
          reinterpret_cast<uint8_t *>(response + received),
          min(static_cast<size_t>(available), capacity));
      if (count > 0) received += static_cast<size_t>(count);
      continue;
    }
    if (!client.connected()) break;
    vTaskDelay(1);
  }
  if (received == sizeof(response) && client.available() > 0) tooLarge = true;
  const bool closed = !client.connected();
  client.stop();
  if (tooLarge) {
    invalidateRuntimeResolution(request);
    result.code = PullResultCode::ResponseTooLarge;
    return;
  }
  if (!closed) {
    invalidateRuntimeResolution(request);
    result.code = PullResultCode::ReadTimeout;
    return;
  }

  size_t bodyOffset = 0;
  size_t bodyLength = 0;
  if (!validateAuthenticatedResponse(response, received, nonce, request.key,
                                     kCrypto, bodyOffset, bodyLength) ||
      bodyLength > kMaximumBodyBytes ||
      memchr(response + bodyOffset, '\0', bodyLength)) {
    invalidateRuntimeResolution(request);
    result.code = PullResultCode::AuthenticationFailed;
    return;
  }
  memcpy(result.body, response + bodyOffset, bodyLength);
  result.body[bodyLength] = '\0';
  result.bodyLength = static_cast<uint16_t>(bodyLength);
  result.code = PullResultCode::Success;
}

const char *resultCodeName(PullResultCode code) {
  switch (code) {
    case PullResultCode::Success: return "success";
    case PullResultCode::InvalidConfiguration: return "invalid-config";
    case PullResultCode::ResolveFailed: return "resolve-failed";
    case PullResultCode::UnsafeResolution: return "unsafe-resolution";
    case PullResultCode::ConnectFailed: return "connect-failed";
    case PullResultCode::WriteFailed: return "write-failed";
    case PullResultCode::ReadTimeout: return "read-timeout";
    case PullResultCode::ResponseTooLarge: return "response-too-large";
    case PullResultCode::AuthenticationFailed: return "authentication-failed";
  }
  return "unknown";
}

}  // namespace budget_pull
}  // namespace imdisplay
