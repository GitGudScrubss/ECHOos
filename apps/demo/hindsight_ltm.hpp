// ECHO OS demo — Hindsight as the cognitive core's long-term memory.
//
// HindsightBridgeMemory implements memory::ILongTermMemory by talking JSON over
// HTTP to scripts/hindsight_bridge.py on localhost. The bridge owns the API key
// and the hindsight-client SDK (the same wrapper the ESP32 brain uses), so no
// credential is ever compiled into, or read by, the C++ binary.
//
//   POST /retain  {"text","context","when"}   -> 202, stored in the background
//   POST /answer  {"question","now"}          -> {"answer": "..." | null}
//
// The bridge answers only when Hindsight's memory actually contains the answer
// (it asks reflect for a structured {found, answer}), which is what lets this
// sit behind the core's grounding rule.
//
// Opt-in: echo-demo only builds this with --hindsight. retain() never blocks the
// conversational loop (a worker thread sends), and recall_answer() is bounded by
// the HTTP timeout; any failure is std::nullopt, so the core abstains as before.
#pragma once

#include "echo/apps/net/http.hpp"
#include "echo/memory/long_term_memory.hpp"

#include <condition_variable>
#include <deque>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>

namespace echo::demo {

class HindsightBridgeMemory final : public memory::ILongTermMemory {
public:
    HindsightBridgeMemory(std::unique_ptr<apps::net::IHttpClient> http, std::string base_url,
                          int answer_timeout_ms = 20000);
    ~HindsightBridgeMemory() override;

    HindsightBridgeMemory(const HindsightBridgeMemory&)            = delete;
    HindsightBridgeMemory& operator=(const HindsightBridgeMemory&) = delete;

    void retain(const std::string& text, const std::string& context, memory::UnixTime when) override;
    std::optional<std::string> recall_answer(const std::string& question, memory::UnixTime now) override;

    // Is the bridge up? (GET /health). Used once at startup to warn early.
    bool reachable();

private:
    void drain();

    std::unique_ptr<apps::net::IHttpClient> http_;
    std::string                             base_;
    int                                     answer_timeout_ms_;

    std::mutex              mu_;
    std::condition_variable cv_;
    std::deque<std::string> pending_;  // JSON bodies for /retain
    bool                    stopping_ = false;
    std::thread             worker_;
};

// JSON string literal for `s` (quotes included), escaping per RFC 8259.
std::string json_quote(const std::string& s);

// An IHttpClient that shells out to the `curl` command-line tool, for builds
// without ECHO_WITH_NETWORK (libcurl). curl ships with Windows 10+ and every
// Linux/macOS. Only used for the local Hindsight bridge.
std::unique_ptr<apps::net::IHttpClient> make_curl_cli_http_client();

}  // namespace echo::demo
