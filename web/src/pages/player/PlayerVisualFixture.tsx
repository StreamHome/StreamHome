import { useThemeStore } from "../../stores/themeStore";
import { MemoryRouter } from "react-router-dom";

import { useProfileStore } from "../../stores/profileStore";
import type { Profile } from "../../types/api";
import { PlayerPage, type ResolvedPlayback } from "./PlayerPage";


const fixtureProfile: Profile = {
  id: "player-visual-fixture",
  name: "Player Visual Fixture",
  avatarColor: "from-orange-600 to-red-700",
  theme: "ember",
  pinEnabled: false,
};

const fixturePlayback: ResolvedPlayback = {
  asset: {
    id: "m_player_visual_fixture",
    movieId: "m_player_visual_fixture",
    title: "The Nice Guys",
    subtitle: "",
    durationLabel: "12s",
    skipMarkers: {},
  },
  episodeSequence: [],
  runResponse: {
    runId: "player-visual-fixture-run",
    mediaId: "m_player_visual_fixture",
    movieId: "m_player_visual_fixture",
    episodeId: null,
    sourceFingerprint: "visual-fixture-dual-audio-v1",
    resumePosition: 0,
    sourceMetadata: {
      duration: 12,
      container: "mov,mp4,m4a,3gp,3g2,mj2",
      codec: "h264",
      width: 1920,
      height: 1080,
      frameRate: 24,
      sourceFormat: "MP4",
      progressiveCompatible: false,
    },
    tracks: [
      { id: "audio_en", label: "English · 440 Hz", language: "en", channels: 2, default: true, source: "embedded", streamIndex: 0, ready: true, status: "ready" },
      { id: "audio_tr", label: "Turkish · 880 Hz", language: "tr", channels: 2, default: false, source: "external", streamIndex: 0, ready: true, status: "ready" },
    ],
    renditions: [
      { id: "video_540p", label: "540p", height: 540, width: 960, original: true, ready: true, status: "ready" },
    ],
    subtitles: [],
    ticket: "player-visual-fixture-ticket",
    ticketExpiresAt: 4_102_444_800,
    manifestUrl: "/__player-visual-fixture/master.m3u8",
    progressiveUrl: "/__player-visual-fixture.mp4",
    nextEpisodeId: null,
    preparationState: "ready",
    preparationError: null,
    preparationProgress: { stage: "streamable", queuePosition: 0, readySegments: 4, activeWorkers: 0 },
    seekableUntil: 120,
    resumeReady: true,
    switchingReady: true,
    fullyPrepared: true,
    nextSequenceNumber: 1,
  },
};


export function PlayerVisualFixture() {
  const serverFixture = new URLSearchParams(window.location.search).get("fixture") === "server";
  const query = new URLSearchParams(window.location.search);
  const selectedProfile = { ...fixtureProfile, id: serverFixture ? query.get("profile") || "isolated-player-profile" : fixtureProfile.id, theme: query.get("theme") || "ember" };
  useThemeStore.getState().setTheme(selectedProfile.theme);
  const profile = useProfileStore.getState().activeProfile;
  if (profile?.id !== selectedProfile.id || profile.theme !== selectedProfile.theme) {
    useProfileStore.setState({ profiles: [selectedProfile], activeProfile: selectedProfile, isAdmin: false });
  }
  const fixtureState = new URLSearchParams(window.location.search).get("fixtureState");
  const playback = fixtureState === "preparing"
    ? {
        ...fixturePlayback,
        runResponse: {
          ...fixturePlayback.runResponse,
          preparationState: "preparing" as const,
          preparationProgress: { stage: "packaging" as const, queuePosition: 0, readySegments: 2, activeWorkers: 2 },
          seekableUntil: 8,
          resumeReady: false,
          switchingReady: false,
          fullyPrepared: false,
        },
      }
    : query.get("fixture") === "semantic" ? {
        ...fixturePlayback,
        runResponse: { ...fixturePlayback.runResponse, manifestUrl: "/__player-semantic-fixture/master.m3u8", progressiveUrl: "", sourceMetadata: { ...fixturePlayback.runResponse.sourceMetadata, width: 320, height: 180, frameRate: 25 },
          tracks: [
            { id: "audio_embedded_1_2e7314e345_en", label: "English", language: "en", channels: 2, default: true, source: "embedded" as const, streamIndex: 1, ready: true, status: "ready" as const },
            { id: "audio_embedded_2_bfc7290ec7_tr", label: "Turkish", language: "tr", channels: 2, default: false, source: "embedded" as const, streamIndex: 2, ready: true, status: "ready" as const },
          ], renditions: [{ id: "video_original", label: "180p", height: 180, width: 320, original: true, ready: true, status: "ready" as const }, { id: "video_144p", label: "144p", height: 144, width: 256, original: false, ready: true, status: "ready" as const }],
          subtitles: [{ id: "semantic-en", label: "English", language: "en" },{ id: "semantic-tr", label: "Turkish", language: "tr" }],
        },
      } : fixturePlayback;
  return (
    <MemoryRouter initialEntries={[serverFixture ? `/?profile=${encodeURIComponent(selectedProfile.id)}&view=watch&media=${encodeURIComponent(query.get("media") || "m_player_visual_fixture")}` : "/?profile=player-visual-fixture&view=watch&media=m_player_visual_fixture"]}>
      <PlayerPage visualFixture={serverFixture ? undefined : playback} />
    </MemoryRouter>
  );
}
