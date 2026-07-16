import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { BrandLogo } from "./BrandLogo";

describe("BrandLogo", () => {
  it("renders the public logo with the StreamHome wordmark", () => {
    const { container } = render(<BrandLogo />);
    const image = container.querySelector("img");
    expect(image?.getAttribute("src")).toBe("/logo.png");
    expect(image?.getAttribute("aria-hidden")).toBe("true");
    expect(screen.getByText("STREAMHOME")).toBeTruthy();
  });

  it("gives the standalone mark an accessible name", () => {
    render(<BrandLogo showWordmark={false} />);
    expect(screen.getByAltText("StreamHome")).toBeTruthy();
  });
});
