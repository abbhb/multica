import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithI18n } from "../test/i18n";
import { DesktopDownload } from "./desktop-download";

const state = vi.hoisted(() => ({ desktop: false }));
vi.mock("../platform/local-directory", () => ({ isDesktopShell: () => state.desktop }));
beforeEach(() => { state.desktop = false; });

describe("DesktopDownload", () => {
  it("opens the same server's download page in a new tab", () => {
    renderWithI18n(<DesktopDownload />);
    const link = screen.getByRole("link", { name: "Desktop app" });
    expect(link).toHaveAttribute("href", "/download");
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("does not offer a desktop download inside the desktop client", () => {
    state.desktop = true;
    renderWithI18n(<DesktopDownload />);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});
