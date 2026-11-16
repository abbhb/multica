import { fireEvent, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Issue } from "@multica/core/types";
import { AppLink, NavigationProvider, type NavigationAdapter } from "../../navigation";
import { renderWithI18n } from "../../test/i18n";
import { BoardParentIssue } from "./board-parent-issue";

const getIssue = vi.hoisted(() => vi.fn());
vi.mock("@multica/core/api", () => ({ api: { getIssue } }));
vi.mock("@multica/core/hooks", () => ({ useWorkspaceId: () => "ws-1" }));
vi.mock("@multica/core/paths", () => ({
  useWorkspacePaths: () => ({ issueDetail: (id: string) => `/acme/issues/${id}` }),
}));

const parent = { id: "parent-50", identifier: "MUL-50", title: "Optimize node APIs" } as Issue;
const navigation: NavigationAdapter = {
  push: vi.fn(), replace: vi.fn(), back: vi.fn(),
  openInNewTab: vi.fn(),
  pathname: "/acme/issues",
  searchParams: new URLSearchParams(),
  hash: "",
  getShareableUrl: (path) => `https://app.example${path}`,
};

function renderParent(options: { loaded?: boolean; siblings?: boolean; onPointerDown?: () => void } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return renderWithI18n(
    <QueryClientProvider client={client}>
      <NavigationProvider value={navigation}>
        <div onPointerDown={options.onPointerDown}>
          <AppLink href="/acme/issues/child-51">
            Open child
            <BoardParentIssue parentId={parent.id} parentIssue={options.loaded ? parent : undefined} />
          </AppLink>
          {options.siblings && <BoardParentIssue parentId={parent.id} />}
        </div>
      </NavigationProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.clearAllMocks());

describe("BoardParentIssue", () => {
  it("uses the parent already loaded on the board without another request or nested anchor", () => {
    const { container } = renderParent({ loaded: true });
    expect(screen.getByRole("link", { name: "Parent issue MUL-50 Optimize node APIs" })).toBeInTheDocument();
    expect(getIssue).not.toHaveBeenCalled();
    expect(container.querySelector("a a")).toBeNull();
  });

  it("resolves a parent outside the visible board once for siblings", async () => {
    getIssue.mockResolvedValue(parent);
    renderParent({ siblings: true });
    expect(await screen.findAllByRole("link", { name: "Parent issue MUL-50 Optimize node APIs" })).toHaveLength(2);
    expect(getIssue).toHaveBeenCalledExactlyOnceWith(parent.id);
  });

  it("opens the parent with click and keyboard without opening or dragging the child", () => {
    const onPointerDown = vi.fn();
    renderParent({ loaded: true, onPointerDown });
    const link = screen.getByRole("link", { name: "Parent issue MUL-50 Optimize node APIs" });
    fireEvent.pointerDown(link);
    expect(onPointerDown).not.toHaveBeenCalled();
    fireEvent.pointerUp(document);
    fireEvent.click(link);
    expect(navigation.push).toHaveBeenCalledExactlyOnceWith("/acme/issues/parent-50");
    vi.mocked(navigation.push).mockClear();
    fireEvent.keyDown(link, { key: "Enter" });
    expect(navigation.push).toHaveBeenCalledExactlyOnceWith("/acme/issues/parent-50");
  });

  it("preserves modifier and middle-click navigation to the parent", () => {
    renderParent({ loaded: true });
    const link = screen.getByRole("link", { name: "Parent issue MUL-50 Optimize node APIs" });
    fireEvent.click(link, { metaKey: true });
    expect(navigation.openInNewTab).toHaveBeenLastCalledWith("/acme/issues/parent-50", "MUL-50");
    fireEvent(link, new MouseEvent("auxclick", { button: 1, bubbles: true, cancelable: true }));
    expect(navigation.openInNewTab).toHaveBeenCalledTimes(2);
    expect(navigation.push).not.toHaveBeenCalled();
  });

  it("shows unavailable parents without exposing the raw ID or opening the child", async () => {
    getIssue.mockRejectedValue(new Error("Parent not found"));
    renderParent();
    await waitFor(() => expect(screen.getByText("Parent issue unavailable")).toBeInTheDocument());
    expect(screen.queryByRole("link", { name: /^Parent issue/ })).not.toBeInTheDocument();
    expect(screen.queryByText(parent.id)).not.toBeInTheDocument();
  });
});
