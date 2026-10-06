# ADR 0004: Generation and embedding providers

- **Status**: Accepted. The generation decision is superseded by ADR 0006; the embedding and
  citation decisions still apply.
- **Date**: 2026-10-03
- **Scope**: `specs/001-repository-qa`

## Context

Repository Q&A needs two models:

- **Generation**: answers questions only from supplied evidence, cites that evidence, and returns
  output the server can validate.
- **Embeddings**: support meaning-based search over Markdown documentation.

The provider was left open in the design document.

## Options considered

- **Generation**:
  - Claude Opus 5.5.
  - Claude Sonnet 5.5, at about half the price.
  - Another vendor's model.
- **Embeddings**:
  - Voyage AI `voyage-4`.
  - OpenAI `text-embedding-3-small`.
  - A local sentence-transformers model.
- **Citation mechanism**:
  - Structured outputs that cite our own evidence IDs.
  - Anthropic's Citations feature.

## Decision

- **Generation**: `claude-opus-5-5` through the Anthropic Python SDK.
  - Calls use `messages.parse()` structured outputs with a Pydantic schema.
  - `effort` is explicit, starting at `medium`.
  - At most two calls per attempt: an answer and one repair.
- **Embeddings**: `voyage-4` at 1024 dimensions, for documentation only. The model and dimension
  are recorded in each snapshot's index version.
- **Citations**: the model cites evidence IDs (`E1` to `En`). The server validates them and
  builds every citation from stored evidence.

## Trade-off

- **Gained**:
  - Answer quality from the default current model.
  - Validated JSON instead of parsed prose.
  - Citations that can never point outside retrieved evidence.
  - Embedding cost is negligible at pilot volume.
- **Accepted**:
  - About $0.12 to $0.20 per question, bounded by the 20-per-day workspace limit.
  - Two vendor keys.
  - The Citations feature is not used, because it cannot be combined with structured outputs.
- **Revisit when**:
  - the evaluation shows Sonnet 5.5 matches quality: switching is a configuration change, and the
    owner decides;
  - Recall@5 falls below 0.80: add `voyage-code-4` code embeddings; or
  - deployment moves inference to Amazon Bedrock or Claude Platform on AWS.

Details: `specs/001-repository-qa/research.md` R11 and R12.
