"""Patch sun8i-a40i-sk.dts: GMAC okay/syscon, dc1sw always-on, PHY reset timing."""
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

# PHY reset-deassert-us: 500 ms for both GMAC and EMAC PHYs.
# IP101A cold-boot: oscillator + PLL need adequate time after reset deassert
# before MDIO access; 200 ms was insufficient on production boards.
s = re.sub(r'reset-deassert-us\s*=\s*<0x[0-9a-f]+>', 'reset-deassert-us = <0x0007a120>', s)

# i2c@1c2b800: должен быть okay (pre-start + PCA9536); unbind — через udev на platform
node_start = s.find("i2c@1c2b800 {")
if node_start >= 0:
    node_end = s.find("};", node_start) + 2
    node = s[node_start:node_end]
    node = re.sub(r'status = "disabled"', 'status = "okay"', node, count=1)
    s = s[:node_start] + node + s[node_end:]

# GMAC
node_start = s.find("ethernet@1c50000 {")
if node_start < 0:
    raise SystemExit("ethernet@1c50000 not found")
node_end = s.find("};", node_start) + 2
node = s[node_start:node_end]
node = re.sub(r'status = "disabled"', 'status = "okay"', node)
node = re.sub(r'syscon = <0x[0-9a-f]+>', 'syscon = <0x02>', node)
s = s[:node_start] + node + s[node_end:]

with open("/tmp/patch.dts", "w") as f:
    f.write(s)
print("dtb patched ok")
