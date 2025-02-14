import os
import sys
import time
import getpass
from ctypes import byref, cast
from ctypes import POINTER, c_int, c_uint32, c_char

from xdg.BaseDirectory import load_data_paths
import simplepam as pam

from pyxtrlock.cursor_file import load_cursor
from pyxtrlock import panic
from pyxtrlock import require_x11_session
try:
    import pyxtrlock.xcb as xcb
except ImportError as err:
    panic(err)

try:
    import pyxtrlock.X as X
except ImportError as err:
    panic(err)

require_x11_session()

if getpass.getuser() == 'root' and sys.argv[1:] != ['-f']:
    msg = (
        "refusing to run as root. Use -f to force. Warning: "
        "Your PAM configuration may deny unlocking as root."
    )
    panic(msg)

# load cursor data file
try:
    for directory in load_data_paths("pyxtrlock"):
        f_name = os.path.join(directory, "cursor.json")
        if os.path.exists(f_name):
            with open(f_name, "r") as f:
                cursor = load_cursor(f)
            break
    else:
        from pyxtrlock.default_cursor import DEFAULT_CURSOR as cursor
except OSError as e:
    panic("error reading cursor:", e.strerror)
except Exception as e:
    panic("error reading cursor:", str(e))

display = X.create_window(None)
conn = X.get_xcb_connection(display)

if not display:
    panic("Could not connect to X server")

screen_num = c_int()

setup = xcb.get_setup(conn)

iter_ = xcb.setup_roots_iterator(setup)

while screen_num.value:
    xcb.screen_next(byref(iter_))
    screen_num.value -= 1

screen = iter_.data.contents

# create window
window = xcb.generate_id(conn)

attribs = (c_uint32 * 2)(1, xcb.EVENT_MASK_KEY_PRESS)
ret = xcb.create_window(conn, xcb.COPY_FROM_PARENT, window, screen.root,
                        0, 0, 1, 1, 0, xcb.WINDOW_CLASS_INPUT_ONLY,
                        xcb.VisualID(xcb.COPY_FROM_PARENT),
                        xcb.CW_OVERRIDE_REDIRECT | xcb.CW_EVENT_MASK,
                        cast(byref(attribs), POINTER(c_uint32)))

# create cursor
csr_map = xcb.image_create_pixmap_from_bitmap_data(conn, window,
                                                   cursor["fg_bitmap"],
                                                   cursor["width"],
                                                   cursor["height"],
                                                   1, 0, 0, None)
csr_mask = xcb.image_create_pixmap_from_bitmap_data(conn, window,
                                                    cursor["bg_bitmap"],
                                                    cursor["width"],
                                                    cursor["height"],
                                                    1, 0, 0, None)

try:
    r, g, b = cursor["bg_color"]
    csr_bg = xcb.alloc_color_sync(conn, screen.default_colormap,
                                  r, g, b)
    r, g, b = cursor["fg_color"]
    csr_fg = xcb.alloc_color_sync(conn, screen.default_colormap,
                                  r, g, b)
except ValueError as e:
    panic(str(e))
except xcb.XCBError as e:
    panic("Could not allocate colors")

try:
    cursor = xcb.create_cursor_sync(conn, csr_map, csr_mask, csr_fg, csr_bg,
                                    cursor["x_hot"], cursor["y_hot"])
except xcb.XCBError as e:
    panic("Could not create cursor")

# map window
xcb.map_window(conn, window)

# Grab keyboard
# Use the method from the original xtrlock code:
#  "Sometimes the WM doesn't ungrab the keyboard quickly enough if
#  launching xtrlock from a keystroke shortcut, meaning xtrlock fails
#  to start We deal with this by waiting (up to 100 times) for 10,000
#  microsecs and trying to grab each time. If we still fail
#  (i.e. after 1s in total), then give up, and emit an error"

for i in range(100):
    try:
        status = xcb.grab_keyboard_sync(conn, 0, window,
                                        xcb.CURRENT_TIME,
                                        xcb.GRAB_MODE_ASYNC,
                                        xcb.GRAB_MODE_ASYNC)

        if status == xcb.GrabSuccess:
            break
        else:
            time.sleep(0.01)
    except xcb.XCBError as e:
        time.sleep(0.01)
else:
    panic("Could not grab keyboard")

# Grab pointer
for i in range(100):
    try:
        status = xcb.grab_pointer_sync(conn, False, window, 0,
                                       xcb.GRAB_MODE_ASYNC,
                                       xcb.GRAB_MODE_ASYNC,
                                       xcb.WINDOW_NONE, cursor,
                                       xcb.CURRENT_TIME)

        if status == xcb.GrabSuccess:
            break
        else:
            time.sleep(0.01)
    except xcb.XCBError as e:
        time.sleep(0.01)
else:
    panic("Could not grab pointing device")

xcb.flush(conn)

# implement the XSS_SLEEP_LOCK_FD sleep delay protocol
xss_fd = os.getenv("XSS_SLEEP_LOCK_FD")
if xss_fd is not None:
    try:
        os.close(int(xss_fd))
    except OSError:
        # ignore if the fd was invalid
        pass
    except ValueError:
        # ignore if the variable did not contain an fd
        pass

# Prepare X Input
im = X.open_IM(display, None, None, None)
if not im:
    panic("Could not open Input Method")

ic = X.create_IC(im, X.N_INPUT_STYLE,
                 X.IM_PRE_EDIT_NOTHING | X.IM_STATUS_NOTHING, None)
if not ic:
    panic("Could not open Input Context")

X.set_ic_focus(ic)

# pwd length limit to prevent memory exhaustion (and therefore
# possible failure due to OOM killing)
PWD_LENGTH_LIMIT = 100 * 1024

# timeout algorithm constants
TIMEOUTPERATTEMPT = 30000
MAXGOODWILL = TIMEOUTPERATTEMPT * 5
INITIALGOODWILL = MAXGOODWILL
GOODWILLPORTION = 0.3

# main event loop
pwd = []
timeout = 0
goodwill = INITIALGOODWILL
while True:
    with xcb.wait_for_event(conn) as event:
        if not event:
            # this test should always be true, we have it here as an
            # extra precaution, so we do not kill pyxtrlock if there
            # still is a connection to the X server (although
            # wait_for_event should never return None in that case)
            if xcb.connection_has_error(conn):
                # prevent looping if the server connection breaks
                break
            else:
                continue

        if event.contents.response_type == xcb.KEY_PRESS:
            xcb_key_press_event = cast(event,
                                       POINTER(xcb.KeyPressEvent)).contents
            time_stamp = xcb_key_press_event.time
            if time_stamp < timeout:
                continue

            x_key_press_event = X.KeyEvent.from_xcb_event(display,
                                                          xcb_key_press_event)

            status = X.Status()
            keysym = X.Keysym()
            size = 0
            buf = bytearray(size)

            length = X.utf8_lookup_string(ic, byref(x_key_press_event), None,
                                          size, byref(keysym), byref(status))
            if status.value == X.BUFFER_OVERFLOW:
                buf = bytearray(length)
                buf_p = cast((c_char * length).from_buffer(buf),
                             POINTER(c_char))
                length = X.utf8_lookup_string(ic, byref(x_key_press_event),
                                              buf_p, length, byref(keysym),
                                              byref(status))

            status = status.value
            keysym = keysym.value
            if status == X.LOOKUP_BOTH or status == X.LOOKUP_KEYSYM:
                if keysym == X.K_Escape or keysym == X.K_Clear:
                    pwd = []
                    continue
                elif keysym == X.K_Delete or keysym == X.K_BackSpace:
                    if pwd:
                        pwd.pop()
                    continue
                elif keysym == X.K_LineFeed or keysym == X.K_Return:
                    if pam.authenticate(getpass.getuser(), b''.join(pwd)):
                        break
                    else:
                        pwd = []
                        if timeout:
                            goodwill += time_stamp - timeout
                            if goodwill > MAXGOODWILL:
                                goodwill = MAXGOODWILL
                        timeout = -int(goodwill * GOODWILLPORTION)
                        goodwill += timeout
                        timeout += time_stamp + TIMEOUTPERATTEMPT
                        continue

            if status == X.LOOKUP_BOTH or status == X.LOOKUP_CHARS:
                if length and sum(map(len, pwd)) < PWD_LENGTH_LIMIT:
                    pwd.append(bytes(buf[:length]))

X.close_window(display)
