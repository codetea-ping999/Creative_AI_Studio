import { requestJson } from "../studioClient";

export type BatchTemplate = {
  name: string;
  description: string;
  media_type?: string;
  axes?: Array<{ name: string; value_count: number }>;
  first_stage_items?: number;
  stages?: Array<{ name: string; keep_top_n: number | null }>;
  error?: string;
};

export type BatchItem = {
  id: string;
  index: number;
  label: string;
  stage_name: string;
  stage_index: number;
  axis_values: Record<string, string>;
  job_id: string | null;
  status: string;
  score: number | null;
  output_path: string | null;
  preview_path: string | null;
  error_message: string | null;
  promoted: boolean;
};

export type BatchAggregate = {
  total: number;
  pending: number;
  running: number;
  succeeded: number;
  failed: number;
  cancelled: number;
  average_score: number | null;
  best_item_id: string | null;
};

export type Batch = {
  id: string;
  name: string;
  media_type: string;
  project_id: string | null;
  status: string;
  stage_index: number;
  stage_names: string[];
  aggregate: BatchAggregate;
  items: BatchItem[];
  created_at: string;
  updated_at: string;
};

export const terminalBatchStatuses = new Set([
  "succeeded",
  "partial",
  "failed",
  "cancelled",
]);

export function listBatchTemplates(): Promise<BatchTemplate[]> {
  return requestJson<BatchTemplate[]>("/batches/templates");
}

export function listBatches(options: {
  projectId?: string;
  limit?: number;
} = {}): Promise<{ items: Batch[] }> {
  const search = new URLSearchParams();
  if (options.projectId) {
    search.set("project_id", options.projectId);
  }
  if (options.limit) {
    search.set("limit", String(options.limit));
  }
  const query = search.toString();
  return requestJson<{ items: Batch[] }>(`/batches${query ? `?${query}` : ""}`);
}

export function getBatch(batchId: string): Promise<Batch> {
  return requestJson<Batch>(`/batches/${batchId}`);
}

export function createBatchFromTemplate(payload: {
  template: string;
  overrides: Record<string, unknown>;
}): Promise<Batch> {
  return requestJson<Batch>("/batches", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function cancelBatch(batchId: string): Promise<Batch> {
  return requestJson<Batch>(`/batches/${batchId}/cancel`, { method: "POST" });
}

export function advanceBatch(batchId: string): Promise<Batch> {
  return requestJson<Batch>(`/batches/${batchId}/advance`, { method: "POST" });
}

export function promoteBatchItem(
  batchId: string,
  itemId: string,
): Promise<Batch> {
  return requestJson<Batch>(`/batches/${batchId}/items/${itemId}/promote`, {
    method: "POST",
  });
}

/**
 * Items of the newest stage, best score first.
 *
 * Comparison only makes sense within one stage: a refined 1024px render and a
 * 640px probe are not judged on the same terms.
 */
export function currentStageItems(batch: Batch | null): BatchItem[] {
  if (!batch) {
    return [];
  }
  return [...batch.items]
    .filter((item) => item.stage_index === batch.stage_index)
    .sort((left, right) => {
      const leftScore = left.score ?? -1;
      const rightScore = right.score ?? -1;
      if (leftScore !== rightScore) {
        return rightScore - leftScore;
      }
      return left.index - right.index;
    });
}

export function batchProgressLabel(batch: Batch | null): string {
  if (!batch) {
    return "";
  }
  const { succeeded, failed, cancelled, total } = batch.aggregate;
  const done = succeeded + failed + cancelled;
  const stage = batch.stage_names[batch.stage_index] ?? "stage";
  return `${stage}: ${done}/${total} 完了 (成功 ${succeeded} / 失敗 ${failed})`;
}
