#!/usr/bin/env bash
# Runs Xvfb as the agent's display and tells systemd it is ready once Xvfb
# accepts connections: Xvfb writes the display number to -displayfd only then.
# Xvfb picks the lowest free display, so a number other than the expected one
# means :0 is taken, and Xvfb is stopped. YOLOBOX_XVFB_DISPLAY (":0") and
# YOLOBOX_XVFB_XKBDIR are set by the build.
display="${YOLOBOX_XVFB_DISPLAY:?}"
xkbdir="${YOLOBOX_XVFB_XKBDIR:?}"
xvfb_pid=$$

announce_when_ready() {
    local number
    read -r number
    if [ ":$number" != "$display" ]; then
        echo "yolobox-xvfb: Xvfb took display :$number, not $display" >&2
        kill "$xvfb_pid"
        return
    fi
    systemd-notify --ready
}

# -nolisten tcp keeps the display off the network. There is no -auth: any
# local uid can connect over the unix socket, and the display is the agent's.
# exec: the shell becomes Xvfb, so $xvfb_pid is Xvfb.
exec Xvfb -screen 0 1920x1080x24 -nolisten tcp -xkbdir "$xkbdir" -displayfd 3 3> >(announce_when_ready)
