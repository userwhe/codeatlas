# ADR 0006: Gemini 3.8 Flash for answer generation

- **Status**: Accepted
- **Date**: 2026-10-05
- **Supersedes**: The generation decision in ADR 0004. The embedding and citation decisions in
  ADR 0004 still apply.
- **Scope**: `specs/001-repository-qa`

## Context

ADR 0004 chose Claude Opus 5.5 for answer generation and left any model switch to the owner.
The owner chose Gemini 3.8 Flash.

The requirements are unchanged:

- Answer only from supplied evidence.
- Cite evidence IDs.
- Return output the server can validate.
- Keep cost bounded.
- Do not retain private source excerpts beyond the request.

## Options considered

1. Gemini 3.8 Flash through the Interactions API.
2. Gemini 3.8 Flash through the legacy `generateContent` API.
3. Keep Claude Opus 5.5.

## Decision

Option 1:

- `gemini-3.8-flash` through the `google-genai` SDK, called with `client.interactions.create`.
- `store=False`, so the provider keeps no interaction state.
- A JSON-schema response format generated from the Pydantic output model.
- `thinking_level` set explicitly, starting at `medium`.
- No tools.
- A paid-tier API key is required for real repositories.
- The citation mechanism from ADR 0004 is unchanged: evidence IDs that the server validates.

## Trade-off

- **Gained**:
  - About $0.02 to $0.07 per question, compared with $0.12 to $0.20.
  - Flash-tier latency.
- **Accepted**:
  - Answer quality has to be re-verified on the evaluation set (SC-004 to SC-006).
  - The price doubles on 2027-01-01.
  - The Interactions API is newer than `generateContent`.
  - Generation and embeddings now come from two vendors, Google and Voyage AI.
- **Revisit when**:
  - the evaluation misses SC-005 or SC-006 even at `high` thinking; or
  - a single model vendor becomes a goal. Gemini embeddings would require re-indexing.

Details: `specs/001-repository-qa/research.md` R11.
