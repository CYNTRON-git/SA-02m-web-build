"""Patch sun8i-a40i-sk.dts: GMAC okay/syscon, dc1sw always-on, PHY reset timing, IP101A compatible."""
import re

with open("/tmp/patch.dts") as f:
    s = f.read()

# dc1sw: regulator-always-on for vcc-gmac-phy
if "dc1sw {" in s:
    parts = s.split("dc1sw {", 1)
    head, rest = parts[0], parts[1]
    body, tail = rest.split("};", 1)
    if "regulator-always-on" not in body:
        body = body.replace(
            'regulator-name = "vcc-gmac-phy";',
            'regulator-always-on;\n\t\t\t\t\t\tregulator-name = "vcc-gmac-phy";',
            1,
        )
    s = head + "dc1sw {" + body + "};" + tail

# i2c@1c2b800: должен быть okay (pre-start + PCA9536); unbind — через udev на platform
node_start = s.find("i2c@1c2b800 {")
if node_start >= 0:
    node_end = s.find("};", node_start) + 2
    node = s[node_start:node_end]
    node = re.sub(r'status = "disabled"', 'status = "okay"', node, count=1)
    s = s[:node_start] + node + s[node_end:]

# GMAC: status okay, syscon fix, IP101A compatible string, reset-deassert-us=500ms
node_start = s.find("ethernet@1c50000 {")
if node_start < 0:
    raise SystemExit("ethernet@1c50000 not found")
# Find the closing }; of the ethernet@1c50000 block (it contains nested mdio {})
# Use brace counting to find the real end
depth = 0
i = node_start
while i < len(s):
    if s[i] == '{':
        depth += 1
    elif s[i] == '}':
        depth -= 1
        if depth == 0:
            node_end = i + 1
            # skip optional ';'
            if node_end < len(s) and s[node_end] == ';':
                node_end += 1
            break
    i += 1
node = s[node_start:node_end]
node = re.sub(r'status = "disabled"', 'status = "okay"', node)
node = re.sub(r'syscon = <0x[0-9a-f]+>', 'syscon = <0x02>', node)

# Inside the GMAC mdio block (snps,dwmac-mdio), fix the ethernet-phy@0 node:
# 1. Add icplus,ip101a as primary compatible (with ieee802.3-c22 fallback)
# 2. Ensure reset-deassert-us = 500ms (0x7a120)
def patch_gmac_phy(node_text):
    mdio_marker = 'compatible = "snps,dwmac-mdio"'
    if mdio_marker not in node_text:
        return node_text
    mdio_start = node_text.find(mdio_marker)
    # Find ethernet-phy@0 after the mdio marker
    phy_start = node_text.find("ethernet-phy@0 {", mdio_start)
    if phy_start < 0:
        return node_text
    phy_end = node_text.find("};", phy_start) + 2
    phy_node = node_text[phy_start:phy_end]

    # Replace compatible string: add icplus,ip101a as primary
    phy_node = re.sub(
        r'compatible = "ethernet-phy-ieee802\.3-c22";',
        'compatible = "icplus,ip101a", "ethernet-phy-ieee802.3-c22";',
        phy_node,
    )
    # Set reset-deassert-us to 500ms (500000 us = 0x7a120)
    phy_node = re.sub(
        r'reset-deassert-us = <0x[0-9a-f]+>;',
        'reset-deassert-us = <0x7a120>;',
        phy_node,
    )
    # Also handle decimal form
    phy_node = re.sub(
        r'reset-deassert-us = <\d+>;',
        'reset-deassert-us = <0x7a120>;',
        phy_node,
    )
    return node_text[:phy_start] + phy_node + node_text[phy_end:]

node = patch_gmac_phy(node)
s = s[:node_start] + node + s[node_end:]

with open("/tmp/patch.dts", "w") as f:
    f.write(s)
print("dtb patched ok")
