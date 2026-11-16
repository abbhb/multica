import type { Root } from "hast";
import { visit } from "unist-util-visit";

export const CALLOUT_KINDS = ["note", "tip", "important", "warning", "caution"] as const;
export type CalloutKind = (typeof CALLOUT_KINDS)[number];

export function isCalloutKind(value: unknown): value is CalloutKind {
  return CALLOUT_KINDS.some((kind) => kind === value);
}

export function parseCalloutMarker(text: string): { kind: CalloutKind; length: number } | null {
  const marker = /^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\](?:[\t ]*\n|[\t ]*$)/i.exec(text);
  const kind = marker?.[1]?.toLowerCase();
  return marker && isCalloutKind(kind) ? { kind, length: marker[0].length } : null;
}

/** Runs after sanitization; only parsed blockquotes can become callouts. */
export function rehypeCallouts() {
  return (tree: Root) => {
    visit(tree, "element", (node) => {
      if (node.tagName !== "blockquote") return;
      const paragraph = node.children.find((child) => child.type === "element");
      if (paragraph?.type !== "element" || paragraph.tagName !== "p") return;
      const first = paragraph.children[0];
      if (first?.type !== "text") return;
      const marker = parseCalloutMarker(first.value);
      if (!marker) return;
      const { kind } = marker;

      first.value = first.value.slice(marker.length);
      if (!first.value) {
        paragraph.children.shift();
        // remark-breaks emits a <br> between the marker and its body.
        if (paragraph.children[0]?.type === "element" && paragraph.children[0].tagName === "br") {
          paragraph.children.shift();
        }
        const body = paragraph.children[0];
        if (body?.type === "text") body.value = body.value.replace(/^\n/, "");
      }
      if (paragraph.children.length === 0) {
        node.children.splice(node.children.indexOf(paragraph), 1);
      }
      node.properties.dataCallout = kind;
    });
  };
}
