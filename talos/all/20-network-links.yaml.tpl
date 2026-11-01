# Primary interface (VLAN 66), selected by MAC address.
apiVersion: v1alpha1
kind: LinkAliasConfig
name: ethSel0
selector:
  match: glob("{{ .Node.Data.macAddr }}", mac(link.hardware_addr))
---
apiVersion: v1alpha1
kind: LinkConfig
name: ethSel0
mtu: 1500
addresses:
  - address: "{{ .Node.IP }}/23"
routes:
  - gateway: 10.20.66.1
