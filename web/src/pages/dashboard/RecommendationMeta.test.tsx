import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Movie } from "../../types/api";
import { AvailabilityBadge, RecommendationReason } from "./RecommendationMeta";

const movie = (availability: Movie["availability"]): Movie => ({
  id: "m_1",
  title: "Example",
  description: "",
  thumbnailUrl: "/poster.jpg",
  bannerUrl: null,
  videoUrl: availability === "available" ? "/media/example.mp4" : "",
  genres: [],
  duration: "",
  releaseYear: 2026,
  rating: null,
  cast: [],
  director: null,
  type: "movie",
  quality: "",
  languages: [],
  subtitles: [],
  voteAverage: 0,
  voteCount: 0,
  skipMarkers: {},
  source: availability === "available" ? "server" : "tmdb_cache",
  availability,
});

describe("AvailabilityBadge", () => {
  it.each([
    ["available", "Stored on StreamHome server", "database"],
    ["cached", "Metadata cached — media unavailable", "cloud"],
    ["processing", "Media processing", "sync"],
  ] as const)("renders %s as an accessible icon without a visible label", (availability, label, icon) => {
    const { container } = render(<AvailabilityBadge movie={movie(availability)} />);
    const badge = screen.getByRole("img", { name: label });
    expect(badge.getAttribute("data-availability")).toBe(availability);
    expect(badge.getAttribute("data-icon")).toBe(icon);
    expect(badge.getAttribute("title")).toBe(label);
    expect(container.querySelector("svg")).not.toBeNull();
    expect(container.textContent).not.toMatch(/server|cache|processing/i);
  });
});

describe("RecommendationReason", () => {
  it("renders structured auteur explanations and preserves fallback text", () => {
    const auteur = movie("available");
    auteur.recommendationReasons = ["Legacy reason"];
    auteur.recommendationReasonDetails = [{ code: "auteur_director", subject: "Guy Ritchie", fallbackText: "Fallback" }];
    const { rerender } = render(<RecommendationReason movie={auteur} />);
    expect(screen.getByText("Because you love Guy Ritchie's directing style.").getAttribute("data-reason-code")).toBe("auteur_director");
    auteur.recommendationReasonDetails = [{ code: "pacing_match", fallbackText: "Based on your preference for fast-paced, witty dialogue." }];
    rerender(<RecommendationReason movie={auteur} />);
    expect(screen.getByText("Based on your preference for fast-paced, witty dialogue.")).not.toBeNull();
  });
});
