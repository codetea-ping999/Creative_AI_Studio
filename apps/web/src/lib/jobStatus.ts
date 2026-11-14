/** Japanese labels for job and batch states, shared by every panel. */
const jobStatusLabels: Record<string, string> = {
  pending: "待機",
  queued: "待機",
  preparing: "準備中",
  running: "生成中",
  postprocessing: "仕上げ中",
  succeeded: "完了",
  partial: "一部完了",
  failed: "失敗",
  cancelled: "中断",
};

export function jobStatusLabel(status: string): string {
  return jobStatusLabels[status] ?? status;
}
