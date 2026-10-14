import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AdminHelp } from "./AdminHelp";

describe("admin contextual help", () => {
  it("opens a named dialog outside panel motion and contains keyboard focus before returning it", async () => {
    render(<div className="admin-shell"><div data-testid="moving-panel" style={{ transform: "translateY(8px)" }}><AdminHelp label="Data locations" title="Profile data locations" variant="details"><p>Profile activity is durable.</p><a href="#records">Read about records</a></AdminHelp></div></div>);
    const trigger = screen.getByRole("button", { name: "Data locations" });
    expect(screen.queryByText("Profile activity is durable.")).toBeNull();
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "Profile data locations" });
    expect(screen.getByTestId("moving-panel").contains(dialog)).toBe(false);
    expect(dialog.closest(".admin-shell")).toBeTruthy();
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    const close = screen.getByRole("button", { name: "Close Profile data locations" });
    const link = screen.getByRole("link", { name: "Read about records" });
    expect(document.activeElement).toBe(close);
    expect(document.body.style.overflow).toBe("hidden");
    fireEvent.keyDown(window, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(link);
    fireEvent.keyDown(window, { key: "Tab" });
    expect(document.activeElement).toBe(close);
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(trigger);
    expect(document.body.style.overflow).toBe("");
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
  });

  it("releases document scroll when navigation removes an open help dialog", () => {
    const { unmount } = render(<AdminHelp label="HEVC help"><p>Compatibility depends on the device.</p></AdminHelp>);
    fireEvent.click(screen.getByRole("button", { name: "HEVC help" }));
    expect(document.body.style.overflow).toBe("hidden");
    unmount();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.body.style.overflow).toBe("");
  });

  it("provides a keyboard focus stop for scrolling diagnostics that contain no interactive controls", async () => {
    render(<div className="admin-shell"><AdminHelp label="Release history" variant="details">{Array.from({ length: 20 }, (_, index) => <p key={index}>Release record {index + 1}</p>)}</AdminHelp></div>);
    const trigger = screen.getByRole("button", { name: "Release history" });
    trigger.focus();
    fireEvent.click(trigger);
    const close = screen.getByRole("button", { name: "Close Release history" });
    const scrollRegion = screen.getByRole("region", { name: "Release history" });
    expect(scrollRegion.tabIndex).toBe(0);
    expect(scrollRegion.querySelector("button,a,input,select,textarea")).toBeNull();
    expect(document.activeElement).toBe(close);
    fireEvent.keyDown(window, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(scrollRegion);
    fireEvent.keyDown(window, { key: "Tab" });
    expect(document.activeElement).toBe(close);
    scrollRegion.focus();
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(trigger);
    expect(document.body.style.overflow).toBe("");
  });
});
