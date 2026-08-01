# Security policy

## Supported versions

Until the first stable release, security fixes target the latest release and
the `main` branch.

## Report a vulnerability

Please use GitHub's private vulnerability reporting for this repository. Do
not open a public issue with exploit details, credentials, private hostnames,
or tailnet information. Include the affected version or commit, reproduction
steps, impact, and any proposed mitigation.

## Trust boundary

Slancha-Mesh is designed for a trusted LAN or a Tailscale/Headscale network.
The router and node-info services are not hardened public-Internet gateways.
Keep model and node-info ports behind the network boundary, restrict
`tag:specialist` ownership, and allow gateway-to-specialist traffic only on
the documented ports.

The core package does not read cloud-provider credentials or execute cloud
fallbacks. A caller that handles a typed punt owns consent, spend controls,
provider authentication, and data-egress policy.
