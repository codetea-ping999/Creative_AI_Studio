import type { MediaType } from "../components/PromptForm";

export type QuickReviewIssueTag =
  | "composition"
  | "subject_shape"
  | "mood"
  | "color_lighting"
  | "remove_text"
  | "duration_tempo";

type QuickReviewIssueOption = {
  id: QuickReviewIssueTag;
  label: string;
  promptInstruction: string;
  mediaTypes: readonly MediaType[];
};

const quickReviewIssueOptions: readonly QuickReviewIssueOption[] = [
  {
    id: "composition",
    label: "構図",
    promptInstruction: "Improve the composition.",
    mediaTypes: ["image", "video"],
  },
  {
    id: "subject_shape",
    label: "人物・物の形",
    promptInstruction: "Improve the subject shape.",
    mediaTypes: ["image", "video"],
  },
  {
    id: "mood",
    label: "雰囲気",
    promptInstruction: "Refine the mood.",
    mediaTypes: ["image", "audio", "video"],
  },
  {
    id: "color_lighting",
    label: "色・光",
    promptInstruction: "Improve the color and lighting.",
    mediaTypes: ["image", "video"],
  },
  {
    id: "remove_text",
    label: "文字を消す",
    promptInstruction: "Remove all visible text.",
    mediaTypes: ["image", "video"],
  },
  {
    id: "duration_tempo",
    label: "長さ・テンポ",
    promptInstruction: "Adjust the duration or tempo.",
    mediaTypes: ["audio", "video"],
  },
];

export function getQuickReviewIssueOptions(
  mediaType: MediaType,
): readonly QuickReviewIssueOption[] {
  return quickReviewIssueOptions.filter((option) => option.mediaTypes.includes(mediaType));
}

export function buildQuickReviewPrompt(
  prompt: string,
  issueTags: QuickReviewIssueTag[],
): string {
  if (issueTags.length === 0) {
    return prompt;
  }

  const instructions = quickReviewIssueOptions
    .filter((option) => issueTags.includes(option.id))
    .map((option) => option.promptInstruction);
  return [prompt.trim(), "Keep the main subject and intent.", ...instructions]
    .filter(Boolean)
    .join(" ");
}
