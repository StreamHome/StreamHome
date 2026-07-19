import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const read = (path: string) => readFileSync(resolve(path), "utf8");

describe("category renderer parity", () => {
  it("keeps the visual genre gallery reversibly disabled without preparing artwork", () => {
    const features = read("src/pages/dashboard/catalogFeatures.ts");
    const presentation = read("src/pages/dashboard/catalogPresentation.ts");
    const shared = read("src/pages/dashboard/ThemeDashboard.tsx");
    const ember = read("src/pages/dashboard/ember/EmberDashboard.tsx");
    expect(features).toContain("ENABLE_VISUAL_GENRE_CATEGORIES = false");
    expect(presentation).toContain("ENABLE_VISUAL_GENRE_CATEGORIES\n    ? genreCategoryCards");
    expect(shared).toContain("ENABLE_VISUAL_GENRE_CATEGORIES && <GenreCategoryGallery");
    expect(ember).toContain("ENABLE_VISUAL_GENRE_CATEGORIES && <GenreCategoryGallery");
  });

  it("keeps Ember and compatibility themes on the shared behavior model", () => {
    const shared = read("src/pages/dashboard/ThemeDashboard.tsx");
    const ember = read("src/pages/dashboard/ember/EmberDashboard.tsx");
    for (const source of [shared, ember]) {
      expect(source).toContain("buildCatalogPresentation");
      expect(source).toContain("<CategoryFilterRail");
      expect(source).toContain("<GenreCategoryGallery");
      expect(source).toContain("cards={model.genreCards}");
      expect(source).toContain("model.billboardItems");
      expect(source).toContain("model.gridItems");
      expect(source).toContain("model.sections.map");
      expect(source).toContain("preservedCatalogCategory");
      expect(source).toContain("collection.showReasons");
      expect(source).toContain("recommendationHasMore");
      expect(source).toContain("loadMoreRecommendations");
      expect(source).toContain("recommendationError");
      expect(source).toContain("AvailabilityBadge");
    }
  });

  it("defines distinct picker treatments and responsive complete grids", () => {
    const application = read("src/themes/application/application.css");
    const ember = read("src/themes/ember/ember-application.css");
    const interactions = read("src/themes/application/interactions.css");
    for (const theme of ["aurora", "cinema", "gemini"]) expect(application).toContain(`.theme-app--${theme} .category-filter button`);
    for (const theme of ["aurora", "cinema", "gemini"]) expect(application).toContain(`.theme-app--${theme} .genre-category-gallery`);
    expect(ember).toContain(".ember-category-discovery .category-filter button[data-active=\"true\"]");
    expect(ember).toContain(".ember-category-discovery .genre-category-gallery__grid > button");
    expect(application).toContain("scroll-snap-type: x proximity");
    expect(application).toContain(".category-filter::before,.category-filter::after");
    expect(application).toContain(".category-catalog-grid");
    expect(ember).toContain(".ember-category-grid");
    expect(application).toContain(".catalog-feed-status");
    expect(application).toContain(".catalog-load-more");
    expect(application).toContain("data-card-theme=\"glass\"");
    expect(application).toContain("data-card-theme=\"poster\"");
    expect(application).toContain("data-card-theme=\"module\"");
    expect(ember).toContain(".ember-feed-status");
    expect(ember).toContain(".ember-load-more");
    for (const profile of ["terminal", "editorial", "cinematic", "workspace"]) expect(interactions).toContain(`data-interaction=\"${profile}\"] .category-filter button`);
    for (const profile of ["terminal", "editorial", "cinematic", "workspace"]) expect(interactions).toContain(`data-interaction=\"${profile}\"] .genre-category-gallery__grid > button:hover`);
  });

  it("keeps availability signals icon-only and on artwork surfaces", () => {
    const meta = read("src/pages/dashboard/RecommendationMeta.tsx");
    const shared = read("src/pages/dashboard/ThemeDashboard.tsx");
    const emberDashboard = read("src/pages/dashboard/ember/EmberDashboard.tsx");
    const application = read("src/themes/application/application.css");
    const ember = read("src/themes/ember/ember-application.css");
    expect(meta).toContain('data-icon={icon} role="img"');
    expect(meta).toContain("Stored on StreamHome server");
    expect(meta).toContain("Metadata cached — media unavailable");
    expect(shared).toContain('<div className="terminal-feature__art">{art}<AvailabilityBadge');
    expect(shared).toContain('<div className="workspace-art">{art}<AvailabilityBadge');
    expect(emberDashboard).not.toContain("PROFILE FEED ONLINE");
    expect(emberDashboard).toContain('<div className="ember-hero__shade" /><AvailabilityBadge');
    expect(application).toContain(".feature-stage--cinematic > .media-availability");
    expect(ember).toContain(".ember-hero > .media-availability");
  });
});
