import React, { useState } from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { Modal } from "./Modal";
it("preserves Modal appearance and cycles currently enabled visible controls", () => {
  function Fixture() { const [open, setOpen] = useState(false); const [extra, setExtra] = useState(false); return <><button onClick={() => setOpen(true)}>Open dialog</button><Modal isOpen={open} onClose={() => setOpen(false)} ariaLabel="Shared dialog"><button onClick={() => setExtra(true)}>Add control</button><button disabled>Disabled</button><button hidden>Hidden</button>{extra && <button>Last control</button>}</Modal></>; }
  render(<Fixture />); const invoker = screen.getByRole("button", { name: "Open dialog" }); invoker.focus(); fireEvent.click(invoker); const dialog = screen.getByRole("dialog"); expect(dialog.className).toContain("max-w-lg"); const add = screen.getByRole("button", { name: "Add control" }); expect(document.activeElement).toBe(add); fireEvent.click(add); fireEvent.keyDown(window, { key: "Tab", shiftKey: true }); expect(document.activeElement).toBe(screen.getByRole("button", { name: "Last control" })); fireEvent.keyDown(window, { key: "Tab" }); expect(document.activeElement).toBe(add); fireEvent.keyDown(window, { key: "Escape" }); expect(document.activeElement).toBe(invoker); expect(document.body.style.overflow).toBe("");
});


it.each([false, true])("keeps pending submit focus contained when all draft controls disabled=%s and preserves the original invoker", async (disableDraft) => {
  let finish!: () => void; const response = new Promise<void>((resolve) => { finish = resolve; });
  function Fixture() {
    const [open, setOpen] = useState(false); const [pending, setPending] = useState(false); const [draft, setDraft] = useState("retained draft");
    return <><button onClick={() => setOpen(true)}>Open pending dialog</button><Modal isOpen={open} onClose={() => { if (!pending) setOpen(false); }} ariaLabel="Pending dialog"><label>Draft<input disabled={pending && disableDraft} value={draft} onChange={(event) => setDraft(event.target.value)} /></label><button disabled={pending} onClick={async () => { setPending(true); await response; setPending(false); }}>{pending ? "Saving…" : "Submit"}</button><button disabled={pending} onClick={() => setOpen(false)}>Cancel pending</button></Modal></>;
  }
  render(<Fixture />); const invoker = screen.getByRole("button", { name: "Open pending dialog" }); invoker.focus(); fireEvent.click(invoker);
  const input = screen.getByRole("textbox", { name: "Draft" }); fireEvent.change(input, { target: { value: "edited draft" } });
  const submit = screen.getByRole("button", { name: "Submit" }); submit.focus(); fireEvent.click(submit);
  const dialog = screen.getByRole("dialog", { name: "Pending dialog" });
  expect(document.activeElement).toBe(disableDraft ? dialog : input);
  expect(screen.getByRole<HTMLButtonElement>("button", { name: "Saving…" }).disabled).toBe(true);
  expect(screen.getByRole<HTMLInputElement>("textbox", { name: "Draft" }).value).toBe("edited draft");
  fireEvent.keyDown(window, { key: "Escape" }); expect(dialog.contains(document.activeElement)).toBe(true);
  await act(async () => { finish(); await response; });
  expect(dialog.contains(document.activeElement)).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Cancel pending" })); expect(document.activeElement).toBe(invoker);
});
