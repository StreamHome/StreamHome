import React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useThemeStore } from "../../stores/themeStore";
import { ServerDownloads } from "./ServerDownloads";
import { EmberDownloads } from "./ember/EmberDownloads";
import { DownloadsPanel } from "../admin/panels/DownloadsPanel";
import { useDownloadStream } from "./useDownloadStream";

vi.mock("./useDownloadStream", () => ({ useDownloadStream: vi.fn() }));
const task = { id: "task-reference", title: "Example Movie", status: "Failed", progress: 0, speed: "Failed", eta: "00:00:00" };
const failure = { code: "SOURCE_FORBIDDEN", message: "The source provider refused access from this server.", guidance: "Check source permissions or contact the source provider.", stage: "source_preparation", inputRole: "video", httpStatus: 403 };
afterEach(cleanup);

describe("failure explanations across download surfaces", () => {
  it("shows source denial, guidance and accessible details in every theme and Admin Center", () => {
    vi.mocked(useDownloadStream).mockReturnValue({ downloads: [{ ...task, failure }], connectionState: "connected" });
    for (const [theme, View] of [["ember", EmberDownloads], ["aurora", ServerDownloads], ["cinema", ServerDownloads], ["ember", DownloadsPanel]] as const) {
      useThemeStore.setState({ activeTheme: theme });
      const { container, unmount } = render(<View />);
      expect(screen.getByText("Source access denied (HTTP 403)")).toBeTruthy();
      expect(screen.getByText(failure.message)).toBeTruthy();
      expect(screen.getByText(failure.guidance)).toBeTruthy();
      expect(screen.queryByText(/ETA/)).toBeNull();
      const details = container.querySelector("details")!;
      expect(details.open).toBe(false);
      fireEvent.click(screen.getByText("Details"));
      expect(details.open).toBe(true);
      expect(screen.getByText("task-reference")).toBeTruthy();
      expect(screen.getByText("Source preparation")).toBeTruthy();
      expect(screen.queryByText(/network is blocked/)).toBeNull();
      unmount();
    }
  });
  it("distinguishes confirmed provider evidence from legacy fallback", () => {
    vi.mocked(useDownloadStream).mockReturnValue({ downloads: [{ ...task, failure: { ...failure, provider: "cloudflare", providerCode: "1005" } }], connectionState: "connected" });
    const { unmount } = render(<ServerDownloads />);
    expect(screen.getByText("Server network blocked (Cloudflare 1005)")).toBeTruthy();
    unmount();
    vi.mocked(useDownloadStream).mockReturnValue({ downloads: [task], connectionState: "connected" });
    render(<ServerDownloads />);
    expect(screen.getByText("The download could not be completed.")).toBeTruthy();
    expect(screen.queryByText(/403|Cloudflare/)).toBeNull();
  });
  it("does not render failure descriptions on active or completed tasks", () => {
    vi.mocked(useDownloadStream).mockReturnValue({ downloads: [{ ...task, status: "Downloading", failure, speed: "8 MB/s" }, { ...task, id: "done", status: "Completed", failure }], connectionState: "connected" });
    render(<EmberDownloads />);
    expect(screen.getByText(/8 MB\/s · ETA/)).toBeTruthy();
    expect(screen.queryByText(failure.message)).toBeNull();
  });
});
