// Command talos-compat asks Talos's own compatibility tables whether an upgrade is supported.
//
//	talos-compat <from-talos> <to-talos> <to-kubernetes>
//
// It fails when the target Talos cannot be upgraded to from the running Talos, or when the
// target Kubernetes version is outside the range the target Talos supports.
package main

import (
	"fmt"
	"os"

	"github.com/siderolabs/talos/pkg/machinery/api/machine"
	"github.com/siderolabs/talos/pkg/machinery/compatibility"
)

func main() {
	if len(os.Args) != 4 {
		fmt.Fprintln(os.Stderr, "usage: talos-compat <from-talos> <to-talos> <to-kubernetes>")
		os.Exit(2)
	}

	from, err := compatibility.ParseTalosVersion(&machine.VersionInfo{Tag: os.Args[1]})
	if err != nil {
		fail(2, "parse from-talos: %v", err)
	}

	to, err := compatibility.ParseTalosVersion(&machine.VersionInfo{Tag: os.Args[2]})
	if err != nil {
		fail(2, "parse to-talos: %v", err)
	}

	k8s, err := compatibility.ParseKubernetesVersion(os.Args[3])
	if err != nil {
		fail(2, "parse to-kubernetes: %v", err)
	}

	if err := to.UpgradeableFrom(from); err != nil {
		fail(1, "talos %s -> %s: %v", os.Args[1], os.Args[2], err)
	}

	if err := k8s.SupportedWith(to); err != nil {
		fail(1, "kubernetes %s on talos %s: %v", os.Args[3], os.Args[2], err)
	}

	fmt.Printf("talos %s -> %s with kubernetes %s is supported\n", os.Args[1], os.Args[2], os.Args[3])
}

func fail(code int, format string, args ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", args...)
	os.Exit(code)
}
