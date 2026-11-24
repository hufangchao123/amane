import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import {
  getConfigOptions,
  getConfigQueryKey,
  updateConfigMutation,
} from "@/client/@tanstack/react-query.gen";
import { getConfigSchema } from "@/client/sdk.gen";
import type { JSONSchemaObject } from "@/components/schema-form/schema";
import type { HotSettings } from "@/client/types.gen";
import { useAppMutation } from "@/hooks/use-app-mutation";

export function useConfigSchema() {
  return useQuery({
    queryKey: ["config-schema"],
    queryFn: async () => {
      const { data } = await getConfigSchema();
      // OpenAPI schema 运行时对象 → Schema 表单内部类型.
      return data as JSONSchemaObject;
    },
  });
}

export function useConfig() {
  // 接口响应已脱敏 (密钥位置是 ***), 因此不带 response_model; 结构仍是 HotSettings, 供字段读取用.
  return useQuery({ ...getConfigOptions(), select: (data) => data as HotSettings });
}

export function useUpdateConfig() {
  const { t } = useTranslation("common");
  return useAppMutation({
    mutationOptions: updateConfigMutation(),
    invalidates: [getConfigQueryKey()],
    successToast: t("toast.configSaved"),
  });
}
