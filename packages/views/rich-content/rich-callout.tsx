import type { ComponentPropsWithoutRef } from "react";
import type { ExtraProps } from "react-markdown";
import { CircleAlert, Info, Lightbulb, TriangleAlert } from "@multica/ui/icons";
import { useT } from "../i18n";
import { isCalloutKind, type CalloutKind } from "./callouts";

const ICONS = { note: Info, tip: Lightbulb, important: CircleAlert, warning: TriangleAlert, caution: CircleAlert };

export function CalloutTitle({ kind }: { kind: CalloutKind }) {
  const { t } = useT("editor");
  const Icon = ICONS[kind];
  return <div className="rich-callout-title"><Icon aria-hidden="true" /><span>{t(($) => $.callouts[kind])}</span></div>;
}

export function RichCallout({ node, children, ...props }: ComponentPropsWithoutRef<"blockquote"> & ExtraProps) {
  const { t } = useT("editor");
  const kind = node?.properties.dataCallout;
  if (!isCalloutKind(kind)) return <blockquote {...props}>{children}</blockquote>;
  const label = t(($) => $.callouts[kind]);
  return (
    <aside className="rich-callout" data-callout={kind} role="note" aria-label={label}>
      <CalloutTitle kind={kind} />
      <div className="rich-callout-body">{children}</div>
    </aside>
  );
}
