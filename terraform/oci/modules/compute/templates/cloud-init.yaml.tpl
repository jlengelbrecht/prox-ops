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
  - fail2ban
%{ if cloudflare_dns_only ~}
  - certbot
  - python3-certbot-dns-cloudflare
%{ endif ~}
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
      # MTU must match K8s WireGuard gateway (1420) - OCI defaults to 8920 from jumbo frames
      MTU = 1420

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
%{ if cloudflare_dns_only ~}
  # Cloudflare API credentials for Let's Encrypt DNS-01 challenge
  - path: /etc/letsencrypt/cloudflare.ini
    permissions: '0600'
    content: |
      dns_cloudflare_api_token = ${cloudflare_api_token}

  # Initial nginx config (HTTP only, for Let's Encrypt to issue cert)
  # After certbot runs, this will be replaced with HTTPS config
  - path: /etc/nginx/sites-available/plex-proxy
    permissions: '0644'
    content: |
      # Temporary HTTP-only config for initial Let's Encrypt cert issuance
      # Certbot will add HTTPS after obtaining certificate

      # Hide NGINX version
      server_tokens off;

      # Default server - reject direct IP access (HTTP)
      server {
          listen 80 default_server;
          listen [::]:80 default_server;
          server_name _;
          return 444;
      }

      server {
          listen 80;
          listen [::]:80;
          server_name ${nginx_server_name};

          # For certbot webroot (not used with DNS challenge, but harmless)
          location /.well-known/acme-challenge/ {
              root /var/www/html;
          }

          # Redirect everything else to HTTPS (once cert is ready)
          location / {
              return 503 "Certificate not yet issued. Please wait.";
          }
      }

  # Full HTTPS nginx config (applied after certbot succeeds)
  - path: /etc/nginx/sites-available/plex-proxy-https
    permissions: '0644'
    content: |
      # Plex reverse proxy with Let's Encrypt certificate
      # DNS-only mode: Traffic goes directly to origin (no Cloudflare proxy)
      # Traffic flow: Client -> Nginx (443) -> WireGuard (10.200.200.2:32400)
      #
      # Security hardening applied:
      # - Direct IP access blocked (default_server returns 444)
      # - NGINX version hidden (server_tokens off)
      # - Security headers (HSTS, X-Frame-Options, etc.)
      # - TLS 1.2/1.3 only with strong ciphers

      # Hide NGINX version in all responses
      server_tokens off;

      # WebSocket connection upgrade mapping
      map $http_upgrade $connection_upgrade {
          default upgrade;
          '' close;
      }

      # Default server - reject direct IP access (HTTPS)
      # Uses self-signed cert just to complete TLS handshake before rejecting
      server {
          listen 443 ssl http2 default_server;
          listen [::]:443 ssl http2 default_server;
          server_name _;

          # Self-signed cert for default_server (generated by cloud-init)
          ssl_certificate /etc/nginx/ssl/selfsigned.crt;
          ssl_certificate_key /etc/nginx/ssl/selfsigned.key;
          ssl_protocols TLSv1.2 TLSv1.3;

          # Reject all requests to IP address
          return 444;
      }

      # Main Plex proxy server
      server {
          listen 443 ssl http2;
          listen [::]:443 ssl http2;
          server_name ${nginx_server_name};

          # Let's Encrypt certificate (auto-renewed by certbot)
          ssl_certificate /etc/letsencrypt/live/${nginx_server_name}/fullchain.pem;
          ssl_certificate_key /etc/letsencrypt/live/${nginx_server_name}/privkey.pem;

          # Modern TLS settings
          ssl_protocols TLSv1.2 TLSv1.3;
          ssl_prefer_server_ciphers off;
          ssl_session_timeout 1d;
          ssl_session_cache shared:SSL:10m;
          ssl_session_tickets off;

          # OCSP Stapling
          ssl_stapling on;
          ssl_stapling_verify on;
          resolver 1.1.1.1 1.0.0.1 valid=300s;
          resolver_timeout 5s;

          # Security headers
          add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;
          add_header X-Content-Type-Options "nosniff" always;
          add_header X-Frame-Options "SAMEORIGIN" always;
          add_header Referrer-Policy "strict-origin-when-cross-origin" always;

          # Send timeout for long-running streams (matches proxy_send_timeout)
          send_timeout 24h;

          # Plex client body size (for uploads)
          client_max_body_size 100M;

          # Reverse proxy to Plex via WireGuard tunnel
          location / {
              proxy_pass ${nginx_backend_url};
              proxy_http_version 1.1;

              # WebSocket support (required for Plex)
              proxy_set_header Upgrade $http_upgrade;
              proxy_set_header Connection $connection_upgrade;

              # Standard proxy headers
              proxy_set_header Host $host;
              proxy_set_header X-Real-IP $remote_addr;
              proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
              proxy_set_header X-Forwarded-Proto $scheme;

              # Rewrite backend HTTP redirects to HTTPS
              # Required because Plex generates http:// URLs when behind a proxy
              proxy_redirect http:// https://;

              # Plex-specific optimizations
              proxy_buffering off;
              proxy_request_buffering off;

              # Timeouts for long-running streams
              proxy_read_timeout 86400s;
              proxy_send_timeout 86400s;
          }
      }

      # Default server - reject direct IP access (HTTP)
      server {
          listen 80 default_server;
          listen [::]:80 default_server;
          server_name _;
          return 444;
      }

      # Redirect HTTP to HTTPS (only for valid hostname)
      server {
          listen 80;
          listen [::]:80;
          server_name ${nginx_server_name};
          return 301 https://$host$request_uri;
      }
%{ else ~}
  # Nginx configuration for Cloudflare proxy mode (Origin Certificate)
  - path: /etc/nginx/sites-available/plex-proxy
    permissions: '0644'
    content: |
      # Plex reverse proxy with Cloudflare Origin Certificate
      # Proxy mode: Traffic flow: Cloudflare (443) -> Nginx (443) -> WireGuard (10.200.200.2:32400)
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
  # Configure Nginx reverse proxy
  - mkdir -p /etc/nginx/ssl
  - mkdir -p /var/www/html

  # Generate self-signed certificate for default_server block (rejects direct IP access)
  - |
    if [ ! -f /etc/nginx/ssl/selfsigned.crt ]; then
      openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
        -keyout /etc/nginx/ssl/selfsigned.key \
        -out /etc/nginx/ssl/selfsigned.crt \
        -subj "/CN=invalid" 2>/dev/null
      echo "Self-signed certificate generated for default_server block" >> /var/log/cloud-init-custom.log
    fi

  - rm -f /etc/nginx/sites-enabled/default
  - ln -sf /etc/nginx/sites-available/plex-proxy /etc/nginx/sites-enabled/plex-proxy
%{ if cloudflare_dns_only ~}
  # DNS-only mode: Use Let's Encrypt with Cloudflare DNS challenge
  - |
    echo "Starting Let's Encrypt certificate issuance..." >> /var/log/cloud-init-custom.log

    # Start nginx with temporary HTTP-only config first
    if nginx -t 2>/var/log/nginx-test.log; then
      systemctl start nginx
      systemctl enable nginx
      echo "Nginx started with temporary HTTP config" >> /var/log/cloud-init-custom.log
    else
      echo "ERROR: Initial nginx config test failed" >> /var/log/cloud-init-custom.log
      cat /var/log/nginx-test.log >> /var/log/cloud-init-custom.log
    fi

    # Issue Let's Encrypt certificate using Cloudflare DNS challenge
    # This works even before DNS is updated since we're using DNS-01 (not HTTP-01)
    echo "Requesting Let's Encrypt certificate for ${nginx_server_name}..." >> /var/log/cloud-init-custom.log

    # Run certbot and capture its exit code properly
    # NOTE: Using a separate command instead of pipeline to avoid $? capturing tee's exit code
    certbot certonly \
      --dns-cloudflare \
      --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
      --dns-cloudflare-propagation-seconds 60 \
      -d ${nginx_server_name} \
      --email ${letsencrypt_email} \
      --agree-tos \
      --non-interactive \
      >> /var/log/certbot.log 2>&1
    CERTBOT_EXIT=$?

    if [ $CERTBOT_EXIT -eq 0 ]; then
      echo "Let's Encrypt certificate issued successfully" >> /var/log/cloud-init-custom.log

      # Switch to HTTPS config
      rm -f /etc/nginx/sites-enabled/plex-proxy
      ln -sf /etc/nginx/sites-available/plex-proxy-https /etc/nginx/sites-enabled/plex-proxy

      # Test and reload nginx with HTTPS config
      if nginx -t 2>/var/log/nginx-test.log; then
        systemctl reload nginx
        echo "Nginx reloaded with Let's Encrypt HTTPS config for ${nginx_server_name}" >> /var/log/cloud-init-custom.log
      else
        echo "ERROR: HTTPS nginx config test failed" >> /var/log/cloud-init-custom.log
        cat /var/log/nginx-test.log >> /var/log/cloud-init-custom.log
      fi
    else
      echo "ERROR: Let's Encrypt certificate issuance failed (exit code: $CERTBOT_EXIT). Check /var/log/certbot.log" >> /var/log/cloud-init-custom.log
      # Log last 20 lines of certbot output for quick debugging
      tail -20 /var/log/certbot.log >> /var/log/cloud-init-custom.log 2>/dev/null || true
    fi

  # Setup automatic certificate renewal
  - echo "0 3 * * * root certbot renew --quiet --post-hook 'systemctl reload nginx'" > /etc/cron.d/certbot-renew
%{ else ~}
  # Proxy mode: Use Cloudflare Origin Certificate
  - |
    if nginx -t 2>/var/log/nginx-test.log; then
      systemctl restart nginx
      systemctl enable nginx
      echo "Nginx reverse proxy configured with Cloudflare Origin cert for ${nginx_server_name}" >> /var/log/cloud-init-custom.log
    else
      echo "ERROR: Nginx config test failed. Check /var/log/nginx-test.log" >> /var/log/cloud-init-custom.log
      cat /var/log/nginx-test.log >> /var/log/cloud-init-custom.log
    fi
%{ endif ~}
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

%{ if enable_nginx_proxy ~}
  # Enable fail2ban for brute-force protection
  - |
    if command -v fail2ban-server &> /dev/null; then
      systemctl enable fail2ban
      systemctl start fail2ban || true
      echo "Fail2ban started for SSH and nginx protection" >> /var/log/cloud-init-custom.log
    fi
%{ endif ~}

final_message: "Instance ${hostname} is ready!"
