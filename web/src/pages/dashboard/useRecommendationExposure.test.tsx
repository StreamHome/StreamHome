import React from "react";
import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { sendRecommendationExposures } from "../../api/recommendations";
import { useRecommendationExposure } from "./useRecommendationExposure";

vi.mock("../../api/recommendations", () => ({ sendRecommendationExposures: vi.fn() }));

class VisibleObserver {
  constructor(private callback: IntersectionObserverCallback) {}
  observe(target: Element) {
    this.callback([{ isIntersecting: true, intersectionRatio: .5, target } as IntersectionObserverEntry], this as unknown as IntersectionObserver);
  }
  disconnect() {}
  unobserve() {}
  takeRecords() { return []; }
  root = null;
  rootMargin = "0px";
  thresholds = [.5];
}

function ExposureProbe() {
  const ref = useRecommendationExposure({
    profileId: "delivery-profile",
    movie_id: "m_1",
    feed_generation: "feed",
    surface: "home",
    scope: "home",
    category: "recommended",
    position: 0,
    enabled: true,
  });
  return <div ref={ref} />;
}

describe("recommendation exposure delivery", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    window.localStorage.clear();
    vi.stubGlobal("IntersectionObserver", VisibleObserver);
    vi.mocked(sendRecommendationExposures).mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("keeps a failed bounded batch and removes it only after server acknowledgement", async () => {
    vi.mocked(sendRecommendationExposures).mockRejectedValueOnce(new Error("offline")).mockResolvedValue(undefined);
    render(<ExposureProbe />);

    await act(async () => { await vi.advanceTimersByTimeAsync(7000); });
    expect(sendRecommendationExposures).toHaveBeenCalledTimes(1);
    expect(window.localStorage.getItem("streamhome_pending_exposures:delivery-profile")).toContain('"movie_id":"m_1"');

    await act(async () => { await vi.advanceTimersByTimeAsync(16000); });
    expect(sendRecommendationExposures).toHaveBeenCalledTimes(2);
    expect(window.localStorage.getItem("streamhome_pending_exposures:delivery-profile")).toBeNull();
  });
});
