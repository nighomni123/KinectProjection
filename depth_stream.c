#include <math.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#include <libfreenect/libfreenect.h>
#include <libfreenect/libfreenect_registration.h>

enum {
    WIDTH = 640, HEIGHT = 480, FRAME_BYTES = WIDTH * HEIGHT * sizeof(uint16_t),
    RGB_SCALE = 4, RGB_WIDTH = WIDTH / RGB_SCALE, RGB_HEIGHT = HEIGHT / RGB_SCALE,
    RGB_BYTES = RGB_WIDTH * RGB_HEIGHT * 3, REG_X_SCALE = 256,
    STARTUP_RETRY = 75
};
static volatile sig_atomic_t stopping;
static int parent_pipe;
static int rgb_protocol;
static int rgb_started;
static int rgb_pending;
static double rgb_received;
static double rgb_sent;
static freenect_registration registration;
static uint8_t rgb_frame[WIDTH * HEIGHT * 3];
static uint8_t rgb_aligned[RGB_BYTES];

static double seconds_now(void)
{
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return now.tv_sec + now.tv_nsec / 1e9;
}

static void stop(int signal_number)
{
    (void)signal_number;
    stopping = 1;
}

static int parent_closed(void)
{
    if (!parent_pipe)
        return 0;
    struct pollfd control = { .fd = STDIN_FILENO, .events = POLLIN };
    if (poll(&control, 1, 0) <= 0)
        return 0;
    if (control.revents & (POLLHUP | POLLERR | POLLNVAL))
        return 1;
    char byte;
    return (control.revents & POLLIN) && read(STDIN_FILENO, &byte, 1) == 0;
}

static void trace_shutdown(const char *stage)
{
    if (getenv("KINECT_USB_DEBUG"))
        fprintf(stderr, "Kinect shutdown: %s\n", stage);
}

static void send_depth(freenect_device *device, void *depth, uint32_t timestamp)
{
    (void)device;
    (void)timestamp;
    if (stopping)
        return;
    if (fwrite(depth, 1, FRAME_BYTES, stdout) != FRAME_BYTES) {
        stopping = 1;
        return;
    }
    if (!rgb_protocol)
        return;

    double now = seconds_now();
    int paired = rgb_pending && now - rgb_received <= .10 && now - rgb_sent >= .06;
    if (fputc(paired, stdout) == EOF) {
        stopping = 1;
        return;
    }
    if (!paired)
        return;
    rgb_sent = now;
    rgb_pending = 0;

    // Sample the calibrated RGB coordinate for each low resolution depth pixel.
    // This keeps full resolution depth in its original camera space.
    const uint16_t *mm = depth;
    const int offset = HEIGHT * registration.reg_pad_info.start_lines;
    memset(rgb_aligned, 0, sizeof(rgb_aligned));
    for (int row = 0; row < RGB_HEIGHT; row++) {
        for (int col = 0; col < RGB_WIDTH; col++) {
            int index = (row * RGB_SCALE + RGB_SCALE / 2) * WIDTH + col * RGB_SCALE + RGB_SCALE / 2;
            uint16_t z = mm[index];
            if (!z || z >= FREENECT_DEPTH_MM_MAX_VALUE)
                continue;
            int fixed_x = registration.registration_table[index][0] + registration.depth_to_rgb_shift[z];
            if (fixed_x < 0 || fixed_x >= WIDTH * REG_X_SCALE)
                continue;
            int64_t target = (int64_t)registration.registration_table[index][1] * WIDTH
                + fixed_x / REG_X_SCALE - offset;
            if (target >= 0 && target < WIDTH * HEIGHT)
                memcpy(rgb_aligned + (row * RGB_WIDTH + col) * 3, rgb_frame + target * 3, 3);
        }
    }
    if (fwrite(rgb_aligned, 1, RGB_BYTES, stdout) != RGB_BYTES)
        stopping = 1;
}

static void receive_rgb(freenect_device *device, void *video, uint32_t timestamp)
{
    (void)device;
    (void)timestamp;
    memcpy(rgb_frame, video, sizeof(rgb_frame));
    rgb_received = seconds_now();
    rgb_pending = 1;
}

static int send_intrinsics(freenect_device *device)
{
    double x, y;
    freenect_camera_to_world(device, 420, 340, 1000, &x, &y);
    double fx = isfinite(x) && x > 0 ? 100000 / x : 0;
    double fy = isfinite(y) && y > 0 ? 100000 / y : 0;
    if (rgb_protocol)
        return fprintf(stdout, "KDP2 640 480 320 240 %.9g %.9g %d %d\n", fx, fy, RGB_WIDTH, RGB_HEIGHT);
    return fprintf(stdout, "KDP1 640 480 320 240 %.9g %.9g\n", fx, fy);
}

int main(int argc, char **argv)
{
    freenect_context *context = NULL;
    freenect_device *device = NULL;
    int result = EXIT_FAILURE;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--rgb-assist") == 0)
            rgb_protocol = 1;
        else if (strcmp(argv[i], "--parent-pipe") == 0)
            parent_pipe = 1;
        else {
            fputs("Usage: depth_stream [--rgb-assist] [--parent-pipe]\n", stderr);
            return result;
        }
    }

    signal(SIGTERM, stop);
    signal(SIGINT, stop);
    signal(SIGPIPE, SIG_IGN);
    setvbuf(stdout, NULL, _IONBF, 0);
    if (parent_closed())
        return EXIT_SUCCESS;

    if (freenect_init(&context, NULL) < 0) {
        fputs("Could not initialize libfreenect.\n", stderr);
        return result;
    }

    freenect_set_log_level(context, FREENECT_LOG_WARNING);
    freenect_select_subdevices(context, FREENECT_DEVICE_CAMERA);
    int open_result = freenect_open_device(context, &device, 0);
    if (open_result < 0) {
        int cameras = freenect_num_devices(context);
        fprintf(stderr, "Could not open Kinect camera (driver %d, cameras found %d).\n", open_result, cameras);
        if (cameras > 0)
            result = STARTUP_RETRY;
        if (device != NULL)
            freenect_close_device(device);
        goto shutdown;
    }
    if (stopping || parent_closed()) {
        result = EXIT_SUCCESS;
        goto close_device;
    }

    freenect_frame_mode mode = freenect_find_depth_mode(FREENECT_RESOLUTION_MEDIUM, FREENECT_DEPTH_MM);
    if (!mode.is_valid || mode.bytes != FRAME_BYTES || freenect_set_depth_mode(device, mode) < 0) {
        fputs("Could not configure Kinect depth in millimetres.\n", stderr);
        goto close_device;
    }

    freenect_set_depth_callback(device, send_depth);
    if (freenect_start_depth(device) < 0) {
        fputs("Could not start the Kinect depth stream.\n", stderr);
        result = STARTUP_RETRY;
        goto close_device;
    }

    if (rgb_protocol) {
        freenect_frame_mode video = freenect_find_video_mode(FREENECT_RESOLUTION_MEDIUM, FREENECT_VIDEO_RGB);
        if (video.is_valid && video.bytes == sizeof(rgb_frame) && freenect_set_video_mode(device, video) == 0) {
            freenect_set_video_callback(device, receive_rgb);
            if (freenect_start_video(device) == 0) {
                rgb_started = 1;
                registration = freenect_copy_registration(device);
            }
        }
        if (!rgb_started)
            fputs("Kinect RGB unavailable. Continuing with depth only.\n", stderr);
    }

    result = EXIT_SUCCESS;
    if (send_intrinsics(device) < 0)
        stopping = 1;
    while (!stopping && !parent_closed()) {
        struct timeval timeout = {0, 100000};
        int code = freenect_process_events_timeout(context, &timeout);
        if (code < 0 && !stopping) {
            fprintf(stderr, "Kinect depth stream failed (%d).\n", code);
            result = EXIT_FAILURE;
            break;
        }
    }
    stopping = 1;
    if (rgb_started) {
        trace_shutdown("stopping RGB");
        freenect_stop_video(device);
        freenect_destroy_registration(&registration);
    }
    trace_shutdown("stopping depth");
    freenect_stop_depth(device);

close_device:
    trace_shutdown("closing camera");
    freenect_close_device(device);
shutdown:
    trace_shutdown("shutting down driver");
    freenect_shutdown(context);
    trace_shutdown("finished");
    return result;
}
