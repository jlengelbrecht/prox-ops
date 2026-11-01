# IoT VLAN 62 interface (eth1), selected by PCI bus path.
apiVersion: v1alpha1
kind: LinkAliasConfig
name: ethSel1
selector:
  match: glob("0000:06:13.0", link.bus_path)
---
apiVersion: v1alpha1
kind: LinkConfig
name: ethSel1
mtu: 1500
addresses:
  - address: "{{ .Node.Data.iotAddr }}"
---
# DMZ VLAN 81 interface (eth2), selected by PCI bus path.
apiVersion: v1alpha1
kind: LinkAliasConfig
name: ethSel2
selector:
  match: glob("0000:06:14.0", link.bus_path)
---
apiVersion: v1alpha1
kind: LinkConfig
name: ethSel2
mtu: 1500
addresses:
  - address: "{{ .Node.Data.dmzAddr }}"
