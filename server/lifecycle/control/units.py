"""Publisher-bound systemd/polkit contents; no application-provided directives."""
from __future__ import annotations


RUNTIME_UNIT = """[Unit]
Description=StreamHome runtime supervisor
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=2

[Service]
Type=notify
NotifyAccess=main
User=streamhome
Group=streamhome
WorkingDirectory=/
ExecStartPre=+/usr/local/libexec/streamhome/provision --before-start
ExecStart=/usr/local/libexec/streamhome/provision --supervisor
ExecStopPost=+/usr/local/libexec/streamhome/provision --unit-stopped
RuntimeDirectory=streamhome
RuntimeDirectoryMode=0700
UMask=0077
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONNOUSERSITE=1
TimeoutStartSec=150
TimeoutStopSec=60
KillMode=mixed
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ProtectControlGroups=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
RestrictSUIDSGID=yes
LockPersonality=yes
CapabilityBoundingSet=
AmbientCapabilities=
ReadWritePaths=/opt/streamhome /etc/streamhome/application /etc/streamhome/domains /var/lib/streamhome /var/cache/streamhome /run/streamhome
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""

OPERATION_UNIT = """[Unit]
Description=StreamHome operation %i
Wants=network-online.target
After=network-online.target
StopPropagatedFrom=streamhome.service

[Service]
Type=exec
User=streamhome
Group=streamhome
Slice=system.slice
WorkingDirectory=/
ExecStart=/usr/local/libexec/streamhome/provision --operation %i
UMask=0077
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONNOUSERSITE=1
TimeoutStartSec=30
TimeoutStopSec=15
RuntimeMaxSec=3600
KillMode=control-group
Restart=no
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ProtectControlGroups=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
RestrictSUIDSGID=yes
LockPersonality=yes
CapabilityBoundingSet=
AmbientCapabilities=
ReadWritePaths=/opt/streamhome /etc/streamhome/application /etc/streamhome/domains /var/lib/streamhome /var/cache/streamhome -/run/streamhome
StandardOutput=journal
StandardError=journal
"""

# Preparation has its own lifetime and cgroup. Losing the initiating terminal
# cannot leave an untracked package-manager child or start a second builder.
PROVISION_UNIT = OPERATION_UNIT.replace(
    "Description=StreamHome operation %i", "Description=StreamHome first-install preparation %i",
).replace("--operation %i", "--provision-worker %i")

PREPARE_UNIT = OPERATION_UNIT.replace(
    "Description=StreamHome operation %i", "Description=StreamHome disposable runtime preparation %i",
).replace("--operation %i", "--prepare-runtime %i")

# ExecStartPre schedules the root observer in parallel. Ordering it after the
# notify service would deadlock a saved deployment's guarded initial cutover.
# The runtime's stop propagates to this unit, while an internal child drain does
# not stop the supervisor or its independent observer.
STARTUP_UNIT = """[Unit]
Description=StreamHome saved deployment activation %i
BindsTo=streamhome.service

[Service]
Type=exec
User=root
Group=root
Slice=system.slice
WorkingDirectory=/
ExecStart=/usr/local/libexec/streamhome/provision --activate-pending %i
UMask=0077
TimeoutStartSec=30
TimeoutStopSec=15
RuntimeMaxSec=900
KillMode=control-group
Restart=no
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=strict
ReadWritePaths=/etc/streamhome/control /opt/streamhome /etc/streamhome/application /etc/streamhome/domains /var/lib/streamhome /var/cache/streamhome /run/streamhome
StandardOutput=journal
StandardError=journal
"""

# Starting this one unprivileged template does not authorize an OS effect. Its
# protected dispatcher still requires the exact root-reviewed operation request.
POLKIT_RULE = """polkit.addRule(function(action, subject) {
  if (action.id !== "org.freedesktop.systemd1.manage-units" || subject.user !== "streamhome") return;
  var unit = action.lookup("unit");
  var verb = action.lookup("verb");
  if (verb === "start" && typeof unit === "string" && /^streamhome-operation@[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}\\.service$/.test(unit)) {
    return polkit.Result.YES;
  }
});
"""


def unit_files() -> dict[str, bytes]:
    return {
        "/etc/systemd/system/streamhome.service": RUNTIME_UNIT.encode(),
        "/etc/systemd/system/streamhome-operation@.service": OPERATION_UNIT.encode(),
        "/etc/systemd/system/streamhome-provision@.service": PROVISION_UNIT.encode(),
        "/etc/systemd/system/streamhome-prepare@.service": PREPARE_UNIT.encode(),
        "/etc/systemd/system/streamhome-startup@.service": STARTUP_UNIT.encode(),
        "/etc/polkit-1/rules.d/49-streamhome-operations.rules": POLKIT_RULE.encode(),
    }
