# Support Process

## How To Report An Issue
Include the following in every ticket/message:
- `document_id`
- `job_id` (if available)
- `report_id` (if available)
- tenant ID used (`X-Tenant-Id`)
- UTC timestamp of failure
- endpoint called and HTTP status/response body

## Evidence To Provide
- API request/response snippet (redacted where needed).
- Screenshot of UI state (jobs/review/report page).
- Relevant container logs:
  - `docker compose --env-file .env logs -n 200 api`
  - `docker compose --env-file .env logs -n 200 worker`

## Issue Classification
- Bug: expected behaviour does not occur (error, incorrect output, crash).
- Feature request: new capability not currently implemented.
- Mapping/taxonomy question: code selection or ambiguity requiring SME decision.

## Escalation Path
1. Raise in primary pilot channel (Slack/Teams) with identifiers.
2. If unresolved, escalate to pilot coordinator.
3. Critical blocker (pilot cannot continue): escalate to technical lead immediately.

## Turnaround Expectations
- Critical blocker: first response within 2 business hours.
- High impact (workaround exists): first response within 1 business day.
- Normal bug/request: triage within 2 business days.
- Taxonomy/mapping queries: initial response within 1 business day; resolution depends on SME decision cadence.
