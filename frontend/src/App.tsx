import { CSSProperties, FormEvent, useEffect, useMemo, useState } from "react";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000/api/v1";
const API_ORIGIN = API_BASE.replace(/\/api\/v1\/?$/, "");
const DEFAULT_USER_ID = import.meta.env.VITE_DEMO_USER_ID ?? "demo-user";
const DEFAULT_TENANT_ID = import.meta.env.VITE_DEMO_TENANT_ID ?? "123456";

type AppScreen = "upload" | "jobs" | "review" | "review-detail" | "reports";

type AuthContext = {
  userId: string;
  tenantId: string;
};

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
};

type ReportRow = {
  report_id: string;
  document_id: string;
  filename: string;
  status: string;
  row_count: number;
  submission_period: string | null;
  created_at: string | null;
  download_endpoint: string;
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
  rerun: {
    document_id: string;
    status: string;
    report_id: string;
    classification_reran: boolean;
  };
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function buildHeaders(auth: AuthContext, includeJson: boolean = true): HeadersInit {
  const headers: Record<string, string> = {
    "X-User-Id": auth.userId,
    "X-Tenant-Id": auth.tenantId,
  };
  if (includeJson) {
    headers["Content-Type"] = "application/json";
  }
  return headers;
}

function resolveApiPath(path: string): string {
  if (path.startsWith("http://") || path.startsWith("https://")) {
    return path;
  }
  if (path.startsWith("/")) {
    return `${API_ORIGIN}${path}`;
  }
  return `${API_BASE}/${path}`;
}

function getErrorMessage(res: unknown): string | null {
  if (typeof res === "string") {
    return res;
  }
  if (!isRecord(res)) {
    return null;
  }
  if ("detail" in res && typeof res.detail === "string") {
    return res.detail;
  }
  if ("message" in res && typeof res.message === "string") {
    return res.message;
  }
  if ("error" in res && typeof res.error === "string") {
    return res.error;
  }
  return null;
}

function extractErrorMessage(payload: unknown): string {
  return getErrorMessage(payload) ?? "Request failed";
}

async function apiRequest<T>(path: string, auth: AuthContext, init?: RequestInit): Promise<T> {
  const response = await fetch(resolveApiPath(path), {
    ...init,
    headers: {
      ...buildHeaders(auth, !(init?.body instanceof FormData)),
      ...(init?.headers ?? {}),
    },
  });

  const contentType = response.headers.get("content-type") ?? "";
  let payload: unknown = null;
  if (contentType.includes("application/json")) {
    payload = await response.json();
  } else {
    payload = await response.text();
  }

  if (!response.ok) {
    throw new Error(extractErrorMessage(payload));
  }

  return payload as T;
}

async function fetchImageBlob(path: string, auth: AuthContext): Promise<string> {
  const response = await fetch(resolveApiPath(path), {
    headers: buildHeaders(auth, false),
  });
  if (!response.ok) {
    throw new Error(`Image fetch failed (${response.status})`);
  }
  const blob = await response.blob();
  return URL.createObjectURL(blob);
}

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

  const [jobs, setJobs] = useState<JobRow[]>([]);
  const [reviewTasks, setReviewTasks] = useState<ReviewTaskRow[]>([]);
  const [reports, setReports] = useState<ReportRow[]>([]);

  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [reviewDetail, setReviewDetail] = useState<ReviewDetail | null>(null);
  const [selectedPageNo, setSelectedPageNo] = useState<number | null>(null);
  const [imageUrl, setImageUrl] = useState<string | null>(null);
  const [fieldEdits, setFieldEdits] = useState<Record<string, string>>({});
  const [classificationSelection, setClassificationSelection] = useState<string>("");

  const [loading, setLoading] = useState<boolean>(false);
  const [message, setMessage] = useState<string>("Ready.");

  const candidateOptions = useMemo(() => {
    return reviewDetail ? parseCandidates(reviewDetail.classification.candidates) : [];
  }, [reviewDetail]);

  useEffect(() => {
    void refreshDashboard();
  }, []);

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

    let revokedUrl: string | null = null;
    void fetchImageBlob(page.image_endpoint, auth)
      .then((url) => {
        revokedUrl = url;
        setImageUrl(url);
      })
      .catch(() => {
        setImageUrl(null);
      });

    return () => {
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
    const payload = await apiRequest<{ tasks: ReviewTaskRow[] }>("/review/tasks?status=pending", auth);
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
    const response = await fetch(resolveApiPath(`/pipeline/run/${documentId}`), {
      method: "POST",
    });
    const contentType = response.headers.get("content-type") ?? "";
    let payload: unknown = null;
    if (contentType.includes("application/json")) {
      payload = await response.json();
    } else {
      payload = await response.text();
    }
    if (
      response.ok &&
      isRecord(payload) &&
      typeof payload.document_id === "string" &&
      typeof payload.status === "string" &&
      typeof payload.report_id === "string" &&
      typeof payload.review_task_count === "number"
    ) {
      return payload as PipelineRunResponse;
    }
    return { detail: getErrorMessage(payload) ?? "Pipeline run failed" };
  }

  async function handleUpload(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (!selectedFile) {
      setMessage("Select a file first.");
      return;
    }

    setLoading(true);
    setMessage("Creating upload URL...");

    try {
      const presign = await apiRequest<PresignResponse>("/documents/upload/presign", auth, {
        method: "POST",
        body: JSON.stringify({
          filename: selectedFile.name,
          mime_type: selectedFile.type || "application/octet-stream",
          size_bytes: selectedFile.size,
        }),
      });

      if (!presign.upload_url.startsWith("http://") && !presign.upload_url.startsWith("https://")) {
        throw new Error("Presigned URL is not HTTP. Run through Docker MinIO for browser uploads.");
      }

      setMessage("Uploading to object storage...");
      const uploadResponse = await fetch(presign.upload_url, {
        method: "PUT",
        headers: {
          "Content-Type": selectedFile.type || "application/octet-stream",
        },
        body: selectedFile,
      });
      if (!uploadResponse.ok) {
        throw new Error(`Upload failed (${uploadResponse.status}).`);
      }

      setMessage("Finalising upload and creating job...");
      const finalise = await apiRequest<FinaliseResponse>("/documents/upload/finalise", auth, {
        method: "POST",
        body: JSON.stringify({ upload_id: presign.upload_id }),
      });

      setMessage("Running pipeline for uploaded document...");
      const runResult = await runPipeline(finalise.document_id);
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
      setScreen("jobs");
      setSelectedFile(null);
      setMessage(`Upload complete. Job ${shortId(finalise.job_id)} is ${runResult.status}.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Upload failed.");
    } finally {
      setLoading(false);
    }
  }

  async function handleRunJob(documentId: string): Promise<void> {
    setLoading(true);
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
            reviewer: auth.userId,
          }),
        },
      );

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
          <form onSubmit={(event) => void handleUpload(event)} style={{ display: "grid", gap: "0.8rem" }}>
            <input
              type="file"
              accept="application/pdf,image/png,image/jpeg,image/tiff"
              onChange={(event) => {
                const nextFile = event.target.files?.[0] ?? null;
                setSelectedFile(nextFile);
              }}
            />
            <button type="submit" disabled={loading || !selectedFile}>
              Upload + Run Pipeline
            </button>
          </form>
        </section>
      )}

      {screen === "jobs" && (
        <section style={{ ...cardStyle, marginTop: "1rem" }}>
          <h2 style={{ marginTop: 0 }}>Jobs List + Status</h2>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                <th align="left">Job</th>
                <th align="left">File</th>
                <th align="left">Document Status</th>
                <th align="left">Stage</th>
                <th align="left">Created</th>
                <th align="left">Actions</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <tr key={job.job_id}>
                  <td>{shortId(job.job_id)}</td>
                  <td>{job.filename}</td>
                  <td>{job.status}</td>
                  <td>{job.current_stage}</td>
                  <td>{formatTimestamp(job.created_at)}</td>
                  <td>
                    <button
                      type="button"
                      disabled={loading}
                      onClick={() => void handleRunJob(job.document_id)}
                    >
                      Run
                    </button>
                  </td>
                </tr>
              ))}
              {jobs.length === 0 && (
                <tr>
                  <td colSpan={6}>No jobs yet.</td>
                </tr>
              )}
            </tbody>
          </table>
        </section>
      )}

      {screen === "review" && (
        <section style={{ ...cardStyle, marginTop: "1rem" }}>
          <h2 style={{ marginTop: 0 }}>Review Tasks Queue</h2>
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
        <section style={{ ...cardStyle, marginTop: "1rem" }}>
          <h2 style={{ marginTop: 0 }}>Reports List + Download</h2>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                <th align="left">Report</th>
                <th align="left">File</th>
                <th align="left">Status</th>
                <th align="left">Rows</th>
                <th align="left">Created</th>
                <th align="left">Action</th>
              </tr>
            </thead>
            <tbody>
              {reports.map((report) => (
                <tr key={report.report_id}>
                  <td>{shortId(report.report_id)}</td>
                  <td>{report.filename}</td>
                  <td>{report.status}</td>
                  <td>{report.row_count}</td>
                  <td>{formatTimestamp(report.created_at)}</td>
                  <td>
                    <a href={resolveApiPath(report.download_endpoint)} target="_blank" rel="noreferrer">
                      Download CSV
                    </a>
                  </td>
                </tr>
              ))}
              {reports.length === 0 && (
                <tr>
                  <td colSpan={6}>No reports generated yet.</td>
                </tr>
              )}
            </tbody>
          </table>
        </section>
      )}
    </main>
  );
}
