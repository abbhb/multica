"use client";

import { useQuery } from "@tanstack/react-query";
import type { Issue } from "@multica/core/types";
import { useWorkspaceId } from "@multica/core/hooks";
import { useWorkspacePaths } from "@multica/core/paths";
import { issueDetailOptions } from "@multica/core/issues/queries";
import { CornerUpLeft } from "@multica/ui/icons";
import { CardControl } from "../../common/card-control";
import { useT } from "../../i18n";
import { resolveClickIntent, useIntentNavigate } from "../../navigation";

export function BoardParentIssue({
  parentId,
  parentIssue,
}: {
  parentId: string;
  parentIssue?: Issue;
}) {
  const { t } = useT("issues");
  const { t: tCommon } = useT("common");
  const wsId = useWorkspaceId();
  const paths = useWorkspacePaths();
  const navigate = useIntentNavigate();
  const { data, isError } = useQuery({
    ...issueDetailOptions(wsId, parentId),
    enabled: !parentIssue,
  });
  const parent = parentIssue ?? data;
  const rowClass = "flex min-w-0 items-start gap-1.5 rounded-md bg-muted/50 px-2 py-1.5 text-caption text-muted-foreground";

  if (!parent) {
    return (
      <div className={`${rowClass} mt-2`}>
        <CornerUpLeft aria-hidden="true" className="mt-0.5 size-3 shrink-0" />
        <span>{isError ? t(($) => $.card.parent_unavailable) : `${t(($) => $.detail.section_parent_issue)} · ${tCommon(($) => $.loading)}`}</span>
      </div>
    );
  }

  // The card is already an anchor. Follow the same nested-control pattern as
  // duplicate references so the parent link neither opens nor drags the child.
  const href = paths.issueDetail(parent.id);
  const stop = (event: React.SyntheticEvent) => {
    event.preventDefault();
    event.stopPropagation();
  };
  return (
    <CardControl className="mt-2">
      <span
        role="link"
        tabIndex={0}
        title={`${t(($) => $.detail.section_parent_issue)} ${parent.identifier} ${parent.title}`}
        className={`${rowClass} cursor-pointer transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring`}
        onClick={(event) => {
          stop(event);
          navigate(href, resolveClickIntent(event), parent.identifier);
        }}
        onAuxClick={(event) => {
          if (event.button !== 1) return;
          stop(event);
          navigate(href, "background-tab", parent.identifier);
        }}
        onKeyDown={(event) => {
          if (event.key !== "Enter") return;
          stop(event);
          navigate(href, "push", parent.identifier);
        }}
      >
        <CornerUpLeft aria-hidden="true" className="mt-0.5 size-3 shrink-0" />
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-1">
            <span>{t(($) => $.detail.section_parent_issue)}</span>{" "}
            <span className="font-medium">{parent.identifier}</span>
          </span>{" "}
          <span className="mt-0.5 block text-foreground [overflow-wrap:anywhere]">{parent.title}</span>
        </span>
      </span>
    </CardControl>
  );
}
