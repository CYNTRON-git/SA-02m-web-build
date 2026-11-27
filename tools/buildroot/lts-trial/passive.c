#define _GNU_SOURCE
#include <errno.h>
#include <glob.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/utsname.h>
#include <unistd.h>

static char *model(void) {
    static char text[96];
    FILE *fp = fopen("/sys/firmware/devicetree/base/model", "r");
    if (!fp) return NULL;
    size_t n = fread(text, 1, sizeof(text) - 1, fp);
    fclose(fp);
    return n > 0 ? text : NULL;
}

static size_t inventory(const char *pattern, const char *label) {
    glob_t g = {0};
    char path[PATH_MAX];
    if (glob(pattern, 0, NULL, &g) != 0) {
        printf("HIL %s=absent\n", label);
        return 0;
    }
    for (size_t i = 0; i < g.gl_pathc; ++i)
        printf("HIL %s path=%s resolved=%s\n", label, g.gl_pathv[i],
               realpath(g.gl_pathv[i], path) ? path : "unresolved");
    size_t n = g.gl_pathc;
    globfree(&g);
    return n;
}

int main(void) {
    struct utsname u;
    setvbuf(stdout, NULL, _IONBF, 0);
    if (mount("proc", "/proc", "proc", MS_RDONLY, NULL) ||
        mount("sysfs", "/sys", "sysfs", MS_RDONLY, NULL) ||
        mount("devtmpfs", "/dev", "devtmpfs", 0, NULL) || uname(&u)) {
        perror("HIL mount/utsname");
        goto hold;
    }
    const char *name = model();
    printf("HIL kernel=%s machine=%s model=%s\n", u.release, u.machine, name ? name : "?");
    if (!name || strcmp(u.release, EXPECT_RELEASE) || strcmp(name, "Cyntron A40i-2Eth")) {
        puts("HIL_REJECT_IDENTITY");
        goto hold;
    }
    sleep(12);
    inventory("/sys/class/net/*/device/of_node", "net-node");
    inventory("/sys/class/net/*/device/driver", "net-driver");
    inventory("/sys/class/net/*/phydev/driver", "phy-driver");
    inventory("/sys/bus/mdio_bus/devices/*", "mdio");
    inventory("/sys/class/block/mmcblk*/device", "mmc");
    inventory("/sys/class/block/mmcblk*p[0-9]*", "partition");
    inventory("/sys/class/tty/ttyS*/device/driver", "serial");
    inventory("/sys/class/sound/card*", "alsa");
    inventory("/sys/class/watchdog/watchdog*/device/driver", "wdt");
    inventory("/sys/bus/usb/devices/usb*", "usb");
    inventory("/sys/class/rtc/rtc*", "rtc");
    puts("HIL_PASSIVE_INVENTORY_COMPLETE");
hold:
    puts("HIL_HOLD_RAM_ONLY");
    for (;;) pause();
}
