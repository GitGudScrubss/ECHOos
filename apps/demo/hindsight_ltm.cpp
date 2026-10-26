#include "hindsight_ltm.hpp"

#include "echo/json.hpp"
#include "echo/log.hpp"

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <random>
#include <sstream>
#include <utility>

namespace echo::demo {

std::string json_quote(const std::string& s) {
    std::string out = "\"";
    for (unsigned char c : s) {
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n";  break;
            case '\r': out += "\\r";  break;
            case '\t': out += "\\t";  break;
            default:
                if (c < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof buf, "\\u%04x", c);
                    out += buf;
                } else {
                    out += static_cast<char>(c);  // UTF-8 passes through unchanged
                }
        }
    }
    return out + "\"";
}

namespace {

apps::net::HttpRequest post(const std::string& url, std::string body, int timeout_ms) {
    apps::net::HttpRequest req;
    req.method     = apps::net::Method::Post;
    req.url        = url;
    req.body       = std::move(body);
    req.timeout_ms = timeout_ms;
    req.headers["Content-Type"] = "application/json";
    return req;
}

}  // namespace

HindsightBridgeMemory::HindsightBridgeMemory(std::unique_ptr<apps::net::IHttpClient> http,
                                             std::string base_url, int answer_timeout_ms)
    : http_(std::move(http)), base_(std::move(base_url)), answer_timeout_ms_(answer_timeout_ms) {
    while (!base_.empty() && base_.back() == '/') base_.pop_back();
    worker_ = std::thread([this] { drain(); });
}

HindsightBridgeMemory::~HindsightBridgeMemory() {
    {
        std::lock_guard<std::mutex> lock(mu_);
        stopping_ = true;
    }
    cv_.notify_all();
    if (worker_.joinable()) worker_.join();  // sends what is queued, then exits
}

void HindsightBridgeMemory::retain(const std::string& text, const std::string& context,
                                   memory::UnixTime when) {
    std::string body = "{\"text\":" + json_quote(text) + ",\"context\":" + json_quote(context) +
                       ",\"when\":" + std::to_string(when) + "}";
    {
        std::lock_guard<std::mutex> lock(mu_);
        if (pending_.size() >= 64) pending_.pop_front();  // bridge down: don't grow forever
        pending_.push_back(std::move(body));
    }
    cv_.notify_one();
}

void HindsightBridgeMemory::drain() {
    for (;;) {
        std::string body;
        {
            std::unique_lock<std::mutex> lock(mu_);
            cv_.wait(lock, [this] { return stopping_ || !pending_.empty(); });
            if (pending_.empty()) return;  // stopping, and nothing left to send
            body = std::move(pending_.front());
            pending_.pop_front();
        }
        auto resp = http_->send(post(base_ + "/retain", std::move(body), 10000));
        if (!resp.ok())
            log_warn("hindsight", "retain not delivered: " +
                                      (resp.error.empty() ? "HTTP " + std::to_string(resp.status)
                                                          : resp.error));
    }
}

std::optional<std::string> HindsightBridgeMemory::recall_answer(const std::string& question,
                                                                memory::UnixTime now) {
    std::string body = "{\"question\":" + json_quote(question) + ",\"now\":" + std::to_string(now) + "}";
    auto resp = http_->send(post(base_ + "/answer", std::move(body), answer_timeout_ms_));
    if (!resp.ok()) {
        log_warn("hindsight", "recall failed: " +
                                  (resp.error.empty() ? "HTTP " + std::to_string(resp.status) : resp.error));
        return std::nullopt;
    }
    const Json doc = Json::parse(resp.body);
    const Json& answer = doc["answer"];
    if (!answer.is_string()) return std::nullopt;  // null: memory has no answer
    std::string text = doc.str_or("answer");
    if (text.empty()) return std::nullopt;
    return text;
}

bool HindsightBridgeMemory::reachable() {
    apps::net::HttpRequest req;
    req.url        = base_ + "/health";
    req.timeout_ms = 3000;
    return http_->send(req).ok();
}

// --- curl CLI transport --------------------------------------------------------

namespace {

#if defined(_WIN32)
#  define ECHO_POPEN  _popen
#  define ECHO_PCLOSE _pclose
#else
#  define ECHO_POPEN  popen
#  define ECHO_PCLOSE pclose
#endif

// The bridge is ours and local; still, never let request text reach a shell.
// Bodies go through a temp file and only fixed, quoted arguments are composed.
class CurlCliHttpClient final : public apps::net::IHttpClient {
public:
    apps::net::HttpResponse send(const apps::net::HttpRequest& req) override {
        apps::net::HttpResponse resp;
        for (char c : req.url) {
            if (c == '"' || c == '\'' || c == '`' || c == '$' || c == '&' || c == '|' ||
                c == ';' || c == '<' || c == '>' || c == '\n' || c == '\r') {
                resp.error = "refusing unsafe URL";
                return resp;
            }
        }

        namespace fs = std::filesystem;
        std::error_code ec;
        std::mt19937_64 rng(std::random_device{}());
        const fs::path body_file =
            fs::temp_directory_path(ec) / ("echo-hs-" + std::to_string(rng()) + ".json");

        std::ostringstream cmd;
        cmd << "curl -s -S -m " << std::max(1, req.timeout_ms / 1000)
            << " -w \"\\n%{http_code}\"";
        if (req.method == apps::net::Method::Post) {
            std::ofstream(body_file, std::ios::binary) << req.body;
            cmd << " -X POST -H \"Content-Type: application/json\" --data-binary \"@"
                << body_file.string() << "\"";
        }
        cmd << " \"" << req.url << "\"";
#if !defined(_WIN32)
        cmd << " 2>/dev/null";
#else
        cmd << " 2>NUL";
#endif

        std::string out;
        if (FILE* p = ECHO_POPEN(cmd.str().c_str(), "r")) {
            char buf[4096];
            std::size_t n;
            while ((n = std::fread(buf, 1, sizeof buf, p)) > 0) out.append(buf, n);
            ECHO_PCLOSE(p);
        }
        fs::remove(body_file, ec);

        // Last line is the status code written by -w.
        const auto nl = out.find_last_of('\n');
        if (nl == std::string::npos) {
            resp.error = "curl produced no output (is curl installed? is the bridge running?)";
            return resp;
        }
        resp.status = std::strtol(out.c_str() + nl + 1, nullptr, 10);
        resp.body   = out.substr(0, nl);
        if (resp.status == 0) resp.error = "no response from " + req.url;
        return resp;
    }
};

}  // namespace

std::unique_ptr<apps::net::IHttpClient> make_curl_cli_http_client() {
    return std::make_unique<CurlCliHttpClient>();
}

}  // namespace echo::demo
