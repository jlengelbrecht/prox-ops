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
%{ if enable_nginx_proxy ~}
  - nginx
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

%{ if wg_forward_port > 0 && !enable_nginx_proxy ~}
      # NAT rules for port forwarding (uses dynamic interface detection for OCI compatibility)
      # OCI VMs use ens3 instead of eth0, so we detect the default interface at runtime
      # NOTE: These rules are SKIPPED when nginx proxy is enabled (nginx handles routing)
      PostUp = IFACE=$(ip route show default | awk '{print $5}' | head -1); iptables -t nat -A PREROUTING -i $IFACE -p tcp --dport ${wg_forward_port} -j DNAT --to-destination ${wg_forward_target_ip}:${wg_forward_port}
      PostUp = IFACE=$(ip route show default | awk '{print $5}' | head -1); iptables -t nat -A PREROUTING -i $IFACE -p udp --dport ${wg_forward_port} -j DNAT --to-destination ${wg_forward_target_ip}:${wg_forward_port}
      PostUp = iptables -t nat -A POSTROUTING -o wg0 -j MASQUERADE
      PostUp = IFACE=$(ip route show default | awk '{print $5}' | head -1); iptables -A FORWARD -i $IFACE -o wg0 -p tcp --dport ${wg_forward_port} -j ACCEPT
      PostUp = IFACE=$(ip route show default | awk '{print $5}' | head -1); iptables -A FORWARD -i $IFACE -o wg0 -p udp --dport ${wg_forward_port} -j ACCEPT
      PostUp = IFACE=$(ip route show default | awk '{print $5}' | head -1); iptables -A FORWARD -i wg0 -o $IFACE -m state --state RELATED,ESTABLISHED -j ACCEPT

      PostDown = IFACE=$(ip route show default | awk '{print $5}' | head -1); iptables -t nat -D PREROUTING -i $IFACE -p tcp --dport ${wg_forward_port} -j DNAT --to-destination ${wg_forward_target_ip}:${wg_forward_port}
      PostDown = IFACE=$(ip route show default | awk '{print $5}' | head -1); iptables -t nat -D PREROUTING -i $IFACE -p udp --dport ${wg_forward_port} -j DNAT --to-destination ${wg_forward_target_ip}:${wg_forward_port}
      PostDown = iptables -t nat -D POSTROUTING -o wg0 -j MASQUERADE
      PostDown = IFACE=$(ip route show default | awk '{print $5}' | head -1); iptables -D FORWARD -i $IFACE -o wg0 -p tcp --dport ${wg_forward_port} -j ACCEPT
      PostDown = IFACE=$(ip route show default | awk '{print $5}' | head -1); iptables -D FORWARD -i $IFACE -o wg0 -p udp --dport ${wg_forward_port} -j ACCEPT
      PostDown = IFACE=$(ip route show default | awk '{print $5}' | head -1); iptables -D FORWARD -i wg0 -o $IFACE -m state --state RELATED,ESTABLISHED -j ACCEPT
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

%{ if enable_nginx_proxy ~}
  # Nginx configuration for Cloudflare reverse proxy
  - path: /etc/nginx/sites-available/plex-proxy
    permissions: '0644'
    content: |
      # Plex reverse proxy with Cloudflare Origin Certificate
      # Traffic flow: Cloudflare (443) -> Nginx (443) -> WireGuard (10.200.200.2:32400)
      server {
          listen 443 ssl http2;
          listen [::]:443 ssl http2;
          server_name ${nginx_server_name};

          # Cloudflare Origin Certificate (15-year validity)
          ssl_certificate /etc/nginx/ssl/cloudflare-origin.pem;
          ssl_certificate_key /etc/nginx/ssl/cloudflare-origin-key.pem;

          # TLS settings optimized for Cloudflare
          ssl_protocols TLSv1.2 TLSv1.3;
          ssl_prefer_server_ciphers off;
          ssl_session_timeout 1d;
          ssl_session_cache shared:SSL:10m;

          # Reverse proxy to Plex via WireGuard tunnel
          location / {
              proxy_pass ${nginx_backend_url};
              proxy_http_version 1.1;

              # WebSocket support (required for Plex)
              proxy_set_header Upgrade $http_upgrade;
              proxy_set_header Connection "upgrade";

              # Standard proxy headers
              proxy_set_header Host $host;
              proxy_set_header X-Real-IP $remote_addr;
              proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
              proxy_set_header X-Forwarded-Proto $scheme;

              # Plex-specific optimizations
              proxy_buffering off;
              proxy_request_buffering off;
              client_max_body_size 100M;

              # Timeouts for long-running streams
              proxy_read_timeout 86400s;
              proxy_send_timeout 86400s;
          }
      }

      # Redirect HTTP to HTTPS
      server {
          listen 80;
          listen [::]:80;
          server_name ${nginx_server_name};
          return 301 https://$host$request_uri;
      }

  # Cloudflare Origin Certificate
  - path: /etc/nginx/ssl/cloudflare-origin.pem
    permissions: '0644'
    content: |
      ${indent(6, nginx_origin_cert)}

  # Cloudflare Origin Certificate Private Key
  - path: /etc/nginx/ssl/cloudflare-origin-key.pem
    permissions: '0600'
    content: |
      ${indent(6, nginx_origin_key)}
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

%{ if enable_nginx_proxy ~}
  # Configure Nginx reverse proxy with error handling
  - mkdir -p /etc/nginx/ssl
  - rm -f /etc/nginx/sites-enabled/default
  - ln -sf /etc/nginx/sites-available/plex-proxy /etc/nginx/sites-enabled/plex-proxy
  - |
    if nginx -t 2>/var/log/nginx-test.log; then
      systemctl restart nginx
      systemctl enable nginx
      echo "Nginx reverse proxy configured for ${nginx_server_name}" >> /var/log/cloud-init-custom.log
    else
      echo "ERROR: Nginx config test failed. Check /var/log/nginx-test.log" >> /var/log/cloud-init-custom.log
      cat /var/log/nginx-test.log >> /var/log/cloud-init-custom.log
    fi
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
      # Enable routing for forwarded traffic (required for NAT/DNAT to work)
      ufw default allow routed
%{ endif ~}
%{ if enable_nginx_proxy ~}
      # Allow HTTPS for nginx reverse proxy (Cloudflare traffic)
      ufw allow 443/tcp
      ufw allow 80/tcp  # For HTTP to HTTPS redirect
%{ endif ~}
%{ if !enable_nginx_proxy ~}
%{ for port in [for p in try(jsondecode("[{\"port\":${wg_forward_port}}]"), []) : p if p.port > 0] ~}
      ufw allow ${port.port}/tcp
      ufw allow ${port.port}/udp
%{ endfor ~}
%{ endif ~}
      ufw --force enable
    fi

  # Log completion
  - echo "Cloud-init complete at $(date)" >> /var/log/cloud-init-custom.log
%{ if enable_wireguard ~}
  - echo "WireGuard configured on wg0" >> /var/log/cloud-init-custom.log
%{ endif ~}

final_message: "Instance ${hostname} is ready!"
