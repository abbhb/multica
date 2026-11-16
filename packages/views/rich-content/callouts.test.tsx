import { render } from "@testing-library/react";
import ReactMarkdown from "react-markdown";
import rehypeRaw from "rehype-raw";
import rehypeSanitize from "rehype-sanitize";
import remarkBreaks from "remark-breaks";
import { describe, expect, it } from "vitest";
import { CALLOUT_KINDS, rehypeCallouts } from "./callouts";

function parse(content: string) {
  return render(<ReactMarkdown
    remarkPlugins={[remarkBreaks]}
    rehypePlugins={[rehypeRaw, rehypeSanitize, rehypeCallouts]}
    components={{ blockquote: ({ node, children }) => <blockquote data-callout={node?.properties.dataCallout}>{children}</blockquote> }}
  >{content}</ReactMarkdown>).container;
}

describe("Markdown callouts", () => {
  it.each(CALLOUT_KINDS)("renders a %s marker without exposing the marker as body text", (kind) => {
    const container = parse(`> [!${kind.toUpperCase()}]\n> Keep **this decision** visible.\n>\n> - First step\n> - Second step`);
    expect(container.querySelector("blockquote")?.getAttribute("data-callout")).toBe(kind);
    expect(container.querySelector("strong")?.textContent).toBe("this decision");
    expect(container.querySelectorAll("li")).toHaveLength(2);
    expect(container.textContent).not.toContain(`[!${kind.toUpperCase()}]`);
    expect(container.querySelector("p")?.firstChild?.nodeName).not.toBe("BR");
  });

  it.each(["> An ordinary quote", "> [!UNKNOWN]\n> Keep it literal", "> [!NOTE] inline text", "```md\n> [!NOTE]\n> Example\n```"])("preserves literal content: %s", (content) => {
    expect(parse(content).querySelector("[data-callout]")).toBeNull();
  });

  it("retains sanitization inside a callout", () => {
    const container = parse('> [!WARNING]\n> <script>alert(1)</script>\n> <img src="x" onerror="alert(1)">');
    expect(container.querySelector('[data-callout="warning"]')).not.toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("[onerror]")).toBeNull();
  });
});
