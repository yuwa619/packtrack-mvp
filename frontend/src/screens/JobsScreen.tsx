import { CSSProperties } from "react";

type JobRow = {
  job_id: string;
  document_id: string;
  filename: string;
  status: string;
  current_stage: string;
  created_at: string | null;
};

type JobsScreenProps = {
  jobs: JobRow[];
  loading: boolean;
  cardStyle: CSSProperties;
  onRunJob: (documentId: string) => void;
  formatTimestamp: (value: string | null) => string;
  shortId: (value: string) => string;
};

export function JobsScreen({
  jobs,
  loading,
  cardStyle,
  onRunJob,
  formatTimestamp,
  shortId,
}: JobsScreenProps) {
  return (
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
                  onClick={() => onRunJob(job.document_id)}
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
  );
}
