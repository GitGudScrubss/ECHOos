// ECHO OS — optional long-term memory (Hindsight) in the cognitive core.
//
// The grounding rule (Phase 21) says a question about the wearer's own life is
// answered from the record or not at all. Long-term memory widens "the record"
// to free-text memories retained over days and weeks; it must not widen it to
// guesses. These suites drive the REAL cognitive core and memory engine, with
// the LLM replaced by tests/fake_llm.hpp and Hindsight replaced by a scripted
// fake, and pin down:
//
//   * a memory hit turns "I don't have a record of that" into a grounded,
//     labelled answer, and the model is still never asked;
//   * the store still wins when it has the answer;
//   * a miss, or a dead backend, abstains exactly as before;
//   * what gets written: namings and confident statements, never questions,
//     never a transcript that failed the 0.72 input gate.
//
// Runs entirely in the dependency-free stub build — no network, no Hindsight.
#include "echo/cognitive/cognitive_core.hpp"
#include "echo/memory/long_term_memory.hpp"
#include "echo/memory/memory_engine.hpp"
#include "echo/memory/utterance.hpp"
#include "echo/perception/perception_engine.hpp"
#include "echo/types.hpp"

#include "core_factory.hpp"  // cognitive-core/src (private DI seam)

#include "check.hpp"
#include "fake_llm.hpp"

#include <memory>
#include <optional>
#include <string>
#include <utility>
#include <vector>

using namespace echo;

namespace {

constexpr memory::UnixTime kNow = 1'700'000'000;

perception::Perception heard(const std::string& text, float conf = 0.95f) {
    perception::Perception p;
    perception::Transcript t;
    t.text       = text;
    t.confidence = Confidence{conf};
    t.endpointed = true;
    p.speech     = std::move(t);
    return p;
}

// Hindsight, scripted: answers with `answer` (or nothing), records every write.
class FakeLongTermMemory final : public memory::ILongTermMemory {
public:
    std::optional<std::string> answer;
    std::vector<std::string>   retained;
    std::vector<std::string>   asked;

    void retain(const std::string& text, const std::string&, memory::UnixTime) override {
        retained.push_back(text);
    }
    std::optional<std::string> recall_answer(const std::string& question, memory::UnixTime) override {
        asked.push_back(question);
        return answer;
    }
};

struct Rig {
    std::unique_ptr<memory::IMemoryEngine>     mem;
    std::unique_ptr<FakeLongTermMemory>        ltm;
    std::unique_ptr<cognitive::ICognitiveCore> core;
    echo::test::FakeLlm*                       llm = nullptr;  // owned by `core`
};

Rig rig(bool with_ltm = true) {
    Rig r;
    r.mem = memory::make_memory_engine();
    (void)r.mem->open(":memory:");
    if (with_ltm) r.ltm = std::make_unique<FakeLongTermMemory>();
    auto fake = std::make_unique<echo::test::FakeLlm>();
    r.llm  = fake.get();
    r.core = cognitive::make_cognitive_core_with_llm({}, r.mem.get(), std::move(fake), r.ltm.get());
    (void)r.core->initialize();
    return r;
}

const std::string kUnverified = cognitive::SafeModeConfig{}.unverified_response;

// --- 1. The headline: no record -> answered from long-term memory ------------
void test_memory_hit_answers_self_referential_question() {
    auto r = rig();
    r.ltm->answer = "Dr. Rao, Wednesday 28th at 10:30. Priya picks you up at 10.";
    r.llm->text   = "It's on Friday morning.";  // a confident invention

    auto resp = r.core->respond(heard("when is my appointment with the doctor"));
    CHECK(resp.is_ok());
    CHECK(resp.value().kind == cognitive::ResponseKind::Normal);
    CHECK(resp.value().text ==
          "From your memory: Dr. Rao, Wednesday 28th at 10:30. Priya picks you up at 10.");
    CHECK(resp.value().flag_caregiver == false);
    CHECK(!r.llm->consulted());          // grounding still never asks the model
    CHECK(r.ltm->asked.size() == 1);
    CHECK(r.ltm->retained.empty());      // a question is never stored
}

// --- 2. Before/after: same question, without long-term memory ---------------
void test_without_long_term_memory_still_abstains() {
    auto r = rig(/*with_ltm=*/false);
    auto resp = r.core->respond(heard("when is my appointment with the doctor"));
    CHECK(resp.value().kind == cognitive::ResponseKind::Unverified);
    CHECK(resp.value().text == kUnverified);
}

// --- 3. A miss (or a dead backend: same nullopt) abstains as before ----------
void test_memory_miss_abstains() {
    auto r = rig();
    r.ltm->answer = std::nullopt;
    auto resp = r.core->respond(heard("where did i leave my keys"));
    CHECK(resp.value().kind == cognitive::ResponseKind::Unverified);
    CHECK(resp.value().text == kUnverified);
    CHECK(r.ltm->asked.size() == 1);

    r.ltm->answer = std::string{};       // an empty answer is not an answer
    resp = r.core->respond(heard("where did i leave my keys"));
    CHECK(resp.value().kind == cognitive::ResponseKind::Unverified);
}

// --- 4. The store still wins when it knows ------------------------------------
void test_store_answer_takes_precedence() {
    auto r = rig();
    (void)r.mem->add_reminder("take blood pressure medication", kNow - 3600,
                              memory::Recurrence::Daily);
    r.ltm->answer = "Something from long-term memory.";
    auto resp = r.core->respond(heard("what am i supposed to take today"));
    CHECK(resp.value().kind == cognitive::ResponseKind::Normal);
    CHECK(resp.value().text.find("blood pressure medication") != std::string::npos);
    CHECK(r.ltm->asked.empty());         // never consulted when the store answered
}

// --- 5. [route:memory] with an empty store falls through to long-term memory --
void test_route_memory_uses_long_term_memory() {
    auto r = rig();
    r.llm->text   = "Let me check.";
    r.llm->intent = "memory";
    r.ltm->answer = "Your appointment with Dr. Rao is Wednesday at 10:30.";
    auto resp = r.core->respond(heard("tell me about the clinic visit"));
    CHECK(resp.value().kind == cognitive::ResponseKind::Normal);
    CHECK(resp.value().text.find("Dr. Rao") != std::string::npos);
    CHECK(resp.value().text.find("Let me check") == std::string::npos);
}

// --- 6. What gets written ------------------------------------------------------
void test_confident_statement_is_retained() {
    auto r = rig();
    r.llm->text = "Okay.";
    (void)r.core->respond(heard("i left my keys in the blue bowl by the door"));
    CHECK(r.ltm->retained.size() == 1);
    CHECK(r.ltm->retained[0].find("blue bowl") != std::string::npos);
}

void test_questions_are_never_retained() {
    auto r = rig();
    r.llm->text = "It's three o'clock.";
    (void)r.core->respond(heard("what time is it"));
    (void)r.core->respond(heard("is it going to rain today?"));
    (void)r.core->respond(heard("could you turn the radio on"));
    CHECK(r.ltm->retained.empty());
}

void test_low_confidence_is_never_retained() {
    auto r = rig();
    r.llm->text = "Okay.";
    auto resp = r.core->respond(heard("i put the pills in the drawer", 0.40f));
    CHECK(resp.value().kind == cognitive::ResponseKind::SafeMode);
    CHECK(r.ltm->retained.empty());      // a shaky transcript is never remembered
}

void test_naming_is_retained() {
    auto r = rig();
    auto resp = r.core->respond(heard("this is my daughter priya"));
    CHECK(resp.value().text.find("Priya") != std::string::npos);
    CHECK(r.ltm->retained.size() == 1);
    CHECK(r.ltm->retained[0].find("Priya") != std::string::npos);
    CHECK(r.ltm->retained[0].find("daughter") != std::string::npos);
}

// --- 7. The question classifier -------------------------------------------------
void test_is_question() {
    CHECK(memory::is_question("where are my keys?"));
    CHECK(memory::is_question("did i take my tablets"));
    CHECK(memory::is_question("Remind me what Priya said"));
    CHECK(memory::is_question("   "));
    CHECK(!memory::is_question("I left my keys in the blue bowl"));
    CHECK(!memory::is_question("Priya is coming on Sunday."));
}

}  // namespace

int main() {
    test_memory_hit_answers_self_referential_question();
    test_without_long_term_memory_still_abstains();
    test_memory_miss_abstains();
    test_store_answer_takes_precedence();
    test_route_memory_uses_long_term_memory();
    test_confident_statement_is_retained();
    test_questions_are_never_retained();
    test_low_confidence_is_never_retained();
    test_naming_is_retained();
    test_is_question();
    return echo::test::report("long-term-memory");
}
