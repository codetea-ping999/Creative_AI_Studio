import { startTransition, useCallback, useEffect, useState } from "react";
import { terminalStatuses, type JobResponse } from "../studio";
import { requestJson } from "../studioClient";

export interface UseJobPollingOptions {
  /**
   * Called after every fetched job update, terminal or not, with the same
   * refreshAfterFinish flag the caller passed to loadJob. Gallery/project
   * refresh and any UI messaging live here, not in this hook, so a caller
   * can keep its existing refreshStudio() -> loadProjects() sequencing.
   */
  onJobUpdate: (job: JobResponse, refreshAfterFinish: boolean) => void | Promise<void>;
  onJobError: (error: unknown) => void;
}

/**
 * Owns the currently-tracked job and polls it every 2s while active.
 *
 * The dependency array below intentionally omits `loadJob`: onJobUpdate/
 * onJobError are recreated every render on the caller's side, so including
 * it would tear down and restart the interval on every unrelated re-render
 * instead of only when the tracked job changes. This mirrors the polling
 * effect's original dependency array before this hook existed.
 */
export function useJobPolling({ onJobUpdate, onJobError }: UseJobPollingOptions) {
  const [latestJob, setLatestJob] = useState<JobResponse | null>(null);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);

  const loadJob = useCallback(
    async (jobId: string, refreshAfterFinish = false): Promise<void> => {
      try {
        const payload = await requestJson<JobResponse>(`/jobs/${jobId}`);
        startTransition(() => {
          setLatestJob(payload);
        });

        if (terminalStatuses.has(payload.status)) {
          setActiveJobId(null);
        }
        await onJobUpdate(payload, refreshAfterFinish);
      } catch (error) {
        setActiveJobId(null);
        onJobError(error);
      }
    },
    [onJobUpdate, onJobError],
  );

  useEffect(() => {
    if (!activeJobId) {
      return undefined;
    }

    const timer = window.setInterval(() => {
      void loadJob(activeJobId, true);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [activeJobId]);

  return { latestJob, setLatestJob, activeJobId, setActiveJobId, loadJob };
}
