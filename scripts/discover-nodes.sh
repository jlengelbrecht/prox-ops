#!/usr/bin/env bash

# Node discovery script for 15-node Talos cluster
# Run this from a machine that can reach 10.20.67.0/24

set -euo pipefail

echo "=== Talos Node Discovery ==="
echo "Script will discover all 15 nodes and gather:"
echo "  - Disk information"
echo "  - MAC addresses"
echo "  - Network interface details"
echo ""
echo "Prerequisites:"
echo "  - talosctl must be installed"
echo "  - Network access to 10.20.67.1-15"
echo ""

# Check if talosctl is available
if ! command -v talosctl &> /dev/null; then
    echo "ERROR: talosctl not found. Please install it first."
    echo "Visit: https://www.talos.dev/latest/introduction/getting-started/"
    exit 1
fi

# Function to discover a single node
discover_node() {
    local node_name=$1
    local node_ip=$2
    local is_controller=$3

    echo ""
    echo "========================================="
    echo "Node: $node_name ($node_ip)"
    echo "Role: $([ "$is_controller" == "true" ] && echo "Controller" || echo "Worker")"
    echo "========================================="

    # Check if node is reachable
    if ! timeout 3 bash -c "echo > /dev/tcp/$node_ip/50000" 2>/dev/null; then
        echo "❌ ERROR: Node not reachable on Talos API port 50000"
        echo "   Check that VM is running and network is accessible"
        return 1
    fi

    echo "✓ Node is reachable"
    echo ""

    # Get disk information
    echo "📀 Disk Information:"
    echo "-------------------"
    if talosctl disks --nodes "$node_ip" --insecure 2>/dev/null; then
        echo ""
    else
        echo "❌ Failed to get disk information"
        echo ""
    fi

    # Get network interfaces
    echo "🌐 Network Interfaces:"
    echo "---------------------"
    if talosctl get links --nodes "$node_ip" --insecure 2>/dev/null; then
        echo ""
    else
        echo "❌ Failed to get network interfaces"
        echo ""
    fi

    # Get system information
    echo "💻 System Information:"
    echo "---------------------"
    if talosctl version --nodes "$node_ip" --insecure 2>/dev/null | grep -A 2 "Server:"; then
        echo ""
    else
        echo "❌ Failed to get system information"
        echo ""
    fi
}

# Discover controller nodes (10.20.67.1-3)
echo ""
echo "╔═══════════════════════════════════════╗"
echo "║      CONTROLLER NODES (3)             ║"
echo "╚═══════════════════════════════════════╝"

for i in {1..3}; do
    discover_node "k8s-ctrl-$i" "10.20.67.$i" "true"
done

# Discover worker nodes (10.20.67.4-15)
echo ""
echo "╔═══════════════════════════════════════╗"
echo "║       WORKER NODES (12)               ║"
echo "╚═══════════════════════════════════════╝"

for i in {4..15}; do
    worker_num=$((i-3))
    discover_node "k8s-work-$worker_num" "10.20.67.$i" "false"
done

echo ""
echo "=== Discovery Complete ==="
echo ""
echo "📝 Next Steps:"
echo "  1. Review the output above"
echo "  2. Note the disk paths (e.g., /dev/sda, /dev/vda)"
echo "  3. Note the MAC addresses for eth0 on each node"
echo "  4. Visit https://factory.talos.dev/ to create a Talos schematic"
echo "     - Select Talos 1.11.3"
echo "     - Add extension: qemu-guest-agent"
echo "     - Copy the schematic ID"
echo "  5. Edit cluster.yaml with your network configuration"
echo "  6. Edit nodes.yaml with discovered information:"
echo "     - Add all 15 nodes"
echo "     - Use discovered disk paths and MAC addresses"
echo "     - Use your schematic ID from factory.talos.dev"
echo "  7. Run: task configure"
echo ""
