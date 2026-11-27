import { Stack } from "@mantine/core";
import { SubtitleGapPanel } from "@/features/subtitles/gap-panel";
import { SubtitleProgressPanel } from "@/features/subtitles/progress-panel";
import { SubtitleRecentRuns } from "@/features/subtitles/recent-runs";
import { SubtitleStatusCards } from "@/features/subtitles/status-cards";

/** 字幕补全页的内容区: 状态与入口在上, 进度次之, 缺字幕清单再次, 最近几轮在下. */
export function SubtitlesView() {
  return (
    <Stack gap="lg">
      <SubtitleStatusCards />
      <SubtitleProgressPanel />
      <SubtitleGapPanel />
      <SubtitleRecentRuns />
    </Stack>
  );
}
