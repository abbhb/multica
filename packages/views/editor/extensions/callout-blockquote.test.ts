import { afterEach, describe, expect, it } from "vitest";
import { Editor } from "@tiptap/core";
import { createEditorExtensions } from ".";

let editor: Editor | undefined;
afterEach(() => { editor?.destroy(); document.body.innerHTML = ""; });

describe("callout blockquote authoring", () => {
  it("retains the marker and body through the production Markdown round trip", () => {
    const element = document.createElement("div");
    document.body.appendChild(element);
    editor = new Editor({ element, extensions: createEditorExtensions({ placeholder: "", disableMentions: true, enableSlashCommands: false, onUploadFileRef: { current: undefined } }) });
    editor.commands.setContent(
      "> [!IMPORTANT]\n> Back up **the database** before upgrading.\n\n> An ordinary quote.",
      { contentType: "markdown" },
    );
    const initialDoc = editor.getJSON();
    const markdown = editor.getMarkdown();
    expect(markdown).toContain("[!IMPORTANT]");
    expect(markdown).toContain("**the database**");
    expect(markdown).not.toContain("rich-callout");
    editor.commands.setContent(markdown, { contentType: "markdown" });
    expect(editor.getJSON()).toEqual(initialDoc);
  });
});
