#include "budget_pull_protocol.h"

#include <ctype.h>
#include <stdio.h>
#include <string.h>

namespace imdisplay {
namespace budget_pull {
namespace {

constexpr char kRequestCanonicalPrefix[] =
    "imdisplay-cache-v1\nrequest\nGET\n/v1/quota\n";
constexpr char kResponseCanonicalPrefix[] =
    "imdisplay-cache-v1\nresponse\n200\n";

void encodeHex(const uint8_t *bytes, size_t length, char *output) {
  static constexpr char digits[] = "0123456789abcdef";
  for (size_t i = 0; i < length; ++i) {
    output[i * 2] = digits[bytes[i] >> 4];
    output[i * 2 + 1] = digits[bytes[i] & 0x0f];
  }
  output[length * 2] = '\0';
}

bool spanEquals(const char *start, size_t length, const char *expected) {
  return strlen(expected) == length && !memcmp(start, expected, length);
}

const char *findCrlf(const char *start, const char *end) {
  for (const char *cursor = start; cursor + 1 < end; ++cursor) {
    if (cursor[0] == '\r' && cursor[1] == '\n') return cursor;
  }
  return nullptr;
}

bool copyHeaderValue(const char *start, size_t length, char *output,
                     size_t capacity) {
  if (!length || length >= capacity) return false;
  memcpy(output, start, length);
  output[length] = '\0';
  return true;
}

bool parseContentLength(const char *start, size_t length, size_t &value) {
  if (!length || length > 4 || (length > 1 && start[0] == '0')) return false;
  size_t parsed = 0;
  for (size_t i = 0; i < length; ++i) {
    if (start[i] < '0' || start[i] > '9') return false;
    parsed = parsed * 10 + static_cast<size_t>(start[i] - '0');
  }
  if (!parsed || parsed > kMaximumBodyBytes) return false;
  value = parsed;
  return true;
}

}  // namespace

bool isLowerHex(const char *value, size_t length) {
  if (!value) return false;
  size_t actualLength = 0;
  while (actualLength <= length && value[actualLength] != '\0') {
    ++actualLength;
  }
  if (actualLength != length) return false;
  for (size_t i = 0; i < length; ++i) {
    if (!((value[i] >= '0' && value[i] <= '9') ||
          (value[i] >= 'a' && value[i] <= 'f'))) {
      return false;
    }
  }
  return true;
}

bool decodeKeyHex(const char *value, uint8_t output[32]) {
  if (!isLowerHex(value, kSha256HexLength)) return false;
  for (size_t i = 0; i < 32; ++i) {
    const char high = value[i * 2];
    const char low = value[i * 2 + 1];
    const uint8_t highValue = high <= '9' ? high - '0' : high - 'a' + 10;
    const uint8_t lowValue = low <= '9' ? low - '0' : low - 'a' + 10;
    output[i] = static_cast<uint8_t>((highValue << 4) | lowValue);
  }
  return true;
}

bool parsePrivateIpv4(const char *value, uint8_t octets[4]) {
  if (!value || !*value) return false;
  const char *cursor = value;
  for (size_t part = 0; part < 4; ++part) {
    if (!isdigit(static_cast<unsigned char>(*cursor))) return false;
    unsigned number = 0;
    size_t digits = 0;
    while (isdigit(static_cast<unsigned char>(*cursor))) {
      if (++digits > 3) return false;
      number = number * 10 + static_cast<unsigned>(*cursor - '0');
      ++cursor;
    }
    if (number > 255 || (digits > 1 && value[0] == '0')) return false;
    octets[part] = static_cast<uint8_t>(number);
    if (part < 3) {
      if (*cursor++ != '.') return false;
      value = cursor;
    } else if (*cursor != '\0') {
      return false;
    }
  }
  return isPrivateIpv4(octets);
}

bool isPrivateIpv4(const uint8_t octets[4]) {
  return octets &&
         (octets[0] == 10 ||
          (octets[0] == 172 && octets[1] >= 16 && octets[1] <= 31) ||
          (octets[0] == 192 && octets[1] == 168));
}

bool isLocalHostname(const char *value) {
  if (!value) return false;
  const size_t length = strnlen(value, kMaximumHostLength + 1);
  constexpr char suffix[] = ".local";
  constexpr size_t suffixLength = sizeof(suffix) - 1;
  if (length <= suffixLength || length > kMaximumHostLength ||
      strcmp(value + length - suffixLength, suffix)) {
    return false;
  }
  const size_t labelLength = length - suffixLength;
  if (!isalnum(static_cast<unsigned char>(value[0])) ||
      !isalnum(static_cast<unsigned char>(value[labelLength - 1]))) {
    return false;
  }
  for (size_t index = 0; index < labelLength; ++index) {
    const char character = value[index];
    if (!((character >= 'a' && character <= 'z') ||
          (character >= '0' && character <= '9') || character == '-')) {
      return false;
    }
  }
  return true;
}

bool isValidPullHost(const char *value) {
  uint8_t octets[4];
  return parsePrivateIpv4(value, octets) || isLocalHostname(value);
}

bool copyLocalHostnameLabel(const char *host, char *output, size_t capacity) {
  if (!output || !capacity || !isLocalHostname(host)) return false;
  constexpr size_t suffixLength = sizeof(".local") - 1;
  const size_t labelLength = strlen(host) - suffixLength;
  if (labelLength >= capacity) return false;
  memcpy(output, host, labelLength);
  output[labelLength] = '\0';
  return true;
}

bool constantTimeEqual(const char *left, const char *right, size_t length) {
  if (!left || !right) return false;
  uint8_t difference = 0;
  for (size_t i = 0; i < length; ++i) {
    difference |= static_cast<uint8_t>(left[i] ^ right[i]);
  }
  return difference == 0;
}

bool buildAuthenticatedRequest(char *output, size_t capacity,
                               const char *host, uint16_t port,
                               const char nonce[kNonceHexLength + 1],
                               const uint8_t key[32],
                               const CryptoProvider &crypto) {
  if (!output || !host || !key || !crypto.hmacSha256 ||
      !isValidPullHost(host) || port < 1024 ||
      !isLowerHex(nonce, kNonceHexLength)) {
    return false;
  }
  char canonical[128];
  const int canonicalLength =
      snprintf(canonical, sizeof(canonical), "%s%s\n", kRequestCanonicalPrefix, nonce);
  if (canonicalLength <= 0 || static_cast<size_t>(canonicalLength) >= sizeof(canonical)) {
    return false;
  }
  uint8_t digest[32];
  if (!crypto.hmacSha256(key, 32, reinterpret_cast<const uint8_t *>(canonical),
                         static_cast<size_t>(canonicalLength), digest)) {
    return false;
  }
  char authorization[kSha256HexLength + 1];
  encodeHex(digest, sizeof(digest), authorization);
  const int written = snprintf(
      output, capacity,
      "GET /v1/quota HTTP/1.1\r\n"
      "Host: %s:%u\r\n"
      "Connection: close\r\n"
      "X-imDisplay-Protocol: 1\r\n"
      "X-imDisplay-Nonce: %s\r\n"
      "X-imDisplay-Authorization: %s\r\n\r\n",
      host, static_cast<unsigned>(port), nonce, authorization);
  return written > 0 && static_cast<size_t>(written) < capacity &&
         static_cast<size_t>(written) <= kMaximumRequestBytes;
}

bool validateAuthenticatedResponse(
    const char *response, size_t responseLength,
    const char expectedNonce[kNonceHexLength + 1], const uint8_t key[32],
    const CryptoProvider &crypto, size_t &bodyOffset, size_t &bodyLength) {
  bodyOffset = 0;
  bodyLength = 0;
  if (!response || !key || !crypto.sha256 || !crypto.hmacSha256 ||
      responseLength > kMaximumResponseBytes ||
      !isLowerHex(expectedNonce, kNonceHexLength)) {
    return false;
  }
  const char *const end = response + responseLength;
  const char *lineEnd = findCrlf(response, end);
  if (!lineEnd || !spanEquals(response, lineEnd - response, "HTTP/1.1 200 OK")) {
    return false;
  }
  const char *cursor = lineEnd + 2;
  bool contentTypeSeen = false;
  bool contentLengthSeen = false;
  bool connectionSeen = false;
  bool protocolSeen = false;
  bool nonceSeen = false;
  bool bodyHashSeen = false;
  bool authorizationSeen = false;
  char nonce[kNonceHexLength + 1]{};
  char bodyHash[kSha256HexLength + 1]{};
  char authorization[kSha256HexLength + 1]{};
  size_t parsedContentLength = 0;
  while (cursor < end) {
    lineEnd = findCrlf(cursor, end);
    if (!lineEnd) return false;
    if (lineEnd == cursor) {
      cursor += 2;
      break;
    }
    const char *colon = nullptr;
    for (const char *candidate = cursor; candidate < lineEnd; ++candidate) {
      if (*candidate == ':') {
        colon = candidate;
        break;
      }
    }
    if (!colon || colon + 2 > lineEnd || colon[1] != ' ') return false;
    const char *value = colon + 2;
    const size_t nameLength = static_cast<size_t>(colon - cursor);
    const size_t valueLength = static_cast<size_t>(lineEnd - value);
    if (spanEquals(cursor, nameLength, "Content-Type") && !contentTypeSeen) {
      contentTypeSeen = spanEquals(value, valueLength, "application/json");
      if (!contentTypeSeen) return false;
    } else if (spanEquals(cursor, nameLength, "Content-Length") && !contentLengthSeen) {
      contentLengthSeen = parseContentLength(value, valueLength, parsedContentLength);
      if (!contentLengthSeen) return false;
    } else if (spanEquals(cursor, nameLength, "Connection") && !connectionSeen) {
      connectionSeen = spanEquals(value, valueLength, "close");
      if (!connectionSeen) return false;
    } else if (spanEquals(cursor, nameLength, "X-imDisplay-Protocol") && !protocolSeen) {
      protocolSeen = spanEquals(value, valueLength, "1");
      if (!protocolSeen) return false;
    } else if (spanEquals(cursor, nameLength, "X-imDisplay-Nonce") && !nonceSeen) {
      nonceSeen = copyHeaderValue(value, valueLength, nonce, sizeof(nonce)) &&
                  isLowerHex(nonce, kNonceHexLength);
      if (!nonceSeen) return false;
    } else if (spanEquals(cursor, nameLength, "X-imDisplay-Body-SHA256") &&
               !bodyHashSeen) {
      bodyHashSeen = copyHeaderValue(value, valueLength, bodyHash, sizeof(bodyHash)) &&
                     isLowerHex(bodyHash, kSha256HexLength);
      if (!bodyHashSeen) return false;
    } else if (spanEquals(cursor, nameLength, "X-imDisplay-Authorization") &&
               !authorizationSeen) {
      authorizationSeen =
          copyHeaderValue(value, valueLength, authorization, sizeof(authorization)) &&
          isLowerHex(authorization, kSha256HexLength);
      if (!authorizationSeen) return false;
    } else {
      return false;
    }
    cursor = lineEnd + 2;
  }
  if (!(contentTypeSeen && contentLengthSeen && connectionSeen && protocolSeen &&
        nonceSeen && bodyHashSeen && authorizationSeen) ||
      !constantTimeEqual(nonce, expectedNonce, kNonceHexLength)) {
    return false;
  }
  bodyOffset = static_cast<size_t>(cursor - response);
  bodyLength = parsedContentLength;
  if (bodyOffset + bodyLength != responseLength) return false;

  uint8_t digest[32];
  if (!crypto.sha256(reinterpret_cast<const uint8_t *>(response + bodyOffset),
                     bodyLength, digest)) {
    return false;
  }
  char calculatedBodyHash[kSha256HexLength + 1];
  encodeHex(digest, sizeof(digest), calculatedBodyHash);
  if (!constantTimeEqual(bodyHash, calculatedBodyHash, kSha256HexLength)) return false;

  char canonical[192];
  const int canonicalLength = snprintf(canonical, sizeof(canonical), "%s%s\n%s\n",
                                       kResponseCanonicalPrefix, nonce, bodyHash);
  if (canonicalLength <= 0 || static_cast<size_t>(canonicalLength) >= sizeof(canonical) ||
      !crypto.hmacSha256(key, 32, reinterpret_cast<const uint8_t *>(canonical),
                         static_cast<size_t>(canonicalLength), digest)) {
    return false;
  }
  char calculatedAuthorization[kSha256HexLength + 1];
  encodeHex(digest, sizeof(digest), calculatedAuthorization);
  return constantTimeEqual(authorization, calculatedAuthorization,
                           kSha256HexLength);
}

bool deadlineReached(uint32_t now, uint32_t deadline) {
  return static_cast<int32_t>(now - deadline) >= 0;
}

uint32_t retryBackoffMs(uint8_t consecutiveFailures) {
  if (consecutiveFailures <= 1) return kMinimumPollIntervalMs;
  if (consecutiveFailures == 2) return kMinimumPollIntervalMs * 2;
  if (consecutiveFailures == 3) return kMinimumPollIntervalMs * 4;
  return kMaximumRetryBackoffMs;
}

void requestImmediate(PullSchedule &schedule) { schedule.immediate = true; }

bool shouldStart(const PullSchedule &schedule, uint32_t now, bool relevant,
                 bool connected, bool configured, bool inFlight,
                 bool updateInProgress) {
  if (!connected || !configured || inFlight || updateInProgress ||
      !(schedule.immediate || relevant)) {
    return false;
  }
  return !schedule.hasAttempt || deadlineReached(now, schedule.notBeforeMs);
}

void recordAttempt(PullSchedule &schedule, uint32_t now) {
  schedule.hasAttempt = true;
  schedule.immediate = false;
  schedule.notBeforeMs = now + kMinimumPollIntervalMs;
}

void recordResult(PullSchedule &schedule, uint32_t now, bool success) {
  if (success) {
    schedule.consecutiveFailures = 0;
    schedule.notBeforeMs = now + kMinimumPollIntervalMs;
    return;
  }
  if (schedule.consecutiveFailures < 255) ++schedule.consecutiveFailures;
  schedule.notBeforeMs = now + retryBackoffMs(schedule.consecutiveFailures);
}

}  // namespace budget_pull
}  // namespace imdisplay
