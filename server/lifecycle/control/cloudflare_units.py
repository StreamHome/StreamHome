"""Optional StreamHome-owned tunnel units; the core six-unit ABI is unchanged."""

TUNNEL_UNIT='''[Unit]
Description=StreamHome existing Cloudflare tunnel %i
After=network-online.target streamhome.service
Wants=network-online.target
PartOf=streamhome.service
Requisite=streamhome.service

[Service]
Type=exec
DynamicUser=yes
User=streamhome-tunnel
Group=streamhome-tunnel
LoadCredential=tunnel-token:/etc/streamhome/control/cloudflare/connectors/%i/token
ExecStart=/usr/local/libexec/streamhome/cloudflared/2026.10.3/cloudflared tunnel --no-autoupdate --loglevel warn --metrics 127.0.0.1:0 run --token-file %d/tunnel-token
WorkingDirectory=/
UMask=0077
TimeoutStartSec=30
TimeoutStopSec=30
Restart=on-failure
RestartSec=5
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
StandardOutput=journal
StandardError=journal
'''

RECONCILE_UNIT='''[Unit]
Description=StreamHome accepted tunnel configuration
After=streamhome.service
PartOf=streamhome.service
Requisite=streamhome.service

[Service]
Type=oneshot
User=root
Group=root
WorkingDirectory=/
UMask=0077
ExecStart=/usr/local/libexec/streamhome/provision --reconcile-tunnel
TimeoutStartSec=950
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
CapabilityBoundingSet=CAP_SETUID CAP_SETGID
AmbientCapabilities=
ReadWritePaths=/etc/streamhome/control
StandardOutput=journal
StandardError=journal
'''


def unit_files():
    return {'/etc/systemd/system/streamhome-tunnel@.service':TUNNEL_UNIT.encode(),
        '/etc/systemd/system/streamhome-tunnel-reconcile.service':RECONCILE_UNIT.encode()}
