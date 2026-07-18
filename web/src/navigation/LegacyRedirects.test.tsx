import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";
import { useProfileStore } from "../stores/profileStore";
import { LegacyAccountSecurityRedirect } from "./LegacyRedirects";

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>;
}

function renderRedirect() {
  return render(
    <MemoryRouter initialEntries={["/account/security"]}>
      <Routes>
        <Route path="/account/security" element={<LegacyAccountSecurityRedirect />} />
        <Route path="/" element={<LocationProbe />} />
        <Route path="/profiles" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("legacy Account Security routing", () => {
  beforeEach(() => {
    localStorage.clear();
    useProfileStore.setState({ activeProfile: null, isAdmin: false });
  });

  it("canonicalizes the old path with the persisted profile", async () => {
    localStorage.setItem("streamhome_profile", "1");
    renderRedirect();
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/?profile=1&view=admin&section=security"));
  });

  it("returns to profile selection when no profile is available", async () => {
    renderRedirect();
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/profiles"));
  });
});
