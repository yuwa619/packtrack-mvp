from __future__ import annotations

from ..constants import DocumentStatus

PIPELINE_STATES = {
    DocumentStatus.QUEUED,
    DocumentStatus.PREPROCESSING,
    DocumentStatus.EXTRACTING,
    DocumentStatus.CLASSIFYING,
    DocumentStatus.REPORTING,
    DocumentStatus.COMPLETE,
    DocumentStatus.FAILED,
}

VALID_TRANSITIONS = {
    DocumentStatus.QUEUED: {DocumentStatus.PREPROCESSING, DocumentStatus.FAILED},
    DocumentStatus.PREPROCESSING: {DocumentStatus.EXTRACTING, DocumentStatus.FAILED},
    DocumentStatus.EXTRACTING: {DocumentStatus.CLASSIFYING, DocumentStatus.FAILED},
    DocumentStatus.CLASSIFYING: {DocumentStatus.REPORTING, DocumentStatus.FAILED},
    DocumentStatus.REPORTING: {DocumentStatus.COMPLETE, DocumentStatus.FAILED},
    DocumentStatus.COMPLETE: set(),
    DocumentStatus.FAILED: set(),
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
