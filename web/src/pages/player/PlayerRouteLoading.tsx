import { useAppMotion } from "../../motion/motionSystem";
import { useThemeStore } from "../../stores/themeStore";
import { getThemeDefinition } from "../../themes/application/themeRegistry";
import { useMobilePlayerEnvironment } from "./useMobilePlayerEnvironment";

// This small fallback is loaded with the route, before the player/media chunk.
export function PlayerRouteLoading() {
  const theme = useThemeStore((state) => state.activeTheme);
  const { reduced } = useAppMotion();
  const { mobilePlayer, forcedLandscape, stageStyle } = useMobilePlayerEnvironment();
  return <div
    className="player-view player-route-loading"
    data-theme={theme}
    data-player-theme={getThemeDefinition(theme).playerVariant}
    data-mobile-player={mobilePlayer ? "true" : "false"}
    data-mobile-orientation={forcedLandscape ? "forced-landscape" : "native-landscape"}
    style={stageStyle}
    role="status"
    aria-label="Loading player"
  ><span className="player-loading-spinner" data-reduced-motion={reduced} aria-hidden="true" /></div>;
}
