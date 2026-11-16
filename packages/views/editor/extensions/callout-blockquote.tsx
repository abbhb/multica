import Blockquote from "@tiptap/extension-blockquote";
import { NodeViewContent, NodeViewWrapper, ReactNodeViewRenderer, type NodeViewProps } from "@tiptap/react";
import { Plugin } from "@tiptap/pm/state";
import { Decoration, DecorationSet } from "@tiptap/pm/view";
import { parseCalloutMarker } from "../../rich-content/callouts";
import { CalloutTitle } from "../../rich-content/rich-callout";

function BlockquoteView({ node }: NodeViewProps) {
  const marker = parseCalloutMarker(node.firstChild?.firstChild?.text ?? "");
  return (
    <NodeViewWrapper as="blockquote" className={marker ? "rich-callout" : undefined} data-callout={marker?.kind}>
      {marker && <div contentEditable={false}><CalloutTitle kind={marker.kind} /></div>}
      <NodeViewContent className={marker ? "rich-callout-body" : undefined} />
    </NodeViewWrapper>
  );
}

// Preserve the ordinary blockquote schema and Markdown serialization. The
// marker is only hidden visually while the editor is unfocused.
export const CalloutBlockquote = Blockquote.extend({
  renderMarkdown(node, helpers) {
    return (node.content ?? []).map((child, index) => {
      let content = helpers.renderChild?.(child, index) ?? helpers.renderChildren([child]);
      if (index === 0) {
        content = content.replace(/^\\\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\\\](?=\n|$)/i, "[!$1]");
      }
      return content.split("\n").map((line) => line.trim() ? `> ${line}` : ">").join("\n");
    }).join("\n>\n");
  },
  addNodeView() {
    return ReactNodeViewRenderer(BlockquoteView);
  },
  addProseMirrorPlugins() {
    return [new Plugin({
      props: {
        decorations(state) {
          const decorations: Decoration[] = [];
          state.doc.descendants((node, pos) => {
            if (node.type.name !== "blockquote") return;
            const paragraph = node.firstChild;
            const marker = parseCalloutMarker(paragraph?.firstChild?.text ?? "");
            if (!marker) return;
            const hardBreak = paragraph && paragraph.childCount > 1 && paragraph.child(1).type.name === "hardBreak";
            decorations.push(Decoration.inline(pos + 2, pos + 2 + marker.length + (hardBreak ? 1 : 0), { class: "callout-marker" }));
          });
          return DecorationSet.create(state.doc, decorations);
        },
      },
    })];
  },
});
