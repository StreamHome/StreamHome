export const SETUP_CHECKPOINT_KEY = "streamhome.setup.checkpoint.v1";

export interface SetupCheckpoint {
  version: 1;
  step: number;
  email: string;
  publicUrl: string;
  webPort: number;
  hevc: "auto" | "on" | "off";
  backups: boolean;
  updates: boolean;
  storage: "LOCAL" | "CLOUD";
  remoteName: string;
  audience: "external" | "internal";
  publishingStatus: "testing" | "production";
  driveJobId: string;
  drivePath: string;
}

interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

function validStep(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 && value <= 7;
}

function validPort(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 1 && value <= 65535;
}

export function readSetupCheckpoint(storage: StorageLike = window.sessionStorage): SetupCheckpoint | null {
  try {
    const raw = storage.getItem(SETUP_CHECKPOINT_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<SetupCheckpoint>;
    if (
      value.version !== 1
      || !validStep(value.step)
      || typeof value.email !== "string"
      || typeof value.publicUrl !== "string"
      || !validPort(value.webPort)
      || !["auto", "on", "off"].includes(value.hevc ?? "")
      || typeof value.backups !== "boolean"
      || typeof value.updates !== "boolean"
      || !["LOCAL", "CLOUD"].includes(value.storage ?? "")
      || typeof value.remoteName !== "string"
      || !["external", "internal"].includes(value.audience ?? "")
      || !["testing", "production"].includes(value.publishingStatus ?? "")
      || typeof value.driveJobId !== "string"
      || typeof value.drivePath !== "string"
    ) {
      storage.removeItem(SETUP_CHECKPOINT_KEY);
      return null;
    }
    return value as SetupCheckpoint;
  } catch {
    return null;
  }
}

export function writeSetupCheckpoint(checkpoint: SetupCheckpoint, storage: StorageLike = window.sessionStorage): void {
  try {
    storage.setItem(SETUP_CHECKPOINT_KEY, JSON.stringify(checkpoint));
  } catch {
    // Private browsing and locked-down browsers may reject storage. Setup still
    // remains usable in the current tab.
  }
}

export function clearSetupCheckpoint(storage: StorageLike = window.sessionStorage): void {
  try {
    storage.removeItem(SETUP_CHECKPOINT_KEY);
  } catch {
    // Clearing a best-effort checkpoint must never block setup completion.
  }
}

export async function copySetupText(
  value: string,
  clipboard: Pick<Clipboard, "writeText"> | undefined = navigator.clipboard,
  documentRef: Document = document,
): Promise<boolean> {
  if (!value) return false;
  if (clipboard?.writeText) {
    try {
      await clipboard.writeText(value);
      return true;
    } catch {
      // HTTP private-network origins commonly deny the async Clipboard API.
      // Fall through to a temporary selected textarea.
    }
  }

  const textarea = documentRef.createElement("textarea");
  textarea.value = value;
  textarea.readOnly = true;
  textarea.setAttribute("aria-hidden", "true");
  textarea.style.position = "fixed";
  textarea.style.inset = "-9999px auto auto -9999px";
  documentRef.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  try {
    return documentRef.execCommand("copy");
  } catch {
    return false;
  } finally {
    textarea.remove();
  }
}
