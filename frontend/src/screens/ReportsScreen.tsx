import { CSSProperties, Fragment } from "react";

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

type ReportsScreenProps = {
  reports: ReportRow[];
  loading: boolean;
  cardStyle: CSSProperties;
  expandedWarnings: Record<string, boolean>;
  onToggleWarning: (reportId: string) => void;
  onDownloadReport: (report: ReportRow) => void;
  formatTimestamp: (value: string | null) => string;
  shortId: (value: string) => string;
};

export type { ReportRow };

export function ReportsScreen({
  reports,
  loading,
  cardStyle,
  expandedWarnings,
  onToggleWarning,
  onDownloadReport,
  formatTimestamp,
  shortId,
}: ReportsScreenProps) {
  return (
    <section style={{ ...cardStyle, marginTop: "1rem" }}>
      <h2 style={{ marginTop: 0 }}>Reports List + Download</h2>
      <table style={{ width: "100%", borderCollapse: "collapse" }}>
        <thead>
          <tr>
            <th align="left">Report</th>
            <th align="left">File</th>
            <th align="left">Status</th>
            <th align="left">Rows</th>
            <th align="left">Warnings</th>
            <th align="left">Created</th>
            <th align="left">Action</th>
          </tr>
        </thead>
        <tbody>
          {reports.map((report) => {
            const isExpanded = expandedWarnings[report.report_id] ?? false;
            return (
              <Fragment key={report.report_id}>
                <tr>
                  <td>{shortId(report.report_id)}</td>
                  <td>
                    {report.filename}
                    {report.report_scope === "batch" && report.document_count ? (
                      <div style={{ opacity: 0.7 }}>
                        Combined batch report for {report.document_count} document(s)
                      </div>
                    ) : null}
                  </td>
                  <td>{report.status}</td>
                  <td>{report.row_count}</td>
                  <td>
                    {report.warning_count}
                    {report.warning_count > 0 && (
                      <button
                        type="button"
                        onClick={() => onToggleWarning(report.report_id)}
                        style={{ marginLeft: "0.4rem" }}
                      >
                        {isExpanded ? "Hide" : "Show"}
                      </button>
                    )}
                  </td>
                  <td>{formatTimestamp(report.created_at)}</td>
                  <td>
                    <button
                      type="button"
                      disabled={loading}
                      onClick={() => onDownloadReport(report)}
                    >
                      Download CSV
                    </button>
                  </td>
                </tr>
                {isExpanded && report.validation_warnings && (
                  <tr>
                    <td colSpan={7}>
                      <strong>Warning summary:</strong>{" "}
                      {report.validation_warnings.overall.join(" | ") || "No overall warnings"}
                      {report.validation_warnings.per_document &&
                        report.validation_warnings.per_document.length > 0 && (
                          <div style={{ marginTop: "0.4rem" }}>
                            {report.validation_warnings.per_document.map((entry) => (
                              <div key={`${report.report_id}-${entry.document_id}`}>
                                {entry.filename}: {entry.warning_count} warning(s)
                                {entry.overall.length > 0 ? ` | ${entry.overall.join(" | ")}` : ""}
                              </div>
                            ))}
                          </div>
                        )}
                      <div style={{ marginTop: "0.4rem" }}>
                        {report.validation_warnings.missing_fields_by_row.map((entry) => (
                          <div key={`${report.report_id}-${entry.row_index}-${entry.material_key}`}>
                            Row {entry.row_index}
                            {entry.filename ? ` in ${entry.filename}` : ""}
                            {" "}({entry.material_key}):{" "}
                            {entry.missing_fields.join(", ")}
                          </div>
                        ))}
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
          {reports.length === 0 && (
            <tr>
              <td colSpan={7}>No reports generated yet.</td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  );
}
