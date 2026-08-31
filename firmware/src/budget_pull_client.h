#pragma once

#include <stdint.h>

#include "budget_pull_protocol.h"

namespace imdisplay {
namespace budget_pull {

enum class PullResultCode : uint8_t {
  Success,
  InvalidConfiguration,
  ResolveFailed,
  UnsafeResolution,
  ConnectFailed,
  WriteFailed,
  ReadTimeout,
  ResponseTooLarge,
  AuthenticationFailed,
};

struct PullRequest {
  char host[kMaximumHostLength + 1]{};
  uint16_t port = 0;
  uint8_t key[32]{};
};

struct PullResult {
  PullResultCode code = PullResultCode::InvalidConfiguration;
  uint16_t bodyLength = 0;
  char body[kMaximumBodyBytes + 1]{};
};

void performPull(const PullRequest &request, PullResult &result);
const char *resultCodeName(PullResultCode code);

}  // namespace budget_pull
}  // namespace imdisplay
