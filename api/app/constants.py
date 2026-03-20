"""Centralised status and type constants for the PackTrack backend.

Import from here instead of using string literals. This prevents typos,
enables IDE autocompletion, and makes status changes a single-place edit.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Document / pipeline statuses
# ---------------------------------------------------------------------------
class DocumentStatus:
    UPLOADED = "uploaded"
    QUEUED = "QUEUED"
    PREPROCESSING = "PREPROCESSING"
    EXTRACTING = "EXTRACTING"
    CLASSIFYING = "CLASSIFYING"
    REPORTING = "REPORTING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# Review task statuses and types
# ---------------------------------------------------------------------------
class ReviewStatus:
    PENDING = "pending"
    RESOLVED = "resolved"


class ReviewTaskType:
    EXTRACTION_REVIEW = "EXTRACTION_REVIEW"
    CLASSIFICATION_REVIEW = "CLASSIFICATION_REVIEW"


# ---------------------------------------------------------------------------
# Report statuses
# ---------------------------------------------------------------------------
class ReportStatus:
    PENDING = "pending"
    GENERATED = "generated"


# ---------------------------------------------------------------------------
# Batch statuses
# ---------------------------------------------------------------------------
class BatchStatus:
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# Material source labels
# ---------------------------------------------------------------------------
class MaterialSource:
    AUTO = "auto"
    REVIEW = "review"
