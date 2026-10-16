import React from "react";
import type { CatalogController } from "./useCatalogController";

function retryFrom(event: React.MouseEvent<HTMLButtonElement>, retry: () => void) {
  const button = event.currentTarget;
  if (document.activeElement === button) button.closest<HTMLElement>("[data-catalog-region]")?.focus({ preventScroll: true });
  retry();
}

export function CatalogLandingState({ pending, error, view, category, controller }: { pending: boolean; error: string; view: string; category?: string; controller: CatalogController }) {
  const retry = () => { controller.retryCatalog?.(); controller.retryRecommendations(); };
  if (pending) return <div className="catalog-landing-skeleton" role="status" aria-label="Loading titles"><div className="catalog-landing-skeleton__hero" aria-hidden="true"><i /><i /><i /></div><div className="catalog-landing-skeleton__rail" aria-hidden="true">{Array.from({ length: 6 }, (_, index) => <i key={index} />)}</div></div>;
  const filtered = category && !["recommended", "all"].includes(category.toLocaleLowerCase());
  return <div className="catalog-landing-state" role={error ? "alert" : undefined}><h1 tabIndex={-1}>{error ? "Titles unavailable" : filtered ? `No ${category} titles` : view === "movies" ? "No movies yet" : view === "series" ? "No series yet" : "Your library is empty"}</h1><p>{error ? "Titles could not be loaded. Please try again." : filtered ? "No titles match this category." : "Titles will appear here when they are added to your library."}</p>{error && <button type="button" onClick={(event) => retryFrom(event, retry)}>Retry loading titles</button>}</div>;
}

export function CatalogLandingNotice({ controller }: { controller: CatalogController }) {
  if (!controller.recommendationError && !controller.catalogError) return null;
  return <div className="catalog-landing-notice" role="status">{controller.catalogError && <div><span>Your library could not be refreshed.</span><button type="button" onClick={(event) => retryFrom(event, () => controller.retryCatalog?.())}>Retry library</button></div>}{controller.recommendationError && <div><span>Some suggestions could not be updated.</span><button type="button" onClick={(event) => retryFrom(event, controller.retryRecommendations)}>Retry suggestions</button></div>}</div>;
}
