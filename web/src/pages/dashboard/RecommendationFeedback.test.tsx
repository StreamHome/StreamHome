import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { RecommendationFeedback } from "./RecommendationFeedback";

describe("RecommendationFeedback", () => {
  it("sets and clears one explicit preference", async () => {
    const onChange = vi.fn().mockResolvedValue(undefined);
    const { rerender } = render(<RecommendationFeedback movieId="m_1" preference={null} onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "Set love for this title" }));
    await waitFor(() => expect(onChange).toHaveBeenCalledWith("m_1", "love"));
    rerender(<RecommendationFeedback movieId="m_1" preference="love" onChange={onChange} />);
    expect(screen.getByRole("button", { name: "Remove love for this title" }).getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "Remove love for this title" }));
    await waitFor(() => expect(onChange).toHaveBeenLastCalledWith("m_1", null));
  });
});
