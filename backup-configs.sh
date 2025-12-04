#!/bin/bash
# Emergency backup script - encrypt configs for offline storage
set -euo pipefail

echo "Creating encrypted backup of Talos configs..."

# Check for GPG
if ! command -v gpg &>/dev/null; then
  echo "ERROR: gpg not installed"
  exit 1
fi

# Create backup
BACKUP_FILE="talos-configs-backup-$(date +%Y%m%d-%H%M%S).tar.gz.gpg"

tar czf - talos/clusterconfig/*.yaml talos/clusterconfig/talosconfig | \
  gpg --symmetric --cipher-algo AES256 --output "$BACKUP_FILE"

if [[ $? -eq 0 ]]; then
  echo "✓ Backup created: $BACKUP_FILE"
  echo ""
  echo "Store this file securely offline!"
  echo "To restore: gpg -d $BACKUP_FILE | tar xzf -"
else
  echo "ERROR: Backup failed"
  exit 1
fi
