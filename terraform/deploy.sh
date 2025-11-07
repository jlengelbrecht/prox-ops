#!/usr/bin/env bash

# =============================================================================
# Terraform Deployment Script for Talos Kubernetes Cluster
# =============================================================================
#
# This script automates the deployment of a Talos Kubernetes cluster on
# Proxmox using Terraform and nocloud images.
#
# Usage:
#   ./deploy.sh [command]
#
# Commands:
#   init       - Initialize Terraform
#   plan       - Plan infrastructure changes
#   apply      - Apply infrastructure changes
#   destroy    - Destroy all infrastructure
#   validate   - Validate Terraform configuration
#   check      - Pre-flight checks before deployment
#   status     - Show deployment status
#   help       - Show this help message
#
# =============================================================================

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# =============================================================================
# Helper Functions
# =============================================================================

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# =============================================================================
# Pre-flight Checks
# =============================================================================

check_prerequisites() {
    log_info "Checking prerequisites..."

    local missing_tools=()

    # Check required tools
    for tool in terraform curl xz ssh; do
        if ! command_exists "$tool"; then
            missing_tools+=("$tool")
        fi
    done

    if [ ${#missing_tools[@]} -gt 0 ]; then
        log_error "Missing required tools: ${missing_tools[*]}"
        log_info "Install missing tools and try again"
        return 1
    fi

    # Check Terraform version
    local tf_version
    tf_version=$(terraform version -json | jq -r '.terraform_version')
    log_info "Terraform version: $tf_version"

    # Check if terraform.tfvars exists
    if [ ! -f "terraform.tfvars" ]; then
        log_error "terraform.tfvars not found"
        log_info "Create it from terraform.tfvars.example:"
        log_info "  cp terraform.tfvars.example terraform.tfvars"
        log_info "  nano terraform.tfvars"
        return 1
    fi

    # Check SSH agent
    if [ -z "${SSH_AUTH_SOCK:-}" ]; then
        log_warning "SSH agent not running"
        log_info "Start SSH agent and add key: eval \$(ssh-agent) && ssh-add ~/.ssh/id_rsa"
    else
        log_success "SSH agent is running"
    fi

    # Check SSH connectivity to Proxmox
    log_info "Checking Proxmox connectivity..."
    local proxmox_host
    proxmox_host=$(grep '^proxmox_endpoint' terraform.tfvars | cut -d'"' -f2 | sed 's|https://||' | cut -d':' -f1)

    if [ -n "$proxmox_host" ]; then
        if ssh -o ConnectTimeout=5 -o BatchMode=yes "root@$proxmox_host" "echo connected" >/dev/null 2>&1; then
            log_success "Proxmox SSH connectivity OK"
        else
            log_warning "Cannot connect to Proxmox via SSH: root@$proxmox_host"
            log_info "Ensure SSH key is configured or add it to SSH agent:"
            log_info "  ssh-add ~/.ssh/id_rsa"
            log_info "Template creation requires SSH access"
        fi
    fi

    # Check disk space for image downloads
    local available_space
    available_space=$(df /tmp | tail -1 | awk '{print $4}')
    if [ "$available_space" -lt 5242880 ]; then  # 5GB in KB
        log_warning "Less than 5GB available in /tmp"
        log_info "Talos images require ~3GB temporary space"
    fi

    log_success "Prerequisites check passed"
    return 0
}

# =============================================================================
# Terraform Commands
# =============================================================================

tf_init() {
    log_info "Initializing Terraform..."
    terraform init
    log_success "Terraform initialized"
}

tf_validate() {
    log_info "Validating Terraform configuration..."
    terraform validate
    log_success "Configuration is valid"
}

tf_plan() {
    log_info "Planning infrastructure changes..."
    terraform plan -out=tfplan
    log_success "Plan saved to tfplan"
    log_info ""
    log_info "Review the plan above carefully"
    log_info "To apply: ./deploy.sh apply"
}

tf_apply() {
    log_info "Applying infrastructure changes..."

    if [ ! -f "tfplan" ]; then
        log_error "No plan file found. Run './deploy.sh plan' first"
        return 1
    fi

    log_warning "This will create/modify/destroy infrastructure"
    read -p "Continue? (yes/no): " -r
    if [[ ! $REPLY =~ ^[Yy]es$ ]]; then
        log_info "Aborted"
        return 1
    fi

    terraform apply tfplan
    rm -f tfplan

    log_success "Infrastructure deployed successfully!"
    log_info ""
    log_info "Next steps:"
    log_info "1. Wait for VMs to finish cloning (check Proxmox UI)"
    log_info "2. Apply Talos configuration:"
    log_info "   cd /home/devbox/repos/jlengelbrecht/prox-ops"
    log_info "   task bootstrap:talos"
    log_info ""
    log_info "For detailed instructions, see: TERRAFORM_MIGRATION_GUIDE.md"
}

tf_destroy() {
    log_warning "This will DESTROY all Terraform-managed infrastructure"
    log_warning "Including:"
    log_warning "  - All VM templates"
    log_warning "  - All VMs (15 nodes)"
    log_warning "  - All data on those VMs"
    log_info ""
    read -p "Are you sure? Type 'destroy-all' to confirm: " -r
    if [[ ! $REPLY == "destroy-all" ]]; then
        log_info "Aborted"
        return 1
    fi

    log_info "Destroying infrastructure..."
    terraform destroy

    log_success "Infrastructure destroyed"
}

tf_status() {
    log_info "Current infrastructure status:"
    log_info ""

    if [ ! -f "terraform.tfstate" ]; then
        log_warning "No Terraform state found. Infrastructure not deployed."
        return 0
    fi

    # Show resource count
    local resource_count
    resource_count=$(terraform state list | wc -l)
    log_info "Total resources: $resource_count"

    # Show templates
    log_info ""
    log_info "Templates:"
    terraform state list | grep "talos_template" || log_info "  None"

    # Show VMs
    log_info ""
    log_info "Control Plane Nodes:"
    terraform state list | grep "control_plane_nodes" || log_info "  None"

    log_info ""
    log_info "Worker Nodes:"
    terraform state list | grep "worker_nodes" || log_info "  None"

    # Show outputs
    log_info ""
    log_info "Cluster Summary:"
    terraform output -json cluster_summary 2>/dev/null | jq -r 'to_entries[] | "\(.key): \(.value)"' || log_info "  Not available"
}

# =============================================================================
# Main Script
# =============================================================================

show_help() {
    cat << EOF
Terraform Deployment Script for Talos Kubernetes Cluster

Usage:
  ./deploy.sh [command]

Commands:
  init       - Initialize Terraform (run first)
  plan       - Plan infrastructure changes (dry-run)
  apply      - Apply infrastructure changes (deploy)
  destroy    - Destroy all infrastructure (WARNING: destructive!)
  validate   - Validate Terraform configuration
  check      - Run pre-flight checks
  status     - Show current deployment status
  help       - Show this help message

Examples:
  # Initial deployment
  ./deploy.sh check      # Check prerequisites
  ./deploy.sh init       # Initialize Terraform
  ./deploy.sh plan       # Review what will be created
  ./deploy.sh apply      # Deploy infrastructure

  # Check status
  ./deploy.sh status     # Show current infrastructure

  # Destroy
  ./deploy.sh destroy    # Remove all infrastructure

For detailed documentation, see: TERRAFORM_MIGRATION_GUIDE.md
EOF
}

main() {
    local command="${1:-help}"

    case "$command" in
        init)
            check_prerequisites || exit 1
            tf_init
            ;;
        plan)
            check_prerequisites || exit 1
            tf_validate
            tf_plan
            ;;
        apply)
            tf_apply
            ;;
        destroy)
            tf_destroy
            ;;
        validate)
            tf_validate
            ;;
        check)
            check_prerequisites
            ;;
        status)
            tf_status
            ;;
        help|--help|-h)
            show_help
            ;;
        *)
            log_error "Unknown command: $command"
            show_help
            exit 1
            ;;
    esac
}

main "$@"
