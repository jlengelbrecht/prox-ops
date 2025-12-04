#!/bin/bash
set -e

echo "Migrating Terraform state from controller/worker to base/gpu naming..."
echo ""

# Baldar
echo "Migrating Baldar templates..."
terraform state mv 'module.template_baldar_controller' 'module.template_baldar_base'
terraform state mv 'module.template_baldar_worker' 'module.template_baldar_gpu'

# Heimdall
echo "Migrating Heimdall templates..."
terraform state mv 'module.template_heimdall_controller' 'module.template_heimdall_base'
terraform state mv 'module.template_heimdall_worker' 'module.template_heimdall_gpu'

# Odin
echo "Migrating Odin templates..."
terraform state mv 'module.template_odin_controller' 'module.template_odin_base'
terraform state mv 'module.template_odin_worker' 'module.template_odin_gpu'

# Thor
echo "Migrating Thor templates..."
terraform state mv 'module.template_thor_controller' 'module.template_thor_base'
terraform state mv 'module.template_thor_worker' 'module.template_thor_gpu'

echo ""
echo "State migration complete!"
echo "Verifying new module names..."
terraform state list | grep template
