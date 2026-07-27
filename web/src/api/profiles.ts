import { apiGet, apiPost, apiDelete } from "./client";
import type { Profile, CreateProfileRequest, SaveProfileRequest } from "../types/api";

export const getProfiles = () => apiGet<Profile[]>("/api/profiles");
export const createProfile = (data: CreateProfileRequest) => apiPost<Profile>("/api/profiles", data);
export const saveProfile = (data: SaveProfileRequest) => apiPost<Profile>("/api/profiles", data);
export const unlockProfile = (id: string, pin: string) => apiPost<{ verified: true }>(`/api/profiles/${encodeURIComponent(id)}/unlock`, { pin });
export const deleteProfile = (id: string) => apiDelete<{ status: string }>(`/api/profiles/${encodeURIComponent(id)}`);
