# STORY-060: Media Status Page & Documentation Platform

**Epic**: EPIC-021 (Media Stack Migration)
**Created**: 2025-12-03
**Updated**: 2025-12-04
**Status**: In Progress
**Priority**: P2
**Effort**: 8-12 hours
**Agent**: homelab-infra-architect

---

## User Stories

### Status Page
**As a** media stack end-user (Plex server member),
**I want to** view a public health status dashboard showing uptime of Plex and all automation services,
**So that** I can quickly check if any part of the media system is down when I experience issues.

### Documentation Platform
**As a** media stack administrator,
**I want to** provide documentation to users with role-based access control,
**So that** Plex users see only media-related docs while admins see all infrastructure documentation.

---

## Background

### Current State
- **Status Page**: Gatus deployed but has OIDC complexity issues
- **Documentation**: BookStack running on VM 101 (docker-sandbox2) in Docker
- **Goal**: Migrate to Kubernetes with proper architecture

### Services to Monitor (13 total)
- **Plex**: Media streaming server
- **Arr Stack**: Sonarr, Radarr, Prowlarr (media management)
- **Download Clients**: qBittorrent, SABnzbd
- **Request/Management**: Overseerr, Wizarr, Huntarr, Maintainerr, Cleanuparr
- **Monitoring**: Tautulli, Notifiarr

---

## Technical Solution

### Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              EXTERNAL ACCESS                                 │
│                    (Plex users, external guests)                            │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┴───────────────┐
                    │                               │
                    ▼                               ▼
┌───────────────────────────────┐   ┌───────────────────────────────────────┐
│      UPTIME KUMA              │   │           BOOKSTACK                    │
│   media-status.homelab0.org   │   │       docs.homelab0.org                │
│      (PUBLIC - No Auth)       │   │     (OIDC via Authentik)               │
│                               │   │                                        │
│  ┌─────────────────────────┐  │   │  Authentication:                       │
│  │    YOUR LOGO HERE       │  │   │  ├── Plex OAuth → media-viewers group  │
│  │                         │  │   │  ├── Authentik Local → power-users     │
│  │  All Systems            │  │   │  └── Admin accounts → admins group     │
│  │  Operational ✓          │  │   │                                        │
│  │                         │  │   │  Permissions:                          │
│  │  ● Plex ........... UP  │  │   │  ├── Media Docs → media-viewers+       │
│  │  ● Sonarr ......... UP  │  │   │  ├── User Guides → power-users+        │
│  │  ● Radarr ......... UP  │  │   │  └── Infrastructure → admins only      │
│  │  ● Overseerr ...... UP  │  │   │                                        │
│  └─────────────────────────┘  │   │  Migrated from: VM 101 docker-sandbox2 │
└───────────────────────────────┘   └───────────────────────────────────────┘
                                                    │
                                                    │ OIDC
                                                    ▼
                                    ┌───────────────────────────────────────┐
                                    │            AUTHENTIK                   │
                                    │      authentik.homelab0.org            │
                                    │                                        │
                                    │  Sources:                              │
                                    │  ├── Plex OAuth                        │
                                    │  │   └── Auto-group: media-viewers     │
                                    │  └── Local Authentik                   │
                                    │       └── Manual groups: power-users,  │
                                    │           admins                       │
                                    │                                        │
                                    │  OIDC Provider:                        │
                                    │  └── BookStack Application             │
                                    │      └── Sends group claims            │
                                    └───────────────────────────────────────┘
```

### Component Details

#### Uptime Kuma (Status Page)
- **Purpose**: Public status page for media stack health
- **Helm Chart**: [dirsigler/uptime-kuma-helm](https://github.com/dirsigler/uptime-kuma-helm)
- **Namespace**: `media`
- **Hostname**: `media-status.homelab0.org`
- **Authentication**: None (public Status Page feature)
- **Branding**: Custom logo, title, colors via UI
- **Storage**: Ceph PVC 2Gi

#### BookStack (Documentation)
- **Purpose**: Role-based documentation platform
- **Helm Chart**: Custom or solidnerd/bookstack
- **Namespace**: `media` (or `docs`)
- **Hostname**: `docs.homelab0.org`
- **Authentication**: OIDC via Authentik
- **Storage**: Ceph PVC 10Gi + MySQL 5Gi
- **Migration**: Data from VM 101 docker-sandbox2

#### Authentik Integration
- **Existing**: Already deployed in `security` namespace
- **New Provider**: OIDC for BookStack
- **Group Mapping**: Authentik groups → BookStack roles

---

## Acceptance Criteria

### Status Page (Uptime Kuma)
- [ ] Uptime Kuma deployed with public Status Page
- [ ] Custom branding (logo, title) configured
- [ ] All 13 media services monitored
- [ ] Accessible at media-status.homelab0.org without login
- [ ] Admin dashboard protected by Uptime Kuma's own auth
- [ ] Newsletter can link directly to status page

### Documentation (BookStack)
- [ ] BookStack deployed with OIDC via Authentik
- [ ] Data migrated from VM 101 (docker-sandbox2)
- [ ] Plex users → media-viewers group → Media docs only
- [ ] Authentik local users → power-users group → Media + User guides
- [ ] Admins → Full access to all documentation
- [ ] Group claims properly passed from Authentik to BookStack

### Integration
- [ ] Both apps accessible via Cloudflare Tunnel
- [ ] NetworkPolicies properly configured
- [ ] Old Gatus deployment removed

---

## Tasks

### Phase 1: Remove Gatus, Deploy Uptime Kuma

#### Task 1.1: Update HelmRepository
Replace Gatus chart with Uptime Kuma:
```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: HelmRepository
metadata:
  name: uptime-kuma
  namespace: media
spec:
  interval: 1h
  url: https://helm.irsigler.cloud
```

#### Task 1.2: Deploy Uptime Kuma HelmRelease
```yaml
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: uptime-kuma
  namespace: media
spec:
  chart:
    spec:
      chart: uptime-kuma
      version: 2.22.0
      sourceRef:
        kind: HelmRepository
        name: uptime-kuma
        namespace: media
  interval: 1h
  values:
    image:
      repository: louislam/uptime-kuma
      tag: 1.23.16

    service:
      port: 3001

    persistence:
      enabled: true
      storageClass: ceph-block
      size: 2Gi

    resources:
      requests:
        cpu: 50m
        memory: 128Mi
      limits:
        memory: 512Mi
```

#### Task 1.3: Update HTTPRoutes
- Update external route: hostname `media-status.homelab0.org`, port 3001
- Update internal route: same hostname for LAN access
- Remove Gatus-specific configurations

#### Task 1.4: Simplify NetworkPolicy
```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: uptime-kuma
  namespace: media
spec:
  endpointSelector:
    matchLabels:
      app.kubernetes.io/name: uptime-kuma
  ingress:
    # Allow from Envoy gateways
    - fromEndpoints:
        - matchLabels:
            io.kubernetes.pod.namespace: network
            gateway.envoyproxy.io/owning-gateway-name: envoy-external
        - matchLabels:
            io.kubernetes.pod.namespace: network
            gateway.envoyproxy.io/owning-gateway-name: envoy-internal
      toPorts:
        - ports:
            - port: "3001"
              protocol: TCP
  egress:
    # Allow DNS
    - toEndpoints:
        - matchLabels:
            io.kubernetes.pod.namespace: kube-system
            k8s-app: kube-dns
      toPorts:
        - ports:
            - port: "53"
              protocol: UDP
            - port: "53"
              protocol: TCP
    # Allow to all monitored services (media namespace)
    - toEndpoints:
        - matchLabels:
            io.kubernetes.pod.namespace: media
    # Allow to downloads namespace
    - toEndpoints:
        - matchLabels:
            io.kubernetes.pod.namespace: downloads
```

#### Task 1.5: Update Cloudflare Tunnel
Change hostname from `gatus.${SECRET_DOMAIN}` to `media-status.${SECRET_DOMAIN}`

#### Task 1.6: Remove Gatus Resources
- Delete ExternalSecret (OIDC creds not needed)
- Clean up old PVC if present

#### Task 1.7: Configure Uptime Kuma via UI
After deployment:
1. Access admin at media-status.homelab0.org
2. Set admin password
3. Create Status Page with slug (e.g., "status")
4. Upload custom logo
5. Configure branding (title, description, colors)
6. Add monitors for all 13 services
7. Assign monitors to Status Page

### Phase 2: BookStack Data Migration

#### Task 2.1: Assess Current BookStack on VM 101
```bash
# SSH to docker-sandbox2 (VM 101)
ssh docker-sandbox2

# Find BookStack containers
docker ps | grep bookstack

# Check data volumes
docker volume ls | grep bookstack
docker inspect bookstack_data  # or whatever the volume name is

# Check database
docker exec bookstack-db mysqldump -u bookstack -p bookstack > bookstack_backup.sql
```

#### Task 2.2: Export BookStack Data
```bash
# On VM 101
# Export uploads/images
docker cp bookstack:/config/www/uploads ./bookstack-uploads/
docker cp bookstack:/config/www/files ./bookstack-files/

# Export database
docker exec bookstack-db mysqldump -u bookstack -pPASSWORD bookstack > bookstack-db.sql

# Compress for transfer
tar -czf bookstack-migration.tar.gz bookstack-uploads/ bookstack-files/ bookstack-db.sql
```

#### Task 2.3: Transfer to Kubernetes PVC
```bash
# Create temporary pod with PVC mounted
kubectl run bookstack-migration --image=alpine -n media -- sleep 3600

# Copy data
kubectl cp bookstack-migration.tar.gz media/bookstack-migration:/tmp/

# Extract into PVC
kubectl exec -n media bookstack-migration -- tar -xzf /tmp/bookstack-migration.tar.gz -C /data/
```

### Phase 3: Deploy BookStack

#### Task 3.1: Create BookStack Namespace Resources
```yaml
# Use media namespace or create docs namespace
```

#### Task 3.2: Create BookStack Secrets in 1Password
Create item `bookstack` with fields:
- `app-key` (Laravel APP_KEY: `base64:xxxx...`)
- `db-password` (MySQL password)
- `oidc-client-id` (from Authentik)
- `oidc-client-secret` (from Authentik)

#### Task 3.3: Create ExternalSecret for BookStack
```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: bookstack-secret
  namespace: media
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: ClusterSecretStore
    name: onepassword-connect
  target:
    name: bookstack-secret
    creationPolicy: Owner
  data:
    - secretKey: APP_KEY
      remoteRef:
        key: bookstack
        property: app-key
    - secretKey: DB_PASSWORD
      remoteRef:
        key: bookstack
        property: db-password
    - secretKey: OIDC_CLIENT_ID
      remoteRef:
        key: bookstack
        property: oidc-client-id
    - secretKey: OIDC_CLIENT_SECRET
      remoteRef:
        key: bookstack
        property: oidc-client-secret
```

#### Task 3.4: Deploy BookStack with MySQL
```yaml
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: bookstack
  namespace: media
spec:
  chart:
    spec:
      chart: bookstack
      sourceRef:
        kind: HelmRepository
        name: solidnerd
        namespace: media
  values:
    image:
      repository: lscr.io/linuxserver/bookstack
      tag: latest

    env:
      APP_URL: https://docs.homelab0.org
      DB_HOST: bookstack-mysql
      DB_DATABASE: bookstack
      DB_USERNAME: bookstack

      # OIDC Configuration
      AUTH_METHOD: oidc
      OIDC_NAME: "Sign in with Plex"
      OIDC_DISPLAY_NAME_CLAIMS: name
      OIDC_CLIENT_ID: # From secret
      OIDC_CLIENT_SECRET: # From secret
      OIDC_ISSUER: https://authentik.homelab0.org/application/o/bookstack/
      OIDC_ISSUER_DISCOVER: true

      # Group sync
      OIDC_USER_TO_GROUPS: true
      OIDC_GROUPS_CLAIM: groups
      OIDC_REMOVE_FROM_GROUPS: true

    persistence:
      enabled: true
      storageClass: ceph-block
      size: 10Gi

    mysql:
      enabled: true
      auth:
        database: bookstack
        username: bookstack
        # password from secret
      primary:
        persistence:
          enabled: true
          storageClass: ceph-block
          size: 5Gi
```

#### Task 3.5: Create HTTPRoute for BookStack
```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: bookstack-external
  namespace: media
spec:
  parentRefs:
    - name: envoy-external
      namespace: network
      sectionName: https
  hostnames:
    - docs.homelab0.org
  rules:
    - backendRefs:
        - name: bookstack
          port: 80
```

#### Task 3.6: Create NetworkPolicy for BookStack
```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: bookstack
  namespace: media
spec:
  endpointSelector:
    matchLabels:
      app.kubernetes.io/name: bookstack
  ingress:
    - fromEndpoints:
        - matchLabels:
            io.kubernetes.pod.namespace: network
            gateway.envoyproxy.io/owning-gateway-name: envoy-external
      toPorts:
        - ports:
            - port: "80"
              protocol: TCP
  egress:
    # DNS
    - toEndpoints:
        - matchLabels:
            io.kubernetes.pod.namespace: kube-system
            k8s-app: kube-dns
      toPorts:
        - ports:
            - port: "53"
              protocol: UDP
    # MySQL
    - toEndpoints:
        - matchLabels:
            io.kubernetes.pod.namespace: media
            app.kubernetes.io/name: bookstack-mysql
      toPorts:
        - ports:
            - port: "3306"
              protocol: TCP
    # Authentik OIDC
    - toEndpoints:
        - matchLabels:
            io.kubernetes.pod.namespace: security
            app.kubernetes.io/name: authentik
      toPorts:
        - ports:
            - port: "9000"
              protocol: TCP
    # External HTTPS (OIDC discovery)
    - toEntities:
        - world
      toPorts:
        - ports:
            - port: "443"
              protocol: TCP
```

### Phase 4: Authentik Configuration for BookStack

#### Task 4.1: Create BookStack OIDC Provider
In Authentik Admin UI:
1. Applications → Providers → Create
2. Type: OAuth2/OpenID Connect
3. Name: `BookStack Provider`
4. Authorization flow: default-provider-authorization-implicit-consent
5. Client type: Confidential
6. Redirect URIs: `https://docs.homelab0.org/oidc/callback`
7. Scopes: openid, email, profile, groups
8. **Important**: Include groups claim

#### Task 4.2: Create BookStack Application
1. Applications → Applications → Create
2. Name: `Documentation`
3. Slug: `bookstack`
4. Provider: BookStack Provider
5. Launch URL: `https://docs.homelab0.org`

#### Task 4.3: Configure Group Claims
Ensure Authentik sends group information:
1. Customization → Property Mappings
2. Create/verify OAuth2 Scope for groups
3. Expression: `return list(request.user.ak_groups.values_list("name", flat=True))`

#### Task 4.4: Create Authentik Groups
| Group | Members | BookStack Access |
|-------|---------|------------------|
| `media-viewers` | Auto: Plex OAuth users | Media shelf only |
| `power-users` | Manual: Trusted users | Media + User guides |
| `admins` | Manual: You | All documentation |

#### Task 4.5: Configure BookStack Roles
In BookStack Admin:
1. Settings → Roles
2. Create roles matching Authentik groups:
   - `media-viewers` → Can view Media shelf
   - `power-users` → Can view Media + User Guides
   - `Admin` → Full access
3. Set default role for new OIDC users

#### Task 4.6: Configure Shelf Permissions
| Shelf | Visible to |
|-------|-----------|
| Media Documentation | media-viewers, power-users, admins |
| User Guides | power-users, admins |
| Infrastructure | admins only |

### Phase 5: Update Cloudflare Tunnel

#### Task 5.1: Add BookStack to Tunnel Config
```yaml
# In cloudflare-tunnel helmrelease
ingress:
  # ... existing entries ...

  # Documentation (BookStack with Authentik OIDC)
  - hostname: "docs.${SECRET_DOMAIN}"
    service: https://envoy-external.{{ .Release.Namespace }}.svc.cluster.local:443
    originRequest:
      http2Origin: true
      originServerName: docs.${SECRET_DOMAIN}
```

### Phase 6: Validation

#### Task 6.1: Test Uptime Kuma
- [ ] Access media-status.homelab0.org without login
- [ ] Verify all 13 services monitored
- [ ] Verify custom branding visible
- [ ] Test from external network (mobile data)

#### Task 6.2: Test BookStack Migration
- [ ] All existing books/chapters/pages present
- [ ] Images and uploads working
- [ ] No broken links

#### Task 6.3: Test BookStack OIDC
- [ ] Plex user login → lands in media-viewers role
- [ ] Plex user can see Media shelf only
- [ ] Plex user CANNOT see Infrastructure shelf
- [ ] Admin login → full access

#### Task 6.4: Test Permission Matrix
| User Type | Media Docs | User Guides | Infrastructure |
|-----------|-----------|-------------|----------------|
| Plex user | ✅ | ❌ | ❌ |
| Power user | ✅ | ✅ | ❌ |
| Admin | ✅ | ✅ | ✅ |

### Phase 7: Cleanup

#### Task 7.1: Remove Old Gatus Resources
- Delete Gatus HelmRelease
- Delete Gatus ExternalSecret
- Delete old PVC
- Remove from kustomization.yaml

#### Task 7.2: Shutdown VM 101 BookStack (After Validation)
```bash
# Only after confirming migration success
ssh docker-sandbox2
docker-compose -f bookstack/docker-compose.yml down
# Keep data for 30 days before full removal
```

---

## Key URLs

| URL | Purpose | Auth |
|-----|---------|------|
| `media-status.homelab0.org` | Public status page | None |
| `media-status.homelab0.org/dashboard` | Uptime Kuma admin | Internal password |
| `docs.homelab0.org` | Documentation | OIDC via Authentik |
| `authentik.homelab0.org` | Identity provider | Local/Plex |

---

## Secrets Required (1Password)

| Item | Field | Purpose |
|------|-------|---------|
| `bookstack` | `app-key` | Laravel encryption key |
| `bookstack` | `db-password` | MySQL password |
| `bookstack` | `oidc-client-id` | Authentik OIDC |
| `bookstack` | `oidc-client-secret` | Authentik OIDC |

---

## Resource Requirements

| Component | CPU Request | Memory Request | Memory Limit | Storage |
|-----------|-------------|----------------|--------------|---------|
| Uptime Kuma | 50m | 128Mi | 512Mi | 2Gi |
| BookStack | 100m | 256Mi | 512Mi | 10Gi |
| BookStack MySQL | 100m | 256Mi | 512Mi | 5Gi |
| **Total** | ~250m | ~640Mi | ~1.5Gi | ~17Gi |

---

## Migration Notes

### BookStack Data Location on VM 101
- **Container**: bookstack (linuxserver/bookstack)
- **Database**: bookstack-db (MySQL/MariaDB)
- **Data paths**:
  - `/config/www/uploads` - User uploads
  - `/config/www/files` - File attachments
  - `/config/www/images` - Images
  - MySQL database: `bookstack`

### Migration Checklist
- [ ] Export database dump
- [ ] Export uploads directory
- [ ] Export files directory
- [ ] Create Kubernetes PVCs
- [ ] Import database
- [ ] Import files
- [ ] Test all pages/images
- [ ] Update any hardcoded URLs
- [ ] Keep VM 101 data for 30 days

---

## References

- [Uptime Kuma Helm Chart](https://github.com/dirsigler/uptime-kuma-helm)
- [BookStack OIDC Documentation](https://www.bookstackapp.com/docs/admin/oidc-auth/)
- [Authentik BookStack Integration](https://docs.goauthentik.io/integrations/services/bookstack/)
- [Authentik Group Claims](https://docs.goauthentik.io/docs/providers/oauth2/)

---

## Change Log

| Date | Change | Author |
|------|--------|--------|
| 2025-12-03 | Story created with Gatus + Authentik OIDC | PM (John) |
| 2025-12-04 | Rewritten: Gatus → Uptime Kuma (public), added BookStack with OIDC | Claude |
| 2025-12-04 | Added BookStack data migration from VM 101 | Claude |

---

## Status: In Progress

**Phase 1 (Uptime Kuma)**: In development
**Phase 2-4 (BookStack)**: Pending Phase 1 completion
