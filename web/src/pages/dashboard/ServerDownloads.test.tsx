import React from "react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useThemeStore } from "../../stores/themeStore";
import { ServerDownloads } from "./ServerDownloads";
import { useDownloadStream } from "./useDownloadStream";

vi.mock("./useDownloadStream", () => ({ useDownloadStream: vi.fn() }));

const download = { id: "task-1", title: "Example Movie", status: "Downloading", progress: 42, speed: "8 MB/s", eta: "00:01:20" };

describe("themed server downloads", () => {
  beforeEach(() => {
    vi.mocked(useDownloadStream).mockReturnValue({ downloads: [download], connectionState: "connected" });
  });
  afterEach(cleanup);

  it("renders the live queue under every compatibility theme", () => {
    for (const theme of ["aurora", "cinema"] as const) {
      useThemeStore.setState({ activeTheme: theme });
      const { container, unmount } = render(<ServerDownloads />);
      expect(container.querySelector(".server-downloads")?.getAttribute("data-download-theme")).toBe(theme);
      expect(screen.getByRole("heading", { name: "Example Movie" })).toBeTruthy();
      expect(screen.getByText("Live")).toBeTruthy();
      expect((container.querySelector(".server-downloads__progress > div") as HTMLElement).style.width).toBe("42%");
      unmount();
    }
  });

  it("shows a truthful reconnecting state without inventing queue activity", () => {
    useThemeStore.setState({ activeTheme: "cinema" });
    vi.mocked(useDownloadStream).mockReturnValue({ downloads: [], connectionState: "reconnecting" });
    render(<ServerDownloads />);
    expect(screen.getByText("Restoring the live queue")).toBeTruthy();
    expect(screen.getByText("Reconnecting")).toBeTruthy();
  });
  it("shows speed and remaining time only for an active transfer", () => {
    vi.mocked(useDownloadStream).mockReturnValue({ downloads: [download, { ...download, id: "done", status: "Completed", speed: "—", eta: "—" }, { ...download, id: "failed", status: "Failed", speed: "—", eta: "—" }], connectionState: "connected" });
    render(<ServerDownloads />);
    expect(screen.getByText("8 MB/s · ETA 00:01:20")).toBeTruthy();
    expect(screen.queryByText("— · ETA —")).toBeNull();
  });

  it("defines dedicated layout contracts for Aurora and Cinema", () => {
    const css = readFileSync(resolve("src/themes/application/application.css"), "utf8");
    for (const theme of ["aurora", "cinema"]) expect(css).toContain(`data-download-theme="${theme}"`);
    expect(css).toContain('.server-downloads[data-download-theme="cinema"] { width: 100%; padding: 118px');
    expect(css).toContain(".admin-content .server-downloads");
  });
});
