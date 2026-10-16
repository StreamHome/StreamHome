import type React from "react";
import type { AppQueryState, AppView } from "../../navigation/queryState";
import type { CatalogController } from "../../pages/dashboard/useCatalogController";
import type { Profile } from "../../types/api";
import type { ThemeId } from "../../types/theme";
import type { ThemeMotionDefinition } from "../../motion/motionSystem";

export type MobileNavigationView = "home" | "movies" | "series" | "watchlist" | "downloads" | "search" | "admin";

export interface ThemeNavigationProps {
  profile: Profile;
  activeView: AppView;
  query?: string;
  isAdmin: boolean;
  onView: (view: AppView) => void;
  onSearch: (query: string) => void;
  onProfiles: () => void;
  onEditProfile: () => void;
  onAdmin: () => void;
  onLogout: () => void;
}

export type ThemeInteractionId = "terminal" | "editorial" | "cinematic";

export interface ThemeInteractionProfile {
  id: ThemeInteractionId;
  action: "edge-grow" | "soft-bloom" | "theatrical-grow";
  navigation: "signal-line" | "glass-pill" | "spotlight-underline";
  card: "technical-tilt" | "floating-glass" | "poster-depth";
  rail: "square" | "orb" | "cinema-disc";
  timing: { enterMs: number; exitMs: number; pressMs: number };
  easing: string;
}

export interface ThemePresentation {
  id: ThemeId;
  label: string;
  Background: React.ComponentType;
  Navigation: React.ComponentType<ThemeNavigationProps>;
  shellClass: string;
  heroVariant: "terminal" | "editorial" | "cinematic";
  browseVariant: "technical" | "masonry" | "rails";
  cardVariant: "sharp" | "glass" | "poster";
  detailsVariant: "terminal" | "editorial" | "cinematic";
  playerVariant: "terminal" | "minimal" | "cinematic";
  interaction: ThemeInteractionProfile;
  motion: ThemeMotionDefinition;
}

export interface ThemeApplicationProps {
  query: AppQueryState;
  controller: CatalogController;
  presentation: ThemePresentation;
}
