import { ApiError, apiGet, apiPost, apiDelete } from "./client";
import type { Profile, CreateProfileRequest, SaveProfileRequest } from "../types/api";

function isProfile(value: unknown): value is Profile {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const profile = value as Partial<Profile>;
  return typeof profile.id === "string" && profile.id.trim().length > 0
    && typeof profile.name === "string" && profile.name.trim().length > 0
    && typeof profile.avatarColor === "string"
    && (profile.theme === null || typeof profile.theme === "string")
    && typeof profile.pinEnabled === "boolean";
}
export async function getProfiles(): Promise<Profile[]> {
  const value = await apiGet<Profile[]>("/api/profiles");
  if (!Array.isArray(value) || !value.every(isProfile)) throw new ApiError("The server returned an invalid profile list. Try loading it again.", 0, "invalid_response");
  return value;
}
async function writeProfile(data: SaveProfileRequest): Promise<Profile> {
  const value = await apiPost<Profile>("/api/profiles", data);
  if (!isProfile(value) || value.id !== data.id) {
    throw new ApiError("The profile change response was incomplete. Check the server profile list before trying again.", 0, "invalid_response");
  }
  return value;
}
export const createProfile = (data: CreateProfileRequest) => writeProfile(data);
export const saveProfile = (data: SaveProfileRequest) => writeProfile(data);
export const selectProfileAccess = (id: string) => apiPost<{ selected: true }>(`/api/profiles/${encodeURIComponent(id)}/select`, {});
export const unlockProfile = (id: string, pin: string) => apiPost<{ verified: true }>(`/api/profiles/${encodeURIComponent(id)}/unlock`, { pin });
export const deleteProfile = (id: string) => apiDelete<{ status: string }>(`/api/profiles/${encodeURIComponent(id)}`);
