// ECHO OS memory — optional long-term episodic memory (the Hindsight seam).
//
// The on-device store (memory_engine.hpp) holds structured facts: people,
// reminders, a narrow event log. It cannot answer "what did Priya say she'd
// bring on Sunday?" or "where did I leave my keys?", because nobody ever
// stored the sentence that would answer it. The cognitive core then abstains
// with "I don't have a record of that" — correct, but it never gets better.
//
// ILongTermMemory is a second, optional source of record for those questions:
// free-text memories retained as the wearer goes about their day, recalled by
// meaning later. The production backend is Hindsight (apps/demo, through a
// local bridge that owns the API key); tests use a fake.
//
// The contract mirrors the store's, so the grounding rule (Phase 21) is not
// weakened:
//   * recall_answer() returns text only when memory actually holds an answer.
//     "Nothing found", "backend down", "timed out" are all std::nullopt, and
//     the core then abstains exactly as before. It is never a guess.
//   * retain() is only called on the confident path (past the 0.72 input
//     gate), never for questions, so a shaky transcript is never remembered.
//   * Implementations must never throw and must bound their own latency.
//
// Opt-in and consent-gated (ADR-5 amendment, docs/DECISIONS.md): with no
// backend attached (nullptr) every long-term branch is skipped and the core
// behaves exactly as it did before.
#pragma once

#include "echo/memory/memory_engine.hpp"

#include <optional>
#include <string>

namespace echo::memory {

class ILongTermMemory {
public:
    virtual ~ILongTermMemory() = default;

    // Store one line of the wearer's life. `context` is a short label such as
    // "introduction" or "statement". Fire-and-forget: must return quickly.
    virtual void retain(const std::string& text, const std::string& context, UnixTime when) = 0;

    // A grounded answer to a question about the wearer's own life, or nullopt.
    virtual std::optional<std::string> recall_answer(const std::string& question, UnixTime now) = 0;
};

}  // namespace echo::memory
