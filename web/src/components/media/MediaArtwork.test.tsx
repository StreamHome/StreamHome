import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MediaArtwork } from "./MediaArtwork";

describe("MediaArtwork", () => {
  it("does not request a web-local fallback asset", () => {
    render(<MediaArtwork src="/poster.jpg" alt="Example" />);
    expect(screen.getByLabelText("Example artwork unavailable")).toBeTruthy();
    expect(screen.queryByRole("img", { name: "Example" })).toBeNull();
  });

  it("falls back when a server image fails", () => {
    render(<MediaArtwork src="/media/Movies/example/poster.jpg" alt="Example" />);
    fireEvent.error(screen.getByRole("img", { name: "Example" }));
    expect(screen.getByLabelText("Example artwork unavailable")).toBeTruthy();
  });
});
