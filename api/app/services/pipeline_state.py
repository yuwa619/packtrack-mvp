from __future__ import annotations

PIPELINE_STATES = {
    "QUEUED",
    "PREPROCESSING",
    "EXTRACTING",
    "CLASSIFYING",
    "REPORTING",
    "COMPLETE",
    "FAILED",
}

VALID_TRANSITIONS = {
    "QUEUED": {"PREPROCESSING", "FAILED"},
    "PREPROCESSING": {"EXTRACTING", "FAILED"},
    "EXTRACTING": {"CLASSIFYING", "FAILED"},
    "CLASSIFYING": {"REPORTING", "FAILED"},
    "REPORTING": {"COMPLETE", "FAILED"},
    "COMPLETE": set(),
    "FAILED": set(),
}


class InvalidTransitionError(ValueError):
    pass


def validate_transition(current_state: str, next_state: str) -> None:
    if current_state not in PIPELINE_STATES:
        raise InvalidTransitionError(f"Unknown current state: {current_state}")
    if next_state not in PIPELINE_STATES:
        raise InvalidTransitionError(f"Unknown next state: {next_state}")
    if next_state not in VALID_TRANSITIONS[current_state]:
        raise InvalidTransitionError(f"Invalid transition: {current_state} -> {next_state}")
