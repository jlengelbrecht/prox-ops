#cloud-config
# =============================================================================
# OCI Compute Instance Cloud-Init
# =============================================================================

hostname: ${hostname}

package_update: true
package_upgrade: true

packages:
  - curl
  - jq
  - htop
  - vim
%{ if enable_wireguard ~}
  - wireguard-tools
  - iptables-persistent
  - netfilter-persistent
%{ endif ~}

write_files:
  # Sysctl for IP forwarding
  - path: /etc/sysctl.d/99-forwarding.conf
    permissions: '0644'
    content: |
      net.ipv4.ip_forward = 1
      net.ipv4.conf.all.forwarding = 1

%{ if enable_wireguard ~}
  # WireGuard configuration
  - path: /etc/wireguard/wg0.conf
    permissions: '0600'
    content: |
      [Interface]
      Address = ${wg_address}
      ListenPort = ${wg_listen_port}
      PrivateKey = ${wg_private_key}

%{ if wg_forward_port > 0 ~}
      # NAT rules for port forwarding
      PostUp = iptables -t nat -A PREROUTING -i eth0 -p tcp --dport ${wg_forward_port} -j DNAT --to-destination ${wg_forward_target_ip}:${wg_forward_port}
      PostUp = iptables -t nat -A PREROUTING -i eth0 -p udp --dport ${wg_forward_port} -j DNAT --to-destination ${wg_forward_target_ip}:${wg_forward_port}
      PostUp = iptables -t nat -A POSTROUTING -o wg0 -j MASQUERADE
      PostUp = iptables -A FORWARD -i eth0 -o wg0 -p tcp --dport ${wg_forward_port} -j ACCEPT
      PostUp = iptables -A FORWARD -i eth0 -o wg0 -p udp --dport ${wg_forward_port} -j ACCEPT
      PostUp = iptables -A FORWARD -i wg0 -o eth0 -m state --state RELATED,ESTABLISHED -j ACCEPT

      PostDown = iptables -t nat -D PREROUTING -i eth0 -p tcp --dport ${wg_forward_port} -j DNAT --to-destination ${wg_forward_target_ip}:${wg_forward_port}
      PostDown = iptables -t nat -D PREROUTING -i eth0 -p udp --dport ${wg_forward_port} -j DNAT --to-destination ${wg_forward_target_ip}:${wg_forward_port}
      PostDown = iptables -t nat -D POSTROUTING -o wg0 -j MASQUERADE
      PostDown = iptables -D FORWARD -i eth0 -o wg0 -p tcp --dport ${wg_forward_port} -j ACCEPT
      PostDown = iptables -D FORWARD -i eth0 -o wg0 -p udp --dport ${wg_forward_port} -j ACCEPT
      PostDown = iptables -D FORWARD -i wg0 -o eth0 -m state --state RELATED,ESTABLISHED -j ACCEPT
%{ endif ~}

%{ if wg_peer_public_key != "" ~}
      [Peer]
      PublicKey = ${wg_peer_public_key}
      AllowedIPs = ${wg_peer_allowed_ips}
%{ endif ~}

  # Keepalive script (prevents Oracle idle reclamation)
  - path: /usr/local/bin/keepalive.sh
    permissions: '0755'
    content: |
      #!/bin/bash
      # Generate minimal CPU activity to prevent Oracle idle reclamation
      dd if=/dev/urandom bs=64K count=16 of=/dev/null 2>/dev/null
      # Log tunnel status if WireGuard is enabled
      if command -v wg &> /dev/null && [ -f /etc/wireguard/wg0.conf ]; then
        wg show wg0 2>/dev/null | logger -t wireguard-keepalive
      fi

  # Health check script
  - path: /usr/local/bin/healthcheck.sh
    permissions: '0755'
    content: |
      #!/bin/bash
      LOG_FILE="/var/log/healthcheck.log"

      log() {
        echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> $LOG_FILE
      }

      # Check WireGuard tunnel if enabled
      if [ -f /etc/wireguard/wg0.conf ]; then
        if ! wg show wg0 > /dev/null 2>&1; then
          log "WireGuard tunnel down, restarting..."
          systemctl restart wg-quick@wg0
          exit 1
        fi

        # Check peer connectivity (last handshake within 3 minutes)
        LAST_HANDSHAKE=$(wg show wg0 latest-handshakes 2>/dev/null | awk '{print $2}')
        if [ -n "$LAST_HANDSHAKE" ] && [ "$LAST_HANDSHAKE" != "0" ]; then
          NOW=$(date +%s)
          if [ $((NOW - LAST_HANDSHAKE)) -gt 180 ]; then
            log "WireGuard peer stale (last handshake: $LAST_HANDSHAKE), restarting..."
            systemctl restart wg-quick@wg0
            exit 1
          fi
        fi
      fi

      log "Health check passed"
      exit 0
%{ endif ~}

runcmd:
  # Apply sysctl settings
  - sysctl -p /etc/sysctl.d/99-forwarding.conf

%{ if enable_wireguard ~}
  # Enable and start WireGuard
  - systemctl enable wg-quick@wg0
  - systemctl start wg-quick@wg0 || true

  # Save iptables rules
  - netfilter-persistent save || true

  # Setup keepalive cron (every 5 minutes)
  - echo "*/5 * * * * root /usr/local/bin/keepalive.sh" > /etc/cron.d/keepalive

  # Setup health check cron (every minute)
  - echo "*/1 * * * * root /usr/local/bin/healthcheck.sh" > /etc/cron.d/healthcheck
%{ endif ~}

  # Configure firewall - Oracle Linux has default iptables rules that conflict with UFW
  # These rules include a REJECT that blocks traffic before UFW chains are reached
  - |
    # Flush Oracle's default iptables rules that conflict with UFW
    # Save the current rules first for debugging
    iptables -L -n > /var/log/iptables-before-flush.log 2>&1 || true

    # Flush the filter table (where the problematic REJECT rule lives)
    iptables -F INPUT || true
    iptables -F FORWARD || true

    # Set default policies to ACCEPT (UFW will manage security)
    iptables -P INPUT ACCEPT || true
    iptables -P FORWARD ACCEPT || true
    iptables -P OUTPUT ACCEPT || true

    echo "Flushed Oracle default iptables rules" >> /var/log/cloud-init-custom.log
  - |
    if command -v ufw &> /dev/null; then
      ufw allow 22/tcp
%{ if enable_wireguard ~}
      ufw allow ${wg_listen_port}/udp
%{ endif ~}
%{ for port in [for p in try(jsondecode("[{\"port\":${wg_forward_port}}]"), []) : p if p.port > 0] ~}
      ufw allow ${port.port}/tcp
      ufw allow ${port.port}/udp
%{ endfor ~}
      ufw --force enable
    fi

  # Log completion
  - echo "Cloud-init complete at $(date)" >> /var/log/cloud-init-custom.log
%{ if enable_wireguard ~}
  - echo "WireGuard configured on wg0" >> /var/log/cloud-init-custom.log
%{ endif ~}

final_message: "Instance ${hostname} is ready!"
