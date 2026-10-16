import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { useProfileStore } from "../stores/profileStore";
import { ProfileEditPage } from "./ProfileEditPage";
vi.mock("../api/profiles", () => ({ getProfiles: vi.fn(), saveProfile: vi.fn(), deleteProfile: vi.fn() }));
beforeEach(() => { useProfileStore.setState({ profiles: [{ id: "2", name: "Viewer", theme: "ember", avatarColor: "blue", pinEnabled: false }], activeProfile: null }); });
it("keeps a native associated compact PIN toggle and matching-digit validation", () => {
  render(<MemoryRouter initialEntries={["/profiles/2/edit"]}><Routes><Route path="/profiles/:profileId/edit" element={<ProfileEditPage />} /></Routes></MemoryRouter>);
  const toggle = screen.getByRole("checkbox", { name: "Require profile PIN" }) as HTMLInputElement;
  expect(toggle.closest("label")?.className).toBe("profile-editor__pin-toggle");
  fireEvent.click(screen.getByText("Require a PIN")); expect(toggle.checked).toBe(true); expect(screen.getByLabelText("Profile PIN")).toBeTruthy();
  fireEvent.change(screen.getByLabelText("Profile PIN"), { target: { value: "1234" } });
  fireEvent.change(screen.getByLabelText("Confirm profile PIN"), { target: { value: "4321" } });
  expect(screen.getByText("Use 4 to 8 matching digits.")).toBeTruthy(); expect((screen.getByRole("button", { name: "Save changes" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(screen.getByLabelText("Confirm profile PIN"), { target: { value: "1234" } }); expect((screen.getByRole("button", { name: "Save changes" }) as HTMLButtonElement).disabled).toBe(false);
});
