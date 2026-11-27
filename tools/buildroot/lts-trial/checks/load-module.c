#include <fcntl.h>
#include <stdio.h>
#include <sys/syscall.h>
#include <unistd.h>
int main(int argc, char **argv) {
    if (argc != 2) return 2;
    int fd = open(argv[1], O_RDONLY | O_CLOEXEC);
    if (fd < 0 || syscall(__NR_finit_module, fd, "", 0) < 0) {
        perror(argv[1]);
        return 1;
    }
    return close(fd) != 0;
}
