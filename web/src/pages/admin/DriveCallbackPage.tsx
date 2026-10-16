import React, { useEffect, useState } from "react";
import { getAdminDriveSetup } from "../../api/adminDrive";
import "./panels/DriveSetupCard.css";

/** Query strings and window messages never establish OAuth success. */
export function DriveCallbackPage() {
  const [message, setMessage] = useState("Checking the connection with the server…");
  useEffect(() => {
    const controller = new AbortController();
    getAdminDriveSetup(controller.signal).then(value => {
      if (controller.signal.aborted) return;
      if (value.job && ["selecting_folder", "ready", "saved"].includes(value.job.status)) {
        setMessage("Authorization received. Return to Storage to finish your connection.");
        if (window.opener) window.close();
      } else setMessage("Return to Storage to check the current connection status.");
    }).catch(() => { if (!controller.signal.aborted) setMessage("The connection could not be confirmed here. Return to Storage and check status."); });
    return () => controller.abort();
  }, []);
  return <main className="admin-drive-callback"><h1>Google Drive authorization</h1><p role="status">{message}</p><a href="/admin/storage">Return to Storage</a></main>;
}
