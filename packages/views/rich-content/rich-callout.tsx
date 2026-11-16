import type { ComponentPropsWithoutRef } from "react";
import type { ExtraProps } from "react-markdown";
import { CircleAlert, Info, Lightbulb, TriangleAlert } from "@multica/ui/icons";
import { useT } from "../i18n";
import { isCalloutKind } from "./callouts";

const ICONS = { note: Info, tip: Lightbulb, important: CircleAlert, warning: TriangleAlert, caution: CircleAlert };

export function RichCallout({ node, children, ...props }: ComponentPropsWithoutRef<"blockquote"> & ExtraProps) {
  const { t } = useT("editor");
  const kind = node?.properties.dataCallout;
  if (!isCalloutKind(kind)) return <blockquote {...props}>{children}</blockquote>;
  const Icon = ICONS[kind];
  const label = t(($) => $.callouts[kind]);
  return (
    <aside className="rich-callout" data-callout={kind} role="note" aria-label={label}>
      <div className="rich-callout-title"><Icon aria-hidden="true" /><span>{label}</span></div>
      <div className="rich-callout-body">{children}</div>
    </aside>
  );
}
