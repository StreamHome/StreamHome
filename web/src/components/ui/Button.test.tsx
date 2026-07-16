import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Button } from "./Button";

describe("button motion contract", () => {
  it("uses the semantic interaction system instead of utility hover transforms", () => {
    render(<Button>Continue</Button>);
    const button = screen.getByRole("button", { name: "Continue" });
    expect(button.className).toContain("interaction-button");
    expect(button.className).not.toContain("hover:");
    expect(button.getAttribute("data-interaction-variant")).toBe("primary");
  });
});
