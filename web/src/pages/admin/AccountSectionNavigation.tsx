import { useEffect, useRef, useState } from "react";
import "./AccountSectionNavigation.css";

const ACCOUNT_SECTIONS = [
  { id: "account-authenticator", label: "Authenticator & recovery" },
  { id: "account-credentials", label: "Email & password" },
  { id: "account-sessions", label: "Sessions" },
  { id: "account-activity", label: "Security activity" },
] as const;

type AccountSection = typeof ACCOUNT_SECTIONS[number]["id"];

function accountHash(): AccountSection | undefined {
  const hash = window.location.hash.slice(1);
  return ACCOUNT_SECTIONS.find((section) => section.id === hash)?.id;
}

function scrollContainer(element: HTMLElement): HTMLElement | null {
  for (let ancestor = element.parentElement; ancestor; ancestor = ancestor.parentElement) {
    if (ancestor === document.body || ancestor === document.documentElement) break;
    if (/(auto|scroll)/.test(window.getComputedStyle(ancestor).overflowY)
      && ancestor.scrollHeight > ancestor.clientHeight) return ancestor;
  }
  return null;
}

/** This navigation is mounted only alongside the server-approved account content. */
export function AccountSectionNavigation() {
  const navRef = useRef<HTMLElement>(null);
  const [active, setActive] = useState<AccountSection>(() => accountHash() ?? ACCOUNT_SECTIONS[0].id);

  useEffect(() => {
    const nav = navRef.current;
    const workspace = nav?.closest<HTMLElement>(".account-workspace");
    if (!nav || !workspace) return;
    const shell = nav.closest<HTMLElement>(".admin-shell");
    const sections = ACCOUNT_SECTIONS.map(({ id }) => document.getElementById(id)).filter((element): element is HTMLElement => element !== null);
    const previousHeight = workspace.style.getPropertyValue("--account-task-nav-height");
    let frame: number | undefined;

    const measure = () => {
      frame = undefined;
      const navHeight = nav.getBoundingClientRect().height;
      workspace.style.setProperty("--account-task-nav-height", `${navHeight}px`);
      const compact = window.getComputedStyle(nav).getPropertyValue("--account-navigation-compact").trim() === "1";
      const headerOffset = Number.parseFloat(window.getComputedStyle(shell ?? workspace).getPropertyValue("--admin-nav-height")) || 96;
      const container = scrollContainer(nav);
      const containerTop = container ? container.getBoundingClientRect().top + container.clientTop : 0;
      const activationLine = containerTop + headerOffset + (compact ? navHeight + 12 : 12);
      const visibleSections = sections.map((element) => ({ element, rect: element.getBoundingClientRect() })).filter(({ rect }) => rect.height > 0);
      if (!visibleSections.length) return;

      const scroller = container ?? document.scrollingElement ?? document.documentElement;
      const viewportHeight = container ? container.clientHeight : window.innerHeight;
      const scrollTop = container ? container.scrollTop : Math.max(window.scrollY, scroller.scrollTop);
      const maximumScroll = scroller.scrollHeight - viewportHeight;
      // A short final section cannot always reach the activation line before the
      // browser reaches the end. Select it only after a real scroll to the bottom.
      const atBottom = scrollTop > 0 && maximumScroll > 0 && scrollTop >= maximumScroll - 2;
      // Native anchor scrolling can round scrollTop while layout retains subpixel
      // coordinates. Keep the anchored section current across that small mismatch.
      const passedSections = visibleSections.filter(({ rect }) => rect.top <= activationLine + 2);
      const lastPassed = passedSections[passedSections.length - 1];
      const nextSection = lastPassed ? visibleSections[visibleSections.indexOf(lastPassed) + 1] : undefined;
      // When resizing puts the reading line in a card gap, the previous card has
      // finished: the next visible card owns the location. Intervening recovery
      // content belongs to Authenticator & recovery and must not be skipped.
      const inCardGap = lastPassed && nextSection && lastPassed.rect.bottom < activationLine
        && lastPassed.element.nextElementSibling === nextSection.element;
      const current = atBottom ? visibleSections[visibleSections.length - 1]
        : inCardGap ? nextSection : lastPassed ?? visibleSections[0];
      setActive(current.element.id as AccountSection);
    };

    const schedule = () => {
      if (frame === undefined) frame = window.requestAnimationFrame(measure);
    };
    const followHash = () => {
      const target = accountHash();
      if (target) {
        setActive(target);
        // Native anchors keep the query/history intact. This also handles a hash
        // present before the asynchronous account gate mounted its target.
        document.getElementById(target)?.scrollIntoView?.({ block: "start", behavior: "auto" });
      }
      schedule();
    };

    // Capture observes both document scrolling and an overflowing application root.
    window.addEventListener("scroll", schedule, { passive: true, capture: true });
    window.addEventListener("resize", schedule);
    window.addEventListener("hashchange", followHash);
    window.addEventListener("popstate", schedule);
    const resizeObserver = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(schedule);
    [nav, workspace, ...sections, shell?.querySelector<HTMLElement>(".admin-nav")].forEach((element) => {
      if (element) resizeObserver?.observe(element);
    });
    // AdminCenter publishes its measured header height on the shell after resize.
    const shellObserver = shell ? new MutationObserver(schedule) : null;
    if (shell) shellObserver?.observe(shell, { attributes: true, attributeFilter: ["style", "class"] });
    measure();
    followHash();

    return () => {
      window.removeEventListener("scroll", schedule, true);
      window.removeEventListener("resize", schedule);
      window.removeEventListener("hashchange", followHash);
      window.removeEventListener("popstate", schedule);
      resizeObserver?.disconnect();
      shellObserver?.disconnect();
      if (frame !== undefined) window.cancelAnimationFrame(frame);
      if (previousHeight) workspace.style.setProperty("--account-task-nav-height", previousHeight);
      else workspace.style.removeProperty("--account-task-nav-height");
    };
  }, []);

  return <nav ref={navRef} className="account-section-navigation" aria-label="Account tasks">
    <p className="account-section-navigation__label">On this page</p>
    <ul>{ACCOUNT_SECTIONS.map(({ id, label }) => <li key={id}>
      <a href={`#${id}`} aria-current={active === id ? "location" : undefined}>{label}</a>
    </li>)}</ul>
  </nav>;
}
