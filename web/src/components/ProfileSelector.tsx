import React, { useState, useEffect } from "react";
import { motion, AnimatePresence } from "motion/react";
import { User, Plus, Pencil, Trash2, X, Check, Lock, Edit3 } from "lucide-react";
import { Profile } from "../types";

interface ProfileSelectorProps {
  profiles: Profile[];
  setProfiles: React.Dispatch<React.SetStateAction<Profile[]>>;
  onSelectProfile: (profile: Profile) => void;
  apiBaseUrl: string;
}

// Beautiful color gradient presets
export const AVATAR_GRADIENTS = [
  "from-blue-600 to-indigo-650",
  "from-red-600 to-rose-650",
  "from-green-500 to-emerald-600",
  "from-amber-500 to-orange-600",
  "from-purple-600 to-pink-650",
  "from-teal-500 to-cyan-600",
  "from-fuchsia-600 to-pink-700",
  "from-zinc-700 to-slate-900"
];

export default function ProfileSelector({ profiles, setProfiles, onSelectProfile, apiBaseUrl }: ProfileSelectorProps) {
  const [isManageMode, setIsManageMode] = useState<boolean>(false);
  const [isCreateModalOpen, setIsCreateModalOpen] = useState<boolean>(false);
  const [isEditModalOpen, setIsEditModalOpen] = useState<boolean>(false);
  
  // Track hovered profile theme to dynamically morph the background & headers (Fix 2: Vibe & Spirit)
  const [hoveredTheme, setHoveredTheme] = useState<string | null>(null);

  // Form states (Create)
  const [newProfileName, setNewProfileName] = useState<string>("");
  const [newAvatarColor, setNewAvatarColor] = useState<string>(AVATAR_GRADIENTS[0]);
  const [newProfileTheme, setNewProfileTheme] = useState<"default" | "netflix" | "prime" | "apple" | "gemini">("netflix");
  const [newPinEnabled, setNewPinEnabled] = useState<boolean>(false);
  const [newPin, setNewPin] = useState<string>("");
  
  // Form states (Edit)
  const [editingProfile, setEditingProfile] = useState<Profile | null>(null);
  const [editProfileName, setEditProfileName] = useState<string>("");
  const [editAvatarColor, setEditAvatarColor] = useState<string>("");
  const [editProfileTheme, setEditProfileTheme] = useState<"default" | "netflix" | "prime" | "apple" | "gemini">("netflix");
  const [editPinEnabled, setEditPinEnabled] = useState<boolean>(false);
  const [editPin, setEditPin] = useState<string>("");

  // PIN Overlay state
  const [pinProfile, setPinProfile] = useState<Profile | null>(null);
  const [pinInput, setPinInput] = useState<string>("");
  const [pinError, setPinError] = useState<string>("");
  const [isPinOverlayOpen, setIsPinOverlayOpen] = useState<boolean>(false);

  const handleProfileSelect = (profile: Profile) => {
    if (profile.pinEnabled && profile.pin) {
      setPinProfile(profile);
      setPinInput("");
      setPinError("");
      setIsPinOverlayOpen(true);
    } else {
      onSelectProfile(profile);
    }
  };

  const handlePinKeyPress = (val: string) => {
    setPinError("");
    if (val === "backspace") {
      setPinInput((prev) => prev.slice(0, -1));
    } else {
      if (pinInput.length >= 4) return;
      const nextInput = pinInput + val;
      setPinInput(nextInput);
      
      if (nextInput.length === 4) {
        if (pinProfile && nextInput === pinProfile.pin) {
          setTimeout(() => {
            onSelectProfile(pinProfile);
            setIsPinOverlayOpen(false);
            setPinProfile(null);
            setPinInput("");
          }, 150);
        } else {
          setTimeout(() => {
            setPinError("Incorrect PIN. Please try again.");
            setPinInput("");
          }, 200);
        }
      }
    }
  };

  const handleCreateProfile = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newProfileName.trim()) return;
    if (newPinEnabled && newPin.length !== 4) {
      alert("Please enter a valid 4-digit security PIN");
      return;
    }

    const newProfile: Profile = {
      id: Date.now().toString(),
      name: newProfileName.trim(),
      avatarColor: newAvatarColor,
      theme: newProfileTheme,
      pinEnabled: newPinEnabled,
      pin: newPinEnabled ? newPin : undefined,
    };

    try {
      const token = localStorage.getItem("stream_access_token");
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      if (token) headers["Authorization"] = `Bearer ${token}`;
      const response = await fetch(`${apiBaseUrl}/profiles`, {
        method: "POST",
        headers: headers,
        body: JSON.stringify(newProfile),
      });
      if (response.ok) {
        const savedProfile = await response.json();
        setProfiles((prev) => [...prev, savedProfile]);
      } else {
        console.warn("Failed to create profile on database, status:", response.status);
        setProfiles((prev) => [...prev, newProfile]);
      }
    } catch (err) {
      console.warn("Failed to create profile on database:", err);
      setProfiles((prev) => [...prev, newProfile]);
    }

    setNewProfileName("");
    setNewAvatarColor(AVATAR_GRADIENTS[Math.floor(Math.random() * AVATAR_GRADIENTS.length)]);
    setNewProfileTheme("netflix");
    setNewPinEnabled(false);
    setNewPin("");
    setIsCreateModalOpen(false);
  };

  const handleSaveEditProfile = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!editingProfile || !editProfileName.trim()) return;
    if (editPinEnabled && editPin.length !== 4) {
      alert("Please enter a valid 4-digit security PIN");
      return;
    }

    const updatedProfile: Profile = {
      ...editingProfile,
      name: editProfileName.trim(),
      avatarColor: editAvatarColor,
      theme: editProfileTheme,
      pinEnabled: editPinEnabled,
      pin: editPinEnabled ? editPin : undefined,
    };

    try {
      const token = localStorage.getItem("stream_access_token");
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      if (token) headers["Authorization"] = `Bearer ${token}`;
      const response = await fetch(`${apiBaseUrl}/profiles`, {
        method: "POST",
        headers: headers,
        body: JSON.stringify(updatedProfile),
      });
      if (response.ok) {
        const savedProfile = await response.json();
        setProfiles((prev) =>
          prev.map((p) => (p.id === editingProfile.id ? savedProfile : p))
        );
      } else {
        console.warn("Failed to update profile on database, status:", response.status);
        setProfiles((prev) =>
          prev.map((p) => (p.id === editingProfile.id ? updatedProfile : p))
        );
      }
    } catch (err) {
      console.warn("Failed to update profile on database:", err);
      setProfiles((prev) =>
        prev.map((p) => (p.id === editingProfile.id ? updatedProfile : p))
      );
    }
    setEditingProfile(null);
    setIsEditModalOpen(false);
  };

  const handleDeleteProfile = async (profileId: string) => {
    if (confirm("Are you sure you want to delete this profile? All saved progress will be removed.")) {
      try {
        const token = localStorage.getItem("stream_access_token");
        const headers: Record<string, string> = {};
        if (token) headers["Authorization"] = `Bearer ${token}`;
        const response = await fetch(`${apiBaseUrl}/profiles/${profileId}`, {
          method: "DELETE",
          headers: headers,
        });
        if (response.ok) {
          setProfiles((prev) => prev.filter((p) => p.id !== profileId));
        } else {
          console.warn("Failed to delete profile from database, status:", response.status);
          setProfiles((prev) => prev.filter((p) => p.id !== profileId));
        }
      } catch (err) {
        console.warn("Failed to delete profile from database:", err);
        setProfiles((prev) => prev.filter((p) => p.id !== profileId));
      }
      if (editingProfile?.id === profileId) {
        setEditingProfile(null);
        setIsEditModalOpen(false);
      }
    }
  };

  const openEditModal = (profile: Profile) => {
    setEditingProfile(profile);
    setEditProfileName(profile.name);
    setEditAvatarColor(profile.avatarColor);
    setEditProfileTheme(profile.theme || "netflix");
    setEditPinEnabled(!!profile.pinEnabled);
    setEditPin(profile.pin || "");
    setIsEditModalOpen(true);
  };

  // Determine active background styles based on hovered theme
  const getBackgroundStyles = () => {
    switch (hoveredTheme) {
      case "prime":
        return "bg-[#0b0e17] text-[#00a8e1] transition-all duration-700";
      case "apple":
        return "bg-gradient-to-br from-[#121212] via-[#09090a] to-[#1a1a1c] text-white transition-all duration-700";
      case "gemini":
        return "bg-[#040508] text-[#a5f3fc] transition-all duration-700";
      case "netflix":
      default:
        return "bg-[#000000] text-white transition-all duration-700";
    }
  };

  // Determine dynamic heading style based on hovered theme
  const renderHeading = () => {
    const titleText = isManageMode ? "Manage Profiles" : "Who's watching?";
    
    switch (hoveredTheme) {
      case "prime":
        return (
          <h1 className="text-4xl md:text-5xl font-bold tracking-tight text-white font-sans text-center transition-all duration-500">
            {titleText}
          </h1>
        );
      case "apple":
        return (
          <h1 className="text-4xl md:text-5xl font-light tracking-wide text-white font-serif text-center transition-all duration-500">
            {titleText}
          </h1>
        );
      case "gemini":
        return (
          <h1 className="text-3xl md:text-4xl font-black tracking-widest text-[#a855f7] font-mono text-center transition-all duration-500 uppercase">
            {isManageMode ? "> MANAGE_PROFILES_" : "> SELECT_USER_"}
          </h1>
        );
      case "netflix":
      default:
        return (
          <h1 className="text-4.5xl md:text-5xl font-sans font-medium tracking-tight text-white text-center transition-all duration-500">
            {titleText}
          </h1>
        );
    }
  };

  return (
    <div 
      id="profile-selection-screen" 
      className={`flex flex-col items-center justify-center min-h-screen px-4 py-12 relative overflow-hidden ${getBackgroundStyles()}`}
    >
      {/* Tech Grid Background (Rendered only during Gemini hover state) */}
      <AnimatePresence>
        {hoveredTheme === "gemini" && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 0.15 }}
            exit={{ opacity: 0 }}
            className="absolute inset-0 pointer-events-none bg-[linear-gradient(rgba(18,16,16,0)_50%,rgba(0,0,0,0.25)_50%),linear-gradient(90deg,rgba(255,0,0,0.06),rgba(0,255,0,0.02),rgba(0,0,255,0.06))] bg-[size:100%_4px,3px_100%] z-0"
          />
        )}
      </AnimatePresence>

      <motion.div
        initial={{ opacity: 0, y: -20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.6 }}
        className="text-center mb-12 z-10"
      >
        {renderHeading()}
        <p className={`mt-3 text-sm md:text-base transition-colors duration-500 ${
          hoveredTheme === "gemini" ? "text-cyan-400 font-mono text-xs uppercase" :
          hoveredTheme === "apple" ? "text-zinc-400 font-serif italic" :
          hoveredTheme === "prime" ? "text-cyan-300/80 font-sans" : "text-zinc-400 font-sans"
        }`}>
          {isManageMode 
            ? "Select a profile to edit its name, avatar, or delete it." 
            : "Select your profile to start streaming."}
        </p>
      </motion.div>

      <div 
        className="flex flex-wrap justify-center gap-6 sm:gap-8 max-w-5xl w-full px-4 z-10" 
        id="profile-list"
      >
        {profiles.map((profile, idx) => {
          const theme = profile.theme || "netflix";
          
          return (
            <motion.div
              key={profile.id}
              initial={{ opacity: 0, scale: 0.9 }}
              animate={{ opacity: 1, scale: 1 }}
              transition={{ duration: 0.4, delay: idx * 0.08 }}
              onMouseEnter={() => setHoveredTheme(theme)}
              onMouseLeave={() => setHoveredTheme(null)}
              onClick={() => {
                if (isManageMode) {
                  openEditModal(profile);
                } else {
                  handleProfileSelect(profile);
                }
              }}
              className="flex flex-col items-center group cursor-pointer relative"
              id={`profile-card-${profile.id}`}
            >
              
              {/* Dynamic Theme Shape and Border Wrapper */}
              {theme === "gemini" ? (
                /* Gemini: Circular glyph with spin laser ring on hover */
                <div className="relative p-[4px] rounded-full group-hover:scale-105 transition-transform duration-300">
                  <div 
                    className="absolute inset-0 rounded-full bg-gradient-to-tr from-purple-500 via-pink-500 to-cyan-400 animate-spin opacity-0 group-hover:opacity-100 transition-opacity duration-300"
                    style={{ animationDuration: '3s' }}
                  />
                  <div className="relative w-24 h-24 sm:w-28 sm:h-28 rounded-full bg-zinc-950 flex items-center justify-center overflow-hidden border border-zinc-800/80 shadow-[0_0_15px_rgba(0,0,0,0.5)]">
                    <div className={`w-full h-full bg-gradient-to-tr ${profile.avatarColor} flex items-center justify-center opacity-85 group-hover:opacity-100 transition-opacity`}>
                      <span className="text-white font-mono font-black text-3xl">{profile.name[0]}</span>
                    </div>
                  </div>
                </div>
              ) : theme === "apple" ? (
                /* Apple: Ultra-rounded squircles (smooth apple corners) with glassmorphism and glow */
                <div className="w-24 h-24 sm:w-28 sm:h-28 rounded-[28px] p-[1.5px] bg-white/10 group-hover:bg-white/20 group-hover:scale-105 group-hover:shadow-[0_0_30px_rgba(255,255,255,0.18)] transition-all duration-550 shadow-2xl relative overflow-hidden">
                  <div className={`w-full h-full rounded-[26px] bg-gradient-to-tr ${profile.avatarColor} flex items-center justify-center`}>
                    <span className="text-white font-serif font-light text-3xl">{profile.name[0]}</span>
                  </div>
                </div>
              ) : theme === "prime" ? (
                /* Prime Video: Circular profiles inside a soft slate-blue container */
                <div className="w-24 h-24 sm:w-28 sm:h-28 rounded-full bg-slate-900 border-2 border-slate-700/80 group-hover:border-cyan-400 group-hover:scale-110 group-hover:shadow-[0_0_20px_rgba(0,168,225,0.4)] transition-all duration-300 shadow-xl relative overflow-hidden p-1.5">
                  <div className={`w-full h-full rounded-full bg-gradient-to-tr ${profile.avatarColor} flex items-center justify-center`}>
                    <span className="text-white font-sans font-bold text-3xl">{profile.name[0]}</span>
                  </div>
                </div>
              ) : (
                /* Default & Netflix: Flat, sharp-edged colored squares with simple border outline */
                <div className="w-24 h-24 sm:w-28 sm:h-28 rounded-none border-4 border-transparent group-hover:border-white group-hover:scale-102 transition-all duration-200 shadow-2xl relative overflow-hidden">
                  <div className={`w-full h-full bg-gradient-to-tr ${profile.avatarColor} flex items-center justify-center`}>
                    <span className="text-white font-sans font-black text-3xl">{profile.name[0]}</span>
                  </div>
                </div>
              )}

              {/* Profile PIN Lock Icon indicator */}
              {profile.pinEnabled && (
                <div className={`absolute top-1.5 right-1.5 p-1 rounded-full text-white bg-black/60 shadow-lg`}>
                  <Lock className="w-3 h-3 text-amber-500" />
                </div>
              )}

              {/* Manage Mode Overlay Icon */}
              {isManageMode && (
                <div className={`absolute inset-0 flex items-center justify-center bg-black/60 backdrop-blur-[2px] transition-opacity duration-300 ${
                  theme === "gemini" ? "rounded-full" : 
                  theme === "apple" ? "rounded-[28px]" : 
                  theme === "prime" ? "rounded-full" : "rounded-none"
                }`}>
                  <Edit3 className="w-6 h-6 text-white bg-zinc-800/80 p-1.5 rounded-full hover:bg-[var(--theme-accent)] transition" />
                </div>
              )}

              {/* Profile Name & Label underneath */}
              <div className="mt-4 flex items-center space-x-1.5">
                <span className={`font-semibold text-sm sm:text-base transition-colors duration-200 ${
                  theme === "gemini" ? "text-cyan-400 group-hover:text-white font-mono" :
                  theme === "apple" ? "text-zinc-200 group-hover:text-white font-serif font-light" :
                  theme === "prime" ? "text-zinc-300 group-hover:text-cyan-400 font-sans" :
                  "text-zinc-400 group-hover:text-white font-sans"
                }`}>
                  {profile.name}
                </span>
                
                {isManageMode && (
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      handleDeleteProfile(profile.id);
                    }}
                    className="p-1 text-zinc-500 hover:text-red-500 transition-colors"
                    title="Delete Profile"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                )}
              </div>
              
              {/* Theme choice label badge */}
              <span className={`text-[9px] uppercase tracking-wider mt-0.5 ${
                theme === "gemini" ? "text-[#a855f7]/85 font-mono" :
                theme === "apple" ? "text-zinc-500 font-serif italic" :
                theme === "prime" ? "text-cyan-600 font-sans" : "text-zinc-500 font-sans"
              }`}>
                {theme === "default" ? "Default" : theme}
              </span>

            </motion.div>
          );
        })}

        {/* Dynamic add profile button */}
        {profiles.length < 5 && (
          <motion.div
            initial={{ opacity: 0, scale: 0.9 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={{ duration: 0.4, delay: profiles.length * 0.08 }}
            whileHover={{ scale: 1.05 }}
            onClick={() => setIsCreateModalOpen(true)}
            className="flex flex-col items-center group cursor-pointer"
            id="profile-card-add"
          >
            {hoveredTheme === "gemini" ? (
              <div className="w-24 h-24 sm:w-28 sm:h-28 rounded-full bg-zinc-950 border-2 border-dashed border-cyan-500/50 flex items-center justify-center transition-all duration-300">
                <Plus className="w-8 h-8 text-cyan-500 group-hover:text-cyan-400" />
              </div>
            ) : hoveredTheme === "apple" ? (
              <div className="w-24 h-24 sm:w-28 sm:h-28 rounded-[28px] bg-white/5 border border-dashed border-white/20 flex items-center justify-center transition-all duration-300 backdrop-blur-md">
                <Plus className="w-8 h-8 text-zinc-400 group-hover:text-white" />
              </div>
            ) : hoveredTheme === "prime" ? (
              <div className="w-24 h-24 sm:w-28 sm:h-28 rounded-full bg-slate-900 border-2 border-dashed border-slate-700 flex items-center justify-center transition-all duration-300">
                <Plus className="w-8 h-8 text-cyan-600 group-hover:text-cyan-400" />
              </div>
            ) : (
              <div className="w-24 h-24 sm:w-28 sm:h-28 rounded-none bg-zinc-950 border-4 border-dashed border-zinc-700 hover:border-zinc-500 flex items-center justify-center transition-all duration-300 relative">
                <Plus className="w-8 h-8 text-zinc-600 group-hover:text-zinc-400" />
              </div>
            )}
            
            <span className={`mt-4 font-semibold text-sm sm:text-base ${
              hoveredTheme === "gemini" ? "text-cyan-500 group-hover:text-white font-mono" :
              hoveredTheme === "apple" ? "text-zinc-400 group-hover:text-white font-serif font-light" :
              hoveredTheme === "prime" ? "text-zinc-400 group-hover:text-cyan-400" :
              "text-zinc-500 group-hover:text-zinc-300"
            }`}>
              Add Profile
            </span>
          </motion.div>
        )}
      </div>

      <div className="flex gap-4 mt-16 z-10">
        <motion.button
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ delay: 0.5 }}
          onClick={() => setIsManageMode(!isManageMode)}
          className={`px-8 py-2.5 border uppercase tracking-widest transition-all duration-200 text-xs sm:text-sm font-bold ${
            isManageMode 
              ? "border-red-650 text-red-500 hover:bg-red-600/10 rounded-md" 
              : hoveredTheme === "gemini" ? "border-cyan-500 text-cyan-400 hover:bg-cyan-950/20 font-mono rounded-none" :
                hoveredTheme === "apple" ? "border-white/20 text-zinc-300 hover:text-white hover:border-white rounded-full bg-white/5 backdrop-blur-md font-serif" :
                hoveredTheme === "prime" ? "border-cyan-400 text-cyan-400 hover:bg-cyan-550/10 rounded-lg" :
                "border-zinc-700 text-zinc-400 hover:text-white hover:border-white rounded-none"
          }`}
          id="manage-profiles-button"
        >
          {isManageMode ? "Finish Editing" : "Manage Profiles"}
        </motion.button>
      </div>

      {/* CREATE NEW PROFILE DIALOG */}
      <AnimatePresence>
        {isCreateModalOpen && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/85 backdrop-blur-md p-4 overflow-y-auto">
            <motion.div
              initial={{ opacity: 0, scale: 0.95, y: 20 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.95, y: 20 }}
              className="bg-[#111113] border border-zinc-800 rounded-xl p-6 w-full max-w-lg shadow-2xl relative my-8"
            >
              <button 
                onClick={() => setIsCreateModalOpen(false)}
                className="absolute top-4 right-4 text-zinc-400 hover:text-white transition"
              >
                <X className="w-5 h-5" />
              </button>

              <h2 className="text-xl font-bold mb-4 uppercase tracking-tight text-white border-b border-zinc-800 pb-2">Create Profile</h2>
              
              <form onSubmit={handleCreateProfile} className="space-y-5">
                <div className="space-y-2">
                  <label className="text-xs uppercase font-extrabold text-zinc-400 tracking-wider">Profile Name</label>
                  <input
                    type="text"
                    placeholder="Enter profile name..."
                    required
                    value={newProfileName}
                    onChange={(e) => setNewProfileName(e.target.value)}
                    className="w-full bg-zinc-900 border border-zinc-700 rounded p-2.5 text-white text-sm focus:border-[var(--theme-accent)] outline-none transition"
                    maxLength={15}
                  />
                </div>

                <div className="space-y-2">
                  <label className="text-xs uppercase font-extrabold text-zinc-400 tracking-wider">Select Avatar Color</label>
                  <div className="grid grid-cols-4 gap-2">
                    {AVATAR_GRADIENTS.map((gradient) => (
                      <button
                        key={gradient}
                        type="button"
                        onClick={() => setNewAvatarColor(gradient)}
                        className={`aspect-square rounded-lg bg-gradient-to-tr ${gradient} relative flex items-center justify-center border-2 ${
                          newAvatarColor === gradient ? "border-white scale-105 shadow-lg" : "border-transparent"
                        } transition hover:scale-105`}
                      >
                        {newAvatarColor === gradient && <Check className="w-5 h-5 text-white" />}
                      </button>
                    ))}
                  </div>
                </div>

                {/* Profile Theme Selector */}
                <div className="space-y-2">
                  <label className="text-xs uppercase font-extrabold text-zinc-400 tracking-wider">Interface Theme</label>
                  <div className="grid grid-cols-2 gap-2">
                    {[
                      { id: "default", name: "Default (Netflix)", desc: "Crimson Red & Sharp Blocks", color: "bg-red-600" },
                      { id: "netflix", name: "Netflix Signature", desc: "Flat Red Theme", color: "bg-[#E50914]" },
                      { id: "prime", name: "Prime Video", desc: "Electric Blue & Navy", color: "bg-[#00a8e1]" },
                      { id: "apple", name: "Apple TV+", desc: "Minimalist Frost & Serif", color: "bg-zinc-200" },
                      { id: "gemini", name: "Gemini Console", desc: "Cyberpunk Glow & Mono", color: "bg-purple-500" },
                    ].map((themeOpt) => (
                      <button
                        key={themeOpt.id}
                        type="button"
                        onClick={() => setNewProfileTheme(themeOpt.id as any)}
                        className={`flex items-center space-x-2.5 p-2 rounded-lg border-2 text-left bg-zinc-900/60 transition hover:border-zinc-500 ${
                          newProfileTheme === themeOpt.id ? "border-[var(--theme-accent)] bg-zinc-900" : "border-zinc-800"
                        }`}
                      >
                        <div className={`w-3.5 h-3.5 rounded-full flex-shrink-0 ${themeOpt.color}`} />
                        <div className="min-w-0">
                          <p className="text-xs font-bold text-white leading-none mb-0.5">{themeOpt.name}</p>
                          <p className="text-[9px] text-zinc-500 leading-none truncate">{themeOpt.desc}</p>
                        </div>
                      </button>
                    ))}
                  </div>
                </div>

                {/* Profile Security Lock Setup */}
                <div className="space-y-3 bg-zinc-900/50 p-3 rounded-lg border border-zinc-800">
                  <div className="flex items-center justify-between">
                    <div>
                      <p className="text-xs font-bold text-white">Profile Security Lock (PIN)</p>
                      <p className="text-[10px] text-zinc-400">Require a 4-digit PIN to access this profile</p>
                    </div>
                    <label className="relative inline-flex items-center cursor-pointer">
                      <input
                        type="checkbox"
                        checked={newPinEnabled}
                        onChange={(e) => {
                          setNewPinEnabled(e.target.checked);
                          if (!e.target.checked) setNewPin("");
                        }}
                        className="sr-only peer"
                      />
                      <div className="w-9 h-5 bg-zinc-800 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-zinc-500 rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-[var(--theme-accent)]"></div>
                    </label>
                  </div>
                  
                  {newPinEnabled && (
                    <div className="space-y-1.5 pt-1">
                      <p className="text-[10px] uppercase font-bold text-zinc-400">Set 4-Digit Security PIN</p>
                      <input
                        type="password"
                        pattern="[0-9]*"
                        inputMode="numeric"
                        maxLength={4}
                        placeholder="e.g. 1234"
                        required={newPinEnabled}
                        value={newPin}
                        onChange={(e) => {
                          const val = e.target.value.replace(/[^0-9]/g, "");
                          setNewPin(val);
                        }}
                        className="w-full bg-zinc-950 border border-zinc-800 rounded p-2 text-white text-center tracking-widest font-mono text-lg focus:border-[var(--theme-accent)] outline-none transition"
                      />
                    </div>
                  )}
                </div>

                <div className="pt-2 flex gap-3">
                  <button
                    type="button"
                    onClick={() => setIsCreateModalOpen(false)}
                    className="w-1/2 bg-zinc-900 hover:bg-zinc-800 text-zinc-350 font-semibold py-2.5 rounded-lg text-xs uppercase tracking-wider transition border border-zinc-800"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    className="w-1/2 bg-[var(--theme-accent)] hover:bg-[var(--theme-accent-hover)] text-white font-semibold py-2.5 rounded-lg text-xs uppercase tracking-wider transition"
                  >
                    Add Profile
                  </button>
                </div>
              </form>
            </motion.div>
          </div>
        )}
      </AnimatePresence>

      {/* EDIT PROFILE DIALOG */}
      <AnimatePresence>
        {isEditModalOpen && editingProfile && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/85 backdrop-blur-md p-4 overflow-y-auto">
            <motion.div
              initial={{ opacity: 0, scale: 0.95, y: 20 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.95, y: 20 }}
              className="bg-[#111113] border border-zinc-800 rounded-xl p-6 w-full max-w-lg shadow-2xl relative my-8"
            >
              <button 
                onClick={() => { setEditingProfile(null); setIsEditModalOpen(false); }}
                className="absolute top-4 right-4 text-zinc-400 hover:text-white transition"
              >
                <X className="w-5 h-5" />
              </button>

              <h2 className="text-xl font-bold mb-4 uppercase tracking-tight text-white border-b border-zinc-800 pb-2">Edit Profile</h2>
              
              <form onSubmit={handleSaveEditProfile} className="space-y-5">
                <div className="space-y-2">
                  <label className="text-xs uppercase font-extrabold text-zinc-400 tracking-wider">Profile Name</label>
                  <input
                    type="text"
                    required
                    value={editProfileName}
                    onChange={(e) => setEditProfileName(e.target.value)}
                    className="w-full bg-zinc-900 border border-zinc-700 rounded p-2.5 text-white text-sm focus:border-[var(--theme-accent)] outline-none transition"
                    maxLength={15}
                  />
                </div>

                <div className="space-y-2">
                  <label className="text-xs uppercase font-extrabold text-zinc-400 tracking-wider">Select Avatar Color</label>
                  <div className="grid grid-cols-4 gap-2">
                    {AVATAR_GRADIENTS.map((gradient) => (
                      <button
                        key={gradient}
                        type="button"
                        onClick={() => setEditAvatarColor(gradient)}
                        className={`aspect-square rounded-lg bg-gradient-to-tr ${gradient} relative flex items-center justify-center border-2 ${
                          editAvatarColor === gradient ? "border-white scale-105 shadow-lg" : "border-transparent"
                        } transition hover:scale-105`}
                      >
                        {editAvatarColor === gradient && <Check className="w-5 h-5 text-white" />}
                      </button>
                    ))}
                  </div>
                </div>

                {/* Profile Theme Selector */}
                <div className="space-y-2">
                  <label className="text-xs uppercase font-extrabold text-zinc-400 tracking-wider">Interface Theme</label>
                  <div className="grid grid-cols-2 gap-2">
                    {[
                      { id: "default", name: "Default (Netflix)", desc: "Crimson Red & Sharp Blocks", color: "bg-red-650" },
                      { id: "netflix", name: "Netflix Signature", desc: "Flat Red Theme", color: "bg-[#E50914]" },
                      { id: "prime", name: "Prime Video", desc: "Electric Blue & Navy", color: "bg-[#00a8e1]" },
                      { id: "apple", name: "Apple TV+", desc: "Minimalist Frost & Serif", color: "bg-zinc-200" },
                      { id: "gemini", name: "Gemini Console", desc: "Cyberpunk Glow & Mono", color: "bg-purple-500" },
                    ].map((themeOpt) => (
                      <button
                        key={themeOpt.id}
                        type="button"
                        onClick={() => setEditProfileTheme(themeOpt.id as any)}
                        className={`flex items-center space-x-2.5 p-2 rounded-lg border-2 text-left bg-zinc-900/60 transition hover:border-zinc-500 ${
                          editProfileTheme === themeOpt.id ? "border-[var(--theme-accent)] bg-zinc-900" : "border-zinc-800"
                        }`}
                      >
                        <div className={`w-3.5 h-3.5 rounded-full flex-shrink-0 ${themeOpt.color}`} />
                        <div className="min-w-0">
                          <p className="text-xs font-bold text-white leading-none mb-0.5">{themeOpt.name}</p>
                          <p className="text-[9px] text-zinc-500 leading-none truncate">{themeOpt.desc}</p>
                        </div>
                      </button>
                    ))}
                  </div>
                </div>

                {/* Profile Security Lock Setup */}
                <div className="space-y-3 bg-zinc-900/50 p-3 rounded-lg border border-zinc-800">
                  <div className="flex items-center justify-between">
                    <div>
                      <p className="text-xs font-bold text-white">Profile Security Lock (PIN)</p>
                      <p className="text-[10px] text-zinc-400">Require a 4-digit PIN to access this profile</p>
                    </div>
                    <label className="relative inline-flex items-center cursor-pointer">
                      <input
                        type="checkbox"
                        checked={editPinEnabled}
                        onChange={(e) => {
                          setEditPinEnabled(e.target.checked);
                          if (!e.target.checked) setEditPin("");
                        }}
                        className="sr-only peer"
                      />
                      <div className="w-9 h-5 bg-zinc-800 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-zinc-500 rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-[var(--theme-accent)]"></div>
                    </label>
                  </div>
                  
                  {editPinEnabled && (
                    <div className="space-y-1.5 pt-1">
                      <p className="text-[10px] uppercase font-bold text-zinc-400">Set 4-Digit Security PIN</p>
                      <input
                        type="password"
                        pattern="[0-9]*"
                        inputMode="numeric"
                        maxLength={4}
                        placeholder="e.g. 1234"
                        required={editPinEnabled}
                        value={editPin}
                        onChange={(e) => {
                          const val = e.target.value.replace(/[^0-9]/g, "");
                          setEditPin(val);
                        }}
                        className="w-full bg-zinc-950 border border-zinc-800 rounded p-2 text-white text-center tracking-widest font-mono text-lg focus:border-[var(--theme-accent)] outline-none transition"
                      />
                    </div>
                  )}
                </div>

                <div className="pt-2 flex gap-3">
                  <button
                    type="button"
                    onClick={() => { setEditingProfile(null); setIsEditModalOpen(false); }}
                    className="w-1/3 bg-zinc-900 hover:bg-zinc-800 text-zinc-350 font-semibold py-2.5 rounded-lg text-xs uppercase tracking-wider transition border border-zinc-800 flex items-center justify-center"
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    onClick={() => handleDeleteProfile(editingProfile.id)}
                    className="w-1/3 bg-zinc-950 hover:bg-red-950/20 border border-red-950/40 text-red-500 font-semibold py-2.5 rounded-lg text-xs uppercase tracking-wider transition flex items-center justify-center space-x-1"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                    <span>Delete</span>
                  </button>
                  <button
                    type="submit"
                    className="w-1/3 bg-[var(--theme-accent)] hover:bg-[var(--theme-accent-hover)] text-white font-semibold py-2.5 rounded-lg text-xs uppercase tracking-wider transition flex items-center justify-center"
                  >
                    Save
                  </button>
                </div>
              </form>
            </motion.div>
          </div>
        )}
      </AnimatePresence>

      {/* PIN INPUT MODAL OVERLAY */}
      <AnimatePresence>
        {isPinOverlayOpen && pinProfile && (
          <div className="fixed inset-0 z-50 flex flex-col items-center justify-center bg-black/95 backdrop-blur-xl p-4">
            <motion.div
              initial={{ opacity: 0, scale: 0.9, y: 30 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.9, y: 30 }}
              className="flex flex-col items-center max-w-sm w-full text-center"
            >
              {/* Profile Avatar under Lock */}
              <div className={`w-20 h-20 rounded bg-zinc-900 border-2 border-white/20 shadow-2xl relative mb-4 overflow-hidden ${
                pinProfile.theme === "gemini" ? "rounded-full" :
                pinProfile.theme === "apple" ? "rounded-[20px]" :
                pinProfile.theme === "prime" ? "rounded-full" : "rounded-none"
              }`}>
                <div className={`w-full h-full bg-gradient-to-tr ${pinProfile.avatarColor} flex items-center justify-center`}>
                  <span className="text-white font-bold text-2xl">{pinProfile.name[0]}</span>
                </div>
              </div>
              
              <h2 className="text-xl font-bold text-white mb-1">Profile Lock</h2>
              <p className="text-zinc-400 text-sm mb-6">Enter your 4-digit PIN to access {pinProfile.name}.</p>
              
              {/* Visual PIN Dots */}
              <div className="flex space-x-4 mb-8">
                {[0, 1, 2, 3].map((i) => (
                  <div
                    key={i}
                    className={`w-4 h-4 rounded-full border-2 transition-all duration-150 ${
                      pinInput.length > i
                        ? "bg-white border-white scale-110 shadow-[0_0_10px_rgba(255,255,255,0.8)]"
                        : "border-zinc-600 bg-transparent"
                    }`}
                  />
                ))}
              </div>
              
              {/* Error Message */}
              {pinError && (
                <motion.p
                  initial={{ x: -10 }}
                  animate={{ x: [10, -10, 10, -10, 0] }}
                  className="text-red-500 text-sm font-semibold mb-6"
                >
                  {pinError}
                </motion.p>
              )}
              
              {/* Numeric Keypad */}
              <div className="grid grid-cols-3 gap-4 w-full max-w-[280px]">
                {[1, 2, 3, 4, 5, 6, 7, 8, 9].map((num) => (
                  <button
                    key={num}
                    type="button"
                    onClick={() => handlePinKeyPress(num.toString())}
                    className="w-16 h-16 rounded-full bg-zinc-900 border border-zinc-800 hover:bg-zinc-800 active:bg-zinc-850 text-2xl font-bold text-white flex items-center justify-center transition-colors duration-150 shadow-md outline-none cursor-pointer"
                  >
                    {num}
                  </button>
                ))}
                <button
                  type="button"
                  onClick={() => {
                    setPinInput("");
                    setPinError("");
                    setIsPinOverlayOpen(false);
                    setPinProfile(null);
                  }}
                  className="w-16 h-16 rounded-full text-zinc-400 hover:text-white text-sm font-bold flex items-center justify-center transition cursor-pointer"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={() => handlePinKeyPress("0")}
                  className="w-16 h-16 rounded-full bg-zinc-900 border border-zinc-800 hover:bg-zinc-800 active:bg-zinc-850 text-2xl font-bold text-white flex items-center justify-center transition-colors duration-150 shadow-md outline-none cursor-pointer"
                >
                  0
                </button>
                <button
                  type="button"
                  onClick={() => handlePinKeyPress("backspace")}
                  className="w-16 h-16 rounded-full text-zinc-400 hover:text-white text-sm font-bold flex items-center justify-center transition cursor-pointer"
                >
                  Clear
                </button>
              </div>
            </motion.div>
          </div>
        )}
      </AnimatePresence>
    </div>
  );
}
