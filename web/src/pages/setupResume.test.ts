import { describe, expect, it, vi } from "vitest";
import {
  SETUP_CHECKPOINT_KEY,
  clearSetupCheckpoint,
  copySetupText,
  readSetupCheckpoint,
  type SetupCheckpoint,
  writeSetupCheckpoint,
} from "./setupResume";

function memoryStorage() {
  const values = new Map<string, string>();
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  };
}

const checkpoint: SetupCheckpoint = {
  version: 1,
  step: 6,
  email: "admin@example.com",
  publicUrl: "https://watch.example.com",
  webPort: 3000,
  hevc: "auto",
  backups: true,
  updates: false,
  storage: "CLOUD",
  remoteName: "streamhome-drive",
  audience: "external",
  publishingStatus: "production",
  driveJobId: "drive-job-id",
  drivePath: "StreamHome",
};

describe("setup resume checkpoint", () => {
  it("round-trips only the explicitly allowlisted non-secret fields", () => {
    const storage = memoryStorage();
    writeSetupCheckpoint(checkpoint, storage);

    expect(readSetupCheckpoint(storage)).toEqual(checkpoint);
    const serialized = storage.getItem(SETUP_CHECKPOINT_KEY) ?? "";
    expect(serialized).not.toContain("password");
    expect(serialized).not.toContain("clientSecret");
    expect(serialized).not.toContain("tmdbToken");
    expect(serialized).not.toContain("totpSecret");
    expect(serialized).not.toContain("bootstrapCode");
  });

  it("rejects malformed checkpoints and clears completed setup state", () => {
    const storage = memoryStorage();
    storage.setItem(SETUP_CHECKPOINT_KEY, JSON.stringify({ ...checkpoint, webPort: 0 }));

    expect(readSetupCheckpoint(storage)).toBeNull();
    expect(storage.getItem(SETUP_CHECKPOINT_KEY)).toBeNull();

    writeSetupCheckpoint(checkpoint, storage);
    clearSetupCheckpoint(storage);
    expect(storage.getItem(SETUP_CHECKPOINT_KEY)).toBeNull();
  });
});

describe("setup clipboard fallback", () => {
  it("uses the Clipboard API when available", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);

    await expect(copySetupText("callback", { writeText })).resolves.toBe(true);
    expect(writeText).toHaveBeenCalledWith("callback");
  });

  it("falls back to a selected textarea when clipboard access is denied", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("denied"));
    const execCommand = vi.fn().mockReturnValue(true);
    Object.defineProperty(document, "execCommand", { configurable: true, value: execCommand });

    await expect(copySetupText("callback", { writeText }, document)).resolves.toBe(true);
    expect(execCommand).toHaveBeenCalledWith("copy");
    expect(document.querySelector("textarea[aria-hidden=true]")).toBeNull();
  });
});
