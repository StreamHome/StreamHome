import React from "react";
import { useIsPresent } from "framer-motion";
import type { ThemeNavigationProps, ThemePresentation } from "./contracts";
import { MobileCatalogDock } from "./MobileCatalogDock";

interface CatalogLayoutProps {
  presentation: ThemePresentation;
  navigationProps: ThemeNavigationProps;
  className?: string;
  mainClassName?: string;
  children: React.ReactNode;
}

/** Keep phone navigation in layout while only the catalog content scrolls. */
export function CatalogLayout({ presentation, navigationProps, className = "", mainClassName = "", children }: CatalogLayoutProps) {
  const isPresent = useIsPresent();
  const Background = presentation.Background;
  const Navigation = presentation.Navigation;

  return <div
    className={`theme-app ${presentation.shellClass} catalog-layout ${className}`.trim()}
    data-theme={presentation.id}
    data-interaction={presentation.interaction.id}
    data-view={navigationProps.activeView}
    data-catalog-present={isPresent ? "true" : "false"}
  >
    <Background />
    <Navigation {...navigationProps} />
    <main className={`theme-main ${mainClassName}`.trim()} data-catalog-scroll data-catalog-focus-anchor tabIndex={-1} aria-label="Catalog content">{children}</main>
    {isPresent && <MobileCatalogDock {...navigationProps} />}
  </div>;
}
