#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

static int report(int fd, const char *data, size_t len)
{
    ssize_t n;
    do {
        n = send(fd, data, len, MSG_NOSIGNAL);
    } while (n < 0 && errno == EINTR);
    return n == (ssize_t)len ? 0 : -1;
}

static int startup_error(int fd, int err)
{
    char msg[64];
    int len = snprintf(msg, sizeof(msg), "E %d", err);
    report(fd, msg, (size_t)len);
    return 125;
}

static void child_error(int pipe_fd, int err)
{
    /* A single int fits in one atomic write to the blocking pipe. */
    while (write(pipe_fd, &err, sizeof(err)) < 0 && errno == EINTR) {}
    _exit(125);
}

int main(int argc, char *argv[])
{
    if (argc < 3) return 125;
    char *end;
    errno = 0;
    long value = strtol(argv[1], &end, 10);
    if (errno || end == argv[1] || *end || value < 3 || value > INT_MAX) return 125;
    int fd = (int)value;

    if (prctl(PR_SET_DUMPABLE, 0, 0L, 0L, 0L) < 0) return startup_error(fd, errno);
    struct sigaction sa = {.sa_handler = SIG_DFL};
    sigemptyset(&sa.sa_mask);
    if (sigaction(SIGCHLD, &sa, NULL) < 0) return startup_error(fd, errno);

    int errors[2];
    if (pipe2(errors, O_CLOEXEC) < 0) return startup_error(fd, errno);
    pid_t child = fork();
    if (child < 0) return startup_error(fd, errno);
    if (child == 0) {
        close(errors[0]);
        if (report(fd, "S", 1) < 0) child_error(errors[1], errno);
        close(fd);
        execvp(argv[2], argv + 2);
        child_error(errors[1], errno);
    }

    close(errors[1]);
    int status;
    struct rusage usage;
    pid_t finished;
    do {
        finished = wait4(child, &status, 0, &usage);
    } while (finished < 0 && errno == EINTR);
    if (finished < 0) return startup_error(fd, errno);

    /* EOF means exec succeeded. A real contestant can itself exit with 125. */
    int err;
    ssize_t n;
    do {
        n = read(errors[0], &err, sizeof(err));
    } while (n < 0 && errno == EINTR);
    if (n < 0) return startup_error(fd, errno);
    if (n > 0) return startup_error(fd, n == (ssize_t)sizeof(err) ? err : EIO);
    close(errors[0]);

    char msg[64];
    int len = snprintf(msg, sizeof(msg), "R %ld", usage.ru_maxrss);
    if (report(fd, msg, (size_t)len) < 0) return 125;
    close(fd);

    if (WIFEXITED(status)) return WEXITSTATUS(status);
    if (WIFSIGNALED(status)) {
        int sig = WTERMSIG(status);
        sigset_t set;
        sigemptyset(&set);
        sigaddset(&set, sig);
        sigaction(sig, &sa, NULL);
        sigprocmask(SIG_UNBLOCK, &set, NULL);
        kill(getpid(), sig);
        return 128 + sig;
    }
    return 125;
}
