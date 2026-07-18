import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const read = (path: string) => readFileSync(resolve(path), "utf8");

describe("category renderer parity", () => {
  it("keeps Ember and compatibility themes on the shared behavior model", () => {
    const shared = read("src/pages/dashboard/ThemeDashboard.tsx");
    const ember = read("src/pages/dashboard/ember/EmberDashboard.tsx");
    for (const source of [shared, ember]) {
      expect(source).toContain("buildCatalogPresentation");
      expect(source).toContain("<CategoryFilterRail");
      expect(source).toContain("model.billboardItems");
      expect(source).toContain("model.gridItems");
      expect(source).toContain("model.sections.map");
      expect(source).toContain("preservedCatalogCategory");
    }
  });

  it("defines distinct picker treatments and responsive complete grids", () => {
    const application = read("src/themes/application/application.css");
    const ember = read("src/themes/ember/ember-application.css");
    const interactions = read("src/themes/application/interactions.css");
    for (const theme of ["aurora", "cinema", "gemini"]) expect(application).toContain(`.theme-app--${theme} .category-filter button`);
    expect(ember).toContain(".ember-category-discovery .category-filter button[data-active=\"true\"]");
    expect(application).toContain("scroll-snap-type: x proximity");
    expect(application).toContain(".category-filter::before,.category-filter::after");
    expect(application).toContain(".category-catalog-grid");
    expect(ember).toContain(".ember-category-grid");
    for (const profile of ["terminal", "editorial", "cinematic", "workspace"]) expect(interactions).toContain(`data-interaction=\"${profile}\"] .category-filter button`);
  });
});
