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

      # Map to check if request has valid Plex authentication token
      # Token can be in X-Plex-Token header or query parameter
      # This allows authenticated Plex clients while blocking unauthenticated access
      map $http_x_plex_token $has_plex_token_header {
          ""      0;
          default 1;
      }
      # Query param check using $query_string regex
      # NOTE: nginx $arg_ variables don't work with hyphenated param names like X-Plex-Token
      # The hyphens are NOT converted to underscores for query params (only for headers)
      # So we must use regex on $query_string instead
      map $query_string $has_plex_token_query {
          "~X-Plex-Token=" 1;
          default 0;
      }
      # Combined check - request is authenticated if either token source is present
      map "$has_plex_token_header:$has_plex_token_query" $plex_authenticated {
          "0:0"   0;
          default 1;
      }

      # Map to identify sensitive API endpoints that leak server info
      # These endpoints should only be accessible with authentication
      # Discovered via penetration testing - blocks info disclosure vulnerabilities
      map $uri $is_sensitive_endpoint {
          default 0;
          # Core identity/account endpoints (CRITICAL - expose auth tokens, emails)
          # Use (/|$) suffix to prevent matching unintended paths like /serverside
          ~^/servers(/|$)      1;
          ~^/accounts(/|$)     1;
          ~^/myplex(/|$)       1;
          ~^/identity(/|$)     1;
          # Library/media endpoints (HIGH - expose file paths, media catalog)
          ~^/library(/|$)      1;
          ~^/hubs(/|$)         1;
          ~^/playlists(/|$)    1;
          ~^/channels(/|$)     1;
          ~^/playQueues(/|$)   1;
          # System/status endpoints (MEDIUM - expose server config, capabilities)
          ~^/system(/|$)       1;
          ~^/status(/|$)       1;
          ~^/devices(/|$)      1;
          ~^/clients(/|$)      1;
          ~^/activities(/|$)   1;
          ~^/butler(/|$)       1;
          ~^/updater(/|$)      1;
          ~^/transcode(/|$)    1;
          ~^/photo(/|$)        1;
          ~^/sync(/|$)         1;
          ~^/resources(/|$)    1;
          # Plex internal endpoints (use :/ prefix)
          ~^/:/prefs(/|$)      1;
          ~^/:/plugins(/|$)    1;
          # Media provider info (CRITICAL - exposes email, machine ID)
          ~^/media/providers(/|$)  1;
          # Server logs (potential sensitive info)
          ~^/logs(/|$)         1;
      }

      # Combined check - block if endpoint is sensitive AND request is unauthenticated
      map "$is_sensitive_endpoint:$plex_authenticated" $block_unauthenticated {
          "1:0"   1;
          default 0;
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
          add_header Strict-Transport-Security "max-age=63072000; includeSubDomains" always;
          add_header X-Content-Type-Options "nosniff" always;
          add_header X-Frame-Options "DENY" always;
          add_header Referrer-Policy "strict-origin-when-cross-origin" always;

          # CORS headers - required because we strip Origin header from requests
          # Browser still expects CORS response headers even though Plex won't send them
          # (since Plex sees no Origin header and treats it as a non-CORS request)
          add_header Access-Control-Allow-Origin "*" always;
          add_header Access-Control-Allow-Methods "GET, POST, PUT, DELETE, OPTIONS, HEAD" always;
          add_header Access-Control-Allow-Headers "X-Plex-Token, X-Plex-Client-Identifier, X-Plex-Product, X-Plex-Version, X-Plex-Device, X-Plex-Device-Name, X-Plex-Platform, X-Plex-Platform-Version, Accept, Content-Type, Origin" always;
          add_header Access-Control-Expose-Headers "X-Plex-Protocol" always;

          # Send timeout for long-running streams (matches proxy_send_timeout)
          send_timeout 24h;

          # Plex client body size (for uploads)
          client_max_body_size 100M;

          # Redirect root URL to Plex web UI (hide API XML from browsers)
          # This improves UX for users navigating directly to streaming.homelab0.org
          location = / {
              return 301 /web/;
          }

          # Block sensitive API endpoints that expose server information
          # /identity exposes: machine ID, email, Plex version, server capabilities
          # This is a security risk - unauthenticated users should not see this
          location = /identity {
              return 403 "Forbidden";
          }

          # Reverse proxy to Plex via WireGuard tunnel
          location / {
              # Handle CORS preflight requests
              # Browser sends OPTIONS first to check if CORS is allowed
              if ($request_method = OPTIONS) {
                  add_header Access-Control-Allow-Origin "*";
                  add_header Access-Control-Allow-Methods "GET, POST, PUT, DELETE, OPTIONS, HEAD";
                  add_header Access-Control-Allow-Headers "X-Plex-Token, X-Plex-Client-Identifier, X-Plex-Product, X-Plex-Version, X-Plex-Device, X-Plex-Device-Name, X-Plex-Platform, X-Plex-Platform-Version, Accept, Content-Type, Origin";
                  add_header Access-Control-Max-Age 86400;
                  return 204;
              }

              # Block sensitive API endpoints for unauthenticated requests
              # Prevents leaking: accounts, auth tokens, library contents, server info
              # Authenticated Plex clients pass X-Plex-Token and are allowed through
              if ($block_unauthenticated) {
                  return 403 "Authentication required";
              }

              proxy_pass ${nginx_backend_url};
              proxy_http_version 1.1;

              # WebSocket support (required for Plex)
              proxy_set_header Upgrade $http_upgrade;
              proxy_set_header Connection $connection_upgrade;

              # Standard proxy headers
              # Use 127.0.0.1 for Host to make Plex accept proxied requests
              proxy_set_header Host 127.0.0.1;
              proxy_set_header X-Plex-Client-Identifier $http_x_plex_client_identifier;
              proxy_set_header X-Plex-Token $http_x_plex_token;
              # Don't forward client IP - let Plex see WireGuard IP for allowedNetworks
              proxy_set_header X-Real-IP "";
              proxy_set_header X-Forwarded-For "";
              proxy_set_header X-Forwarded-Proto https;
              # Strip Origin header to disable CORS checks
              # This makes Plex treat requests as direct (non-CORS), allowing
              # allowedNetworks to work for authentication bypass
              proxy_set_header Origin "";

              # Rewrite backend Location headers
              # Plex sends 127.0.0.1 in redirects because Host header is 127.0.0.1
              # Must rewrite to actual hostname for browser redirects to work
              proxy_redirect https://127.0.0.1 https://$host;
              proxy_redirect http://127.0.0.1 https://$host;
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

      # Hide NGINX version in all responses
      server_tokens off;

      # WebSocket connection upgrade mapping
      map $http_upgrade $connection_upgrade {
          default upgrade;
          '' close;
      }

      # Map to check if request has valid Plex authentication token
      map $http_x_plex_token $has_plex_token_header {
          ""      0;
          default 1;
      }
      # Query param check using $query_string regex
      # NOTE: nginx $arg_ variables don't work with hyphenated param names like X-Plex-Token
      # The hyphens are NOT converted to underscores for query params (only for headers)
      # So we must use regex on $query_string instead
      map $query_string $has_plex_token_query {
          "~X-Plex-Token=" 1;
          default 0;
      }
      map "$has_plex_token_header:$has_plex_token_query" $plex_authenticated {
          "0:0"   0;
          default 1;
      }

      # Map to identify sensitive API endpoints that leak server info
      # These endpoints should only be accessible with authentication
      # Discovered via penetration testing - blocks info disclosure vulnerabilities
      map $uri $is_sensitive_endpoint {
          default 0;
          # Core identity/account endpoints (CRITICAL - expose auth tokens, emails)
          # Use (/|$) suffix to prevent matching unintended paths like /serverside
          ~^/servers(/|$)      1;
          ~^/accounts(/|$)     1;
          ~^/myplex(/|$)       1;
          ~^/identity(/|$)     1;
          # Library/media endpoints (HIGH - expose file paths, media catalog)
          ~^/library(/|$)      1;
          ~^/hubs(/|$)         1;
          ~^/playlists(/|$)    1;
          ~^/channels(/|$)     1;
          ~^/playQueues(/|$)   1;
          # System/status endpoints (MEDIUM - expose server config, capabilities)
          ~^/system(/|$)       1;
          ~^/status(/|$)       1;
          ~^/devices(/|$)      1;
          ~^/clients(/|$)      1;
          ~^/activities(/|$)   1;
          ~^/butler(/|$)       1;
          ~^/updater(/|$)      1;
          ~^/transcode(/|$)    1;
          ~^/photo(/|$)        1;
          ~^/sync(/|$)         1;
          ~^/resources(/|$)    1;
          # Plex internal endpoints (use :/ prefix)
          ~^/:/prefs(/|$)      1;
          ~^/:/plugins(/|$)    1;
          # Media provider info (CRITICAL - exposes email, machine ID)
          ~^/media/providers(/|$)  1;
          # Server logs (potential sensitive info)
          ~^/logs(/|$)         1;
      }

      # Combined check - block if endpoint is sensitive AND request is unauthenticated
      map "$is_sensitive_endpoint:$plex_authenticated" $block_unauthenticated {
          "1:0"   1;
          default 0;
      }

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

          # Security headers
          add_header Strict-Transport-Security "max-age=63072000; includeSubDomains" always;
          add_header X-Content-Type-Options "nosniff" always;
          add_header X-Frame-Options "DENY" always;
          add_header Referrer-Policy "strict-origin-when-cross-origin" always;

          # CORS headers - required because we strip Origin header from requests
          # Browser still expects CORS response headers even though Plex won't send them
          add_header Access-Control-Allow-Origin "*" always;
          add_header Access-Control-Allow-Methods "GET, POST, PUT, DELETE, OPTIONS, HEAD" always;
          add_header Access-Control-Allow-Headers "X-Plex-Token, X-Plex-Client-Identifier, X-Plex-Product, X-Plex-Version, X-Plex-Device, X-Plex-Device-Name, X-Plex-Platform, X-Plex-Platform-Version, Accept, Content-Type, Origin" always;
          add_header Access-Control-Expose-Headers "X-Plex-Protocol" always;

          # Send timeout for long-running streams
          send_timeout 24h;

          # Plex client body size (for uploads)
          client_max_body_size 100M;

          # Redirect root URL to Plex web UI (hide API XML from browsers)
          location = / {
              return 301 /web/;
          }

          # Block sensitive API endpoints that expose server information
          # /identity exposes: machine ID, email, Plex version, server capabilities
          location = /identity {
              return 403 "Forbidden";
          }

          # Reverse proxy to Plex via WireGuard tunnel
          location / {
              # Handle CORS preflight requests
              if ($request_method = OPTIONS) {
                  add_header Access-Control-Allow-Origin "*";
                  add_header Access-Control-Allow-Methods "GET, POST, PUT, DELETE, OPTIONS, HEAD";
                  add_header Access-Control-Allow-Headers "X-Plex-Token, X-Plex-Client-Identifier, X-Plex-Product, X-Plex-Version, X-Plex-Device, X-Plex-Device-Name, X-Plex-Platform, X-Plex-Platform-Version, Accept, Content-Type, Origin";
                  add_header Access-Control-Max-Age 86400;
                  return 204;
              }

              # Block sensitive API endpoints for unauthenticated requests
              # Prevents leaking: accounts, auth tokens, library contents, server info
              # Authenticated Plex clients pass X-Plex-Token and are allowed through
              if ($block_unauthenticated) {
                  return 403 "Authentication required";
              }

              proxy_pass ${nginx_backend_url};
              proxy_http_version 1.1;

              # WebSocket support (required for Plex)
              proxy_set_header Upgrade $http_upgrade;
              proxy_set_header Connection $connection_upgrade;

              # Standard proxy headers
              # Use 127.0.0.1 for Host to make Plex accept proxied requests
              proxy_set_header Host 127.0.0.1;
              proxy_set_header X-Plex-Client-Identifier $http_x_plex_client_identifier;
              proxy_set_header X-Plex-Token $http_x_plex_token;
              # Don't forward client IP - let Plex see WireGuard IP for allowedNetworks
              proxy_set_header X-Real-IP "";
              proxy_set_header X-Forwarded-For "";
              proxy_set_header X-Forwarded-Proto https;
              # Strip Origin header to disable CORS checks
              # This makes Plex treat requests as direct (non-CORS), allowing
              # allowedNetworks to work for authentication bypass
              proxy_set_header Origin "";

              # Rewrite backend Location headers
              # Plex sends 127.0.0.1 in redirects because Host header is 127.0.0.1
              proxy_redirect https://127.0.0.1 https://$host;
              proxy_redirect http://127.0.0.1 https://$host;
              proxy_redirect http:// https://;

              # Plex-specific optimizations
              proxy_buffering off;
              proxy_request_buffering off;

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

      # Check for rate limit error and provide helpful guidance
      if grep -q "too many certificates" /var/log/certbot.log 2>/dev/null; then
        RETRY_AFTER=$(grep -oP 'retry after \K[0-9-]+T[0-9:]+Z|retry after \K[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]+' /var/log/certbot.log 2>/dev/null | tail -1)
        echo "" >> /var/log/cloud-init-custom.log
        echo "=== RATE LIMIT DETECTED ===" >> /var/log/cloud-init-custom.log
        echo "Let's Encrypt rate limit hit (5 certs per domain per 168 hours)." >> /var/log/cloud-init-custom.log
        if [ -z "$RETRY_AFTER" ]; then
          echo "Retry after: (timestamp not found - check /var/log/certbot.log)" >> /var/log/cloud-init-custom.log
        else
          echo "Retry after: $RETRY_AFTER" >> /var/log/cloud-init-custom.log
        fi
        echo "" >> /var/log/cloud-init-custom.log
        echo "MANUAL FIX REQUIRED:" >> /var/log/cloud-init-custom.log
        echo "1. SSH to this VPS after the rate limit expires" >> /var/log/cloud-init-custom.log
        echo "2. Run: sudo certbot certonly --dns-cloudflare --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini --dns-cloudflare-propagation-seconds 60 -d ${nginx_server_name} --email ${letsencrypt_email} --agree-tos --non-interactive" >> /var/log/cloud-init-custom.log
        echo "3. Run: sudo rm -f /etc/nginx/sites-enabled/plex-proxy && sudo ln -sf /etc/nginx/sites-available/plex-proxy-https /etc/nginx/sites-enabled/plex-proxy" >> /var/log/cloud-init-custom.log
        echo "4. Run: sudo nginx -t && sudo systemctl reload nginx" >> /var/log/cloud-init-custom.log
        echo "===========================" >> /var/log/cloud-init-custom.log
      fi
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
  # Configure fail2ban for nginx brute-force protection
  # NOTE: Using printf instead of heredoc because cloud-init YAML parser
  # misinterprets INI-style [section] headers as YAML sequences when at column 1
  - |
    if command -v fail2ban-server &> /dev/null; then
      # Create nginx jail configuration using printf to avoid YAML parsing issues
      mkdir -p /etc/fail2ban/jail.d
      printf '%s\n' \
        "[nginx-http-auth]" \
        "enabled = true" \
        "filter = nginx-http-auth" \
        "port = http,https" \
        "logpath = /var/log/nginx/error.log" \
        "maxretry = 5" \
        "bantime = 3600" \
        "findtime = 600" \
        > /etc/fail2ban/jail.d/nginx.local
      systemctl enable fail2ban
      systemctl restart fail2ban || true
      echo "Fail2ban configured and started for SSH and nginx protection" >> /var/log/cloud-init-custom.log
    fi
%{ endif ~}

final_message: "Instance ${hostname} is ready!"
