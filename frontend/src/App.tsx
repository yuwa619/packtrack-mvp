import { CSSProperties, FormEvent, useEffect, useMemo, useState } from "react";
import {
  apiRequest,
  buildHeaders,
  extractErrorMessage,
  fetchImageBlob,
  isRecord,
  resolveApiPath,
} from "./api";
import type { AuthContext } from "./api";
import { JobsScreen } from "./screens/JobsScreen";
import { ReportsScreen } from "./screens/ReportsScreen";

const DEFAULT_USER_ID = import.meta.env.VITE_DEMO_USER_ID ?? "demo-user";
const DEFAULT_TENANT_ID = import.meta.env.VITE_DEMO_TENANT_ID ?? "123456";

type AppScreen = "upload" | "jobs" | "review" | "review-detail" | "reports";
type ReviewQueueMode = "current" | "all";
type UploadMode = "multiple" | "zip";

type JobRow = {
  job_id: string;
  document_id: string;
  filename: string;
  status: string;
  current_stage: string;
  created_at: string | null;
  report?: {
    report_id: string;
    status: string;
  } | null;
};

type ReviewTaskRow = {
  task_id: string;
  document_id: string;
  task_type: string;
  status: string;
  notes: string | null;
  created_at: string | null;
  filename: string;
};

type ReviewPage = {
  page_number: number;
  image_endpoint: string;
  ocr_text: string | null;
};

type ExtractedField = {
  field_name: string;
  raw_value: string;
  normalized_value: string | null;
  confidence: number | null;
  page_number: number;
};

type ClassificationCandidate = {
  category: string;
  code: string;
  score: number | null;
  reason: string | null;
};

type ReviewMaterial = {
  material_id: string;
  material_key: string | null;
  taxonomy_category: string | null;
  taxonomy_code: string | null;
  material: string;
  subtype: string | null;
  weight_value: number | null;
  weight_unit: string | null;
  confidence: number | null;
  source: string | null;
  created_at: string | null;
};

type MaterialOption = {
  key: string;
  materialKey: string;
  label: string;
  material: string;
  subtype: string | null;
  taxonomyCategory: string;
  taxonomyCode: string;
};

type MaterialEditState = {
  selected: boolean;
  weightValue: string;
  weightUnit: string;
};

/**
 * Fallback material options used while the backend endpoint is loading or
 * unreachable.  The canonical source is GET /review/material-options.
 */
const FALLBACK_MATERIAL_OPTIONS: MaterialOption[] = [
  { key: "plastic-rigid", materialKey: "Plastic Rigid", label: "Plastic Rigid", material: "Plastic", subtype: "Rigid", taxonomyCategory: "Material", taxonomyCode: "Plastic" },
  { key: "plastic-flexible", materialKey: "Plastic Flexible", label: "Plastic Flexible", material: "Plastic", subtype: "Flexible", taxonomyCategory: "Material", taxonomyCode: "Plastic" },
  { key: "paper-cardboard", materialKey: "Paper/Cardboard", label: "Paper/Cardboard", material: "Paper or cardboard", subtype: null, taxonomyCategory: "Material", taxonomyCode: "Paper or cardboard" },
  { key: "aluminium", materialKey: "Aluminium", label: "Aluminium", material: "Aluminium", subtype: null, taxonomyCategory: "Material", taxonomyCode: "Aluminium" },
  { key: "steel", materialKey: "Steel", label: "Steel", material: "Steel", subtype: null, taxonomyCategory: "Material", taxonomyCode: "Steel" },
  { key: "wood", materialKey: "Wood", label: "Wood", material: "Wood", subtype: null, taxonomyCategory: "Material", taxonomyCode: "Wood" },
  { key: "glass", materialKey: "Glass", label: "Glass", material: "Glass", subtype: null, taxonomyCategory: "Material", taxonomyCode: "Glass" },
  { key: "other", materialKey: "Other", label: "Other", material: "Other", subtype: null, taxonomyCategory: "Material", taxonomyCode: "Other" },
];

const DEFAULT_MATERIAL_EDIT: MaterialEditState = {
  selected: false,
  weightValue: "",
  weightUnit: "kg",
};

function materialLookupKey(material: string, subtype: string | null): string {
  return `${material.toLowerCase()}::${(subtype ?? "").toLowerCase()}`;
}

type ReviewDetail = {
  task: {
    task_id: string;
    task_type: string;
    status: string;
    notes: string | null;
    document_id: string;
  };
  document: {
    document_id: string;
    filename: string;
    status: string;
  };
  pages: ReviewPage[];
  extracted_fields: ExtractedField[];
  classification: {
    taxonomy_category: string | null;
    taxonomy_code: string | null;
    confidence: number | null;
    candidates: unknown[];
    rule_reason: string | null;
  };
  materials?: ReviewMaterial[];
};

type ReportRow = {
  report_id: string;
  document_id: string | null;
  batch_id: string | null;
  filename: string;
  status: string;
  row_count: number;
  warning_count: number;
  validation_warnings: {
    missing_fields_by_row: Array<{
      row_index: number;
      material_key: string;
      missing_fields: string[];
      document_id?: string;
      filename?: string;
    }>;
    overall: string[];
    per_document?: Array<{
      document_id: string;
      filename: string;
      warning_count: number;
      missing_weight_count: number;
      overall: string[];
    }>;
    total_warning_count?: number;
  } | null;
  submission_period: string | null;
  created_at: string | null;
  download_endpoint: string;
  report_scope: "document" | "batch";
  document_count: number | null;
};

type PresignResponse = {
  upload_id: string;
  upload_url: string;
  bucket: string;
  object_key: string;
  expires_in: number;
};

type FinaliseResponse = {
  upload_id: string;
  document_id: string;
  job_id: string;
  status: string;
};

type BatchCreateResponse = {
  batch_id: string;
  status: string;
  uploads: Array<{
    upload_id: string;
    filename: string;
    upload_url: string;
    bucket: string;
    object_key: string;
    expires_in: number;
  }>;
};

type ZipBatchPresignResponse = {
  batch_id: string;
  upload_id: string;
  upload_url: string;
};

type ZipBatchFinaliseResponse = {
  batch_id: string;
  accepted_count: number;
  rejected_count: number;
  accepted_files: Array<{
    filename: string;
    document_id: string;
  }>;
  rejected_files: Array<{
    filename: string;
    reason: string;
  }>;
};

type BatchFinaliseResponse = {
  batch_id: string;
  status: string;
  document_ids: string[];
  job_ids: string[];
};

type BatchRunResult = {
  document_id: string;
  job_id: string;
  status: string;
  report_id: string | null;
  review_task_count: number;
  error?: string;
};

type BatchRunResponse = {
  batch_id: string;
  status: string;
  job_ids: string[];
  results: BatchRunResult[];
};

type BatchReportResponse = {
  batch_id: string;
  report_id: string;
  status: string;
  row_count: number;
  warning_count: number;
  validation_warnings: ReportRow["validation_warnings"];
  download_endpoint: string;
};

type PipelineRunResponse = {
  document_id: string;
  status: string;
  report_id: string;
  review_task_count: number;
};

type ApiError = { detail: string } | { message: string } | { error: string };

type ReviewCorrectionResponse = {
  task_id: string;
  status: string;
  detail?: string;
  rerun: {
    document_id: string;
    status: string;
    report_id: string;
    classification_reran: boolean;
  };
};

type BatchFileProgress = {
  filename: string;
  mimeType: string;
  sizeBytes: number;
  uploadId: string | null;
  documentId: string | null;
  jobId: string | null;
  reportId: string | null;
  status:
    | "selected"
    | "presigned"
    | "uploaded"
    | "queued"
    | "running"
    | "complete"
    | "failed";
  error: string | null;
};

// API client functions imported from ./api

function formatTimestamp(value: string | null): string {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function shortId(value: string): string {
  return value.slice(0, 8);
}

function parseCandidates(rawCandidates: unknown[]): ClassificationCandidate[] {
  const normalized: ClassificationCandidate[] = [];
  for (const item of rawCandidates) {
    if (!isRecord(item)) {
      continue;
    }
    const category = typeof item.category === "string" ? item.category : null;
    const code = typeof item.code === "string" ? item.code : null;
    if (!category || !code) {
      continue;
    }
    const score = typeof item.score === "number" ? item.score : null;
    const reason = typeof item.reason === "string" ? item.reason : null;
    normalized.push({
      category,
      code,
      score,
      reason,
    });
  }
  return normalized.slice(0, 3);
}

export function App() {
  const [screen, setScreen] = useState<AppScreen>("upload");
  const [auth, setAuth] = useState<AuthContext>({
    userId: DEFAULT_USER_ID,
    tenantId: DEFAULT_TENANT_ID,
  });
  const [currentDocumentId, setCurrentDocumentId] = useState<string | null>(null);
  const [currentBatchId, setCurrentBatchId] = useState<string | null>(null);
  const [reviewQueueMode, setReviewQueueMode] = useState<ReviewQueueMode>("current");

  const [jobs, setJobs] = useState<JobRow[]>([]);
  const [reviewTasks, setReviewTasks] = useState<ReviewTaskRow[]>([]);
  const [reports, setReports] = useState<ReportRow[]>([]);
  const [expandedWarnings, setExpandedWarnings] = useState<Record<string, boolean>>({});

  const [uploadMode, setUploadMode] = useState<UploadMode>("multiple");
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [selectedZipFile, setSelectedZipFile] = useState<File | null>(null);
  const [batchProgress, setBatchProgress] = useState<BatchFileProgress[]>([]);
  const [latestBatchReport, setLatestBatchReport] = useState<BatchReportResponse | null>(null);
  const [zipFinaliseSummary, setZipFinaliseSummary] = useState<ZipBatchFinaliseResponse | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [reviewDetail, setReviewDetail] = useState<ReviewDetail | null>(null);
  const [selectedPageNo, setSelectedPageNo] = useState<number | null>(null);
  const [imageUrl, setImageUrl] = useState<string | null>(null);
  const [fieldEdits, setFieldEdits] = useState<Record<string, string>>({});
  const [classificationSelection, setClassificationSelection] = useState<string>("");
  const [materialEdits, setMaterialEdits] = useState<Record<string, MaterialEditState>>({});
  const [materialOptions, setMaterialOptions] = useState<MaterialOption[]>(FALLBACK_MATERIAL_OPTIONS);

  const [loading, setLoading] = useState<boolean>(false);
  const [message, setMessage] = useState<string>("Ready.");

  const candidateOptions = useMemo(() => {
    return reviewDetail ? parseCandidates(reviewDetail.classification.candidates) : [];
  }, [reviewDetail]);

  useEffect(() => {
    void refreshDashboard();
    // Fetch canonical material options from the backend.
    apiRequest<MaterialOption[]>("/review/material-options", auth)
      .then((options) => {
        if (Array.isArray(options) && options.length > 0) {
          setMaterialOptions(options);
        }
      })
      .catch(() => {
        // Keep fallback options on error — non-critical.
      });
  }, []);

  useEffect(() => {
    void loadReviewTasks();
  }, [reviewQueueMode, currentDocumentId, auth]);

  useEffect(() => {
    if (!reviewDetail) {
      return;
    }

    const nextEdits: Record<string, string> = {};
    for (const field of reviewDetail.extracted_fields) {
      nextEdits[field.field_name] = field.normalized_value ?? field.raw_value;
    }
    setFieldEdits(nextEdits);

    const firstPage = reviewDetail.pages[0]?.page_number ?? null;
    setSelectedPageNo(firstPage);

    const firstCandidate = parseCandidates(reviewDetail.classification.candidates)[0];
    if (firstCandidate) {
      setClassificationSelection(`${firstCandidate.category}::${firstCandidate.code}`);
    } else {
      setClassificationSelection("");
    }

    const nextMaterialEdits: Record<string, MaterialEditState> = {};
    for (const option of materialOptions) {
      nextMaterialEdits[option.key] = { ...DEFAULT_MATERIAL_EDIT };
    }
    const optionByMaterial = new Map<string, MaterialOption>();
    for (const option of materialOptions) {
      optionByMaterial.set(materialLookupKey(option.material, option.subtype), option);
    }
    for (const row of reviewDetail.materials ?? []) {
      const optionByKey = materialOptions.find(
        (item) => (row.material_key ?? "").toLowerCase() === item.materialKey.toLowerCase(),
      );
      const option = optionByKey ?? optionByMaterial.get(materialLookupKey(row.material, row.subtype));
      if (!option) {
        continue;
      }
      nextMaterialEdits[option.key] = {
        selected: true,
        weightValue: row.weight_value === null ? "" : String(row.weight_value),
        weightUnit: row.weight_unit || "kg",
      };
    }
    setMaterialEdits(nextMaterialEdits);
  }, [reviewDetail]);

  useEffect(() => {
    if (!reviewDetail || selectedPageNo === null) {
      setImageUrl(null);
      return;
    }

    const page = reviewDetail.pages.find((entry) => entry.page_number === selectedPageNo);
    if (!page) {
      setImageUrl(null);
      return;
    }

    const controller = new AbortController();
    let revokedUrl: string | null = null;
    void fetchImageBlob(page.image_endpoint, auth, controller.signal)
      .then((url) => {
        if (controller.signal.aborted) {
          URL.revokeObjectURL(url);
          return;
        }
        revokedUrl = url;
        setImageUrl(url);
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setImageUrl(null);
        }
      });

    return () => {
      controller.abort();
      if (revokedUrl) {
        URL.revokeObjectURL(revokedUrl);
      }
    };
  }, [reviewDetail, selectedPageNo, auth]);

  async function refreshDashboard(): Promise<void> {
    await Promise.all([loadJobs(), loadReviewTasks(), loadReports()]);
  }

  async function loadJobs(): Promise<void> {
    const payload = await apiRequest<{ jobs: JobRow[] }>("/jobs", auth);
    setJobs(payload.jobs);
  }

  async function loadReviewTasks(): Promise<void> {
    const params = new URLSearchParams({ status: "pending" });
    if (reviewQueueMode === "current" && currentDocumentId) {
      params.set("document_id", currentDocumentId);
    }
    const payload = await apiRequest<{ tasks: ReviewTaskRow[] }>(
      `/review/tasks?${params.toString()}`,
      auth,
    );
    setReviewTasks(payload.tasks);
  }

  async function loadReports(): Promise<void> {
    const payload = await apiRequest<{ reports: ReportRow[] }>("/reports", auth);
    setReports(payload.reports);
  }

  async function openTask(taskId: string): Promise<void> {
    setSelectedTaskId(taskId);
    setScreen("review-detail");
    const payload = await apiRequest<ReviewDetail>(`/review/tasks/${taskId}`, auth);
    setReviewDetail(payload);
  }

  async function runPipeline(documentId: string): Promise<PipelineRunResponse | ApiError> {
    try {
      return await apiRequest<PipelineRunResponse>(`/pipeline/run/${documentId}`, auth, {
        method: "POST",
      });
    } catch (error) {
      return { detail: error instanceof Error ? error.message : "Pipeline run failed" };
    }
  }

  function updateBatchProgress(
    matcher: (item: BatchFileProgress) => boolean,
    patch: Partial<BatchFileProgress>,
  ): void {
    setBatchProgress((prev) =>
      prev.map((item) => (matcher(item) ? { ...item, ...patch } : item)),
    );
  }

  async function createCombinedReport(batchId: string): Promise<BatchReportResponse> {
    return await apiRequest<BatchReportResponse>(`/batches/${batchId}/reports/export`, auth, {
      method: "POST",
    });
  }

  async function uploadFileToPresignedUrl(file: File, uploadUrl: string, contentType: string): Promise<void> {
    if (!uploadUrl.startsWith("http://") && !uploadUrl.startsWith("https://")) {
      throw new Error("Presigned URL is not HTTP. Run through Docker MinIO for browser uploads.");
    }

    const uploadResponse = await fetch(uploadUrl, {
      method: "PUT",
      headers: {
        "Content-Type": contentType,
      },
      body: file,
    });
    if (!uploadResponse.ok) {
      throw new Error(`${file.name}: upload failed (${uploadResponse.status}).`);
    }
  }

  async function handleDownloadReport(report: ReportRow): Promise<void> {
    setLoading(true);
    setMessage(`Downloading report ${shortId(report.report_id)}...`);
    try {
      const response = await fetch(resolveApiPath(report.download_endpoint), {
        method: "GET",
        headers: buildHeaders(auth, false),
      });

      if (!response.ok) {
        const contentType = response.headers.get("content-type") ?? "";
        const payload = contentType.includes("application/json")
          ? await response.json()
          : await response.text();
        throw new Error(
          extractErrorMessage(payload) || `Download failed (${response.status})`,
        );
      }

      const blob = await response.blob();
      const disposition = response.headers.get("content-disposition") ?? "";
      const match = disposition.match(/filename=\"?([^\";]+)\"?/i);
      const filename = match?.[1] ?? `${report.report_id}.csv`;
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(objectUrl);
      setMessage(`Downloaded report ${shortId(report.report_id)}.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Download failed.");
    } finally {
      setLoading(false);
    }
  }

  async function handleUpload(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (selectedFiles.length === 0) {
      setMessage("Select at least one file first.");
      return;
    }

    setLoading(true);
    setLatestBatchReport(null);
    setZipFinaliseSummary(null);
    setBatchProgress(
      selectedFiles.map((file) => ({
        filename: file.name,
        mimeType: file.type || "application/octet-stream",
        sizeBytes: file.size,
        uploadId: null,
        documentId: null,
        jobId: null,
        reportId: null,
        status: "selected",
        error: null,
      })),
    );
    setMessage(`Creating batch upload URLs for ${selectedFiles.length} file(s)...`);

    try {
      const batch = await apiRequest<BatchCreateResponse>("/batches", auth, {
        method: "POST",
        body: JSON.stringify({
          name: `Batch ${new Date().toISOString()}`,
          files: selectedFiles.map((file) => ({
            filename: file.name,
            mime_type: file.type || "application/octet-stream",
            size_bytes: file.size,
          })),
        }),
      });
      setCurrentBatchId(batch.batch_id);
      setBatchProgress((prev) =>
        prev.map((item, index) => {
          const upload = batch.uploads[index];
          return upload
            ? { ...item, uploadId: upload.upload_id, status: "presigned" }
            : item;
        }),
      );

      for (const [index, file] of selectedFiles.entries()) {
        const upload = batch.uploads[index];
        if (!upload) {
          throw new Error(`Missing upload session for ${file.name}.`);
        }

        setMessage(`Uploading ${file.name}...`);
        try {
          await uploadFileToPresignedUrl(
            file,
            upload.upload_url,
            file.type || "application/octet-stream",
          );
        } catch (error) {
          updateBatchProgress(
            (item) => item.uploadId === upload.upload_id,
            {
              status: "failed",
              error: error instanceof Error ? error.message : "Upload failed.",
            },
          );
          throw error;
        }
        updateBatchProgress((item) => item.uploadId === upload.upload_id, { status: "uploaded" });
      }

      setMessage("Finalising batch and creating document jobs...");
      const finalise = await apiRequest<BatchFinaliseResponse>(
        `/batches/${batch.batch_id}/finalise`,
        auth,
        {
          method: "POST",
          body: JSON.stringify({
            upload_ids: batch.uploads.map((entry) => entry.upload_id),
          }),
        },
      );

      setBatchProgress((prev) =>
        prev.map((item, index) => ({
          ...item,
          documentId: finalise.document_ids[index] ?? item.documentId,
          jobId: finalise.job_ids[index] ?? item.jobId,
          status: "queued",
        })),
      );
      if (finalise.document_ids.length === 1) {
        setCurrentDocumentId(finalise.document_ids[0]);
        setReviewQueueMode("current");
      } else {
        setCurrentDocumentId(null);
        setReviewQueueMode("all");
      }

      setBatchProgress((prev) => prev.map((item) => ({ ...item, status: "running" })));
      setMessage("Running pipeline for batch...");
      const runResponse = await apiRequest<BatchRunResponse>(`/batches/${batch.batch_id}/run`, auth, {
        method: "POST",
      });
      setBatchProgress((prev) =>
        prev.map((item) => {
          const match = runResponse.results.find((entry) => entry.document_id === item.documentId);
          if (!match) {
            return item;
          }
          return {
            ...item,
            jobId: match.job_id,
            reportId: match.report_id,
            status: match.status === "COMPLETE" ? "complete" : "failed",
            error: match.error ?? null,
          };
        }),
      );

      setMessage("Creating combined DEFRA report...");
      const combinedReport = await createCombinedReport(batch.batch_id);
      setLatestBatchReport(combinedReport);
      await refreshDashboard();
      setScreen("reports");
      setSelectedFiles([]);
      setMessage(
        `Batch complete. Combined report ${shortId(combinedReport.report_id)} has ${combinedReport.row_count} row(s).`,
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Upload failed.");
    } finally {
      setLoading(false);
    }
  }

  async function handleZipUpload(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (!selectedZipFile) {
      setMessage("Select a ZIP file first.");
      return;
    }

    setLoading(true);
    setLatestBatchReport(null);
    setZipFinaliseSummary(null);
    setBatchProgress([]);
    setMessage(`Creating ZIP batch upload for ${selectedZipFile.name}...`);

    try {
      const presign = await apiRequest<ZipBatchPresignResponse>("/batches/upload-zip/presign", auth, {
        method: "POST",
        body: JSON.stringify({
          filename: selectedZipFile.name,
          mime_type: selectedZipFile.type || "application/zip",
          size_bytes: selectedZipFile.size,
          name: `ZIP Batch ${new Date().toISOString()}`,
        }),
      });

      setCurrentBatchId(presign.batch_id);
      setMessage(`Uploading ZIP ${selectedZipFile.name}...`);
      await uploadFileToPresignedUrl(
        selectedZipFile,
        presign.upload_url,
        selectedZipFile.type || "application/zip",
      );

      setMessage("Finalising ZIP and extracting supported invoices...");
      const finalise = await apiRequest<ZipBatchFinaliseResponse>(
        `/batches/${presign.batch_id}/finalise-zip`,
        auth,
        {
          method: "POST",
          body: JSON.stringify({ upload_id: presign.upload_id }),
        },
      );

      setZipFinaliseSummary(finalise);
      setBatchProgress(
        finalise.accepted_files.map((entry) => ({
          filename: entry.filename,
          mimeType: "application/octet-stream",
          sizeBytes: 0,
          uploadId: presign.upload_id,
          documentId: entry.document_id,
          jobId: null,
          reportId: null,
          status: "queued",
          error: null,
        })),
      );

      if (finalise.accepted_files.length === 0) {
        setCurrentDocumentId(null);
        setReviewQueueMode("all");
        await refreshDashboard();
        setMessage(
          `ZIP finalised with no accepted documents. Rejected: ${finalise.rejected_count}.`,
        );
        return;
      }

      if (finalise.accepted_files.length === 1) {
        setCurrentDocumentId(finalise.accepted_files[0].document_id);
        setReviewQueueMode("current");
      } else {
        setCurrentDocumentId(null);
        setReviewQueueMode("all");
      }

      setBatchProgress((prev) => prev.map((item) => ({ ...item, status: "running" })));
      setMessage("Running pipeline for ZIP batch...");
      const runResponse = await apiRequest<BatchRunResponse>(`/batches/${presign.batch_id}/run`, auth, {
        method: "POST",
      });
      setBatchProgress((prev) =>
        prev.map((item) => {
          const match = runResponse.results.find((entry) => entry.document_id === item.documentId);
          if (!match) {
            return item;
          }
          return {
            ...item,
            jobId: match.job_id,
            reportId: match.report_id,
            status: match.status === "COMPLETE" ? "complete" : "failed",
            error: match.error ?? null,
          };
        }),
      );

      setMessage("Creating combined DEFRA report...");
      const combinedReport = await createCombinedReport(presign.batch_id);
      setLatestBatchReport(combinedReport);
      await refreshDashboard();
      setScreen("reports");
      setSelectedZipFile(null);
      setMessage(
        `ZIP batch complete. Accepted ${finalise.accepted_count}, rejected ${finalise.rejected_count}. Combined report ${shortId(combinedReport.report_id)} has ${combinedReport.row_count} row(s).`,
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "ZIP batch upload failed.");
    } finally {
      setLoading(false);
    }
  }

  async function handleCreateCombinedReport(): Promise<void> {
    if (!currentBatchId) {
      setMessage("Run a batch first.");
      return;
    }

    setLoading(true);
    setMessage("Creating combined DEFRA report...");
    try {
      const payload = await createCombinedReport(currentBatchId);
      setLatestBatchReport(payload);
      await loadReports();
      setScreen("reports");
      setMessage(`Combined report ${shortId(payload.report_id)} generated.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Combined report generation failed.");
    } finally {
      setLoading(false);
    }
  }

  async function handleRunJob(documentId: string): Promise<void> {
    setLoading(true);
    setCurrentDocumentId(documentId);
    setReviewQueueMode("current");
    setMessage(`Running pipeline for document ${shortId(documentId)}...`);
    try {
      const runResult = await runPipeline(documentId);
      if ("detail" in runResult) {
        setMessage(runResult.detail);
        return;
      }
      if ("message" in runResult) {
        setMessage(runResult.message);
        return;
      }
      if ("error" in runResult) {
        setMessage(runResult.error);
        return;
      }
      await refreshDashboard();
      setMessage(`Pipeline finished: ${runResult.status}, report ${shortId(runResult.report_id)}.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Job run failed.");
    } finally {
      setLoading(false);
    }
  }

  async function handleSubmitCorrections(): Promise<void> {
    if (!reviewDetail || !selectedTaskId) {
      return;
    }

    const extractedFields = reviewDetail.extracted_fields.map((field) => ({
      field_name: field.field_name,
      value: fieldEdits[field.field_name] ?? field.normalized_value ?? field.raw_value,
      page_number: field.page_number,
    }));

    let classificationChoice: { category: string; code: string } | null = null;
    if (classificationSelection) {
      const [category, code] = classificationSelection.split("::");
      if (category && code) {
        classificationChoice = { category, code };
      }
    }

    const materials = materialOptions.filter(
      (option) => materialEdits[option.key]?.selected,
    ).map((option) => {
      const edit = materialEdits[option.key] ?? DEFAULT_MATERIAL_EDIT;
      const parsedWeight = Number.parseFloat(edit.weightValue);
      return {
        material_key: option.materialKey,
        material: option.material,
        subtype: option.subtype,
        taxonomy_category: option.taxonomyCategory,
        taxonomy_code: option.taxonomyCode,
        weight_value: Number.isFinite(parsedWeight) ? parsedWeight : null,
        weight_unit: edit.weightUnit.trim() || null,
        source: "review",
      };
    });

    setLoading(true);
    setMessage("Saving corrections and rerunning downstream stages...");

    try {
      const payload = await apiRequest<ReviewCorrectionResponse>(
        `/review/tasks/${selectedTaskId}/corrections`,
        auth,
        {
          method: "POST",
          body: JSON.stringify({
            extracted_fields: extractedFields,
            classification_choice: classificationChoice,
            materials,
            reviewer: auth.userId,
          }),
        },
      );

      if (payload.status !== "resolved") {
        await refreshDashboard();
        setMessage(payload.detail ?? "Additional review required before report generation.");
        return;
      }

      await refreshDashboard();
      setReviewDetail(null);
      setSelectedTaskId(null);
      setScreen("review");
      setMessage(`Task resolved. New report ${shortId(payload.rerun.report_id)} generated.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Failed to save corrections.");
    } finally {
      setLoading(false);
    }
  }

  async function handleCompleteTask(): Promise<void> {
    if (!selectedTaskId) {
      return;
    }

    setLoading(true);
    setMessage("Marking review task complete and rerunning pipeline...");

    try {
      const payload = await apiRequest<ReviewCorrectionResponse>(`/review/tasks/${selectedTaskId}/complete`, auth, {
        method: "PATCH",
        body: JSON.stringify({ reviewer: auth.userId }),
      });

      await refreshDashboard();
      setReviewDetail(null);
      setSelectedTaskId(null);
      setScreen("review");
      setMessage(`Task completed. New report ${shortId(payload.rerun.report_id)} generated.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Failed to complete task.");
    } finally {
      setLoading(false);
    }
  }

  const appStyle: CSSProperties = {
    minHeight: "100vh",
    padding: "1.5rem",
    fontFamily: "'IBM Plex Sans', 'Avenir Next', sans-serif",
    background:
      "radial-gradient(circle at top left, #f7efe1 0%, #f0f6ec 36%, #e5efe8 100%)",
    color: "#14251f",
  };

  const cardStyle: CSSProperties = {
    border: "1px solid #b7cbc1",
    borderRadius: "14px",
    background: "rgba(255, 255, 255, 0.84)",
    padding: "1rem",
    boxShadow: "0 10px 26px rgba(20, 37, 31, 0.08)",
  };

  return (
    <main style={appStyle}>
      <header style={{ display: "flex", justifyContent: "space-between", gap: "1rem", flexWrap: "wrap" }}>
        <div>
          <h1 style={{ margin: "0 0 0.3rem 0" }}>PackTrack Review Pilot</h1>
          <p style={{ margin: 0, opacity: 0.8 }}>
            Local-first review workflow: upload, run jobs, review tasks, and download DEFRA reports.
          </p>
        </div>
        <div style={{ ...cardStyle, display: "grid", gap: "0.5rem", minWidth: "280px" }}>
          <label>
            User ID
            <input
              value={auth.userId}
              onChange={(event) => setAuth((prev) => ({ ...prev, userId: event.target.value }))}
              style={{ width: "100%" }}
            />
          </label>
          <label>
            Tenant ID
            <input
              value={auth.tenantId}
              onChange={(event) => setAuth((prev) => ({ ...prev, tenantId: event.target.value }))}
              style={{ width: "100%" }}
            />
          </label>
          <button type="button" onClick={() => void refreshDashboard()} disabled={loading}>
            Refresh Data
          </button>
        </div>
      </header>

      <nav style={{ marginTop: "1rem", display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
        <button type="button" onClick={() => setScreen("upload")}>Upload</button>
        <button type="button" onClick={() => setScreen("jobs")}>Jobs</button>
        <button type="button" onClick={() => setScreen("review")}>Review Queue</button>
        <button type="button" onClick={() => setScreen("reports")}>Reports</button>
      </nav>

      <p style={{ marginTop: "0.8rem", fontFamily: "'IBM Plex Mono', monospace" }}>{message}</p>

      {screen === "upload" && (
        <section style={{ ...cardStyle, marginTop: "1rem" }}>
          <h2 style={{ marginTop: 0 }}>Upload</h2>
          <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap", marginBottom: "1rem" }}>
            <label style={{ display: "flex", gap: "0.4rem", alignItems: "center" }}>
              <input
                type="radio"
                name="upload-mode"
                checked={uploadMode === "multiple"}
                onChange={() => {
                  setUploadMode("multiple");
                  setSelectedZipFile(null);
                  setBatchProgress([]);
                  setZipFinaliseSummary(null);
                }}
              />
              Multiple files
            </label>
            <label style={{ display: "flex", gap: "0.4rem", alignItems: "center" }}>
              <input
                type="radio"
                name="upload-mode"
                checked={uploadMode === "zip"}
                onChange={() => {
                  setUploadMode("zip");
                  setSelectedFiles([]);
                  setBatchProgress([]);
                  setZipFinaliseSummary(null);
                }}
              />
              ZIP batch
            </label>
          </div>

          {uploadMode === "multiple" ? (
            <form onSubmit={(event) => void handleUpload(event)} style={{ display: "grid", gap: "0.8rem" }}>
              <input
                type="file"
                multiple
                accept="application/pdf,image/png,image/jpeg,image/tiff"
                onChange={(event) => {
                  const nextFiles = Array.from(event.target.files ?? []);
                  setSelectedFiles(nextFiles);
                  setBatchProgress([]);
                  setLatestBatchReport(null);
                  setZipFinaliseSummary(null);
                }}
              />
              <div style={{ opacity: 0.8 }}>
                {selectedFiles.length > 0
                  ? `${selectedFiles.length} file(s) selected`
                  : "Select one or more invoices/receipts."}
              </div>
              <button type="submit" disabled={loading || selectedFiles.length === 0}>
                Upload + Run Pipeline (Batch)
              </button>
              {currentBatchId && (
                <button
                  type="button"
                  disabled={loading}
                  onClick={() => void handleCreateCombinedReport()}
                >
                  Create Combined Report
                </button>
              )}
            </form>
          ) : (
            <form onSubmit={(event) => void handleZipUpload(event)} style={{ display: "grid", gap: "0.8rem" }}>
              <input
                type="file"
                accept=".zip,application/zip,application/x-zip-compressed"
                onChange={(event) => {
                  const nextZip = event.target.files?.[0] ?? null;
                  setSelectedZipFile(nextZip);
                  setBatchProgress([]);
                  setLatestBatchReport(null);
                  setZipFinaliseSummary(null);
                }}
              />
              <div style={{ opacity: 0.8 }}>
                {selectedZipFile
                  ? `Selected ZIP: ${selectedZipFile.name}`
                  : "Select one ZIP containing receipts/invoices."}
              </div>
              <button type="submit" disabled={loading || !selectedZipFile}>
                Upload ZIP + Process Batch
              </button>
              {currentBatchId && (
                <button
                  type="button"
                  disabled={loading}
                  onClick={() => void handleCreateCombinedReport()}
                >
                  Export Combined Report
                </button>
              )}
            </form>
          )}

          <div style={{ marginTop: "1rem" }}>
            <h3 style={{ marginBottom: "0.4rem" }}>Batch progress</h3>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr>
                  <th align="left">File</th>
                  <th align="left">Status</th>
                  <th align="left">Document</th>
                  <th align="left">Job</th>
                  <th align="left">Report</th>
                  <th align="left">Notes</th>
                </tr>
              </thead>
              <tbody>
                {batchProgress.map((item) => (
                  <tr key={`${item.filename}-${item.uploadId ?? "pending"}`}>
                    <td>{item.filename}</td>
                    <td>{item.status}</td>
                    <td>{item.documentId ? shortId(item.documentId) : "-"}</td>
                    <td>{item.jobId ? shortId(item.jobId) : "-"}</td>
                    <td>{item.reportId ? shortId(item.reportId) : "-"}</td>
                    <td>{item.error ?? "-"}</td>
                  </tr>
                ))}
                {batchProgress.length === 0 && (
                  <tr>
                    <td colSpan={6}>No batch activity yet.</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          {zipFinaliseSummary && (
            <div style={{ marginTop: "1rem", display: "grid", gap: "0.8rem" }}>
              <div
                style={{
                  padding: "0.75rem",
                  border: "1px solid #c4d3cb",
                  borderRadius: "10px",
                }}
              >
                <strong>ZIP finalise summary:</strong> Accepted {zipFinaliseSummary.accepted_count} | Rejected{" "}
                {zipFinaliseSummary.rejected_count}
              </div>

              <div style={{ display: "grid", gap: "0.4rem" }}>
                <strong>Accepted files</strong>
                {zipFinaliseSummary.accepted_files.length > 0 ? (
                  <ul style={{ margin: 0, paddingLeft: "1.2rem" }}>
                    {zipFinaliseSummary.accepted_files.map((entry) => (
                      <li key={`${entry.filename}-${entry.document_id}`}>
                        {entry.filename} ({shortId(entry.document_id)})
                      </li>
                    ))}
                  </ul>
                ) : (
                  <div>No supported documents were extracted from this ZIP.</div>
                )}
              </div>

              <div style={{ display: "grid", gap: "0.4rem" }}>
                <strong>Rejected files</strong>
                {zipFinaliseSummary.rejected_files.length > 0 ? (
                  <ul style={{ margin: 0, paddingLeft: "1.2rem" }}>
                    {zipFinaliseSummary.rejected_files.map((entry) => (
                      <li key={`${entry.filename}-${entry.reason}`}>
                        {entry.filename}: {entry.reason}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <div>No rejected files.</div>
                )}
              </div>
            </div>
          )}

          {latestBatchReport && (
            <div style={{ marginTop: "1rem", padding: "0.75rem", border: "1px solid #c4d3cb", borderRadius: "10px" }}>
              <strong>Latest combined report:</strong> {shortId(latestBatchReport.report_id)}<br />
              Rows: {latestBatchReport.row_count} | Warnings: {latestBatchReport.warning_count}
            </div>
          )}
        </section>
      )}

      {screen === "jobs" && (
        <JobsScreen
          jobs={jobs}
          loading={loading}
          cardStyle={cardStyle}
          onRunJob={(documentId) => void handleRunJob(documentId)}
          formatTimestamp={formatTimestamp}
          shortId={shortId}
        />
      )}

      {screen === "review" && (
        <section style={{ ...cardStyle, marginTop: "1rem" }}>
          <h2 style={{ marginTop: 0 }}>Review Tasks Queue</h2>
          <div style={{ display: "flex", gap: "0.5rem", alignItems: "center", marginBottom: "0.75rem", flexWrap: "wrap" }}>
            <span>Show:</span>
            <button
              type="button"
              disabled={loading || !currentDocumentId}
              onClick={() => setReviewQueueMode("current")}
            >
              current document
            </button>
            <button
              type="button"
              disabled={loading}
              onClick={() => setReviewQueueMode("all")}
            >
              all pending
            </button>
            {reviewQueueMode === "current" && currentDocumentId && (
              <span style={{ opacity: 0.8 }}>
                Document {shortId(currentDocumentId)}
              </span>
            )}
          </div>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                <th align="left">Task</th>
                <th align="left">Type</th>
                <th align="left">File</th>
                <th align="left">Notes</th>
                <th align="left">Created</th>
                <th align="left">Action</th>
              </tr>
            </thead>
            <tbody>
              {reviewTasks.map((task) => (
                <tr key={task.task_id}>
                  <td>{shortId(task.task_id)}</td>
                  <td>{task.task_type}</td>
                  <td>{task.filename}</td>
                  <td>{task.notes ?? "-"}</td>
                  <td>{formatTimestamp(task.created_at)}</td>
                  <td>
                    <button type="button" onClick={() => void openTask(task.task_id)} disabled={loading}>
                      Review
                    </button>
                  </td>
                </tr>
              ))}
              {reviewTasks.length === 0 && (
                <tr>
                  <td colSpan={6}>No pending review tasks.</td>
                </tr>
              )}
            </tbody>
          </table>
        </section>
      )}

      {screen === "review-detail" && reviewDetail && (
        <section style={{ ...cardStyle, marginTop: "1rem", display: "grid", gap: "1rem" }}>
          <h2 style={{ marginTop: 0 }}>Review Detail</h2>
          <p style={{ margin: 0 }}>
            Task {shortId(reviewDetail.task.task_id)} ({reviewDetail.task.task_type}) for {reviewDetail.document.filename}
          </p>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1rem" }}>
            <div style={{ border: "1px solid #c4d3cb", borderRadius: "10px", padding: "0.75rem" }}>
              <div style={{ display: "flex", gap: "0.5rem", marginBottom: "0.5rem", flexWrap: "wrap" }}>
                {reviewDetail.pages.map((page) => (
                  <button
                    key={page.page_number}
                    type="button"
                    onClick={() => setSelectedPageNo(page.page_number)}
                    disabled={loading}
                  >
                    Page {page.page_number}
                  </button>
                ))}
              </div>
              {imageUrl ? (
                <img src={imageUrl} alt="Document page" style={{ width: "100%", borderRadius: "8px" }} />
              ) : (
                <p>Page image unavailable.</p>
              )}
            </div>

            <div style={{ border: "1px solid #c4d3cb", borderRadius: "10px", padding: "0.75rem" }}>
              <h3 style={{ marginTop: 0 }}>Extracted Fields</h3>
              <div style={{ display: "grid", gap: "0.4rem" }}>
                {reviewDetail.extracted_fields.map((field) => (
                  <label key={field.field_name}>
                    {field.field_name}
                    <input
                      value={fieldEdits[field.field_name] ?? ""}
                      onChange={(event) =>
                        setFieldEdits((prev) => ({
                          ...prev,
                          [field.field_name]: event.target.value,
                        }))
                      }
                      style={{ width: "100%" }}
                    />
                  </label>
                ))}
              </div>

              <h3>Classification Chooser (Top-3)</h3>
              <div style={{ display: "grid", gap: "0.5rem" }}>
                {candidateOptions.length > 0 ? (
                  candidateOptions.map((candidate) => {
                    const key = `${candidate.category}::${candidate.code}`;
                    return (
                      <label key={key}>
                        <input
                          type="radio"
                          name="classification-choice"
                          value={key}
                          checked={classificationSelection === key}
                          onChange={(event) => setClassificationSelection(event.target.value)}
                        />
                        {" "}
                        {candidate.category} / {candidate.code}
                        {candidate.score !== null ? ` (${candidate.score.toFixed(2)})` : ""}
                      </label>
                    );
                  })
                ) : (
                  <p>No classification candidates available.</p>
                )}
              </div>

              <h3>Materials + Weights (multi-row export)</h3>
              <div style={{ display: "grid", gap: "0.3rem" }}>
                {materialOptions.map((option) => {
                  const edit = materialEdits[option.key] ?? DEFAULT_MATERIAL_EDIT;
                  return (
                    <label key={option.key}>
                      <input
                        type="checkbox"
                        checked={edit.selected}
                        onChange={(event) =>
                          setMaterialEdits((prev) => ({
                            ...prev,
                            [option.key]: {
                              ...(prev[option.key] ?? DEFAULT_MATERIAL_EDIT),
                              selected: event.target.checked,
                            },
                          }))
                        }
                      />
                      {" "}
                      {option.label}
                    </label>
                  );
                })}
              </div>

              <table style={{ width: "100%", borderCollapse: "collapse", marginTop: "0.6rem" }}>
                <thead>
                  <tr>
                    <th align="left">Material</th>
                    <th align="left">Weight</th>
                    <th align="left">Unit</th>
                  </tr>
                </thead>
                <tbody>
                  {materialOptions.filter((option) => materialEdits[option.key]?.selected).map(
                    (option) => {
                      const edit = materialEdits[option.key] ?? DEFAULT_MATERIAL_EDIT;
                      return (
                        <tr key={`${option.key}-row`}>
                          <td>{option.label}</td>
                          <td>
                            <input
                              value={edit.weightValue}
                              onChange={(event) =>
                                setMaterialEdits((prev) => ({
                                  ...prev,
                                  [option.key]: {
                                    ...(prev[option.key] ?? DEFAULT_MATERIAL_EDIT),
                                    weightValue: event.target.value,
                                  },
                                }))
                              }
                              placeholder="e.g. 12.5"
                            />
                          </td>
                          <td>
                            <select
                              value={edit.weightUnit}
                              onChange={(event) =>
                                setMaterialEdits((prev) => ({
                                  ...prev,
                                  [option.key]: {
                                    ...(prev[option.key] ?? DEFAULT_MATERIAL_EDIT),
                                    weightUnit: event.target.value,
                                  },
                                }))
                              }
                            >
                              <option value="">Select</option>
                              <option value="g">g</option>
                              <option value="kg">kg</option>
                            </select>
                          </td>
                        </tr>
                      );
                    },
                  )}
                  {materialOptions.every((option) => !materialEdits[option.key]?.selected) && (
                    <tr>
                      <td colSpan={3}>No materials selected.</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          <div style={{ display: "flex", gap: "0.6rem", flexWrap: "wrap" }}>
            <button type="button" disabled={loading} onClick={() => void handleSubmitCorrections()}>
              Save Corrections
            </button>
            <button type="button" disabled={loading} onClick={() => void handleCompleteTask()}>
              Mark Task Complete
            </button>
            <button type="button" disabled={loading} onClick={() => setScreen("review")}>Back to Queue</button>
          </div>
        </section>
      )}

      {screen === "reports" && (
        <ReportsScreen
          reports={reports}
          loading={loading}
          cardStyle={cardStyle}
          expandedWarnings={expandedWarnings}
          onToggleWarning={(reportId) =>
            setExpandedWarnings((prev) => ({
              ...prev,
              [reportId]: !prev[reportId],
            }))
          }
          onDownloadReport={(report) => void handleDownloadReport(report)}
          formatTimestamp={formatTimestamp}
          shortId={shortId}
        />
      )}
    </main>
  );
}
