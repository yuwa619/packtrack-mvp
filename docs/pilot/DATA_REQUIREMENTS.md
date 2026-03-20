# Data Requirements

## Accepted Inputs
- File types:
  - `application/pdf`
  - `image/jpeg`
  - `image/png`
  - `image/tiff`
  - `.zip` batch archives uploaded via the ZIP batch flow
- Default max upload size: 50 MB (`MAX_UPLOAD_SIZE_BYTES`, default `52428800`).
- ZIP batch safety limits:
  - max files per ZIP: `ZIP_MAX_FILE_COUNT` (default `200`)
  - max total uncompressed size: `ZIP_MAX_TOTAL_UNCOMPRESSED_BYTES` (default `262144000`)
  - per-file size inside ZIP: same `MAX_UPLOAD_SIZE_BYTES`

## Upload Methods
- Pilot UI upload flow (frontend).
- API presigned upload flow:
  1. `POST /api/v1/documents/upload/presign`
  2. Upload to returned `upload_url` (HTTP PUT)
  3. `POST /api/v1/documents/upload/finalise`
- Batch upload flow:
  1. `POST /api/v1/batches`
  2. Upload each file to its returned `upload_url`
  3. `POST /api/v1/batches/{batch_id}/finalise`
- ZIP batch flow:
  1. `POST /api/v1/batches/upload-zip/presign`
  2. Upload the ZIP to its returned `upload_url`
  3. `POST /api/v1/batches/{batch_id}/finalise-zip`

## ZIP Batch Constraints
- Accepted file types inside ZIP:
  - `.pdf`
  - `.png`
  - `.jpg`
  - `.jpeg`
  - `.tiff`
- Rejected:
  - directories
  - hidden files
  - executables/scripts
  - nested ZIP files
  - path traversal entries such as `../file.pdf`
- Rejected files are skipped and reported back in the ZIP finalise response.

## Recommended Invoice Content
For best extraction results, invoices should include:
- Supplier name and/or supplier reference.
- Invoice reference and invoice date.
- Product line descriptions.
- Packaging hints (for example: household/non-household, primary/secondary, material terms).
- Weight values with units (`g` or `kg`).

## Redaction Guidance
Before upload, remove or mask unnecessary sensitive data where possible:
- Bank account details and payment card details.
- Personal phone numbers and personal email addresses.
- Personal home addresses not required for packaging reporting.

Do not redact fields needed for extraction/classification (supplier, invoice reference/date, product/packaging text, weights).

## File Naming Guidance
Use predictable names to help traceability:
- `YYYYMMDD_supplier_invoiceRef.pdf`
- Example: `20260305_acme_INV001234.pdf`
- For ZIP batches, keep inner file names unique where possible.

## Pilot Volume Guidance
- Start with 20-50 documents in week 1.
- Then process in manageable batches (10-20 documents per batch).
- Avoid very large one-off batches until review-task throughput is stable.
