"""
Intent agent — natural language → InfraContract.

Uses Claude with two tools:
  ask_clarification  ask the user a single focused question
  emit_contract      produce the final InfraContract JSON

The agent loops until it emits a contract or hits max_clarifications.
All turns are recorded in IntentTrace for observability.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

import anthropic

from ..contract.schema import InfraContract, SchemaVersion
from ..contract.validator import ValidationResult, validate_contract
from .prompts import CLARIFICATION_GUIDANCE, SYSTEM_PROMPT, build_conventions_section

if TYPE_CHECKING:
    from ..conventions.schema import ConventionProfile

# Default model — fast enough for interactive use, capable enough for planning
DEFAULT_MODEL = "claude-opus-4-6"
MAX_TOKENS = 4096


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class Turn:
    role: str  # "user" | "assistant"
    content: str | list  # raw message content


@dataclass
class IntentTrace:
    """Full record of the intent → contract conversation."""
    intent: str
    turns: list[Turn] = field(default_factory=list)
    questions_asked: list[str] = field(default_factory=list)


@dataclass
class IntentResult:
    contract: InfraContract
    validation: ValidationResult
    trace: IntentTrace


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


def _ask_clarification_tool() -> dict:
    return {
        "name": "ask_clarification",
        "description": (
            "Ask the user one focused clarifying question. Use this when the intent "
            "is genuinely ambiguous and the answer would materially change the contract. "
            "Ask at most one question per turn."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The clarifying question to ask the user.",
                }
            },
            "required": ["question"],
        },
    }


def _emit_contract_tool() -> dict:
    """Build the emit_contract tool with schema derived from InfraContract."""
    raw_schema = InfraContract.model_json_schema()
    # Wrap in an object with a single 'contract' key so the model's output
    # is unambiguously the contract payload, not a flat merge with tool metadata.
    return {
        "name": "emit_contract",
        "description": (
            "Emit the final InfraContract. Call this once you have enough information "
            "to produce a complete, valid contract. Always set human_reviewed=false."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contract": raw_schema,
            },
            "required": ["contract"],
            "$defs": raw_schema.get("$defs", {}),
        },
    }


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class IntentAgent:
    """Parses natural language intent into a validated InfraContract.

    Parameters
    ----------
    client:
        An ``anthropic.AsyncAnthropic`` instance. Callers control lifecycle.
    model:
        Claude model to use. Defaults to claude-opus-4-6.
    max_clarifications:
        Maximum clarification rounds before emitting a best-effort contract.
    """

    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        model: str = DEFAULT_MODEL,
        max_clarifications: int = 3,
    ) -> None:
        self.client = client
        self.model = model
        self.max_clarifications = max_clarifications

        self._tools = [_ask_clarification_tool(), _emit_contract_tool()]

    async def parse(
        self,
        intent: str,
        clarification_callback: Callable[[str], Awaitable[str]] | None = None,
        conventions: "ConventionProfile | None" = None,
    ) -> IntentResult:
        """Convert a natural-language intent string into a validated InfraContract.

        Parameters
        ----------
        intent:
            The user's natural-language description of their infrastructure needs.
        clarification_callback:
            Async callable that receives a question string and returns the user's
            answer. If None, clarification questions are skipped and the agent
            emits a best-effort contract from the original intent alone.
        conventions:
            Optional org ConventionProfile. When provided, injects required-tag
            and naming-variable context so the agent captures the right metadata.
        """
        base_system = SYSTEM_PROMPT
        if conventions is not None:
            base_system += build_conventions_section(conventions)

        trace = IntentTrace(intent=intent)
        messages: list[dict] = [{"role": "user", "content": intent}]
        trace.turns.append(Turn(role="user", content=intent))

        clarifications_used = 0

        while True:
            system = base_system
            if clarification_callback is None:
                system += "\n\nIMPORTANT: Do not ask clarifying questions. Emit the best contract you can from the information given."
            else:
                system += CLARIFICATION_GUIDANCE

            response = await self.client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=self._tools,
                messages=messages,
            )

            # Record raw assistant turn
            trace.turns.append(Turn(role="assistant", content=response.content))

            # Add assistant response to message history
            messages.append({"role": "assistant", "content": response.content})

            # Process tool calls
            contract = await self._handle_response(
                response=response,
                messages=messages,
                trace=trace,
                clarification_callback=clarification_callback,
                clarifications_used=clarifications_used,
            )

            if contract is not None:
                validation = validate_contract(contract)
                return IntentResult(
                    contract=contract,
                    validation=validation,
                    trace=trace,
                )

            # Count clarification rounds
            clarifications_used += 1
            if clarifications_used >= self.max_clarifications:
                # Force emit on next turn
                messages.append({
                    "role": "user",
                    "content": (
                        "That's enough context. Please emit the contract now "
                        "with your best judgment for anything still unclear."
                    ),
                })
                trace.turns.append(Turn(
                    role="user",
                    content="[agent] max clarifications reached — forcing emit",
                ))

    async def _handle_response(
        self,
        response: anthropic.types.Message,
        messages: list[dict],
        trace: IntentTrace,
        clarification_callback: Callable[[str], Awaitable[str]] | None,
        clarifications_used: int,
    ) -> InfraContract | None:
        """Process tool calls in a response. Returns InfraContract if emitted, else None."""
        tool_results = []

        for block in response.content:
            if block.type != "tool_use":
                continue

            if block.name == "emit_contract":
                return self._parse_contract(block.input)

            if block.name == "ask_clarification":
                question = block.input["question"]
                trace.questions_asked.append(question)

                if clarification_callback is not None:
                    answer = await clarification_callback(question)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": answer,
                    })
                    trace.turns.append(Turn(role="user", content=f"Q: {question}\nA: {answer}"))
                else:
                    # No callback — tell the model to proceed without the answer
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "[No answer available. Use your best judgment and emit the contract.]",
                    })

        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        return None

    def _parse_contract(self, tool_input: dict) -> InfraContract:
        """Parse and normalise the contract emitted by the model."""
        contract_data = tool_input.get("contract", tool_input)

        # Normalise fields the model shouldn't control
        contract_data["schema_version"] = SchemaVersion.V1.value
        contract_data["id"] = str(uuid.uuid4())
        contract_data["human_reviewed"] = False

        return InfraContract.model_validate(contract_data)
