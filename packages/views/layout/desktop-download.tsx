"use client";

import { Laptop } from "@multica/ui/icons";
import { TooltipHint } from "@multica/ui/components/common/tooltip-hint";
import { isDesktopShell } from "../platform/local-directory";
import { useT } from "../i18n";
import { RAIL_ICON_BUTTON_CLASS } from "./rail-styles";

export function DesktopDownload() {
  const { t } = useT("layout");
  if (isDesktopShell()) return null;
  const label = t(($) => $.help.download_desktop);
  return (
    <TooltipHint label={label} side="right">
      <a href="/download" target="_blank" rel="noopener noreferrer" aria-label={label} className={RAIL_ICON_BUTTON_CLASS}>
        <Laptop aria-hidden="true" />
      </a>
    </TooltipHint>
  );
}
