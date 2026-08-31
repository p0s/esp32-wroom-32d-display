#include "../src/budget_pull_protocol.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

#include <string>

using imdisplay::budget_pull::CryptoProvider;
using imdisplay::budget_pull::PullSchedule;

namespace {

bool fakeSha256(const uint8_t *data, size_t length, uint8_t output[32]) {
  uint8_t accumulator = 0x5a;
  for (size_t i = 0; i < length; ++i) {
    accumulator = static_cast<uint8_t>((accumulator * 33U) ^ data[i]);
  }
  for (size_t i = 0; i < 32; ++i) output[i] = accumulator ^ static_cast<uint8_t>(i * 7U);
  return true;
}

bool fakeHmacSha256(const uint8_t *key, size_t keyLength, const uint8_t *data,
                    size_t length, uint8_t output[32]) {
  if (!fakeSha256(data, length, output)) return false;
  for (size_t i = 0; i < 32; ++i) output[i] ^= key[i % keyLength];
  return true;
}

constexpr CryptoProvider kCrypto = {fakeSha256, fakeHmacSha256};

std::string hex(const uint8_t bytes[32]) {
  static constexpr char digits[] = "0123456789abcdef";
  std::string output(64, '0');
  for (size_t i = 0; i < 32; ++i) {
    output[i * 2] = digits[bytes[i] >> 4];
    output[i * 2 + 1] = digits[bytes[i] & 0x0f];
  }
  return output;
}

std::string signedResponse(const char *nonce, const std::string &body,
                           const uint8_t key[32]) {
  uint8_t digest[32];
  assert(fakeSha256(reinterpret_cast<const uint8_t *>(body.data()), body.size(), digest));
  const std::string bodyHash = hex(digest);
  const std::string canonical = std::string("imdisplay-cache-v1\nresponse\n200\n") +
                                nonce + "\n" + bodyHash + "\n";
  assert(fakeHmacSha256(key, 32,
                       reinterpret_cast<const uint8_t *>(canonical.data()),
                       canonical.size(), digest));
  const std::string authorization = hex(digest);
  return std::string("HTTP/1.1 200 OK\r\n") +
         "Content-Type: application/json\r\n" +
         "Content-Length: " + std::to_string(body.size()) + "\r\n" +
         "Connection: close\r\n" +
         "X-imDisplay-Protocol: 1\r\n" +
         "X-imDisplay-Nonce: " + nonce + "\r\n" +
         "X-imDisplay-Body-SHA256: " + bodyHash + "\r\n" +
         "X-imDisplay-Authorization: " + authorization + "\r\n\r\n" + body;
}

void testBoundedHexValidation() {
  const char exact[] = "0123456789abcdef0123456789abcdef";
  const char shortValue[] = "01234567";
  const char longValue[] = "0123456789abcdef0123456789abcdef0";
  assert(imdisplay::budget_pull::isLowerHex(exact, 32));
  assert(!imdisplay::budget_pull::isLowerHex(shortValue, 32));
  assert(!imdisplay::budget_pull::isLowerHex(longValue, 32));
  assert(!imdisplay::budget_pull::isLowerHex("0123456789ABCDEF0123456789abcdef", 32));

  uint8_t key[32];
  assert(!imdisplay::budget_pull::decodeKeyHex(shortValue, key));
  assert(!imdisplay::budget_pull::decodeKeyHex(
      "00000000000000000000000000000000000000000000000000000000000000000", key));
  assert(imdisplay::budget_pull::decodeKeyHex(
      "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f", key));
}

void testPrivateIpv4Validation() {
  uint8_t octets[4];
  assert(imdisplay::budget_pull::parsePrivateIpv4("192.168.10.4", octets));
  assert(octets[0] == 192 && octets[3] == 4);
  assert(imdisplay::budget_pull::parsePrivateIpv4("10.0.0.1", octets));
  assert(imdisplay::budget_pull::parsePrivateIpv4("172.31.255.254", octets));
  assert(!imdisplay::budget_pull::parsePrivateIpv4("172.32.0.1", octets));
  assert(!imdisplay::budget_pull::parsePrivateIpv4("8.8.8.8", octets));
  assert(!imdisplay::budget_pull::parsePrivateIpv4("192.168.01.4", octets));
  assert(!imdisplay::budget_pull::parsePrivateIpv4("192.168.1.4x", octets));
}

void testBoundedLocalHostnameValidation() {
  char label[64];
  assert(imdisplay::budget_pull::isLocalHostname("imdisplay-mac.local"));
  assert(imdisplay::budget_pull::isLocalHostname("imdisplay-mac-2.local"));
  assert(imdisplay::budget_pull::isValidPullHost("imdisplay-mac.local"));
  assert(imdisplay::budget_pull::isValidPullHost("192.168.1.10"));
  assert(!imdisplay::budget_pull::isLocalHostname("IMDISPLAY-MAC.local"));
  assert(!imdisplay::budget_pull::isLocalHostname("imdisplay-mac.example.local"));
  assert(!imdisplay::budget_pull::isLocalHostname("-imdisplay-mac.local"));
  assert(!imdisplay::budget_pull::isLocalHostname("imdisplay-mac-.local"));
  assert(!imdisplay::budget_pull::isLocalHostname("imdisplay-mac.local."));
  assert(!imdisplay::budget_pull::isValidPullHost("8.8.8.8"));
  assert(!imdisplay::budget_pull::isValidPullHost(
      "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.local"));
  assert(imdisplay::budget_pull::copyLocalHostnameLabel(
      "imdisplay-mac.local", label, sizeof(label)));
  assert(!strcmp(label, "imdisplay-mac"));
  assert(!imdisplay::budget_pull::copyLocalHostnameLabel(
      "imdisplay-mac.local", label, 5));
  assert(!imdisplay::budget_pull::copyLocalHostnameLabel(
      "IMDISPLAY-MAC.local", label, sizeof(label)));
}

void testAuthenticatedRequest() {
  uint8_t key[32]{};
  const char nonce[] = "0123456789abcdef0123456789abcdef";
  char request[imdisplay::budget_pull::kMaximumRequestBytes + 1];
  assert(imdisplay::budget_pull::buildAuthenticatedRequest(
      request, sizeof(request), "192.168.1.10", 47832, nonce, key, kCrypto));
  assert(strstr(request, "GET /v1/quota HTTP/1.1\r\n") == request);
  assert(strstr(request, "Host: 192.168.1.10:47832\r\n"));
  assert(strstr(request, "X-imDisplay-Nonce: 0123456789abcdef0123456789abcdef\r\n"));
  assert(strstr(request, "X-imDisplay-Authorization: "));
  assert(imdisplay::budget_pull::buildAuthenticatedRequest(
      request, sizeof(request), "imdisplay-mac.local", 47832, nonce, key, kCrypto));
  assert(strstr(request, "Host: imdisplay-mac.local:47832\r\n"));
  assert(!imdisplay::budget_pull::buildAuthenticatedRequest(
      request, sizeof(request), "8.8.8.8", 47832, nonce, key, kCrypto));
  const char shortNonce[] = "0123";
  assert(!imdisplay::budget_pull::buildAuthenticatedRequest(
      request, sizeof(request), "192.168.1.10", 47832, shortNonce, key, kCrypto));
  const char longNonce[] = "0123456789abcdef0123456789abcdef0";
  assert(!imdisplay::budget_pull::buildAuthenticatedRequest(
      request, sizeof(request), "192.168.1.10", 47832, longNonce, key, kCrypto));
}

void testAuthenticatedResponseAndReplay() {
  uint8_t key[32];
  for (size_t i = 0; i < sizeof(key); ++i) key[i] = static_cast<uint8_t>(i);
  const char nonce[] = "0123456789abcdef0123456789abcdef";
  const char otherNonce[] = "fedcba9876543210fedcba9876543210";
  const std::string body = "{\"schema\":1,\"ok\":true}";
  const std::string response = signedResponse(nonce, body, key);
  size_t offset = 0;
  size_t length = 0;
  assert(imdisplay::budget_pull::validateAuthenticatedResponse(
      response.data(), response.size(), nonce, key, kCrypto, offset, length));
  assert(response.substr(offset, length) == body);
  assert(!imdisplay::budget_pull::validateAuthenticatedResponse(
      response.data(), response.size(), otherNonce, key, kCrypto, offset, length));

  std::string tampered = response;
  tampered[tampered.size() - 2] ^= 1;
  assert(!imdisplay::budget_pull::validateAuthenticatedResponse(
      tampered.data(), tampered.size(), nonce, key, kCrypto, offset, length));
  tampered = response + "x";
  assert(!imdisplay::budget_pull::validateAuthenticatedResponse(
      tampered.data(), tampered.size(), nonce, key, kCrypto, offset, length));
}

void testResponseHeaderBounds() {
  uint8_t key[32]{};
  const char nonce[] = "0123456789abcdef0123456789abcdef";
  std::string response = signedResponse(nonce, "{}", key);
  const std::string marker = "Connection: close\r\n";
  response.insert(response.find(marker), "Unexpected: value\r\n");
  size_t offset = 0;
  size_t length = 0;
  assert(!imdisplay::budget_pull::validateAuthenticatedResponse(
      response.data(), response.size(), nonce, key, kCrypto, offset, length));
}

void testCadenceBackoffAndWrap() {
  PullSchedule schedule;
  imdisplay::budget_pull::requestImmediate(schedule);
  assert(imdisplay::budget_pull::shouldStart(schedule, 1000, false, true, true,
                                             false, false));
  imdisplay::budget_pull::recordAttempt(schedule, 1000);
  assert(!imdisplay::budget_pull::shouldStart(schedule, 60999, true, true, true,
                                              false, false));
  assert(imdisplay::budget_pull::shouldStart(schedule, 61000, true, true, true,
                                             false, false));
  imdisplay::budget_pull::recordResult(schedule, 61000, false);
  assert(schedule.notBeforeMs == 121000);
  imdisplay::budget_pull::recordResult(schedule, 121000, false);
  assert(schedule.notBeforeMs == 241000);
  imdisplay::budget_pull::recordResult(schedule, 241000, false);
  assert(schedule.notBeforeMs == 481000);
  imdisplay::budget_pull::recordResult(schedule, 481000, false);
  assert(schedule.notBeforeMs == 781000);
  assert(!imdisplay::budget_pull::shouldStart(schedule, 781000, true, false, true,
                                              false, false));
  assert(!imdisplay::budget_pull::shouldStart(schedule, 781000, true, true, true,
                                              true, false));
  assert(!imdisplay::budget_pull::shouldStart(schedule, 781000, true, true, true,
                                              false, true));
  assert(imdisplay::budget_pull::deadlineReached(16, 0xfffffff0U));
}

}  // namespace

int main() {
  testBoundedHexValidation();
  testPrivateIpv4Validation();
  testBoundedLocalHostnameValidation();
  testAuthenticatedRequest();
  testAuthenticatedResponseAndReplay();
  testResponseHeaderBounds();
  testCadenceBackoffAndWrap();
  puts("7 budget pull protocol tests passed");
  return 0;
}
