import { Stack } from "@mantine/core";
import { SubtitleProgressPanel } from "@/features/subtitles/progress-panel";
import { SubtitleRecentRuns } from "@/features/subtitles/recent-runs";
import { SubtitleStatusCards } from "@/features/subtitles/status-cards";

/** 字幕补全页的内容区: 状态与入口在上, 进行中的进度居中, 最近几轮在下. */
export function SubtitlesView() {
  return (
    <Stack gap="lg">
      <SubtitleStatusCards />
      <SubtitleProgressPanel />
      <SubtitleRecentRuns />
    </Stack>
  );
}
