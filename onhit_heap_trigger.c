// Isolated Testbed-only jemalloc profile dump trigger. No BDS hooks or production use.
#define _POSIX_C_SOURCE 200809L
#include <dlfcn.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static void *trigger_loop(void *unused) {
    (void)unused;
    const char *control = getenv("BDS_ONHIT_HEAP_CONTROL");
    const char *prefix = getenv("BDS_ONHIT_HEAP_PREFIX");
    if (!control || !prefix) return NULL;
    typedef int (*mallctl_fn)(const char *, void *, size_t *, void *, size_t);
    mallctl_fn ctl = (mallctl_fn)dlsym(RTLD_DEFAULT, "mallctl");
    if (!ctl) return NULL;
    for (;;) {
        FILE *request = fopen(control, "r");
        if (request) {
            char stage[32] = {0};
            int valid = fscanf(request, "%31[a-zA-Z0-9_-]", stage) == 1;
            fclose(request);
            unlink(control);
            if (valid) {
                char dump[1024], done[1024];
                snprintf(dump, sizeof(dump), "%s.%s.heap", prefix, stage);
                snprintf(done, sizeof(done), "%s.%s.done", prefix, stage);
                bool compiled = false, enabled = false;
                size_t size = sizeof(bool);
                int c1 = ctl("config.prof", &compiled, &size, NULL, 0);
                size = sizeof(bool);
                int c2 = ctl("opt.prof", &enabled, &size, NULL, 0);
                size_t lg_sample = 0;
                size = sizeof(lg_sample);
                int c3 = ctl("opt.lg_prof_sample", &lg_sample, &size, NULL, 0);
                int activated = 0;
                if (strcmp(stage, "warmup") == 0) {
                    bool active = true;
                    activated = ctl("prof.active", NULL, NULL, &active, sizeof(active));
                }
                const char *path = dump;
                int dumped = ctl("prof.dump", NULL, NULL, &path, sizeof(path));
                FILE *ack = fopen(done, "w");
                if (ack) {
                    fprintf(ack, "config=%d:%d opt=%d:%d lg=%d:%zu active=%d dump=%d\n",
                            c1, compiled, c2, enabled, c3, lg_sample, activated, dumped);
                    fclose(ack);
                }
            }
        }
        const struct timespec delay = {.tv_sec = 0, .tv_nsec = 200000000};
        nanosleep(&delay, NULL);
    }
    return NULL;
}

__attribute__((constructor)) static void start_trigger(void) {
    char executable[1024];
    ssize_t length = readlink("/proc/self/exe", executable, sizeof(executable) - 1);
    if (length <= 0) return;
    executable[length] = '\0';
    const char *name = strrchr(executable, '/');
    if (strcmp(name ? name + 1 : executable, "bedrock_server") != 0) return;
    pthread_t thread;
    if (pthread_create(&thread, NULL, trigger_loop, NULL) == 0)
        pthread_detach(thread);
}
