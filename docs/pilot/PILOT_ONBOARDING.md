# Pilot Onboarding

## Who This Pilot Is For
- UK packaging compliance teams preparing DEFRA-aligned packaging data.
- Operational staff who upload invoices and check outputs.
- Subject matter experts (SMEs) who review low-confidence extraction/classification.
- One pilot coordinator per organisation to manage access and support escalations.

## Participant Roles
- Uploader: submits invoice files and starts pipeline runs.
- Reviewer: resolves review tasks and confirms corrected outputs.
- Pilot coordinator: tracks progress, raises support requests, signs off results.
- Admin user (optional): can access `GET /api/v1/metrics/summary` with `X-User-Role: admin`.

## 30-Day Pilot Timeline
- Days 1-5: onboarding, access setup, first 20-50 documents.
- Days 6-20: regular processing, review-task handling, weekly issue triage.
- Days 21-30: final batch run, CSV verification, lessons learned, go/no-go recommendation.

## Pilot Success Criteria
- Documents can be uploaded, processed, reviewed, and exported without manual database intervention.
- DEFRA CSV header/order is correct for all pilot report exports.
- Review process is usable by SMEs (corrections can be saved and rerun successfully).
- No cross-tenant access incidents observed.
- Support issues are triaged and resolved within agreed response times.

## Access Model And Getting Started
- Access is tenant-scoped.
- Every request uses:
  - `X-User-Id`
  - `X-Tenant-Id`
- Start sequence:
  1. Request user and tenant allocation from pilot coordinator.
  2. Confirm API health (`/api/v1/health`, `/api/v1/health/ready`).
  3. Submit first upload and run pipeline for a known sample invoice.
  4. Confirm report download from `/api/v1/reports/{report_id}/download`.

## Support Channels And Response Times
- Primary channel: pilot delivery channel (Slack or Teams) agreed at kickoff.
  - Target acknowledgement: within 4 business hours.
- Secondary channel: pilot support email group agreed at kickoff.
  - Target acknowledgement: within 1 business day.
- Critical blockers (cannot upload/process/export):
  - Escalate immediately to pilot coordinator and technical lead.
  - Target first response: within 2 business hours (business days).
