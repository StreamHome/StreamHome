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
    durationLabel: "1h 44m",
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
      duration: 6240,
      container: "mov,mp4,m4a,3gp,3g2,mj2",
      codec: "h264",
      width: 1920,
      height: 1080,
      frameRate: 24,
      sourceFormat: "MP4",
    },
    tracks: [
      { id: "audio_en", label: "English · 440 Hz", language: "en", channels: 2, default: true, source: "embedded", streamIndex: 0, ready: true, status: "ready" },
      { id: "audio_tr", label: "Turkish · 880 Hz", language: "tr", channels: 2, default: false, source: "external", streamIndex: 0, ready: true, status: "ready" },
    ],
    renditions: [
      { id: "video_original", label: "1080p", height: 1080, width: 1920, original: true, ready: true, status: "ready" },
      { id: "video_720p", label: "720p", height: 720, width: 1280, original: false, ready: true, status: "ready" },
      { id: "video_480p", label: "480p", height: 480, width: 854, original: false, ready: false, status: "preparing" },
      { id: "video_360p", label: "360p", height: 360, width: 640, original: false, ready: false, status: "preparing" },
      { id: "video_240p", label: "240p", height: 240, width: 426, original: false, ready: false, status: "preparing" },
      { id: "video_144p", label: "144p", height: 144, width: 256, original: false, ready: false, status: "preparing" },
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
  const profile = useProfileStore.getState().activeProfile;
  if (profile?.id !== fixtureProfile.id) {
    useProfileStore.setState({ profiles: [fixtureProfile], activeProfile: fixtureProfile, isAdmin: false });
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
    : fixturePlayback;
  return (
    <MemoryRouter initialEntries={["/?profile=player-visual-fixture&view=watch&media=m_player_visual_fixture"]}>
      <PlayerPage visualFixture={playback} />
    </MemoryRouter>
  );
}
