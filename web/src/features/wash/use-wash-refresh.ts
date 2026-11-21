import { useQueryClient } from "@tanstack/react-query";
import {
  washCandidatesQueryKey,
  washRecordsQueryKey,
  washStatusQueryKey,
} from "@/client/@tanstack/react-query.gen";

/** 三个读接口一起重取的交点.
 *
 * 洗版动作只入队: 提交成功时文件还在原处, 记录也还没写库, 立刻失效查询只会拿回旧数据.
 * 因此各表按需调用本 hook, 由页面上的「刷新」按钮决定何时真的重取, 避免猜测任务耗时.
 */
export function useWashRefresh() {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: washStatusQueryKey() });
    void queryClient.invalidateQueries({ queryKey: washCandidatesQueryKey() });
    void queryClient.invalidateQueries({ queryKey: washRecordsQueryKey() });
  };
}
