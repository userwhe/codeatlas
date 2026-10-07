"""The answer-model boundary (research R11). Real and fake answer models follow this protocol.

Each `answer` or `review` call (specs/003-pr-review research R7) is one stateless request with
its own output schema. Prompts, evidence, output text, and the API key are never logged or put
into exception messages; only error classes, status codes, and token counts.
"""

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

from google import genai
from google.genai import types

# google-genai 2.28 does not export the Interactions API's error classes or retry settings
# publicly; these private paths are pinned by uv.lock and covered by the unit tests.
from google.genai._gaos.lib.compat_errors import APIConnectionError, APIError
from google.genai._gaos.utils.retries import RetryConfig
from google.genai.interactions import GenerationConfigParam, Interaction
from pydantic import BaseModel, ValidationError

from codeatlas.config import Settings, get_settings
from codeatlas.providers.errors import ProviderRefused, ProviderUnavailable
from codeatlas.qa.schema import AnswerOutput, Claim
from codeatlas.review.schema import ReviewOutput, RiskOut, Severity, SummaryPoint

logger = logging.getLogger(__name__)
# With GOOGLE_GENAI_DEBUG set, the SDK logs request bodies and headers at DEBUG level.
logging.getLogger("google.genai").setLevel(logging.INFO)

MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 8.0
REQUEST_TIMEOUT_SECONDS = 60
# Statuses worth another attempt, besides timeouts and connection errors.
_RETRYABLE_STATUSES = frozenset({408, 429})
# Error codes the Gemini API uses when policy or safety filters block a generation.
_BLOCK_CODES = frozenset(
    {
        "safety",
        "recitation",
        "language",
        "prohibited_content",
        "spii",
        "blocklist",
        "content_blocked",
    }
)
_MAX_REPORTED_ERRORS = 3


@dataclass(frozen=True)
class AnswerUsage:
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    cached_tokens: int


@dataclass(frozen=True)
class AnswerResult:
    """`output` is None when the text did not parse against the schema; `parse_error` says why."""

    output: AnswerOutput | None
    parse_error: str | None
    usage: AnswerUsage
    model: str


@dataclass(frozen=True)
class ReviewResult:
    """`output` is None when the text did not parse against the schema; `parse_error` says why."""

    output: ReviewOutput | None
    parse_error: str | None
    usage: AnswerUsage
    model: str


class AnswerModel(Protocol):
    def answer(self, *, system: str, user_content: str) -> AnswerResult:
        """One stateless call. Raises `ProviderUnavailable` or `ProviderRefused`."""
        ...

    def review(self, *, system: str, user_content: str) -> ReviewResult:
        """One stateless review call. Raises `ProviderUnavailable` or `ProviderRefused`."""
        ...


class InteractionsAPI(Protocol):
    """The part of `genai.Client().interactions` used here; tests pass a stub."""

    def create(
        self,
        *,
        model: str,
        input: str,
        system_instruction: str,
        response_format: dict[str, Any],
        generation_config: GenerationConfigParam,
        store: bool,
    ) -> Interaction: ...


class GeminiClient(Protocol):
    @property
    def interactions(self) -> InteractionsAPI: ...


def _response_schema(output_model: type[BaseModel]) -> dict[str, Any]:
    """The output model's JSON schema with references inlined and only keywords Gemini documents.

    Dropped keywords (titles, string lengths) stay enforced when the output is validated.
    """
    keywords = {"type", "description", "properties", "required", "enum", "items", "maxItems"}
    full = output_model.model_json_schema()
    definitions: dict[str, Any] = full.get("$defs", {})

    def clean(node: dict[str, Any]) -> dict[str, Any]:
        if "$ref" in node:
            node = definitions[node["$ref"].removeprefix("#/$defs/")]
        cleaned: dict[str, Any] = {}
        for key, value in node.items():
            if key == "properties":
                cleaned[key] = {name: clean(child) for name, child in value.items()}
            elif key == "items":
                cleaned[key] = clean(value)
            elif key in keywords:
                cleaned[key] = value
        return cleaned

    return clean(full)


RESPONSE_SCHEMA = _response_schema(AnswerOutput)
REVIEW_RESPONSE_SCHEMA = _response_schema(ReviewOutput)


class GeminiAnswerModel:
    """Gemini through the Interactions API (research R11, ADR 0006)."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: GeminiClient | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._api_key = settings.gemini_api_key
        self._model = settings.answer_model
        self._generation_config: GenerationConfigParam = {
            "thinking_level": settings.answer_thinking_level,
            "max_output_tokens": settings.answer_max_output_tokens,
        }
        self._sleep = sleep
        self._client = client

    def answer(self, *, system: str, user_content: str) -> AnswerResult:
        interaction = self._interact(system, user_content, RESPONSE_SCHEMA, call="answer")
        output, parse_error = _parse(interaction.output_text, interaction.status, AnswerOutput)
        return AnswerResult(
            output=output, parse_error=parse_error, usage=_usage(interaction), model=self._model
        )

    def review(self, *, system: str, user_content: str) -> ReviewResult:
        interaction = self._interact(system, user_content, REVIEW_RESPONSE_SCHEMA, call="review")
        output, parse_error = _parse(interaction.output_text, interaction.status, ReviewOutput)
        return ReviewResult(
            output=output, parse_error=parse_error, usage=_usage(interaction), model=self._model
        )

    def _interact(
        self, system: str, user_content: str, schema: dict[str, Any], *, call: str
    ) -> Interaction:
        """A completed or incomplete interaction; a block or any other ending raises.

        `schema` is the output model's response schema, and `call` names the call in messages.
        """
        interaction = self._create(system, user_content, schema, call=call)
        codes = [_code_name(error.code) for error in interaction.errors or []]
        if any(code in _BLOCK_CODES for code in codes):
            raise ProviderRefused(f"Gemini blocked the {call} ({', '.join(codes)})")
        if interaction.status not in ("completed", "incomplete"):
            raise ProviderUnavailable(
                f"Gemini interaction ended with status {interaction.status}"
                + (f" ({', '.join(codes)})" if codes else "")
            )
        return interaction

    def _create(
        self, system: str, user_content: str, schema: dict[str, Any], *, call: str
    ) -> Interaction:
        client = self._client_or_build()
        attempt = 1
        while True:
            try:
                return client.interactions.create(
                    model=self._model,
                    input=user_content,
                    system_instruction=system,
                    response_format={
                        "type": "text",
                        "mime_type": "application/json",
                        "schema": schema,
                    },
                    generation_config=self._generation_config,
                    store=False,
                )
            except APIError as exc:
                if _blocked(exc):
                    raise ProviderRefused(f"Gemini blocked the request ({_describe(exc)})") from exc
                if not _retryable(exc):
                    raise ProviderUnavailable(_rejected_message(exc, call)) from exc
                if attempt == MAX_ATTEMPTS:
                    raise ProviderUnavailable(
                        f"Gemini {call} failed after {MAX_ATTEMPTS} attempts: {_describe(exc)}"
                    ) from exc
                delay = min(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), BACKOFF_CAP_SECONDS)
                logger.warning(
                    "Gemini %s attempt %d of %d failed (%s); retrying in %.1f s",
                    call,
                    attempt,
                    MAX_ATTEMPTS,
                    _describe(exc),
                    delay,
                )
                self._sleep(delay)
                attempt += 1

    def _client_or_build(self) -> GeminiClient:
        if self._client is None:
            if not self._api_key:
                raise ProviderUnavailable("GEMINI_API_KEY is not set")
            client = genai.Client(
                api_key=self._api_key,
                vertexai=False,
                http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_SECONDS * 1000),
            )
            # The SDK would otherwise retry 408, 409, 429, 5xx, and connection errors itself,
            # with real sleeps; `_create` owns the retries.
            client.interactions.sdk_configuration.retry_config = RetryConfig("none", None, False)
            # The SDK's overloads use literal model names; the call shape is verified in tests.
            self._client = cast(GeminiClient, client)
        return self._client


def _parse[OutputT: BaseModel](
    text: str | None, status: str, output_model: type[OutputT]
) -> tuple[OutputT | None, str | None]:
    cut_off = "the output hit the token limit; " if status == "incomplete" else ""
    if not text or not text.strip():
        return None, f"{cut_off}the output was empty"
    try:
        return output_model.model_validate_json(text), None
    except ValidationError as exc:
        # Locations and messages only: input values could echo the output text.
        details = [
            f"{'.'.join(str(part) for part in error['loc']) or 'output'}: {error['msg']}"
            for error in exc.errors(include_url=False, include_input=False)
        ]
        return None, cut_off + "; ".join(details[:_MAX_REPORTED_ERRORS])


def _usage(interaction: Interaction) -> AnswerUsage:
    usage = interaction.usage
    if usage is None:
        return AnswerUsage(input_tokens=0, output_tokens=0, thinking_tokens=0, cached_tokens=0)
    return AnswerUsage(
        input_tokens=usage.total_input_tokens or 0,
        output_tokens=usage.total_output_tokens or 0,
        thinking_tokens=usage.total_thought_tokens or 0,
        cached_tokens=usage.total_cached_tokens or 0,
    )


def _code_name(code: str | None) -> str:
    # Codes are snake_case names, possibly given as a URI that ends in the name.
    return (code or "unknown").rstrip("/").rsplit("/", 1)[-1].lower()


def _blocked(exc: APIError) -> bool:
    body = exc.body
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return isinstance(code, str) and _code_name(code) in _BLOCK_CODES


def _retryable(exc: APIError) -> bool:
    # `APIConnectionError` includes `APITimeoutError`.
    if isinstance(exc, APIConnectionError):
        return True
    status = exc.status_code
    return isinstance(status, int) and (status in _RETRYABLE_STATUSES or status >= 500)


def _describe(exc: APIError) -> str:
    status = exc.status_code
    return type(exc).__name__ + (f", HTTP {status}" if isinstance(status, int) else "")


def _rejected_message(exc: APIError, call: str) -> str:
    message = f"Gemini rejected the {call} request ({_describe(exc)})"
    if exc.status_code in (401, 403):
        message += "; check GEMINI_API_KEY"
    return message


FAKE_MODEL = "fake-answer-model"
FAKE_USAGE = AnswerUsage(input_tokens=1200, output_tokens=180, thinking_tokens=320, cached_tokens=0)
_EVIDENCE_LABEL = re.compile(r'<evidence id="(E\d+)"')
# A diff hunk's opening tag in a review prompt, and the change labels in it (specs/003-pr-review
# research R6, R7).
_HUNK_TAG = re.compile(r"<hunk\b([^>]*)>")
_HUNK_LABEL = re.compile(r'\b(before|after)="(E\d+)"')
UNKNOWN_REVIEW_LABEL = "E999"


class FakeAnswerModel:
    """A deterministic answer model driven by `fake_answer_model_mode`, read at call time.

    - `ok`: answers with a fact citing the first evidence label and, when there is a second
      label, an inference citing it. Without evidence it behaves like `insufficient`.
    - `insufficient`: `insufficient_evidence` with one gap.
    - `unavailable`: raises `ProviderUnavailable`.
    - `invalid_citations`: a fact citing `E99`, on every call.
    - `refusal`: raises `ProviderRefused`.

    Reviews follow `fake_review_model_mode`, also read at call time (specs/003-pr-review
    research R13). The change labels are those in the prompt's hunk tags, in prompt order.

    - `ok`: an overview, one summary point citing the first change label, and one `security`
      risk. The risk cites the first `before` label with severity `high` when there is one,
      otherwise the first change label with severity `low`. Without change labels, it gives
      only the overview.
    - `no_risks`: the same without the risk.
    - `partly_invalid`: `ok` plus a risk citing the unknown label `E999`.
    - `invalid_citations`: every summary point and risk cites `E999`, on every call.
    - `unavailable` and `refusal`: raise `ProviderUnavailable` and `ProviderRefused`.

    `calls` counts every call, including those that raise, and `prompts` records the user
    content of every review call, so tests can check what was sent.
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        self.calls = 0
        self.prompts: list[str] = []

    def answer(self, *, system: str, user_content: str) -> AnswerResult:
        self.calls += 1
        mode = self._settings.fake_answer_model_mode
        if mode == "unavailable":
            raise ProviderUnavailable("The fake answer model is configured as unavailable")
        if mode == "refusal":
            raise ProviderRefused("The fake answer model is configured to refuse")
        labels = list(dict.fromkeys(_EVIDENCE_LABEL.findall(user_content)))
        if mode == "invalid_citations":
            output = AnswerOutput(
                status="answered",
                summary="An answer that cites evidence that was never supplied.",
                claims=[
                    Claim(text="This cites an unknown label.", kind="fact", evidence_ids=["E99"])
                ],
            )
        elif mode == "insufficient" or not labels:
            output = AnswerOutput(
                status="insufficient_evidence",
                summary="The evidence does not answer the question.",
                gaps=["No evidence item covers the question."],
            )
        else:
            claims = [
                Claim(
                    text="The first evidence item answers the question.",
                    kind="fact",
                    evidence_ids=[labels[0]],
                )
            ]
            if len(labels) > 1:
                claims.append(
                    Claim(
                        text="The second evidence item supports the answer.",
                        kind="inference",
                        evidence_ids=[labels[1]],
                    )
                )
            output = AnswerOutput(
                status="answered", summary="The evidence answers the question.", claims=claims
            )
        return AnswerResult(output=output, parse_error=None, usage=FAKE_USAGE, model=FAKE_MODEL)

    def review(self, *, system: str, user_content: str) -> ReviewResult:
        self.calls += 1
        self.prompts.append(user_content)
        mode = self._settings.fake_review_model_mode
        if mode == "unavailable":
            raise ProviderUnavailable("The fake review model is configured as unavailable")
        if mode == "refusal":
            raise ProviderRefused("The fake review model is configured to refuse")
        sides = [
            (side, label)
            for attributes in _HUNK_TAG.findall(user_content)
            for side, label in _HUNK_LABEL.findall(attributes)
        ]
        change_labels = [label for _, label in sides]
        before_labels = [label for side, label in sides if side == "before"]
        points: list[SummaryPoint] = []
        risks: list[RiskOut] = []
        if mode == "invalid_citations":
            points.append(_fake_summary_point(UNKNOWN_REVIEW_LABEL))
            risks.append(_fake_risk(UNKNOWN_REVIEW_LABEL, "low"))
        elif change_labels:
            points.append(_fake_summary_point(change_labels[0]))
            if mode != "no_risks":
                risks.append(
                    _fake_risk(before_labels[0], "high")
                    if before_labels
                    else _fake_risk(change_labels[0], "low")
                )
        if mode == "partly_invalid":
            risks.append(_fake_risk(UNKNOWN_REVIEW_LABEL, "medium"))
        output = ReviewOutput(
            overview="The pull request changes the code shown in the diff.",
            summary_points=points,
            risks=risks,
        )
        return ReviewResult(output=output, parse_error=None, usage=FAKE_USAGE, model=FAKE_MODEL)


def _fake_summary_point(label: str) -> SummaryPoint:
    return SummaryPoint(
        change="modified", text="The first change is summarized here.", evidence_ids=[label]
    )


def _fake_risk(label: str, severity: Severity) -> RiskOut:
    return RiskOut(
        title="The change may weaken a check",
        severity=severity,
        category="security",
        basis="observed",
        explanation="The cited lines change how a request is checked.",
        suggested_check="Confirm that the check still refuses requests it refused before.",
        evidence_ids=[label],
    )


_fake_answer_model: FakeAnswerModel | None = None


def get_answer_model(settings: Settings | None = None) -> AnswerModel:
    """Return the shared fake answer model in fake mode, otherwise a Gemini answer model.

    The fake is shared process-wide so tests can read its `calls` after a job runs.
    """
    global _fake_answer_model
    settings = settings or get_settings()
    if not settings.fake_externals:
        return GeminiAnswerModel(settings)
    if _fake_answer_model is None:
        _fake_answer_model = FakeAnswerModel(settings)
    return _fake_answer_model


def reset_fake_answer_model() -> None:
    """Drop the shared fake, so the next one starts at zero calls with the current settings."""
    global _fake_answer_model
    _fake_answer_model = None
